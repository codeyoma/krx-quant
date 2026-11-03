"""Session jobs (#403): scheduled jobs that run in the market's hours on a trading day, not at
night. The order probe's job was the first of them; it left ``JOBS`` (#484), and its command stays
for a run by hand.

The clock is a fake one and the jobs' processes stand-ins (``tests/test_scheduled.py``).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import time, timedelta
from pathlib import Path

import aiohttp
import pytest

from krx_quant.cli import main
from krx_quant.clock import FakeClock
from krx_quant.config import Settings
from krx_quant.github import GitHub
from krx_quant.scheduled import (
    INTERRUPTED,
    JOBS,
    OK,
    RUNNING,
    SESSION_UNTIL,
    STATE_FILE,
    Facts,
    Job,
    JobState,
    Night,
    Scheduler,
    Target,
    Unposted,
    session_facts,
    session_window,
)
from krx_quant.trader.store import DB_NAME as TRADER_DB
from krx_quant.trader.store import TraderStore
from tests.fake_github import REPO, TOKEN, fake_github
from tests.test_scheduled import (
    AT,
    CALENDAR,
    COMMIT,
    FRIDAY,
    MONDAY,
    SATURDAY,
    SUNDAY,
    THURSDAY,
    Runs,
    Told,
    at,
    saved,
    scheduling,
    settle,
)
from tests.waiting import until

TEN = time(10, 0)


def each_day(night: Night) -> bool:
    return night.record is None or night.record.day != night.day


def session_job(
    name: str = "day-job", due: object = each_day, timeout: timedelta = timedelta(minutes=15)
) -> Job:
    assert callable(due)
    return Job(name=name, due=due, command=(name,), timeout=timeout, targets=(Target(1),), session=TEN)


async def test_a_session_job_runs_at_its_time_on_trading_days_and_never_at_night(tmp_path: Path) -> None:
    async with scheduling(tmp_path, at(THURSDAY, "06:00"), [session_job()]) as (clock, runs, _, state):
        clock.set(at(THURSDAY, "09:59"))
        await settle()
        assert runs.started == []
        clock.set(at(THURSDAY, "10:00"))
        await until(lambda: runs.started == [("day-job", at(THURSDAY, "10:00"))])
        clock.set(at(THURSDAY, "22:30"))  # the night's jobs: not this one
        await settle()
        clock.set(at(FRIDAY, "10:00"))
        await until(lambda: len(runs.started) == 2)
        for day in (SATURDAY, SUNDAY):  # not trading days
            clock.set(at(day, "10:00"))
            await settle()
        clock.set(at(MONDAY, "10:00"))
        await until(lambda: len(runs.started) == 3)
        assert [when for _, when in runs.started] == [at(d, "10:00") for d in (THURSDAY, FRIDAY, MONDAY)]
        await until(lambda: state.jobs["day-job"].day == MONDAY)


async def test_a_collector_started_in_the_session_runs_a_due_session_job_at_once(tmp_path: Path) -> None:
    async with scheduling(tmp_path, at(FRIDAY, "11:30"), [session_job()]) as (_, runs, _, state):
        await until(lambda: runs.started == [("day-job", at(FRIDAY, "11:30"))])
        await until(lambda: state.jobs["day-job"].outcome == OK and state.jobs["day-job"].day == FRIDAY)


async def test_a_session_job_that_cannot_end_by_15_15_waits_for_the_next_trading_day(tmp_path: Path) -> None:
    async with scheduling(tmp_path, at(FRIDAY, "15:05"), [session_job()]) as (clock, runs, told, _):
        await until(lambda: bool(told))
        assert told == [
            "예약 작업 day-job(2026-10-16 장중): 지금 시작하면 제한 시간(15분)이 15:15을 넘어 "
            "다음 거래일로 미룹니다."
        ]
        assert runs.started == []
        clock.set(at(MONDAY, "10:00"))
        await until(lambda: runs.started == [("day-job", at(MONDAY, "10:00"))])


async def test_a_collector_started_after_the_session_waits_for_the_next_trading_day(tmp_path: Path) -> None:
    async with scheduling(tmp_path, at(FRIDAY, "15:20"), [session_job()]) as (clock, runs, told, _):
        await settle()
        assert runs.started == [] and told == []  # nothing to say: the session is over
        clock.set(at(MONDAY, "10:00"))
        await until(lambda: len(runs.started) == 1)


async def test_a_session_job_cut_by_a_restart_runs_the_next_trading_day(tmp_path: Path) -> None:
    clock = FakeClock(at(FRIDAY, "09:00"))
    stuck = Runs(clock)
    stuck.hold()
    async with scheduling(tmp_path, at(FRIDAY, "09:00"), [session_job()], runs=stuck, clock=clock):
        clock.set(at(FRIDAY, "10:00"))
        await until(lambda: len(stuck.started) == 1)
    job_state = saved(tmp_path)["jobs"]
    assert isinstance(job_state, dict) and job_state["day-job"]["outcome"] == RUNNING

    async with scheduling(tmp_path, at(FRIDAY, "10:05"), [session_job()]) as (clock, runs, told, state):
        assert state.jobs["day-job"].outcome == INTERRUPTED
        assert told == [
            "예약 작업 day-job(2026-10-16 장중)이 엔진 재시작으로 끊겼습니다. 다음 거래일 장중에 다시 돕니다."
        ]
        await settle()
        assert runs.started == []  # not the same day again
        clock.set(at(MONDAY, "10:00"))
        await until(lambda: runs.started == [("day-job", at(MONDAY, "10:00"))])


async def test_a_session_job_is_asked_with_the_session_s_facts_and_posts_as_a_session_s(
    tmp_path: Path,
) -> None:
    seen: list[Night] = []

    def due(night: Night) -> bool:
        seen.append(night)
        return night.record is None

    night_facts, day_facts = Facts(recorded_days=12), Facts(trader_kis=True)
    async with fake_github() as hub:
        async with scheduling(
            tmp_path,
            at(FRIDAY, "09:00"),
            [session_job(due=due)],
            github=hub,
            facts=night_facts,
            session_facts=day_facts,
        ) as (clock, _, told, state):
            clock.set(at(FRIDAY, "10:00"))
            await until(lambda: "day-job" in state.jobs and bool(state.jobs["day-job"].comments))
        assert [n.facts for n in seen] == [day_facts] and seen[0].day == FRIDAY and seen[0].trading_day
        assert hub.bodies(1)[-1].startswith("**예약 작업 `day-job`** · 2026-10-16 장중 · 성공 · 커밋 ")
        assert told[-1].startswith("예약 작업 day-job(2026-10-16 장중): 성공, 종료 코드 0")


async def test_the_session_loop_leaves_the_results_not_posted_to_the_night(tmp_path: Path) -> None:
    # It wakes at midnight, inside the night: posting them there too could post one twice beside a
    # night's job posting it. They go up at the next night or start (the night's own rule).
    state = JobState(tmp_path / STATE_FILE, unposted=[Unposted("night-job", THURSDAY, 7, "body", (), ())])
    async with fake_github() as hub, aiohttp.ClientSession() as session:
        scheduler = Scheduler(
            [session_job(due=lambda night: False)],
            AT,
            CALENDAR,
            state,
            GitHub(session, TOKEN, REPO, hub.url),
            Told(),
            FakeClock(at(FRIDAY, "10:00")),
            commit=COMMIT,
        )
        await asyncio.wait_for(scheduler.run_session(FRIDAY), 5)
        assert hub.bodies(7) == [] and len(state.unposted) == 1
        await asyncio.wait_for(scheduler.run_night(FRIDAY), 5)
        assert hub.bodies(7) == ["body"] and state.unposted == []


async def test_the_session_facts_are_the_settings_alone() -> None:
    keyed = Settings(
        kis_app_key="",
        kis_app_secret="",
        trader_kis_app_key="a" * 20,
        trader_kis_app_secret="b" * 40,
        trader_kis_account="98765432-01",
    )
    assert await session_facts(keyed) == Facts(trader_kis=True)
    assert await session_facts(replace(keyed, trader_kis_account=None)) == Facts()


# The order probe's job, retired (#484)


def test_the_order_probe_s_job_is_retired_and_its_command_stays(capsys: pytest.CaptureFixture[str]) -> None:
    """Its run of 2026-10-30 did every step but the fill notice, which the trader's recorder and
    ``fill-notice-layout-403`` measure instead; left in, it would order again on the mock account."""
    assert "probe-kis-orders-403" not in {j.name for j in JOBS}
    assert ("probe-kis-orders",) not in {j.command for j in JOBS}
    with pytest.raises(SystemExit):
        main(["--help"])
    assert "probe-kis-orders" in capsys.readouterr().out, "a run by hand"


def test_each_session_job_ends_by_15_15_after_the_opening() -> None:
    for job in (j for j in JOBS if j.session is not None):
        start, end = session_window(job, FRIDAY)
        assert end.time() == SESSION_UNTIL and start + job.timeout <= end
        assert job.session is not None and job.session >= time(9, 5)


async def test_the_session_facts_read_the_mode_the_trader_last_started_in(tmp_path: Path) -> None:
    settings = Settings(kis_app_key="", kis_app_secret="", state_dir=tmp_path)
    assert (await session_facts(settings)).trader_mock is False, "no trader yet"
    store = TraderStore(tmp_path / TRADER_DB)
    try:
        store.add_run(at(THURSDAY, "08:00"), "paper", {})
        assert (await session_facts(settings)).trader_mock is False
        store.add_run(at(FRIDAY, "08:00"), "mock", {})
    finally:
        store.close()
    assert (await session_facts(settings)).trader_mock is True
