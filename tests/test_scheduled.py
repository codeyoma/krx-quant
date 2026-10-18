"""Scheduled jobs (#215): when they run, what they remember, and what they post.

The clock is a fake one (``FakeClock``); the jobs' processes are stand-ins, except where
the process itself is the point (``run_command``) and for the diagnosis job, whose real
command is cheap on an empty data directory.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

import aiohttp
import pytest

from krx_quant.barriers import BARRIER
from krx_quant.clock import FakeClock
from krx_quant.collector import run_collector
from krx_quant.config import Settings, SettingsError
from krx_quant.fake_kis import FakeKis
from krx_quant.feeds.base import KST
from krx_quant.github import GitHub
from krx_quant.market_calendar import MarketCalendar
from krx_quant.notify import Alerts
from krx_quant.scheduled import (
    DIAGNOSE,
    ENTRY_DELAY,
    FAILED,
    INTERRUPTED,
    JOBS,
    NIGHT_PROBE,
    OK,
    RUNNING,
    SIGNAL_REVIEW,
    STATE_FILE,
    STRUCTURE_CHECK,
    STRUCTURE_SUPPLY,
    SUPPLY_PROBE,
    Attachment,
    Facts,
    Job,
    JobRecord,
    JobState,
    Night,
    Ran,
    Scheduler,
    Target,
    first_night,
    run_command,
    sections,
    window,
)
from krx_quant.swap import TRAINING_TIMEOUT, NightTraining, keep_training
from tests.fake_github import REPO, TOKEN, FakeGitHub, fake_github
from tests.test_collector import settings_for
from tests.test_subscription_limit import until

THURSDAY, FRIDAY, SATURDAY, SUNDAY, MONDAY = (date(2026, 10, d) for d in (15, 16, 17, 18, 19))
CALENDAR = MarketCalendar({THURSDAY: True, FRIDAY: True, SATURDAY: False, SUNDAY: False, MONDAY: True})
AT = time(22, 30)
COMMIT = "0123456789abcdef0123"


def at(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm), KST)


async def settle() -> None:
    """Let every task run until it waits on the clock or a process."""
    for _ in range(20):
        await asyncio.sleep(0.005)


def every_night(night: Night) -> bool:
    return night.record is None or night.record.day != night.day


def never_ran(night: Night) -> bool:
    return not night.ran


def job(
    name: str = "job",
    due: Callable[[Night], bool] = never_ran,
    timeout: timedelta = timedelta(hours=1),
    targets: tuple[Target, ...] = (Target(1),),
    attach: tuple[Attachment, ...] = (),
) -> Job:
    return Job(name=name, due=due, command=(name, "--flag"), timeout=timeout, targets=targets, attach=attach)


class Runs:
    """Stand-in job processes: each run is recorded with the time it started; ``hold`` keeps
    them going until ``release``."""

    def __init__(self, clock: FakeClock, ran: Ran | None = None) -> None:
        self.clock = clock
        self.ran = ran or Ran(0, "out\n", "")
        self.started: list[tuple[str, datetime]] = []
        self.ended: list[str] = []
        self.held = asyncio.Event()
        self.held.set()

    def hold(self) -> None:
        self.held.clear()

    def release(self) -> None:
        self.held.set()

    def names(self) -> list[str]:
        return [name for name, _ in self.started]

    async def __call__(self, args: Sequence[str], timeout: timedelta) -> Ran:
        self.started.append((args[0], self.clock.now()))
        await self.held.wait()
        self.ended.append(args[0])
        return self.ran


class Writes(Runs):
    """Stand-in runs that write files as they run: ``files`` gives each path its text."""

    def __init__(self, clock: FakeClock, ran: Ran, files: Mapping[Path, str]) -> None:
        super().__init__(clock, ran)
        self.files = files

    async def __call__(self, args: Sequence[str], timeout: timedelta) -> Ran:
        for path, text in self.files.items():
            path.write_text(text)
        return await super().__call__(args, timeout)


class Told(list[str]):
    async def __call__(self, text: str) -> bool:
        self.append(text)
        return True

    def saying(self, part: str) -> list[str]:
        return [line for line in self if part in line]


@asynccontextmanager
async def scheduling(
    tmp_path: Path,
    start: datetime,
    jobs: Sequence[Job],
    *,
    runs: Runs | None = None,
    github: FakeGitHub | None = None,
    token: str | None = TOKEN,
    training: NightTraining | None = None,
    facts: Facts | Callable[[], Facts] | None = None,
    told: Told | None = None,
    secrets: Sequence[str] = (),
    clock: FakeClock | None = None,
) -> AsyncIterator[tuple[FakeClock, Runs, Told, JobState]]:
    """A scheduler running on a fake clock until the block ends (the collector stops)."""
    clock = clock or FakeClock(start)
    runs = runs or Runs(clock)
    told = Told() if told is None else told
    state = JobState.load(tmp_path / STATE_FILE)

    async def read_facts() -> Facts:
        if callable(facts):  # read each night, as the collector does
            return facts()
        return facts or Facts()

    async with aiohttp.ClientSession() as session:
        hub = GitHub(session, token if github else None, REPO, github.url if github else "http://127.0.0.1:9")
        scheduler = Scheduler(
            jobs,
            AT,
            CALENDAR,
            state,
            hub,
            told,
            clock,
            commit=COMMIT,
            secrets=secrets,
            training=training,
            facts=read_facts,
            runner=runs,
        )
        task = asyncio.create_task(scheduler.run())
        try:
            await settle()
            yield clock, runs, told, state
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def saved(tmp_path: Path) -> dict[str, object]:
    data: dict[str, object] = json.loads((tmp_path / STATE_FILE).read_text())
    return data


# When the jobs run


def test_the_night_runs_from_its_evening_to_05_30() -> None:
    assert window(FRIDAY, AT) == (at(FRIDAY, "22:30"), at(SATURDAY, "05:30"))
    assert window(FRIDAY, time(1, 0)) == (at(SATURDAY, "01:00"), at(SATURDAY, "05:30"))
    assert first_night(AT, at(FRIDAY, "12:00")) == FRIDAY
    assert first_night(AT, at(SATURDAY, "03:00")) == FRIDAY  # under way: at once
    assert first_night(AT, at(SATURDAY, "06:00")) == SATURDAY  # the 06:00 deploy waits


async def test_jobs_start_at_22_30_every_day_trading_or_not(tmp_path: Path) -> None:
    async with scheduling(tmp_path, at(FRIDAY, "12:00"), [job(due=every_night)]) as (clock, runs, _, state):
        clock.set(at(FRIDAY, "22:29"))
        await settle()
        assert runs.started == []
        clock.set(at(FRIDAY, "22:30"))
        await until(lambda: len(runs.started) == 1)
        clock.set(at(SATURDAY, "22:30"))  # not a trading day
        await until(lambda: len(runs.started) == 2)
        assert [when for _, when in runs.started] == [at(FRIDAY, "22:30"), at(SATURDAY, "22:30")]
        assert state.jobs["job"].day == SATURDAY


async def test_the_jobs_wait_for_the_night_s_training(tmp_path: Path) -> None:
    training = NightTraining()
    training.started(at(FRIDAY, "21:30"), at(MONDAY, "21:30"))
    async with scheduling(tmp_path, at(FRIDAY, "22:00"), [job()], training=training) as (clock, runs, _, _):
        clock.set(at(FRIDAY, "22:30"))
        await settle()
        assert runs.started == []
        clock.set(at(FRIDAY, "23:10"))
        training.finished()
        await until(lambda: runs.started == [("job", at(FRIDAY, "23:10"))])


async def test_a_training_due_at_a_start_inside_the_night_is_waited_for(tmp_path: Path) -> None:
    # A restart at 23:00: keep_training catches the night up at once, and says so first.
    training = NightTraining()
    training.due(at(FRIDAY, "21:30"))
    async with scheduling(tmp_path, at(FRIDAY, "23:00"), [job()], training=training) as (clock, runs, _, _):
        assert runs.started == []
        training.started(at(FRIDAY, "23:00"), at(MONDAY, "21:30"))
        await settle()
        assert runs.started == []
        training.finished()
        await until(lambda: len(runs.started) == 1)


async def test_a_training_still_going_after_its_time_limit_holds_the_jobs_no_longer(tmp_path: Path) -> None:
    # A night on Spot whose result never comes (the Cost Guard turned it off, the run failed).
    training = NightTraining()
    training.started(at(FRIDAY, "21:30"), at(MONDAY, "21:30"))
    async with scheduling(tmp_path, at(FRIDAY, "22:30"), [job()], training=training) as (clock, runs, _, _):
        assert runs.started == []
        clock.set(at(FRIDAY, "21:30") + TRAINING_TIMEOUT)
        await until(lambda: runs.started == [("job", at(SATURDAY, "03:30"))])


async def test_the_jobs_run_one_at_a_time_in_the_list_s_order(tmp_path: Path) -> None:
    jobs = [job("first"), job("second")]
    async with scheduling(tmp_path, at(FRIDAY, "22:00"), jobs) as (clock, runs, _, _):
        runs.hold()
        clock.set(at(FRIDAY, "22:30"))
        await until(lambda: runs.names() == ["first"])
        await settle()
        assert runs.names() == ["first"]
        runs.release()
        await until(lambda: runs.ended == ["first", "second"])
        assert runs.names() == ["first", "second"]


async def test_a_job_that_would_end_after_05_30_waits_for_the_next_night(tmp_path: Path) -> None:
    training = NightTraining()
    training.started(at(FRIDAY, "21:30"), at(MONDAY, "21:30"))
    jobs = [job("long", timeout=timedelta(hours=3)), job("short", timeout=timedelta(minutes=30))]
    async with scheduling(tmp_path, at(FRIDAY, "22:30"), jobs, training=training) as (clock, runs, told, _):
        clock.set(at(SATURDAY, "03:00"))  # the training ends late
        training.finished()
        await until(lambda: runs.names() == ["short"])
        assert told.saying("long(2026-10-16 밤)") == [
            "예약 작업 long(2026-10-16 밤): 지금 시작하면 제한 시간(3시간)이 05:30을 넘어 "
            "다음 밤으로 미룹니다."
        ]
        clock.set(at(SATURDAY, "22:30"))
        await until(lambda: runs.names() == ["short", "long"])


async def test_a_training_not_over_by_a_job_s_last_start_postpones_it(tmp_path: Path) -> None:
    training = NightTraining()
    training.started(at(FRIDAY, "23:30"), at(MONDAY, "21:30"))  # a catch-up after a late restart
    big = [job("big", timeout=timedelta(hours=4))]
    async with scheduling(tmp_path, at(FRIDAY, "23:30"), big, training=training) as (clock, runs, told, _):
        clock.set(at(SATURDAY, "01:30"))  # 05:30 - 4 hours
        await until(lambda: bool(told))
        assert told == ["예약 작업 big(2026-10-16 밤): 밤 학습이 01:30까지 끝나지 않아 다음 밤으로 미룹니다."]
        assert runs.started == []


async def test_a_job_that_ran_does_not_run_again_whether_it_passed_or_failed(tmp_path: Path) -> None:
    clock = FakeClock(at(FRIDAY, "22:00"))
    failing = Runs(clock, Ran(1, "", "Traceback ..."))
    jobs = [job("fails")]
    async with scheduling(tmp_path, at(FRIDAY, "22:00"), jobs, runs=failing, clock=clock) as (
        _,
        runs,
        _,
        state,
    ):
        clock.set(at(FRIDAY, "22:30"))
        await until(lambda: "fails" in state.jobs and state.jobs["fails"].outcome == FAILED)
        clock.set(at(SATURDAY, "22:30"))
        clock.set(at(SUNDAY, "22:30"))
        await settle()
        assert runs.names() == ["fails"]
    # Nor after a restart: the state is on disk.
    async with scheduling(tmp_path, at(MONDAY, "22:40"), jobs) as (_, runs, _, state):
        await settle()
        assert runs.started == []
        assert state.jobs["fails"].exit_code == 1


async def test_a_job_cut_by_a_restart_runs_again_the_next_night(tmp_path: Path) -> None:
    clock = FakeClock(at(FRIDAY, "22:00"))
    stuck = Runs(clock)
    stuck.hold()
    async with scheduling(tmp_path, at(FRIDAY, "22:00"), [job()], runs=stuck, clock=clock):
        clock.set(at(FRIDAY, "22:30"))
        await until(lambda: len(stuck.started) == 1)
    # The collector stopped (a restart): the run was left going.
    job_state = saved(tmp_path)["jobs"]
    assert isinstance(job_state, dict) and job_state["job"]["outcome"] == RUNNING

    async with scheduling(tmp_path, at(FRIDAY, "23:00"), [job()]) as (clock, runs, told, state):
        assert state.jobs["job"].outcome == INTERRUPTED
        assert told == ["예약 작업 job(2026-10-16 밤)이 엔진 재시작으로 끊겼습니다. 다음 밤에 다시 돕니다."]
        assert runs.started == []  # not the same night again (a job that kills the engine would loop)
        clock.set(at(SATURDAY, "22:30"))
        await until(lambda: runs.started == [("job", at(SATURDAY, "22:30"))])
        await until(lambda: state.jobs["job"].outcome == OK)


async def test_a_collector_started_inside_the_night_runs_its_jobs_at_once(tmp_path: Path) -> None:
    async with scheduling(tmp_path, at(SATURDAY, "01:00"), [job()]) as (_, runs, _, state):
        await until(lambda: runs.started == [("job", at(SATURDAY, "01:00"))])
        await until(lambda: state.jobs["job"].day == FRIDAY)  # the night it belongs to


async def test_a_collector_started_in_the_morning_waits_for_the_evening(tmp_path: Path) -> None:
    async with scheduling(tmp_path, at(SATURDAY, "06:00"), [job()]) as (clock, runs, _, _):
        assert runs.started == []
        clock.set(at(SATURDAY, "22:30"))
        await until(lambda: len(runs.started) == 1)


async def test_a_condition_sees_the_night_and_what_the_engine_knows(tmp_path: Path) -> None:
    seen: list[Night] = []

    def due(night: Night) -> bool:
        seen.append(night)
        return False

    facts = Facts(recorded_days=10, waiting=("kalman", "mandelbrot"), official=2)
    async with scheduling(tmp_path, at(FRIDAY, "22:00"), [job(due=due)], facts=facts) as (clock, runs, _, _):
        clock.set(at(FRIDAY, "22:30"))
        await until(lambda: len(seen) == 1)
        clock.set(at(SATURDAY, "22:30"))
        await until(lambda: len(seen) == 2)
    assert [(n.day, n.trading_day) for n in seen] == [(FRIDAY, True), (SATURDAY, False)]
    assert seen[0].facts == facts and seen[0].commit == COMMIT and seen[0].record is None
    assert not seen[0].ran
    assert runs.started == []


async def test_a_condition_is_asked_once_the_jobs_before_it_ran_that_night(tmp_path: Path) -> None:
    # What a job that runs one of its kind a night relies on (the pre-validations, #204).
    seen: list[JobRecord | None] = []

    def second(night: Night) -> bool:
        seen.append(night.records.get("first"))
        return False

    jobs = [job("first"), job("second", due=second)]
    async with scheduling(tmp_path, at(FRIDAY, "22:00"), jobs) as (clock, runs, _, _):
        clock.set(at(FRIDAY, "22:30"))
        await until(lambda: bool(seen))
    [first] = seen
    assert first is not None and (first.day, first.outcome) == (FRIDAY, OK)
    assert runs.names() == ["first"]


async def test_a_condition_that_fails_skips_its_job_only(tmp_path: Path) -> None:
    def broken(night: Night) -> bool:
        raise RuntimeError("bug")

    jobs = [job("broken", due=broken), job("fine")]
    async with scheduling(tmp_path, at(FRIDAY, "22:00"), jobs) as (clock, runs, _, _):
        clock.set(at(FRIDAY, "22:30"))
        await until(lambda: runs.names() == ["fine"])


def test_settings_start_the_jobs_at_22_30_unless_off_and_only_at_night() -> None:
    keys = {"KIS_APP_KEY": "key", "KIS_APP_SECRET": "secret"}

    def settings(**env: str) -> Settings:
        return Settings.from_env({**keys, **env}, dotenv_path=None)

    assert settings().jobs_at == time(22, 30)
    assert settings(KRX_JOBS_AT="off").jobs_at is None
    assert settings(KRX_JOBS_AT="01:00").jobs_at == time(1, 0)
    assert settings(KRX_JOBS_AT="20:00", KRX_TRAIN_AT="off").jobs_at == time(20, 0)
    for daytime in ("05:30", "12:00", "19:59"):
        with pytest.raises(SettingsError, match="KRX_JOBS_AT must be from 20:00 to before 05:30"):
            settings(KRX_JOBS_AT=daytime)
    with pytest.raises(SettingsError, match="KRX_JOBS_AT must be HH:MM or off"):
        settings(KRX_JOBS_AT="late")
    with pytest.raises(SettingsError, match="must be after KRX_TRAIN_AT"):
        settings(KRX_TRAIN_AT="23:00")
    assert settings(KRX_TRAIN_AT="23:00", KRX_JOBS_AT="00:30").jobs_at == time(0, 30)
    assert settings(KRX_TRAIN_AT="off", KRX_JOBS_AT="21:00").jobs_at == time(21, 0)


def test_settings_name_the_repository_and_the_token() -> None:
    keys = {"KIS_APP_KEY": "key", "KIS_APP_SECRET": "secret"}
    plain = Settings.from_env(keys, dotenv_path=None)
    assert (plain.github_token, plain.github_repo, plain.github_api_url) == (
        None,
        "codeyoma/krx-quant",
        "https://api.github.com",
    )
    assert plain.bind_address is None
    set_up = Settings.from_env(
        {
            **keys,
            "KRX_GITHUB_TOKEN": " t0ken ",
            "KRX_GITHUB_REPO": "someone/fork",
            "KRX_GITHUB_API_URL": "http://127.0.0.1:1/",
            "KRX_BIND_ADDRESS": "100.64.0.9",
        },
        dotenv_path=None,
    )
    assert (set_up.github_token, set_up.github_repo, set_up.github_api_url, set_up.bind_address) == (
        "t0ken",
        "someone/fork",
        "http://127.0.0.1:1",
        "100.64.0.9",
    )
    with pytest.raises(SettingsError, match="KRX_GITHUB_REPO must be owner/name"):
        Settings.from_env({**keys, "KRX_GITHUB_REPO": "krx-quant"}, dotenv_path=None)


async def test_keep_training_tells_the_jobs_when_the_night_trains() -> None:
    training = NightTraining()
    running = asyncio.Event()
    done = asyncio.Event()
    clock = [at(FRIDAY, "21:30")]

    async def night(day: date) -> str | None:
        running.set()
        await done.wait()
        return None

    async def refresh() -> None:
        pass

    session = aiohttp.ClientSession()
    task = asyncio.create_task(
        keep_training(
            time(21, 30),
            CALENDAR,
            Alerts(session, None, None, "http://127.0.0.1:9"),
            lambda: clock[0],
            refresh,
            night,
            lambda calendar, train_at, started: at(FRIDAY, "21:30"),
            training,
        )
    )
    try:
        await asyncio.wait_for(running.wait(), 5)
        assert training.running_since == at(FRIDAY, "21:30")
        assert training.busy_until(at(FRIDAY, "22:30")) == at(FRIDAY, "21:30") + TRAINING_TIMEOUT
        done.set()
        await until(lambda: training.running_since is None)
        assert training.next_start == at(MONDAY, "21:30")
        assert training.busy_until(at(FRIDAY, "22:30")) is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await session.close()


# The jobs' processes


async def test_a_job_s_process_gives_its_output_and_exit_code() -> None:
    script = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"
    ran = await run_command(["-c", script], timedelta(seconds=20), program=[sys.executable])
    assert ran == Ran(3, "out\n", "err\n")


async def test_a_job_is_stopped_at_its_time_limit_with_what_it_wrote(tmp_path: Path) -> None:
    script = "import time; print('started', flush=True); time.sleep(30)"
    ran = await run_command(["-c", script], timedelta(seconds=1), program=[sys.executable])
    assert ran.timed_out and ran.exit_code != 0
    assert ran.stdout == "started\n"


async def test_a_job_that_closes_its_output_is_still_stopped_at_its_time_limit() -> None:
    script = "import os, time; os.close(1); os.close(2); time.sleep(30)"
    ran = await asyncio.wait_for(
        run_command(["-c", script], timedelta(seconds=1), program=[sys.executable]), timeout=10
    )
    assert ran.timed_out and ran.exit_code != 0


async def test_a_job_stops_with_the_collector(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    script = f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(30)"
    task = asyncio.create_task(run_command(["-c", script], timedelta(minutes=5), program=[sys.executable]))
    await until(lambda: pid_file.exists() and pid_file.read_text() != "")
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    pid = int(pid_file.read_text())

    def gone() -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        return False

    await until(gone)


# What the jobs post


DIAGNOSIS = "\n".join(
    [
        "진단 2026-09-17 – 2026-10-16 (20거래일)",
        "",
        "1. 알고리즘별 표시 지연(초, 봉 마감 → 저장)과 채점 수",
        "  newton: p50 0.12",
        "",
        "8. 장벽 정답(#183): 종목 × 시간대 분포",
        "  1분봉 규칙과 호가 정답의 클래스 일치율: 81%",
        "",
        "9. 뒤집힌 호가(#187): 종목 × 날 × 30분 칸 비율",
        "  000660 2026-10-16: 호가 3.1% · 시간 2.9%",
        "  거래소 귀속: KRX 매수 > NXT 매도 61%",
    ]
)


def first_lines(body: str) -> list[str]:
    return body.splitlines()[:5]


async def test_the_result_goes_on_each_issue_which_is_then_ready_for_an_agent(tmp_path: Path) -> None:
    clock = FakeClock(at(FRIDAY, "22:00"))
    runs = Runs(clock, Ran(0, DIAGNOSIS + "\n", ""))
    async with fake_github() as hub:
        hub.labels = {203: {"ready-for-human"}, 187: {"ready-for-human", "needs-triage", "bug"}}
        async with scheduling(
            tmp_path, at(FRIDAY, "22:00"), [DIAGNOSE], runs=runs, github=hub, clock=clock
        ) as (_, _, told, state):
            clock.set(at(FRIDAY, "22:30"))
            await until(lambda: bool(told))
        assert [issue for issue, _ in hub.comments] == [203, 187]
        assert hub.labels == {203: {"ready-for-agent"}, 187: {"ready-for-agent", "bug"}}

    whole, crossed = hub.bodies(203)[0], hub.bodies(187)[0]
    assert first_lines(whole) == [
        "**예약 작업 `diagnose-203-187`** · 2026-10-16 밤 · 커밋 `0123456789ab` · 종료 코드 0 · 0초",
        "",
        "다시 돌리기: `docker compose run --rm engine diagnose --days 20`",
        "",
        "```",
    ]
    assert f"```\n{DIAGNOSIS}\n```" in whole
    assert "<details>" not in whole  # nothing on stderr
    # #187 gets the heading paragraph and section 9 only.
    assert first_lines(crossed)[2:4] == [
        "다시 돌리기: `docker compose run --rm engine diagnose --days 20`",
        "출력 중 머리말과 9절(뒤집힌 호가)만 싣는다. 전체 출력은 #203에 있다.",
    ]
    assert "진단 2026-09-17 – 2026-10-16 (20거래일)\n\n9. 뒤집힌 호가(#187)" in crossed
    assert "8. 장벽" not in crossed and "1. 알고리즘" not in crossed

    record = state.jobs["diagnose-203-187"]
    assert record.outcome == OK and record.exit_code == 0 and record.commit == COMMIT
    assert record.comments == {
        203: f"https://github.com/{REPO}/issues/203#issuecomment-1",
        187: f"https://github.com/{REPO}/issues/187#issuecomment-2",
    }
    assert state.unposted == []
    assert told == [
        "예약 작업 diagnose-203-187(2026-10-16 밤): 성공, 종료 코드 0, 0초. 댓글: "
        f"https://github.com/{REPO}/issues/203#issuecomment-1 "
        f"https://github.com/{REPO}/issues/187#issuecomment-2"
    ]


async def test_what_is_posted_is_masked(tmp_path: Path) -> None:
    clock = FakeClock(at(FRIDAY, "22:00"))
    secret = "s3cr3t-" + "v" * 20
    leaky = Runs(
        clock, Ran(1, f"key {secret} at 100.64.1.2\n", f"Traceback: krx-quant.tail9.ts.net {secret}")
    )
    async with fake_github() as hub:
        async with scheduling(
            tmp_path, at(FRIDAY, "22:00"), [job()], runs=leaky, github=hub, clock=clock, secrets=[secret]
        ) as (_, _, told, _):
            clock.set(at(FRIDAY, "22:30"))
            await until(lambda: bool(told))
    body = hub.bodies(1)[0]
    assert secret not in body and "100.64.1.2" not in body and "ts.net" not in body
    assert "key [가림] at [가림]" in body
    assert "<details><summary>stderr</summary>\n\n```\nTraceback: [가림] [가림]\n```" in body
    assert "종료 코드 1" in body.splitlines()[0]
    assert told[0].startswith("예약 작업 job(2026-10-16 밤): 실패, 종료 코드 1, 0초. 댓글: ")


async def test_a_part_not_in_the_output_posts_it_whole_and_says_so(tmp_path: Path) -> None:
    clock = FakeClock(at(FRIDAY, "22:00"))
    crashed = Runs(clock, Ran(1, "", "Traceback (most recent call last): ..."))
    targets = (Target(187, sections("진단", "9. 뒤집힌 호가(#187)"), "9절만 싣는다."),)
    async with fake_github() as hub:
        async with scheduling(
            tmp_path, at(FRIDAY, "22:00"), [job(targets=targets)], runs=crashed, github=hub, clock=clock
        ) as (_, _, told, _):
            clock.set(at(FRIDAY, "22:30"))
            await until(lambda: bool(told))
    body = hub.bodies(187)[0]
    assert "출력에서 이 이슈에 올릴 부분을 찾지 못해 출력 전체를 싣는다." in body
    assert "9절만 싣는다." not in body
    assert "Traceback (most recent call last)" in body


async def test_a_file_the_run_wrote_goes_with_its_result_masked(tmp_path: Path) -> None:
    clock = FakeClock(at(FRIDAY, "22:00"))
    secret = "s3cr3t-" + "v" * 20
    report = tmp_path / "reports" / "job.json"
    report.parent.mkdir()
    report.write_text('{"run": "an earlier one"}')
    writes = Writes(clock, Ran(1, "refused\n", "a log line\n"), {report: f'{{"key": "{secret}"}}'})
    attach = (Attachment("리포트 state/reports/job.json", "reports/job.json"),)
    async with fake_github() as hub:
        async with scheduling(
            tmp_path,
            at(FRIDAY, "22:00"),
            [job(attach=attach)],
            runs=writes,
            github=hub,
            clock=clock,
            secrets=[secret],
        ) as (_, _, told, _):
            clock.set(at(FRIDAY, "22:30"))
            await until(lambda: bool(told))
    body = hub.bodies(1)[0]
    assert secret not in body
    summary = "<details><summary>리포트 state/reports/job.json</summary>"
    folded = summary + '\n\n```\n{"key": "[가림]"}\n```\n\n</details>'
    assert folded in body
    assert body.index(folded) < body.index("<details><summary>stderr</summary>")  # the file first
    assert "쓰지 않아" not in body


async def test_a_file_the_run_did_not_write_is_not_attached(tmp_path: Path) -> None:
    left = tmp_path / "left.json"
    left.write_text('{"run": "an earlier one"}')  # an earlier run's: it would pass for this one's
    attach = (Attachment("남은 것", "left.json"), Attachment("없는 것", "reports/none.json"))
    async with fake_github() as hub:
        async with scheduling(tmp_path, at(FRIDAY, "22:00"), [job(attach=attach)], github=hub) as (
            clock,
            _,
            told,
            _,
        ):
            clock.set(at(FRIDAY, "22:30"))
            await until(lambda: bool(told))
    body = hub.bodies(1)[0]
    assert "<details>" not in body and "an earlier one" not in body
    assert body.splitlines()[2:5] == [
        "다시 돌리기: `docker compose run --rm engine job --flag`",
        "`state/left.json`은 이번 실행이 쓰지 않아 붙이지 않는다.",
        "`state/reports/none.json`은 이번 실행이 쓰지 않아 붙이지 않는다.",
    ]


async def test_a_timed_out_job_says_so(tmp_path: Path) -> None:
    clock = FakeClock(at(FRIDAY, "22:00"))
    slow = Runs(clock, Ran(-9, "half\n", "", timed_out=True))
    async with fake_github() as hub:
        async with scheduling(tmp_path, at(FRIDAY, "22:00"), [job()], runs=slow, github=hub, clock=clock) as (
            _,
            _,
            told,
            state,
        ):
            clock.set(at(FRIDAY, "22:30"))
            await until(lambda: bool(told))
    assert "제한 시간 1시간을 넘어 멈췄다. 그때까지의 출력이다." in hub.bodies(1)[0]
    assert told[0].startswith("예약 작업 job(2026-10-16 밤): 실패, 제한 시간 1시간을 넘어 멈춤, 0초.")
    assert state.jobs["job"].outcome == FAILED and state.jobs["job"].timed_out


async def test_without_a_token_the_result_waits_on_the_server_until_there_is_one(tmp_path: Path) -> None:
    async with fake_github() as hub:
        async with scheduling(tmp_path, at(FRIDAY, "22:00"), [DIAGNOSE], github=hub, token=None) as (
            clock,
            _,
            told,
            state,
        ):
            clock.set(at(FRIDAY, "22:30"))
            await until(lambda: bool(told))
            assert [u.issue for u in state.unposted] == [203, 187]
        assert hub.requests == []
        assert told == [
            "예약 작업 diagnose-203-187(2026-10-16 밤): 성공, 종료 코드 0, 0초. GitHub에 올리지 못했습니다"
            "(KRX_GITHUB_TOKEN이 없음). 결과는 서버의 state/scheduled.json에 두고 "
            "다음 밤이나 엔진이 다시 뜰 때 올립니다."
        ]
        unposted = saved(tmp_path)["unposted"]
        assert isinstance(unposted, list) and [u["issue"] for u in unposted] == [203, 187]

        # The token is put in .env and the engine restarted: the results go up at its start.
        async with scheduling(tmp_path, at(SATURDAY, "08:00"), [DIAGNOSE], github=hub) as (
            _,
            runs,
            told,
            state,
        ):
            await until(lambda: len(hub.comments) == 2)
            await until(lambda: bool(told))
            assert runs.started == []  # posted, not run again
            assert state.unposted == []
            assert state.jobs["diagnose-203-187"].comments.keys() == {203, 187}
            assert told == [
                "예약 작업 diagnose-203-187(2026-10-16 밤)의 결과를 이제 올렸습니다. 댓글: "
                f"https://github.com/{REPO}/issues/203#issuecomment-1 "
                f"https://github.com/{REPO}/issues/187#issuecomment-2"
            ]
    assert saved(tmp_path)["unposted"] == []


async def test_a_result_github_refused_is_posted_the_next_night(tmp_path: Path) -> None:
    async with fake_github() as hub:
        hub.fail_comments = 2
        async with scheduling(tmp_path, at(FRIDAY, "22:00"), [DIAGNOSE], github=hub) as (
            clock,
            runs,
            told,
            state,
        ):
            clock.set(at(FRIDAY, "22:30"))
            await until(lambda: bool(told))
            assert hub.comments == [] and len(state.unposted) == 2
            assert "GitHub에 올리지 못했습니다(POST /issues/203/comments: HTTP 502 Bad Gateway)" in told[0]
            clock.set(at(SATURDAY, "22:30"))
            await until(lambda: len(hub.comments) == 2)
            await until(lambda: len(told) == 2)
            assert runs.names() == ["diagnose"]  # posted again, not run again
            assert state.unposted == []
            assert told[1].startswith("예약 작업 diagnose-203-187(2026-10-16 밤)의 결과를 이제 올렸습니다.")


async def test_labels_that_failed_after_the_comment_do_not_post_it_twice(tmp_path: Path) -> None:
    async with fake_github() as hub:
        hub.fail_labels = 1
        hub.labels = {1: {"ready-for-human"}}
        async with scheduling(tmp_path, at(FRIDAY, "22:00"), [job()], github=hub) as (clock, _, told, state):
            clock.set(at(FRIDAY, "22:30"))
            await until(lambda: bool(told))
            assert len(hub.comments) == 1 and len(state.unposted) == 1
            assert state.unposted[0].comment == f"https://github.com/{REPO}/issues/1#issuecomment-1"
            clock.set(at(SATURDAY, "22:30"))
            await until(lambda: not state.unposted)
        assert len(hub.comments) == 1
        assert hub.labels == {1: {"ready-for-agent"}}


# The jobs listed


def night(day: date, record: JobRecord | None = None) -> Night:
    return Night(day, CALENDAR.is_trading_day(day), COMMIT, record, {}, Facts())


def test_the_jobs_have_unique_names_and_fit_in_the_night() -> None:
    names = [j.name for j in JOBS]
    assert len(names) == len(set(names))
    start, end = window(FRIDAY, AT)
    for listed in JOBS:
        assert listed.timeout < end - start, listed.name
        assert listed.targets and all(t.issue > 0 for t in listed.targets)


def test_the_diagnosis_runs_once_on_the_first_trading_day_s_night() -> None:
    assert DIAGNOSE.command == ("diagnose", "--days", "20")
    assert DIAGNOSE.timeout == timedelta(hours=1)
    assert [t.issue for t in DIAGNOSE.targets] == [203, 187]
    assert DIAGNOSE.label == ("ready-for-agent",)
    assert DIAGNOSE.due(night(FRIDAY))
    assert not DIAGNOSE.due(night(SATURDAY))  # not a trading day: it waits for Monday's night
    ran = JobRecord(FRIDAY, OK, at(FRIDAY, "22:30"), COMMIT, 0, 60.0)
    assert not DIAGNOSE.due(night(MONDAY, ran))
    assert not DIAGNOSE.due(night(MONDAY, replace(ran, outcome=FAILED, exit_code=1)))
    assert DIAGNOSE.due(night(MONDAY, replace(ran, outcome=INTERRUPTED)))


def test_the_diagnosis_section_9_goes_to_187_alone() -> None:
    whole, crossed = (t.part(DIAGNOSIS) for t in DIAGNOSE.targets)
    assert whole == DIAGNOSIS
    assert crossed == "\n".join(
        [
            "진단 2026-09-17 – 2026-10-16 (20거래일)",
            "",
            "9. 뒤집힌 호가(#187): 종목 × 날 × 30분 칸 비율",
            "  000660 2026-10-16: 호가 3.1% · 시간 2.9%",
            "  거래소 귀속: KRX 매수 > NXT 매도 61%",
        ]
    )
    assert DIAGNOSE.targets[1].part("진단: 데이터 없음\n\n8. 장벽 정답(#183)\n  없음") is None


@pytest.fixture
def empty_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An engine's data and state directories with nothing recorded, for the real command."""
    monkeypatch.chdir(tmp_path)  # no .env
    monkeypatch.setenv("KRX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


async def test_the_diagnosis_job_runs_the_real_command(empty_server: Path) -> None:
    state = JobState.load(empty_server / STATE_FILE)
    async with fake_github() as hub, aiohttp.ClientSession() as session:
        scheduler = Scheduler(
            [DIAGNOSE],
            AT,
            CALENDAR,
            state,
            GitHub(session, TOKEN, REPO, hub.url),
            Told(),
            FakeClock(at(FRIDAY, "23:00")),
            commit=COMMIT,
        )
        await asyncio.wait_for(scheduler.run_night(FRIDAY), 25)
    whole, crossed = hub.bodies(203)[-1], hub.bodies(187)[-1]
    barrier = f"8. 장벽 정답(#183, 정답 버전 {BARRIER.answer_version})"  # its version named (#187)
    assert "진단: 데이터 없음" in whole and barrier in whole and "9. 뒤집힌 호가(#187)" in whole
    assert "진단: 데이터 없음\n\n9. 뒤집힌 호가(#187)" in crossed and "8. 장벽" not in crossed
    assert state.jobs["diagnose-203-187"].exit_code == 0


def official(day: date, n: int, record: JobRecord | None = None) -> Night:
    return Night(day, CALENDAR.is_trading_day(day), COMMIT, record, {}, Facts(official=n))


def test_the_signal_review_runs_once_on_the_first_night_with_an_official_algorithm() -> None:
    assert SIGNAL_REVIEW.command == ("signal-review", "--days", "20")
    assert SIGNAL_REVIEW.timeout == timedelta(hours=1)
    assert [t.issue for t in SIGNAL_REVIEW.targets] == [206]
    assert SIGNAL_REVIEW.label == ("ready-for-agent",)
    assert JOBS[:2] == (DIAGNOSE, SIGNAL_REVIEW)  # before the pre-validations, which may take the night
    assert not SIGNAL_REVIEW.due(official(FRIDAY, 0))  # no member yet: no consensus, no signal
    assert SIGNAL_REVIEW.due(official(FRIDAY, 1))
    assert SIGNAL_REVIEW.due(official(SATURDAY, 2))  # any night, trading or not
    ran = JobRecord(FRIDAY, OK, at(FRIDAY, "22:30"), COMMIT, 0, 30.0)
    assert not SIGNAL_REVIEW.due(official(MONDAY, 2, ran))
    assert not SIGNAL_REVIEW.due(official(MONDAY, 2, replace(ran, outcome=FAILED, exit_code=1)))
    assert SIGNAL_REVIEW.due(official(MONDAY, 2, replace(ran, outcome=INTERRUPTED)))


def test_the_supply_probe_runs_once_on_the_first_night_trading_or_not() -> None:
    assert SUPPLY_PROBE.command == ("probe-supply",)
    assert SUPPLY_PROBE.timeout == timedelta(minutes=30)
    assert [t.issue for t in SUPPLY_PROBE.targets] == [226]
    assert SUPPLY_PROBE.label == ("ready-for-agent",)
    assert JOBS[:3] == (DIAGNOSE, SIGNAL_REVIEW, SUPPLY_PROBE)  # before the pre-validations
    assert SUPPLY_PROBE.due(night(FRIDAY))
    assert SUPPLY_PROBE.due(night(SATURDAY))  # KIS answers for the latest trading day any night
    ran = JobRecord(SATURDAY, OK, at(SATURDAY, "22:30"), COMMIT, 0, 150.0)
    assert not SUPPLY_PROBE.due(night(MONDAY, ran))
    assert not SUPPLY_PROBE.due(night(MONDAY, replace(ran, outcome=FAILED, exit_code=1)))
    assert SUPPLY_PROBE.due(night(MONDAY, replace(ran, outcome=INTERRUPTED)))


def test_the_structure_check_runs_once_on_the_first_night_trading_or_not() -> None:
    assert STRUCTURE_CHECK.command == ("structure-check",)
    assert STRUCTURE_CHECK.timeout == timedelta(minutes=30)
    assert [t.issue for t in STRUCTURE_CHECK.targets] == [270]
    assert JOBS[:4] == (DIAGNOSE, SIGNAL_REVIEW, SUPPLY_PROBE, STRUCTURE_CHECK)  # before the pre-validations
    assert STRUCTURE_CHECK.due(night(FRIDAY))
    assert STRUCTURE_CHECK.due(night(SATURDAY))  # it reads finished days only
    ran = JobRecord(SATURDAY, OK, at(SATURDAY, "22:30"), COMMIT, 0, 120.0)
    assert not STRUCTURE_CHECK.due(night(MONDAY, ran))
    assert not STRUCTURE_CHECK.due(night(MONDAY, replace(ran, outcome=FAILED, exit_code=1)))
    assert STRUCTURE_CHECK.due(night(MONDAY, replace(ran, outcome=INTERRUPTED)))


def test_the_structure_check_runs_again_once_and_posts_its_turn_of_the_month_to_264() -> None:
    # #270: the first run counted KIS's 0 net buys as figures; the second shows where they begin.
    assert STRUCTURE_SUPPLY.command == ("structure-check",)
    assert STRUCTURE_SUPPLY.timeout == timedelta(minutes=30)
    assert [t.issue for t in STRUCTURE_SUPPLY.targets] == [264]
    assert STRUCTURE_SUPPLY.label == ("ready-for-agent",)
    assert STRUCTURE_SUPPLY in JOBS and STRUCTURE_SUPPLY.name != STRUCTURE_CHECK.name
    first = next(i for i, j in enumerate(JOBS) if j.name.startswith("prevalidate-"))
    assert JOBS.index(STRUCTURE_SUPPLY) < first, "before the pre-validations"
    assert STRUCTURE_SUPPLY.due(night(SATURDAY))
    ran = JobRecord(SATURDAY, OK, at(SATURDAY, "22:30"), COMMIT, 0, 120.0)
    assert not STRUCTURE_SUPPLY.due(night(MONDAY, ran))
    output = "\n\n".join(
        [
            "구조 실측 2025-10-10 – 2026-10-16 (1분봉 249거래일, 005930)\n  기간: …",
            "1. 일 단위 구조(정규장 시가·종가, 1분봉)\n  없음",
            "4. 월말·월초 × 전날 외국인 순매수(일봉 전체, KIS 수급)\n  005930 일봉 1985-01-04 – 2026-10-16,"
            " 외국인 순매수 395일(2025-03-04 – 2026-10-16)",
        ]
    )
    part = STRUCTURE_SUPPLY.targets[0].part(output)
    assert part is not None and part.startswith("구조 실측") and "1. 일 단위" not in part
    assert part.endswith("외국인 순매수 395일(2025-03-04 – 2026-10-16)")


def test_the_entry_delay_runs_the_diagnosis_once_on_a_trading_night_and_posts_section_10() -> None:
    # #262: the diagnosis's section 10 alone, with its header, goes to #262.
    assert ENTRY_DELAY.command == ("diagnose", "--days", "20")
    assert ENTRY_DELAY.timeout == timedelta(hours=1)
    assert [t.issue for t in ENTRY_DELAY.targets] == [262]
    assert JOBS[:5] == (DIAGNOSE, SIGNAL_REVIEW, SUPPLY_PROBE, STRUCTURE_CHECK, ENTRY_DELAY)
    assert ENTRY_DELAY.due(night(FRIDAY))
    assert not ENTRY_DELAY.due(night(SATURDAY))  # it waits for a trading day's recording
    ran = JobRecord(FRIDAY, OK, at(FRIDAY, "22:30"), COMMIT, 0, 60.0)
    assert not ENTRY_DELAY.due(night(MONDAY, ran))
    output = "\n\n".join(
        [
            "진단 2026-09-21 – 2026-10-19 (20거래일)",
            "9. 뒤집힌 호가(#187): 종목 × 날 × 30분 칸 비율\n  없음",
            "10. 예측자별 실력과 진입 지연(#262, 정답 버전 3): 봉 마감·+1초·+5초·+30초에"
            " 들어갔을 때의 목표 도달과 닿음 손익\n  newton: 채점 2 · 정답률 100.0%",
        ]
    )
    part = ENTRY_DELAY.targets[0].part(output)
    assert part is not None and part.startswith("진단 2026-09-21") and "9. 뒤집힌" not in part
    assert part.endswith("newton: 채점 2 · 정답률 100.0%")
    assert ENTRY_DELAY.targets[0].part("진단: 데이터 없음\n\n9. 뒤집힌 호가(#187)\n  없음") is None


def subscribed(day: date, nights: tuple[date, ...], record: JobRecord | None = None) -> Night:
    return Night(day, CALENDAR.is_trading_day(day), COMMIT, record, {}, Facts(night_futures=nights))


def test_the_night_probe_runs_once_after_a_night_subscribed_for() -> None:
    # #264: the first night the collector recorded from its start is read the next evening.
    assert NIGHT_PROBE.command == ("probe-night",)
    assert NIGHT_PROBE.timeout == timedelta(minutes=30)
    assert [t.issue for t in NIGHT_PROBE.targets] == [264]
    assert NIGHT_PROBE.label == ("ready-for-agent",)
    assert JOBS[:6] == (DIAGNOSE, SIGNAL_REVIEW, SUPPLY_PROBE, STRUCTURE_CHECK, ENTRY_DELAY, NIGHT_PROBE)
    assert not NIGHT_PROBE.due(subscribed(THURSDAY, ()))  # the deploy's evening: none recorded yet
    assert not NIGHT_PROBE.due(subscribed(THURSDAY, (THURSDAY,)))  # tonight's is not over
    assert NIGHT_PROBE.due(subscribed(FRIDAY, (THURSDAY,)))
    assert NIGHT_PROBE.due(subscribed(SUNDAY, (FRIDAY,)))  # any night after one
    ran = JobRecord(FRIDAY, OK, at(FRIDAY, "22:30"), COMMIT, 0, 5.0)
    assert not NIGHT_PROBE.due(subscribed(MONDAY, (THURSDAY, FRIDAY), ran))
    assert not NIGHT_PROBE.due(subscribed(MONDAY, (FRIDAY,), replace(ran, outcome=FAILED, exit_code=1)))
    assert NIGHT_PROBE.due(subscribed(MONDAY, (FRIDAY,), replace(ran, outcome=INTERRUPTED)))


async def test_the_night_probe_job_runs_the_real_command(
    empty_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIS_MASTER_URL", "http://127.0.0.1:9/master")  # nothing listens: no network
    state = JobState.load(empty_server / STATE_FILE)

    async def facts() -> Facts:
        return Facts(night_futures=(THURSDAY,))

    async with fake_github() as hub, aiohttp.ClientSession() as session:
        scheduler = Scheduler(
            [NIGHT_PROBE],
            AT,
            CALENDAR,
            state,
            GitHub(session, TOKEN, REPO, hub.url),
            Told(),
            FakeClock(at(FRIDAY, "23:00")),
            commit=COMMIT,
            facts=facts,
        )
        await asyncio.wait_for(scheduler.run_night(FRIDAY), 25)
    [body] = hub.bodies(264)
    assert "야간선물 녹화 확인 (#264)" in body and "2. H0MFCNT0 체결" in body
    assert "fo_cme_code.mst를 읽지 못했다" in body  # said, not fatal
    assert "시세 값이 없다" in body  # the note: what the output holds
    assert state.jobs["probe-night-264"].exit_code == 0


async def test_the_supply_probe_runs_on_the_first_night_then_never(tmp_path: Path) -> None:
    async with scheduling(tmp_path, at(FRIDAY, "22:00"), [SUPPLY_PROBE]) as (clock, runs, _, state):
        clock.set(at(FRIDAY, "22:30"))
        await until(lambda: state.jobs.get("probe-supply-226") is not None)
        await until(lambda: state.jobs["probe-supply-226"].outcome == OK)
        clock.set(at(SATURDAY, "22:30"))
        clock.set(at(MONDAY, "22:30"))
        await settle()
        assert runs.started == [("probe-supply", at(FRIDAY, "22:30"))]


async def test_the_signal_review_waits_for_an_official_algorithm_then_runs_once(tmp_path: Path) -> None:
    members = {THURSDAY: 0, FRIDAY: 1, SATURDAY: 1, SUNDAY: 2}
    clock = FakeClock(at(THURSDAY, "22:00"))

    def facts() -> Facts:  # the first verdicts make one official on Friday
        night = (clock.now() - timedelta(hours=6)).date()
        return Facts(official=members[night])

    async with scheduling(tmp_path, at(THURSDAY, "22:00"), [SIGNAL_REVIEW], facts=facts, clock=clock) as (
        _,
        runs,
        _,
        state,
    ):
        clock.set(at(THURSDAY, "22:30"))
        await settle()
        assert runs.started == []
        clock.set(at(FRIDAY, "22:30"))
        await until(lambda: runs.started == [("signal-review", at(FRIDAY, "22:30"))])
        await until(lambda: state.jobs["signal-review-206"].outcome == OK)
        clock.set(at(SATURDAY, "22:30"))
        clock.set(at(SUNDAY, "22:30"))
        await settle()
        assert runs.names() == ["signal-review"]


async def test_the_signal_review_job_runs_the_real_command(empty_server: Path) -> None:
    state = JobState.load(empty_server / STATE_FILE)

    async def facts() -> Facts:
        return Facts(official=1)

    async with fake_github() as hub, aiohttp.ClientSession() as session:
        scheduler = Scheduler(
            [SIGNAL_REVIEW],
            AT,
            CALENDAR,
            state,
            GitHub(session, TOKEN, REPO, hub.url),
            Told(),
            FakeClock(at(FRIDAY, "23:00")),
            commit=COMMIT,
            facts=facts,
        )
        await asyncio.wait_for(scheduler.run_night(FRIDAY), 25)
    [body] = hub.bodies(206)
    assert "신호 기준 검토: 데이터 없음" in body and "1. 신호 설정과 엔진이 남긴 신호" in body
    assert "KRX_SIGNAL_MIN_PROBABILITY 0.6 · KRX_SIGNAL_MIN_AGREE 2" in body
    assert "추천값" in body  # what the agent does with it
    assert state.jobs["signal-review-206"].exit_code == 0


async def test_the_collector_runs_the_night_s_jobs(kis: FakeKis, empty_server: Path) -> None:
    async with fake_github() as hub:
        settings = replace(
            settings_for(kis, empty_server),
            jobs_at=AT,
            github_token=TOKEN,
            github_api_url=hub.url,
            commit=COMMIT,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(run_collector(settings, stop=stop, clock=lambda: at(FRIDAY, "23:00")))

        def posted() -> bool:
            """Both comments are up and saved (the labels come between a comment and its save)."""
            record = JobState.load(empty_server / "state" / STATE_FILE).jobs.get("diagnose-203-187")
            return record is not None and record.comments.keys() == {203, 187}

        try:
            await until(lambda: len(hub.comments) >= 2 and posted(), timeout=25)
        finally:
            stop.set()
            await asyncio.wait_for(task, 10)
    record = JobState.load(empty_server / "state" / STATE_FILE).jobs["diagnose-203-187"]
    assert record.outcome == OK and record.day == FRIDAY and record.comments.keys() == {203, 187}
