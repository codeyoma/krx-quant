"""The engine restarts once a trading day after the close (#687, ``close_restart.py``).

Input: the time, the market calendar, the bar close the engine last predicted, its last review and
what runs in the collector; a fake clock for the loop. Output: whether it restarts, waits or gives
the day up; the stop, the line and the figures kept; the start's line after it; the setting's bounds.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant.close_restart import (
    RESTART,
    RESTART_RUN,
    SKIP,
    START_RUN,
    WAIT,
    Facts,
    Running,
    decide,
    job_running,
    keep_restart,
    restart_after_close,
    tell_started,
)
from krx_quant.config import Settings, SettingsError
from krx_quant.feeds.base import KST
from krx_quant.market_calendar import MarketCalendar
from krx_quant.predictions import DB_NAME, PredictionStore

TUESDAY, WEDNESDAY, THURSDAY = date(2026, 11, 10), date(2026, 11, 11), date(2026, 11, 12)
EXAM_DAY = date(2026, 11, 19)  # the close at 16:30, the review at 16:40
SATURDAY = date(2026, 11, 14)
# Wednesday is closed: a restart given up on Tuesday waits for Thursday.
CALENDAR = MarketCalendar({TUESDAY: True, WEDNESDAY: False, THURSDAY: True, SATURDAY: False, EXAM_DAY: True})
AT = time(17, 0)


def at(clock: str, day: date = TUESDAY) -> datetime:
    return datetime.combine(day, time.fromisoformat(clock), KST)


def ran(day: date = TUESDAY, busy: str | None = None) -> Facts:
    """An engine that predicted the day's last bar close and ran its review (15:40)."""
    return Facts(at("15:16", day), at("15:40", day), busy)


def test_it_restarts_once_the_session_and_its_review_are_over_from_its_time() -> None:
    assert decide(at("17:00"), AT, CALENDAR, ran()).action == RESTART
    assert decide(at("16:59:59"), AT, CALENDAR, ran()).action == WAIT


def test_it_waits_for_the_day_s_review() -> None:
    """The review is late (the engine was busy): it waits, and restarts once the review ran."""
    late = Facts(at("15:16"), at("15:40", TUESDAY - timedelta(days=1)), None)
    decision = decide(at("17:00"), AT, CALENDAR, late)
    assert (decision.action, decision.why) == (WAIT, "the day's review (15:40) has not run")
    assert decide(at("17:05"), AT, CALENDAR, Facts(at("15:16"), at("15:40"), None)).action == RESTART


def test_on_the_exam_day_it_waits_for_the_later_review() -> None:
    late_close = Facts(at("16:16", EXAM_DAY), at("15:40", EXAM_DAY), None)  # a review of a normal day
    assert decide(at("16:00", EXAM_DAY), time(16, 0), CALENDAR, late_close).action == WAIT
    reviewed = Facts(at("16:16", EXAM_DAY), at("16:40", EXAM_DAY), None)
    assert decide(at("16:41", EXAM_DAY), time(16, 0), CALENDAR, reviewed).action == RESTART


@pytest.mark.parametrize("busy", ["the archive", "the nightly training", "the scheduled job trader-report"])
def test_it_waits_while_a_job_the_training_or_the_archive_runs(busy: str) -> None:
    decision = decide(at("17:10"), AT, CALENDAR, ran(busy=busy))
    assert (decision.action, decision.why) == (WAIT, f"{busy} runs")


def test_not_on_a_day_without_a_session() -> None:
    decision = decide(at("17:00", SATURDAY), AT, CALENDAR, ran(SATURDAY))
    assert (decision.action, decision.why) == (SKIP, "not a trading day")


def test_not_when_this_engine_ran_no_session_today() -> None:
    """Started after the session (a crash, a restart): it is fresh already; nothing predicted at all,
    or only the day before's bar closes."""
    for predicted in (None, at("15:16", TUESDAY - timedelta(days=1))):
        decision = decide(at("17:00"), AT, CALENDAR, Facts(predicted, at("15:40"), None))
        assert decision.action == SKIP


def test_not_from_the_latest_time_on_the_night_futures_open_at_18() -> None:
    assert decide(at("17:49:59"), AT, CALENDAR, ran()).action == RESTART
    decision = decide(at("17:50"), AT, CALENDAR, ran())
    assert decision.action == SKIP
    assert decision.why.startswith("past 17:50")


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    async def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


