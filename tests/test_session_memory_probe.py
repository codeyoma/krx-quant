"""The night's report of the engine's memory after the close (#623, ``session_memory_probe.py``).

Input: an engine's store with the rests of a day (``garbage.REST_RUN``), the heartbeat's readings
of an evening kept by hour (``memory_hours.py``), and a day without a rest. Output: the report's
sections with counts and MB only, exit 1 for a day without a rest, and the nights the job is due.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

from krx_quant import heartbeat, memory_hours
from krx_quant.bar_close_times import tonight
from krx_quant.config import Settings
from krx_quant.feeds.base import KST
from krx_quant.garbage import REST_RUN
from krx_quant.predictions import DB_NAME, PredictionStore
from krx_quant.scheduled import FAILED, INTERRUPTED, OK, SESSION_MEMORY, JobRecord, Night
from krx_quant.session_memory_probe import session_memory

DAY = date(2026, 11, 9)  # a Monday


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


def rest(after: str, before: float, now: float, gone: dict[str, int]) -> str:
    return json.dumps(
        {
            "after": after,
            "let_go": gone,
            "kept": {"reference quotes": 35892, "bars": 1520, "touch windows": 213},
            "rss_before_mb": before,
            "rss_mb": now,
            "swapped_mb": 12.0,
            "python_blocks": 589426,
            "malloc_mb": 38.2,
            "malloc_free_mb": 33.1,
            "arrow_mb": 0.0,
            "seconds": 0.1,
        }
    )


def test_the_report_says_each_rest_and_the_memory_by_hour_from_the_close(tmp_path: Path) -> None:
    """Input: two rests stored on the day (15:20, 15:40), readings at 14:59 (before the hours shown),
    15:00, 15:30 and 21:40, and one of the day before. Output: both rests' counts and memory, the
    15:00 and 21:00 hours with their extremes, nothing of 14:00 or the day before, exit 0."""
    store = PredictionStore(tmp_path / DB_NAME)
    store.add_run(REST_RUN, at("15:20:00", DAY - timedelta(days=3)), None, rest("the session", 1, 1, {}))
    store.add_run(REST_RUN, at("15:20:00"), None, rest("the session", 748.4, 701.2, {"book updates": 179985}))
    store.add_run(REST_RUN, at("15:40:31"), None, rest("the review", 760.0, 730.6, {}))
    store.close()
    memory_hours.note(tmp_path, at("14:59:00"), 700.0, 100.0, 745.0, 0.0)
    memory_hours.note(tmp_path, at("15:00:10"), 690.0, 187.0, 748.0, 3.0)
    memory_hours.note(tmp_path, at("15:30:10"), 640.0, 190.0, 760.0, 5.0)
    memory_hours.note(tmp_path, at("21:40:10"), 410.0, 512.0, 610.0, 120.0)
    memory_hours.note(tmp_path, at("21:41:10"), None, None, None, None)

    report, code = session_memory(settings(tmp_path), at("22:35:00"), DAY)

    assert code == 0
    lines = report.splitlines()
    assert lines[0] == f"장 마감 뒤 메모리 (#623) · {DAY}"
    assert "1. 엔진이 쉰 기록 (2번)" in lines
    assert (
        "- 15:20:00 the session: 놓음 book updates 179985; 남김 reference quotes 35892, bars 1520, touch"
        " windows 213; 엔진 RSS 748 → 701 MB, 스왑에 12 MB; python 589426 블록, malloc 사용 38 MB·빈 것"
        " 33 MB, arrow 0 MB; 0.1초"
    ) in lines
    assert any(line.startswith("- 15:40:31 the review: 놓음 없음; 남김 ") for line in lines)
    assert "| 11-09 15시 | 2 | 640 | 190 | 760 | 5 |" in lines
    assert "| 11-09 21시 | 2 | 410 | 512 | 610 | 120 |" in lines
    assert not any("14시" in line for line in lines)
    assert lines[-1].startswith("3. 지금 (11-09 22:35): 여유 ")


def test_a_day_without_a_rest_exits_1_and_the_job_stays_due(tmp_path: Path) -> None:
    """Input: no engine database; then one with a rest of the Friday before only, asked at 00:30 of
    the night of Monday (``--tonight``: Monday). Output: exit 1 both times; the job is due on trading
    nights until a run ends ok, after a failure the next trading night."""
    after_midnight = at("00:30:00", DAY + timedelta(days=1))
    assert session_memory(settings(tmp_path), after_midnight, tonight(after_midnight)) == (
        f"장 마감 뒤 메모리 (#623) · {DAY}: 엔진 DB가 없다",
        1,
    )
    store = PredictionStore(tmp_path / DB_NAME)
    store.add_run(REST_RUN, at("15:20:00", DAY - timedelta(days=3)), None, rest("the session", 1, 1, {}))
    store.close()
    assert session_memory(settings(tmp_path), after_midnight, tonight(after_midnight)) == (
        f"장 마감 뒤 메모리 (#623) · {DAY}: 그날 엔진이 쉰 기록이 없다",
        1,
    )

    def night(day: date, trading: bool, record: JobRecord | None) -> Night[None]:
        return Night(day, trading, "c1", record, {}, None)

    started = at("22:40:00")
    tuesday = DAY + timedelta(days=1)
    assert SESSION_MEMORY.command == ("session-memory", "--tonight")
    assert [t.issue for t in SESSION_MEMORY.targets] == [623]
    assert not SESSION_MEMORY.due(night(date(2026, 11, 7), False, None))
    assert SESSION_MEMORY.due(night(DAY, True, None))
    assert not SESSION_MEMORY.due(night(DAY, True, JobRecord(DAY, FAILED, started, "c1", exit_code=1)))
    assert SESSION_MEMORY.due(night(tuesday, True, JobRecord(DAY, FAILED, started, "c1", exit_code=1)))
    assert SESSION_MEMORY.due(night(tuesday, True, JobRecord(DAY, INTERRUPTED, started, "c1")))
    assert not SESSION_MEMORY.due(night(tuesday, True, JobRecord(DAY, OK, started, "c1", exit_code=0)))


async def test_the_heartbeat_keeps_each_hour_s_extremes_and_the_latest_hours(tmp_path: Path) -> None:
    """Input: the heartbeat's readings from a made-up ``/proc`` at 15:10 and 15:50 (less available,
    more swap), then readings of 80 more hours. Output: the 15:00 hour holds the lowest available,
    the most swap and the engine's largest memory; only the latest 72 hours are kept; a file that
    cannot be read counts as none."""
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)

    def server(available_kb: int, swap_free_kb: int, rss_kb: int, swapped_kb: int) -> None:
        (proc / "meminfo").write_text(
            f"MemAvailable: {available_kb} kB\nSwapTotal: 1048576 kB\nSwapFree: {swap_free_kb} kB\n"
        )
        (proc / "self" / "status").write_text(f"VmRSS:\t{rss_kb} kB\nVmSwap:\t{swapped_kb} kB\n")

    server(700 * 1024, 1048576 - 100 * 1024, 750 * 1024, 0)
    await heartbeat.readings(tmp_path, lambda: at("15:10:00"), proc)
    server(520 * 1024, 1048576 - 210 * 1024, 906 * 1024, 20 * 1024)
    line = await heartbeat.readings(tmp_path, lambda: at("15:50:00"), proc)

    assert line.startswith("memory available 520 MB, swap used 210 MB; rss engine 906 MB")
    assert memory_hours.read(tmp_path) == {
        f"{DAY}T15": memory_hours.Hour(2, 520.0, 210.0, 906.0, 20.0),
    }
    for hour in range(1, 81):
        memory_hours.note(tmp_path, at("15:00:00") + timedelta(hours=hour), 500.0, 0.0, 600.0, 0.0)
    kept = memory_hours.read(tmp_path)
    assert len(kept) == memory_hours.KEEP_HOURS and f"{DAY}T15" not in kept
    (tmp_path / memory_hours.FILE).write_text("{not json")
    assert memory_hours.read(tmp_path) == {}
