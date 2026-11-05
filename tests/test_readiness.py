"""The readiness line (#614): a trading morning's routine notices as one Telegram line at 08:40, and
the problems that still go at once.

Input: the server's state folder as the collector, the trader and the scheduled jobs leave it (the
trader's store with its state and the day's picks, ``scheduled.json``, the last line told), the
morning swap's outcome, the commit the engine runs, a clock and the market calendar. Output: the
line, when it goes, and what goes to Telegram meanwhile.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from krx_quant.clock import FakeClock
from krx_quant.feeds.base import KST, kst_day
from krx_quant.market_calendar import MarketCalendar
from krx_quant.notify import Alerts
from krx_quant.readiness import (
    READY_AT,
    STATE_FILE,
    Readiness,
    Told,
    cells_said,
    commit_said,
    jobs_said,
    keep_ready,
    limits_said,
    model_said,
)
from krx_quant.scheduled import told
from krx_quant.scheduled.core import FAILED, INTERRUPTED, OK, REFUSED, RUNNING, JobRecord, JobState, Unposted
from krx_quant.scheduled.core import STATE_FILE as JOBS_FILE
from krx_quant.swap import MorningSwap, TrainingWatch
from krx_quant.trader.limits import Guard, Limits
from krx_quant.trader.policy import crown
from krx_quant.trader.selection import Pick, Selection
from krx_quant.trader.store import DB_NAME as TRADER_DB
from krx_quant.trader.store import TraderStore
from krx_quant.trader_watch import Beat, TraderWatch
from krx_quant.training_nights import Failure, TrainingNights
from tests.server_doubles import telegram
from tests.waiting import until

DAY = date(2026, 11, 6)  # a Friday
NIGHT = date(2026, 11, 5)  # the night before: its list is the morning's
MONDAY = date(2026, 11, 9)
CALENDAR = MarketCalendar(
    {NIGHT: True, DAY: True, date(2026, 11, 7): False, date(2026, 11, 8): False, MONDAY: True}
)
OLD = "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b"  # made-up commits
NEW = "5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2c3d4e"
LIMITS = {
    "daily": {"percent": 5.0, "won": 50_000.0, "used": 0.0, "reached_at": None},
    "cumulative": {"percent": 10.0, "won": 100_000.0, "used": 0.035, "reached_at": None},
}


def at(hhmm: str, day: date = DAY) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm), KST)


def pick(kind: str, predictor: str, *, practice: bool = False) -> Pick:
    return Pick(kind, "top", predictor, 0.55, 0.6, 400, 3.0, 1.2, 70_000.0, 100.0, 0.1, practice=practice)


def trader(
    state: Path,
    picks: tuple[Pick, ...] = (pick("barrier", "breiman"), pick("barrier", "consensus-final")),
    *,
    mode: str = "mock",
    written: str = "08:39:58",
    status: str = "running",
    reason: str | None = None,
) -> None:
    """The trader's store as it is at 08:40: its state, the champion (3) and its pick of 08:30, a
    challenger's (5) and an earlier day's pick beside it."""
    store = TraderStore(state / TRADER_DB)
    try:
        crown(store.db, at("22:40", NIGHT), 3, "first", {})
        store.add_selection(mode, at("08:30", NIGHT), Selection((pick("range", "turing"),), policy=3))
        store.add_selection(mode, at("08:30"), Selection(picks, policy=3, reason=reason))
        store.add_selection(mode, at("08:31"), Selection((pick("range", "gauss"),), policy=5))
        store.put_snapshot(at(written), {"mode": mode, "status": status, "limits": LIMITS, "position": None})
    finally:
        store.close()


def run(started: datetime, outcome: str, verdict: str | None = None) -> JobRecord:
    return JobRecord(kst_day(started - timedelta(hours=6)), outcome, started, OLD, 0, 9.0, verdict=verdict)


def jobs(state: Path, line: Readiness) -> None:
    """The night's runs as the scheduler tells them (``Readiness.ran``), and its state, which keeps a
    job's last run alone, with a result not posted yet."""
    runs = {
        "trader-report": run(at("22:31", NIGHT), OK),
        "prevalidate-ulam": run(at("23:10", NIGHT), FAILED),  # killed: a failure
        "prevalidate-bishop": run(at("02:00"), FAILED, REFUSED),  # a verdict, not a failure
        "smt-discovery-519": run(at("03:00"), INTERRUPTED),  # cut by a restart
    }
    for name, record in runs.items():
        line.ran(name, record)
    JobState(state / JOBS_FILE, runs, [Unposted("trader-report", NIGHT, 417, "body", (), ())]).save()


