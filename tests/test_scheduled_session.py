"""Session jobs (#403): scheduled jobs that run in the market's hours on a trading day, not at
night, and the order probe's job, the first of them.

The clock is a fake one and the jobs' processes stand-ins (``tests/test_scheduled.py``), but for
the order probe's real command, which stops before it calls KIS (``conftest.no_trader_kis``).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, time, timedelta
from pathlib import Path

import aiohttp
import pytest

from krx_quant.clock import FakeClock
from krx_quant.config import Settings
from krx_quant.github import GitHub
from krx_quant.order_probe import CLOSED, INCOMPLETE, NO_KEYS
from krx_quant.scheduled import (
    FAILED,
    INTERRUPTED,
    JOBS,
    KIS_ORDER_PROBE,
    OK,
    RUNNING,
    SESSION_UNTIL,
    STATE_FILE,
    Facts,
    Job,
    JobRecord,
    JobState,
    Night,
    Scheduler,
    Target,
    session_facts,
    session_window,
)
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
from tests.test_subscription_limit import until

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
        assert hub.bodies(1)[-1].startswith("**예약 작업 `day-job`** · 2026-10-16 장중 · 커밋 ")
        assert told[-1].startswith("예약 작업 day-job(2026-10-16 장중): 성공, 종료 코드 0")


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


# The order probe's job


def day(when: date, record: JobRecord | None = None, *, keys: bool = False) -> Night:
    return Night(when, CALENDAR.is_trading_day(when), COMMIT, record, {}, Facts(trader_kis=keys))


def test_the_order_probe_runs_in_the_session_and_posts_to_403() -> None:
    assert KIS_ORDER_PROBE in JOBS
    assert KIS_ORDER_PROBE.name == "probe-kis-orders-403"
    assert KIS_ORDER_PROBE.command == ("probe-kis-orders",)
    assert KIS_ORDER_PROBE.session == time(10, 0)
    assert KIS_ORDER_PROBE.timeout == timedelta(minutes=15)
    assert [t.issue for t in KIS_ORDER_PROBE.targets] == [403]
    assert KIS_ORDER_PROBE.label == ("ready-for-agent",)
    note = KIS_ORDER_PROBE.targets[0].note
    assert "키, 계좌번호, 주문번호, 시세 값이 없다" in note and "#404" in note


def test_each_session_job_ends_by_15_15_after_the_opening() -> None:
    sessions = [j for j in JOBS if j.session is not None]
    assert sessions == [KIS_ORDER_PROBE]
    for job in sessions:
        start, end = session_window(job, FRIDAY)
        assert end.time() == SESSION_UNTIL and start + job.timeout <= end
        assert job.session is not None and job.session >= time(9, 5)


def test_the_order_probe_runs_each_trading_day_until_a_run_does_every_step() -> None:
    due = KIS_ORDER_PROBE.due
    assert due(day(FRIDAY)) and not due(day(SATURDAY)), "trading days only"
    ran = JobRecord(FRIDAY, OK, at(FRIDAY, "10:00"), COMMIT, 0, 90.0)
    assert not due(day(MONDAY, ran)), "done: every step ran"
    for code in (1, INCOMPLETE, CLOSED):
        failed = replace(ran, outcome=FAILED, exit_code=code)
        assert due(day(MONDAY, failed)), code
        assert not due(day(FRIDAY, failed)), "not twice a day"
    assert due(day(MONDAY, replace(ran, outcome=INTERRUPTED)))


def test_without_the_keys_the_order_probe_says_so_once_then_waits_for_them() -> None:
    due = KIS_ORDER_PROBE.due
    no_keys = JobRecord(FRIDAY, FAILED, at(FRIDAY, "10:00"), COMMIT, NO_KEYS, 1.0)
    assert not due(day(MONDAY, no_keys)), "said once: no post a day until the keys come"
    assert due(day(MONDAY, no_keys, keys=True)), "the keys set: it runs"
    assert not due(day(SATURDAY, no_keys, keys=True))


@pytest.fixture
def empty_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An engine's state directory, no ``.env`` and no trader keys (``conftest.no_trader_kis``)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


async def test_the_order_probe_job_runs_the_real_command_and_without_keys_says_so(empty_server: Path) -> None:
    state = JobState.load(empty_server / STATE_FILE)
    async with fake_github() as hub, aiohttp.ClientSession() as session:
        scheduler = Scheduler(
            [KIS_ORDER_PROBE],
            AT,
            CALENDAR,
            state,
            GitHub(session, TOKEN, REPO, hub.url),
            Told(),
            FakeClock(at(FRIDAY, "10:00")),
            commit=COMMIT,
        )
        await asyncio.wait_for(scheduler.run_session(FRIDAY), 25)
    body = hub.bodies(403)[-1]
    assert body.startswith("**예약 작업 `probe-kis-orders-403`** · 2026-10-16 장중 · 커밋 ")
    assert "다시 돌리기: `docker compose run --rm engine probe-kis-orders`" in body
    assert "KIS 모의투자 주문 프로브 (#403)" in body and "모의 키가 없어 주문하지 않았다" in body
    record = state.jobs["probe-kis-orders-403"]
    assert (record.outcome, record.exit_code) == (FAILED, NO_KEYS)
    assert not KIS_ORDER_PROBE.due(day(MONDAY, record)), "it waits for the keys"
