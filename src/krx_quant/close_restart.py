"""The engine restarts once a trading day after the close (#687), for the night to start with a fresh
engine process.

The engine's rest after the session (#623, ``garbage.Garbage.rest``) hands back what the session
kept, but a process does not shrink to what it was at its start: on 2026-11-09 the server's
lowest memory available from 21:00 to 23:59 was still 174–212 MB with up to 856 MB in swap, while
the nightly training ran. A process started afresh drops whatever the engine still holds.

When: on a trading day this process predicted a bar close of, from ``KRX_CLOSE_RESTART_AT`` (17:00
unless set) once the day's review is over (the close + 10 minutes) and nothing the restart would
cut is running (``Facts.busy``: the archive after the close, the nightly training, a scheduled
job). 17:00 is after everything the session's end runs: the last answers (about 15:20) and the
rest, the review and its rest (15:40), the closing auctions' prices (15:40), the archive (15:45),
and the same an hour later on the exam day's late close. It is before the night futures, recorded
from 18:00 with no source to fetch a missed minute again: a restart not done by
``CLOSE_RESTART_LATEST`` (17:50) waits for the next trading day. It is long before the 20:30
history run, the 21:00 archive, the 21:30 training and the jobs from 22:30. What the restart cuts
is about a minute of the NXT after-market's recording (until 20:00), as the 06:00 deploy and the
07:20 restart cut theirs.

How: the collector sets its ``stop``, as for the 07:20 restart (``collector.restart_each_day``), and
Docker starts it again (``restart: unless-stopped``). The trader's quote stream reconnects by itself
(``trader.quotes.follow_quotes``), and the Engine API's clients ask again.

Seen: the stopping process logs the reason and its memory, and keeps them in the engine's ``runs``
(``RESTART_RUN``); every start logs and keeps its memory once the Engine API answers
(``START_RUN``). The server cannot read Loki: the night's report (``close_restart_probe.py``, a
scheduled job) reads them there.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from krx_quant import footprint
from krx_quant.config import CLOSE_RESTART_LATEST
from krx_quant.engine import REVIEW_AFTER_CLOSE
from krx_quant.feeds.base import KST
from krx_quant.market_calendar import MarketCalendar
from krx_quant.predictions import PredictionStore

log = logging.getLogger(__name__)

# The ``runs`` kinds (#687): the stopping process's memory and why it stopped; each start's memory
# once the Engine API answers. Their details are JSON.
RESTART_RUN = "close_restart"
START_RUN = "start_memory"
# How often it asks again while it waits for the review or for something running.
CHECK = timedelta(minutes=1)

RESTART, WAIT, SKIP = "restart", "wait", "skip"


@dataclass(frozen=True)
class Facts:
    """What the decision reads from the engine and the collector."""

    predicted_at: datetime | None  # the latest bar close this process predicted
    reviewed_through: datetime | None  # the latest daily review run
    busy: str | None  # what runs that a restart would cut (the archive, the training, a job); None


@dataclass(frozen=True)
class Decision:
    action: str  # RESTART, WAIT (ask again at ``CHECK``), SKIP (not this day)
    why: str


class Running:
    """What runs in the collector that the restart would cut, by name (``Facts.busy``)."""

    def __init__(self) -> None:
        self._names: list[str] = []

    @contextlib.contextmanager
    def during(self, name: str) -> Iterator[None]:
        self._names.append(name)
        try:
            yield
        finally:
            self._names.remove(name)

    def first(self) -> str | None:
        return self._names[0] if self._names else None


def job_running(state_file: Path) -> str | None:
    """The scheduled job the jobs' state file (``scheduled.core.STATE_FILE``) has running, if one:
    read as it is, never set aside or rewritten (the scheduler owns it)."""
    try:
        jobs = json.loads(state_file.read_text()).get("jobs", {})
        running = [name for name, record in jobs.items() if record.get("outcome") == "running"]
    except (OSError, ValueError, AttributeError):
        return None
    return f"the scheduled job {running[0]}" if running else None


def decide(now: datetime, at: time, calendar: MarketCalendar, facts: Facts) -> Decision:
    """Whether the engine restarts at ``now`` (module doc)."""
    local = now.astimezone(KST)
    day = local.date()
    if local.time() < at:
        return Decision(WAIT, f"before {at:%H:%M}")
    session = calendar.session(day)
    if session is None:
        return Decision(SKIP, "not a trading day")
    if facts.predicted_at is None or facts.predicted_at.astimezone(KST).date() != day:
        return Decision(SKIP, "this engine predicted no bar close today: it started after the session")
    if local.time() >= CLOSE_RESTART_LATEST:
        return Decision(SKIP, f"past {CLOSE_RESTART_LATEST:%H:%M}: the night futures are recorded from 18:00")
    review = session.close + REVIEW_AFTER_CLOSE
    if facts.reviewed_through is None or facts.reviewed_through < review:
        return Decision(WAIT, f"the day's review ({review.astimezone(KST):%H:%M}) has not run")
    if facts.busy is not None:
        return Decision(WAIT, f"{facts.busy} runs")
    return Decision(RESTART, f"the session of {day} and its review are over")


def _memory() -> dict[str, Any]:
    return {"rss_mb": footprint.resident_mb(), "swapped_mb": footprint.swapped_mb()}


def _mb(value: Any) -> str:
    return "?" if value is None else f"{float(value):.0f}"


async def restart_after_close(
    at: time,
    calendar: MarketCalendar,
    now: Callable[[], datetime],
    stop: asyncio.Event,
    facts: Callable[[], Awaitable[Facts]],
    keep: Callable[[datetime, dict[str, Any]], None],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Set ``stop`` once ``decide`` says so (module doc), after one INFO line with why and the
    engine's memory, also handed to ``keep`` (the store's ``RESTART_RUN``). A day it gives up is
    told once; the next trading day it asks again. The sleep is a parameter for tests."""
    waited: date | None = None  # the day its wait was told
    while True:
        moment = now()
        decision = decide(moment, at, calendar, await facts())
        local = moment.astimezone(KST)
        if decision.action == RESTART:
            figures = {"why": decision.why, **_memory()}
            log.info(
                "stopping for the restart after the close (#687): %s; engine rss %s MB, swapped %s MB",
                decision.why,
                _mb(figures["rss_mb"]),
                _mb(figures["swapped_mb"]),
            )
            keep(moment, figures)
            stop.set()
            return
        if decision.action == SKIP:
            if calendar.session(local.date()) is not None:
                log.info("no restart after the close today (#687): %s", decision.why)
            until = datetime.combine(local.date() + timedelta(days=1), at, KST)
        elif local.time() < at:
            until = datetime.combine(local.date(), at, KST)
        else:
            if waited != local.date():
                log.info("the restart after the close waits (#687): %s", decision.why)
                waited = local.date()
            until = moment + CHECK
        await sleep(max(0.0, (until - now()).total_seconds()))