def ready(state: Path, sent: list[tuple[datetime, str]], clock: FakeClock, **kwargs: Any) -> Readiness:
    async def send(text: str) -> bool:
        sent.append((clock.now(), text))
        return True

    return Readiness(state, state / TRADER_DB, NEW, send, **kwargs)


def swap(
    day: date = DAY, night: date | None = NIGHT, changed: tuple[str, ...] = ("newton", "gauss")
) -> MorningSwap:
    return MorningSwap(day, night, night is not None, "applied" if night else "no list yet", changed)


def test_the_line_tells_the_deploy_the_models_the_champion_s_cells_the_limits_and_the_failed_jobs(
    tmp_path: Path,
) -> None:
    Told(NIGHT, at("08:40", NIGHT), OLD, date(2026, 11, 4)).save(tmp_path / STATE_FILE)
    trader(tmp_path)
    clock = FakeClock(at("08:40"))
    line = ready(tmp_path, [], clock)
    jobs(tmp_path, line)
    line.swapped(swap())

    assert line.line(DAY, at("08:40")) == (
        "준비 11-06: 배포 1a2b3c4 → 5d6e7f8 · 모델 11-05 밤 적용, 2개 바뀜"
        " · 매매(mock): barrier breiman, barrier consensus-final"
        " · 한도 여유 하루 50,000원, 누적 96,500원"
        " · 예약 작업 4개, 실패 1(prevalidate-ulam), 끊김 1(smt-discovery-519), 이슈에 못 올린 결과 1개"
    )


def test_a_champion_that_practises_is_practice_only_and_a_trader_that_stopped_is_told_down(
    tmp_path: Path,
) -> None:
    trader(
        tmp_path, (pick("barrier", "turing", practice=True),), written="08:12:03", status="cumulative_limit"
    )

    line = ready(tmp_path, [], FakeClock(at("08:40"))).line(DAY, at("08:40"))

    assert "trader 멈춤(마지막 상태 11-06 08:12) · 매매(mock): 연습만(barrier turing)" in line
    assert "한도 여유 하루 50,000원, 누적 96,500원(누적 한도로 멈춤)" in line


def test_the_champion_s_pick_without_a_cell_says_why_and_a_day_without_a_pick_says_so() -> None:
    assert cells_said({"picks": [], "reason": "no_pass"}) == "칸 없음(통과한 칸 없음)"
    assert cells_said({"picks": [], "reason": "something new"}) == "칸 없음(something new)"
    assert cells_said({"picks": [{"kind": "barrier", "predictor": "turing"}], "practice": 1}) == (
        "연습만(barrier turing)"
    )
    assert cells_said(None) == "오늘 고르기 없음"
    assert limits_said({"status": "running"}) == "한도 여유 하루 모름, 누적 모름"


def test_the_trader_s_part_without_a_store_or_a_state(tmp_path: Path) -> None:
    clock = FakeClock(at("08:40"))
    assert " · trader 없음 · " in ready(tmp_path, [], clock).line(DAY, at("08:40"))
    TraderStore(tmp_path / TRADER_DB).close()
    assert " · trader 상태 없음 · " in ready(tmp_path, [], clock).line(DAY, at("08:40"))