async def test_the_loop_stops_the_collector_once_and_keeps_why_and_the_memory(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Started at 08:00; the archive runs until 17:03: it stops at 17:03, says so once while waiting,
    and keeps the reason with the engine's memory (None off Linux)."""
    caplog.set_level(logging.INFO, logger="krx_quant.close_restart")
    clock = Clock(at("08:00"))
    stop = asyncio.Event()
    kept: list[tuple[datetime, dict[str, Any]]] = []

    async def facts() -> Facts:
        return ran(busy="the archive" if clock.now < at("17:03") else None)

    await restart_after_close(
        AT, CALENDAR, lambda: clock.now, stop, facts, lambda t, f: kept.append((t, f)), clock.sleep
    )

    assert stop.is_set()
    assert clock.now == at("17:03")
    [(when, figures)] = kept
    assert when == at("17:03")
    assert figures["why"] == "the session of 2026-11-10 and its review are over"
    assert set(figures) == {"why", "rss_mb", "swapped_mb"}
    said = [r.getMessage() for r in caplog.records]
    assert said[0] == "the restart after the close waits (#687): the archive runs"
    assert said[1].startswith(
        "stopping for the restart after the close (#687): the session of 2026-11-10 and its review are"
        " over; engine rss "
    )
    assert len(said) == 2


async def test_a_day_given_up_waits_for_the_next_trading_day(caplog: pytest.LogCaptureFixture) -> None:
    """The training runs past 17:50 on Tuesday (a late start): no restart that day, told once; on
    Thursday (Wednesday is closed) it restarts at 17:00."""
    caplog.set_level(logging.INFO, logger="krx_quant.close_restart")
    clock = Clock(at("16:00"))
    stop = asyncio.Event()

    async def facts() -> Facts:
        day = clock.now.astimezone(KST).date()
        return ran(day, busy="the nightly training" if day == TUESDAY else None)

    await restart_after_close(AT, CALENDAR, lambda: clock.now, stop, facts, lambda t, f: None, clock.sleep)

    assert clock.now == at("17:00", THURSDAY)
    said = [r.getMessage() for r in caplog.records]
    assert said[:2] == [
        "the restart after the close waits (#687): the nightly training runs",
        "no restart after the close today (#687): past 17:50: the night futures are recorded from 18:00",
    ]


def test_the_start_after_a_restart_says_the_memory_it_stopped_with(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="krx_quant.close_restart")
    store = PredictionStore(tmp_path / DB_NAME)
    tell_started(store, at("06:02"))  # the deploy's start: no restart before it
    keep_restart(store)(at("17:00"), {"why": "over", "rss_mb": 912.4, "swapped_mb": 30.0})
    tell_started(store, at("17:01"))
    tell_started(store, at("17:20"))  # another start (a crash): not the restart's any more
    said = [r.getMessage() for r in caplog.records]
    assert [s.split(": rss")[0] for s in said] == ["engine started"] * 3
    assert "after the restart" not in said[0]
    assert said[1].endswith("; after the restart after the close (#687), which stopped at 912 MB")
    assert "after the restart" not in said[2]
    starts = store.runs(START_RUN, at("00:00"))
    assert [s.at for s in starts] == [at("06:02"), at("17:01"), at("17:20")]
    assert json.loads(starts[1].detail)["after_restart"] is True
    assert [r.at for r in store.runs(RESTART_RUN, at("00:00"))] == [at("17:00")]
    store.close()


def test_a_running_job_is_read_from_the_jobs_state_file_as_it_is(tmp_path: Path) -> None:
    path = tmp_path / "scheduled.json"
    assert job_running(path) is None  # no jobs yet
    path.write_text(json.dumps({"jobs": {"a": {"outcome": "ok"}, "trader-report": {"outcome": "running"}}}))
    assert job_running(path) == "the scheduled job trader-report"
    path.write_text("{not json")
    assert job_running(path) is None
    assert path.read_text() == "{not json"  # never set aside: the scheduler owns it


def test_running_names_what_runs_while_it_runs() -> None:
    running = Running()
    assert running.first() is None
    with running.during("the archive"):
        assert running.first() == "the archive"
    assert running.first() is None


def test_the_setting_is_17_unless_set_off_or_within_its_bounds() -> None:
    keys = {"KIS_APP_KEY": "key", "KIS_APP_SECRET": "secret"}
    assert Settings.from_env(keys, dotenv_path=None).close_restart_at == time(17, 0)
    off = {**keys, "KRX_CLOSE_RESTART_AT": "off"}
    assert Settings.from_env(off, dotenv_path=None).close_restart_at is None
    assert Settings.from_env({**keys, "KRX_CLOSE_RESTART_AT": "15:50"}, dotenv_path=None).close_restart_at
    for outside in ("15:45", "17:50", "21:00"):
        with pytest.raises(SettingsError, match="KRX_CLOSE_RESTART_AT"):
            Settings.from_env({**keys, "KRX_CLOSE_RESTART_AT": outside}, dotenv_path=None)
