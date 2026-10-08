"""REST polling: each job calls one KIS quotation endpoint on a fixed interval."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import aiohttp

from krx_quant.feeds import RAW_SCHEMA, RestFeed
from krx_quant.feeds.base import kst_day
from krx_quant.kis import KisError, RestClient
from krx_quant.recorder import ParquetRecorder

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PollJob:
    feed: RestFeed
    key: str
    interval: float


class Poller:
    """Runs poll jobs, including ones added while the collector is running.

    A failed call is logged and retried on the next interval.
    """

    def __init__(
        self,
        client: RestClient,
        recorder: ParquetRecorder,
        now: Callable[[], datetime],
        stop: asyncio.Event,
        on_received: Callable[[tuple[str, str]], None] | None = None,
    ) -> None:
        self._client = client
        self._recorder = recorder
        self._now = now
        self._stop = stop
        self._on_received = on_received
        self._tasks: dict[tuple[str, str], asyncio.Task[None]] = {}

    def add(self, job: PollJob) -> None:
        """Start polling ``job``; a (feed, key) already being polled is not added twice."""
        key = (job.feed.tr_id, job.key)
        if key in self._tasks:
            return
        self._tasks[key] = asyncio.create_task(self._poll(job, self._client))

    async def close(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for job_error in results:
            if isinstance(job_error, Exception):
                log.error("poll job ended with an error: %r", job_error)

    async def _poll(self, job: PollJob, client: RestClient) -> None:
        params = job.feed.params(job.key)
        while not self._stop.is_set():
            try:
                body = await client.get(job.feed.tr_id, job.feed.path, params)
            except (KisError, aiohttp.ClientError, OSError, ValueError, TimeoutError) as e:
                log.warning("poll %s %s failed: %s", job.feed.tr_id, job.key, e)
            else:
                self._record(job, params, body, self._now())
                if self._on_received is not None:
                    self._on_received((job.feed.tr_id, job.key))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), job.interval)

    def _record(self, job: PollJob, params: dict[str, str], body: dict[str, object], at: datetime) -> None:
        day = kst_day(at)
        received_at = at.astimezone(UTC)
        message = json.dumps({"tr_id": job.feed.tr_id, "params": params, "body": body}, ensure_ascii=False)
        raw_row = {"received_at": received_at, "message": message}
        self._recorder.record("raw", day, job.feed.tr_id, job.key, raw_row, RAW_SCHEMA)
        try:
            rows = job.feed.normalize(body, job.key, received_at)
        except (KeyError, ValueError, TypeError):
            log.exception("could not normalize %s %s; kept raw only", job.feed.tr_id, job.key)
            return
        for row in rows:
            self._recorder.record("events", day, job.feed.tr_id, job.key, row, job.feed.schema)