@pytest.mark.parametrize(
    ("outcome", "told_night", "said"),
    [
        (swap(), date(2026, 11, 4), "모델 11-05 밤 적용, 2개 바뀜"),
        (swap(changed=()), None, "모델 11-05 밤 적용, 바뀐 것 없음"),
        # No night trained since the last line: the same list is not told again (#614).
        (swap(), NIGHT, "모델 그대로(11-05 밤)"),
        (
            MorningSwap(DAY, NIGHT, False, "final at 11-06 07:45, after 07:30", ()),
            None,
            "모델 11-05 밤 쓰지 않음(final at 11-06 07:45, after 07:30)",
        ),
        (swap(night=None), None, "모델 밤 학습 목록 없음"),
        (None, None, "모델 아침 교체를 모름"),
    ],
)
def test_the_morning_swap_is_told_by_whether_its_list_runs_and_how_many_models_changed(
    outcome: MorningSwap | None, told_night: date | None, said: str
) -> None:
    assert model_said(outcome, told_night) == said
    assert model_said(outcome, told_night, swaps=False) == "모델 교체 없음(저장소 없음)"


def test_the_commit_is_told_after_the_one_the_last_line_told() -> None:
    assert commit_said(NEW, OLD) == "배포 1a2b3c4 → 5d6e7f8"
    assert commit_said(NEW, NEW) == "커밋 5d6e7f8 그대로"
    assert commit_said(NEW, None) == "커밋 5d6e7f8"


def test_the_jobs_part_counts_the_runs_told_and_reads_the_state_for_the_rest(tmp_path: Path) -> None:
    path = tmp_path / JOBS_FILE
    failed = {"job": "prevalidate-ulam", "outcome": FAILED, "verdict": None}
    assert jobs_said([], path) == "예약 작업 없음"
    assert jobs_said([failed, {**failed, "day": "x"}], path) == "예약 작업 2개, 실패 2(prevalidate-ulam ×2)"
    JobState(path, {"probe-kis-orders-403": JobRecord(DAY, RUNNING, at("08:35"), NEW)}).save()
    assert jobs_said([], path) == "예약 작업 없음, 도는 중 1"
    path.write_text("{")
    assert jobs_said([], path) == "예약 작업 없음, 예약 작업 상태를 읽지 못함"
    assert path.read_text() == "{", "never set aside: the scheduler's file"


async def test_a_failure_is_told_by_the_next_line_that_goes_though_its_job_ran_well_since(
    tmp_path: Path,
) -> None:
    """Codex on #616: the scheduler's state keeps each job's last run alone. Thursday night's failure,
    Friday's line missed (Telegram did not take it), Friday night's success of the same job: Monday's
    line still tells the failure, and a line after it no more."""
    went = [False, True, True]

    async def send(text: str) -> bool:
        sent.append(text)
        return went[len(sent) - 1]

    sent: list[str] = []
    line = Readiness(tmp_path, tmp_path / TRADER_DB, NEW, send)
    line.ran("prevalidate-ulam", run(at("23:10", date(2026, 11, 5)), FAILED))
    assert not await line.send(DAY, at("08:40"))  # Friday's line did not go
    friday = run(at("23:10"), OK)
    line.ran("prevalidate-ulam", friday)
    JobState(tmp_path / JOBS_FILE, {"prevalidate-ulam": friday}).save()  # the success alone

    again = Readiness(tmp_path, tmp_path / TRADER_DB, NEW, send)  # the 07:20 restart: kept on disk
    assert await again.send(MONDAY, at("08:40", MONDAY))
    again.ran("trader-report", run(at("22:31", MONDAY), OK))
    assert await again.send(date(2026, 11, 10), at("08:40", date(2026, 11, 10)))

    assert sent[0].endswith(" · 예약 작업 1개, 실패 1(prevalidate-ulam)"), sent[0]
    assert sent[1].endswith(" · 예약 작업 2개, 실패 1(prevalidate-ulam)"), sent[1]
    assert sent[2].endswith(" · 예약 작업 1개, 실패 없음"), "told once it went"
    assert Told.load(tmp_path / STATE_FILE).runs == ()