def keep_restart(store: PredictionStore) -> Callable[[datetime, dict[str, Any]], None]:
    """``restart_after_close``'s ``keep``: the figures as the store's ``RESTART_RUN``."""
    return lambda at, figures: store.add_run(RESTART_RUN, at, None, json.dumps(figures))


def tell_started(store: PredictionStore, at: datetime) -> None:
    """Once a start is over (the Engine API answers): one INFO line with the engine's memory, kept
    as the store's ``START_RUN``; after a restart after the close, with the memory it stopped with."""
    figures = _memory()
    stopped = store.last_run(RESTART_RUN)
    started = store.runs(START_RUN, stopped) if stopped is not None else []
    after = ""
    if stopped is not None and not started and at - stopped < timedelta(minutes=30):
        [run] = store.runs(RESTART_RUN, stopped)[-1:]
        try:
            before = json.loads(run.detail).get("rss_mb")
        except (ValueError, AttributeError):
            before = None
        after = f"; after the restart after the close (#687), which stopped at {_mb(before)} MB"
        figures["after_restart"] = True
    log.info(
        "engine started: rss %s MB, swapped %s MB%s",
        _mb(figures["rss_mb"]),
        _mb(figures["swapped_mb"]),
        after,
    )
    store.add_run(START_RUN, at, None, json.dumps(figures))
