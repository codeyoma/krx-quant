"""The night's report of the evening after the restart after the close (#687, ``close_restart_probe.py``)
and the two nights its scheduled jobs run.

Input: an engine's store with a day's restart and the starts around it, the heartbeat's readings of
that evening kept by hour (``memory_hours.py``), and days without a restart. Output: the report's
sections with times, counts and MB only, exit 1 without a restart, the day ``--last`` reads, and the
nights the two jobs are due.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

from krx_quant import memory_hours
from krx_quant.close_restart import RESTART_RUN, START_RUN
from krx_quant.close_restart_probe import close_restart_memory, last_restart_day
from krx_quant.config import Settings
from krx_quant.feeds.base import KST
from krx_quant.predictions import DB_NAME, PredictionStore
from krx_quant.scheduled import (
    CLOSE_RESTART_MEMORY,
    CLOSE_RESTART_MEMORY_AGAIN,
    FAILED,
    INTERRUPTED,
    OK,
    JobRecord,
    Night,
)

DAY = date(2026, 11, 11)  # a Wednesday


def at(clock: str, day: date = DAY) -> datetime:
    return datetime.combine(day, time.fromisoformat(clock), KST)


def settings(tmp_path: Path) -> Settings:
    return Settings(
        kis_app_key="",
        kis_app_secret="",
        symbols=("005930",),
        etf_symbols=(),
        index_codes=(),
        us_symbols=(),
        data_dir=tmp_path / "data",
        state_dir=tmp_path,
    )


def memory(rss: float) -> str:
    return json.dumps({"rss_mb": rss, "swapped_mb": 20.0})


def stored(tmp_path: Path) -> None:
    store = PredictionStore(tmp_path / DB_NAME)
    store.add_run(START_RUN, at("06:02"), None, memory(480.0))  # the deploy's: before the restart, not told
    store.add_run(
        RESTART_RUN,
        at("17:00:00"),
        None,
        json.dumps(
            {"why": "the session of 2026-11-11 and its review are over", "rss_mb": 905.6, "swapped_mb": 41.0}
        ),
    )
    store.add_run(
        START_RUN,
        at("17:00:40"),
        None,
        json.dumps({"rss_mb": 412.3, "swapped_mb": 0.0, "after_restart": True}),
    )
    store.close()


def test_the_report_says_the_restart_the_start_after_and_the_night_against_the_evenings_before(
    tmp_path: Path,
) -> None:
    stored(tmp_path)
    memory_hours.note(tmp_path, at("14:59:00"), 700.0, 100.0, 745.0, 0.0)  # before the hours shown
    memory_hours.note(tmp_path, at("17:30:10"), 820.0, 150.0, 455.0, 0.0)
    memory_hours.note(tmp_path, at("21:40:10"), 512.0, 300.0, 610.0, 0.0)
    memory_hours.note(tmp_path, at("22:10:10"), 431.0, 420.0, 640.0, 0.0)
    memory_hours.note(tmp_path, at("23:59:10"), 470.0, 380.0, 600.0, 0.0)
    memory_hours.note(
        tmp_path, at("00:10:10", DAY + timedelta(days=1)), 300.0, 900.0, 600.0, 0.0
    )  # the next day

    report, code = close_restart_memory(settings(tmp_path), DAY)

    assert code == 0
    assert report.splitlines() == [
        "장 마감 뒤 다시 시작 (#687) · 2026-11-11",
        "",
        "1. 다시 시작과 그 뒤의 시작",
        "- 17:00:00 멈춤: the session of 2026-11-11 and its review are over; 엔진 RSS 906 MB, 스왑에 41 MB",
        "- 17:00:40 시작(API가 답한 때): 엔진 RSS 412 MB, 스왑에 0 MB",
        "",
        "2. 시간대별 메모리 (15시부터, heartbeat의 1분마다 값)",
        "| 시 | 읽은 수 | 여유 최소 MB | swap 최대 MB | 엔진 RSS 최대 MB | 엔진의 스왑 최대 MB |",
        "|---|---|---|---|---|---|",
        "| 17시 | 1 | 820 | 150 | 455 | 0 |",
        "| 21시 | 1 | 512 | 300 | 610 | 0 |",
        "| 22시 | 1 | 431 | 420 | 640 | 0 |",
        "| 23시 | 1 | 470 | 380 | 600 | 0 |",
        "",
        "3. 21–23시 (3번 읽음): 여유 최소 431 MB, swap 최대 420 MB",
        "   견줄 값(다시 시작 전, 21–23시): 2026-11-06 여유 최소 203 MB·swap 최대 828 MB; 2026-11-09 여유"
        " 최소 174(시마다 174–212) MB·swap 최대 856 MB",
    ]


def test_a_day_without_a_restart_exits_1(tmp_path: Path) -> None:
    stored(tmp_path)
    assert close_restart_memory(settings(tmp_path), DAY - timedelta(days=1)) == (
        "장 마감 뒤 다시 시작 (#687) · 2026-11-10: 그날 엔진이 장 마감 뒤 다시 시작하지 않았다",
        1,
    )
    assert close_restart_memory(settings(tmp_path), None)[1] == 1


def test_last_reads_the_latest_restart_before_tonight(tmp_path: Path) -> None:
    """The night of the restart's day is not over at 22:30: the day before's restart is read, and none
    before the first restart's next night; after midnight the night is still the day before's."""
    stored(tmp_path)
    assert last_restart_day(settings(tmp_path), at("22:30")) is None
    assert last_restart_day(settings(tmp_path), at("01:00", DAY + timedelta(days=1))) is None
    assert last_restart_day(settings(tmp_path), at("22:30", DAY + timedelta(days=1))) == DAY
    assert last_restart_day(settings(tmp_path), at("22:30", DAY + timedelta(days=3))) == DAY
    assert last_restart_day(tmp_path_settings_without_db(tmp_path), at("22:30")) is None


def tmp_path_settings_without_db(tmp_path: Path) -> Settings:
    empty = tmp_path / "empty"
    empty.mkdir()
    return settings(empty)


def night(
    day: date, facts: date | None, mine: JobRecord | None, first: JobRecord | None = None
) -> Night[date | None]:
    records = {} if first is None else {CLOSE_RESTART_MEMORY.name: first}
    return Night(day, day.weekday() < 5, "new", mine, records, facts)


def record(day: date, outcome: str = OK) -> JobRecord:
    return JobRecord(day, outcome, at("22:40", day), "new", 0 if outcome == OK else 1)


def test_the_first_job_runs_once_an_evening_with_a_restart_is_over_until_ok() -> None:
    first = CLOSE_RESTART_MEMORY.due
    thursday = DAY + timedelta(days=1)
    assert not first(night(DAY, None, None))  # the restart's own night: not over
    assert first(night(thursday, DAY, None))
    assert not first(night(thursday, DAY, record(thursday)))
    assert not first(night(thursday, DAY, record(thursday, FAILED)))  # not again the night it failed
    assert first(night(thursday + timedelta(days=1), DAY, record(thursday, FAILED)))
    assert first(night(thursday + timedelta(days=1), DAY, record(thursday, INTERRUPTED)))


def test_the_second_job_runs_on_a_later_evening_than_the_first_read() -> None:
    again = CLOSE_RESTART_MEMORY_AGAIN.due
    thursday, friday, saturday = (DAY + timedelta(days=n) for n in (1, 2, 3))
    done = record(thursday)
    assert not again(night(thursday, DAY, None, done))  # the first's own night
    assert not again(night(friday, DAY, None, done))  # no restart since: Thursday's did not happen
    assert again(night(friday, thursday, None, done))  # Thursday's evening, after the first's night
    assert again(night(saturday, friday, None, done))  # a Friday's evening on Saturday night
    assert not again(night(friday, thursday, None, record(thursday, FAILED)))  # the first did not end ok
    assert not again(night(friday, thursday, record(friday), done))