async def test_a_run_that_ends_while_the_line_goes_waits_for_the_next(tmp_path: Path) -> None:
    async def send(text: str) -> bool:
        line.ran("trader-report", run(at("08:40"), OK))  # ends meanwhile
        return True

    line = Readiness(tmp_path, tmp_path / TRADER_DB, NEW, send)
    line.ran("prevalidate-ulam", run(at("23:10", NIGHT), FAILED))
    assert await line.send(DAY, at("08:40"))
    assert [r["job"] for r in Told.load(tmp_path / STATE_FILE).runs] == ["trader-report"]


async def test_the_line_goes_once_a_trading_day_at_08_40_and_not_again_after_a_restart(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """At 08:40 after the 08:00 swap; an engine started again at 08:45 sends nothing; the next trading
    day's line tells the same commit and the same night as unchanged (no night trained since)."""
    caplog.set_level(logging.INFO, logger="krx_quant.readiness")
    trader(tmp_path)
    sent: list[tuple[datetime, str]] = []
    clock = FakeClock(at("07:00"))
    first = ready(tmp_path, sent, clock)
    task = asyncio.create_task(keep_ready(first, CALENDAR, clock))
    try:
        clock.set(at("08:00:31"))
        first.swapped(swap())
        clock.set(datetime.combine(DAY, READY_AT, KST))
        await until(lambda: len(sent) == 1)
    finally:
        task.cancel()
    assert sent[0][0] == at("08:40")
    assert sent[0][1].startswith("준비 11-06: 커밋 5d6e7f8 · 모델 11-05 밤 적용, 2개 바뀜 · 매매(mock): ")

    clock.set(at("08:45"))  # the engine started again
    again = ready(tmp_path, sent, clock)
    task = asyncio.create_task(keep_ready(again, CALENDAR, clock))
    try:
        again.swapped(swap())  # the restart's swap of the same morning
        await until(
            lambda: any("2026-11-06 was tried before this start" in r.getMessage() for r in caplog.records)
        )
        clock.set(at("08:00:31", MONDAY))
        again.swapped(swap(MONDAY))  # no night trained since Friday's: the same list, applied already
        clock.set(at("08:40", MONDAY))
        await until(lambda: len(sent) == 2)
    finally:
        task.cancel()
    assert [when for when, _ in sent] == [at("08:40"), at("08:40", MONDAY)], "none after the restart"
    assert sent[1][1].startswith("준비 11-09: 커밋 5d6e7f8 그대로 · 모델 그대로(11-05 밤) · ")


async def test_the_line_waits_for_the_swap_until_the_open(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="krx_quant.readiness")
    sent: list[tuple[datetime, str]] = []
    clock = FakeClock(at("08:39:30"))  # the engine started again just before 08:40: no swap yet
    line = ready(tmp_path, sent, clock)
    task = asyncio.create_task(keep_ready(line, CALENDAR, clock))
    try:
        clock.set(at("08:40"))
        await until(
            lambda: any("waits for the morning swap until 09:00" in r.getMessage() for r in caplog.records)
        )
        clock.set(at("09:00"))
        await until(lambda: len(sent) == 1)
    finally:
        task.cancel()
    assert sent[0][0] == at("09:00"), "it waited for the swap until the open"
    assert " · 모델 아침 교체를 모름 · " in sent[0][1]
    assert any("no morning swap of 2026-11-06 by the open" in r.getMessage() for r in caplog.records)


async def test_an_engine_started_after_the_open_sends_no_line_that_day(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="krx_quant.readiness")
    sent: list[tuple[datetime, str]] = []
    clock = FakeClock(at("09:30"))
    task = asyncio.create_task(keep_ready(ready(tmp_path, sent, clock), CALENDAR, clock))
    try:
        await until(
            lambda: any(
                "is not sent: the engine started after the open" in r.getMessage() for r in caplog.records
            )
        )
    finally:
        task.cancel()
    assert sent == []


async def test_a_line_that_did_not_go_leaves_the_commit_night_and_jobs_to_the_next(tmp_path: Path) -> None:
    async def refused(text: str) -> bool:
        return False

    line = Readiness(tmp_path, tmp_path / TRADER_DB, NEW, refused)
    Told(NIGHT, at("08:40", NIGHT), OLD, NIGHT).save(tmp_path / STATE_FILE)
    line.told = Told.load(tmp_path / STATE_FILE)
    line.swapped(swap(night=DAY))
    line.ran("trader-report", run(at("22:31", NIGHT), OK))

    assert not await line.send(DAY, at("08:40"))
    assert Told.load(tmp_path / STATE_FILE) == Told(DAY, at("08:40", NIGHT), OLD, NIGHT, line.told.runs)
    assert len(line.told.runs) == 1


# What is never held for the line: sent at once, as before (#614 "바꾸지 않는다").


@asynccontextmanager
async def alerts() -> AsyncIterator[tuple[Alerts, list[str]]]:
    async with telegram(200) as (url, sent), aiohttp.ClientSession() as session:
        found = Alerts(session, "123:fake", "42", url)
        try:
            yield found, sent
        finally:
            await found.close()


async def test_the_trader_s_loss_limit_and_a_withheld_order_go_at_once_before_the_line(
    tmp_path: Path,
) -> None:
    """At 08:35, before the readiness line: the trader's own notices (the broker's last check withholding
    a buy, #543; the daily loss limit) are on Telegram at once."""
    async with alerts() as (found, sent):
        store = TraderStore(tmp_path / TRADER_DB)
        try:
            guard = Guard(store, "mock", Limits(1_000_000, 0.5, 20.0), at("08:35"), found.notice)
            guard.order_failed(at("08:35:01"), "buy", "error", "the broker's last check withheld it")
            await until(lambda: len(sent) == 1)
            guard.mark(-6_000.0, at("08:35:02"))
            await until(lambda: len(sent) == 2)
        finally:
            store.close()
    assert sent[0].startswith("[알림] 자동 매매(mock): 매수 주문을 보내지 못했습니다(the broker's last check")
    assert sent[1].startswith("[알림] 자동 매매(mock): 하루 손실 한도에 닿았습니다.")


async def test_the_trader_s_silence_and_a_failed_night_go_at_once(tmp_path: Path) -> None:
    async with alerts() as (found, sent):
        watch = TraderWatch(tmp_path / TRADER_DB, CALENDAR, found, tmp_path)
        watch.check(at("09:10:00"), Beat(at("09:08:59"), "mock", False, None))
        await until(lambda: len(sent) == 1)
        nights = TrainingWatch(TrainingNights(tmp_path), found, CALENDAR, lambda: at("23:40", NIGHT))
        nights.ended(NIGHT, Failure("exit -9 (SIGKILL), 마지막으로 시작한 단계 turing", -9, "turing"))
        await until(lambda: len(sent) == 2)
    assert sent[0].startswith("[장애] 자동 매매(mock): trader가 ") and "상태를 쓰지 않았습니다" in sent[0]
    assert sent[1].startswith("[장애] 2026-11-05 밤 학습이 실패했습니다: exit -9 (SIGKILL)")


async def test_a_scheduled_job_s_line_is_logged_and_not_sent(caplog: pytest.LogCaptureFixture) -> None:
    """The scheduler's lines (a run's outcome, a deferral) go to the log: the readiness line counts the
    runs."""
    caplog.set_level(logging.INFO, logger="krx_quant.scheduled")
    line = "예약 작업 prevalidate-ulam(2026-11-05 밤): 실패, 종료 코드 -9, 51분. 댓글: …"

    assert await told(line)

    [record] = [r for r in caplog.records if r.name == "krx_quant.scheduled"]
    assert (record.levelno, record.getMessage()) == (logging.INFO, f"scheduled: {line}")
