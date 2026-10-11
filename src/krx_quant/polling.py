"""REST polling: each job calls one KIS quotation endpoint on a fixed interval."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import aiohttp

from krx_quant.feeds import RAW_SCHEMA, RestFeed
from krx_quant.feeds.base import kst_day
from krx_quant.kis import KisError, RestClient
from krx_quant.recorder import ParquetRecorder

log = logging.getLogger(__name__)

# A call that failed: logged, and asked again on the next interval.
CALL_ERRORS = (KisError, aiohttp.ClientError, OSError, ValueError, TimeoutError)
# An answer that could not be normalized: kept raw only.
NORMALIZE_ERRORS = (KeyError, ValueError, TypeError)

_UNSEEN = object()


@dataclass(frozen=True)
class PollJob:
    feed: RestFeed
    key: str
    interval: float
    # Record an answer only when its news differs from the one before (a list that rarely
    # changes).
    changes_only: bool = False
    # Poll only at times this allows (default: always).
    when: Callable[[datetime], bool] | None = None
    # The first answer only sets what later ones are compared with: when its values were
    # true is unknown (before a session it is the last one's).
    first_is_baseline: bool = False

    @classmethod
    def fallback(cls, feed: RestFeed, key: str, interval: float) -> PollJob:
        """Polling ``feed`` for ``key``, whose WebSocket registration was cut.

        A stand-in records only news, as the feed it stands in for sends only on news; its
        answers mostly carry no time to tell a repeat, or the last session's values, from news.
        """
        stand_in = feed.stands_in_for is not None
        return cls(feed, key, interval, changes_only=stand_in, first_is_baseline=stand_in)


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
        on_added: Callable[[tuple[str, str]], None] | None = None,
        on_received: Callable[[tuple[str, str]], None] | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._client = client
        self._recorder = recorder
        self._now = now
        self._stop = stop
        self._on_added = on_added
        self._on_received = on_received
        self._on_event = on_event
        self._tasks: dict[tuple[str, str], asyncio.Task[None]] = {}

    def add(self, job: PollJob) -> None:
        """Start polling ``job``; a (feed, key) already being polled is not added twice."""
        key = (job.feed.tr_id, job.key)
        if key in self._tasks:
            return
        self._tasks[key] = asyncio.create_task(self._poll(job, self._client))
        if self._on_added is not None:
            self._on_added(key)

    async def close(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for job_error in results:
            if isinstance(job_error, Exception):
                log.error("poll job ended with an error: %r", job_error)

    async def _poll(self, job: PollJob, client: RestClient) -> None:
        last: object = _UNSEEN  # the news of the last answer, for changes_only
        while not self._stop.is_set():
            now = self._now()
            if job.when is None or job.when(now):
                day = kst_day(now)  # the answer is recorded under the day it was asked for
                params = job.feed.params(job.key, day)
                try:
                    body = await client.get(job.feed.tr_id, job.feed.path, params)
                except CALL_ERRORS as e:
                    log.warning("poll %s %s failed: %s", job.feed.tr_id, job.key, e)
                else:
                    news = job.feed.news(body)
                    if job.first_is_baseline and last is _UNSEEN:
                        self._read_baseline(job, params, body, self._now(), day)
                    elif not (job.changes_only and news == last):
                        self._record(job, params, body, self._now(), day)
                    last = news
                    if self._on_received is not None:
                        self._on_received((job.feed.tr_id, job.key))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), job.interval)

    def _read_baseline(
        self, job: PollJob, params: dict[str, str], body: dict[str, object], at: datetime, day: date
    ) -> None:
        """Read a baseline answer like any other without recording it (when its values were
        true is unknown); one that cannot be read is kept raw, to look into.
        """
        try:
            job.feed.normalize(body, job.key, at.astimezone(UTC))
        except NORMALIZE_ERRORS:
            log.exception("could not normalize %s %s; kept raw only", job.feed.tr_id, job.key)
            self._record_raw(job, params, body, at, day)

    def _record_raw(
        self, job: PollJob, params: dict[str, str], body: dict[str, object], at: datetime, day: date
    ) -> None:
        message = json.dumps({"tr_id": job.feed.tr_id, "params": params, "body": body}, ensure_ascii=False)
        raw_row = {"received_at": at.astimezone(UTC), "message": message}
        self._recorder.record("raw", day, job.feed.tr_id, job.key, raw_row, RAW_SCHEMA)

    def _record(
        self, job: PollJob, params: dict[str, str], body: dict[str, object], at: datetime, day: date
    ) -> None:
        received_at = at.astimezone(UTC)
        self._record_raw(job, params, body, at, day)
        try:
            rows = job.feed.normalize(body, job.key, received_at)
        except NORMALIZE_ERRORS:
            log.exception("could not normalize %s %s; kept raw only", job.feed.tr_id, job.key)
            return
        events_feed = job.feed.events_feed
        for row in rows:
            # By the event's symbol, as the stream records them (a US ticker, not its key).
            symbol = row.get("symbol") or job.key
            self._recorder.record("events", day, events_feed, symbol, row, job.feed.schema)
            if self._on_event is not None:
                self._on_event(events_feed, row)
