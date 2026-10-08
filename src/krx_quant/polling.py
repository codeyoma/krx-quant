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

    With no client (no REST access token) jobs are logged and skipped.
    A failed call is logged and retried on the next interval.
    """

    def __init__(
        self,
        client: RestClient | None,
        recorder: ParquetRecorder,
        now: Callable[[], datetime],
        stop: asyncio.Event,
    ) -> None:
        self._client = client
        self._recorder = recorder
        self._now = now
        self._stop = stop
        self._tasks: list[asyncio.Task[None]] = []

    def add(self, job: PollJob) -> None:
        if self._client is None:
            log.error("not polling %s %s: REST is disabled", job.feed.tr_id, job.key)
            return
        self._tasks.append(asyncio.create_task(self._poll(job, self._client)))

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        results = await asyncio.gather(*self._tasks, return_exceptions=True)
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
