"""The simulated probations give way to a pre-validation that would miss its last start (#376).

The 2026-10-23 and 2026-10-25 nights: ``simulate-bar-close-298`` ran first (1 h 24 min, then
2 h 22 min) and the night's pre-validation, which must start by 05:30 less its limit (00:30, the
Breimans 23:30), went to the next night. The decision of 2026-10-26: the pre-validation runs first
when the simulation, run first to its time limit, would carry it past its last start; the simulation
runs after it, that night if it can still start, else the next; both fitting, the simulation stays
first. A simulation that gave way on two nights runs first.

The jobs are the real ones; their processes are stand-ins that take time on the fake clock.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path

import pytest

from krx_quant.clock import FakeClock
from krx_quant.prevalidation import MIN_DAYS
from krx_quant.process_log import Child
from krx_quant.scheduled import (
    BAR_CLOSE_SIMULATION,
    DAILY_SIMULATION,
    GIVE_WAY_NIGHTS,
    INTERRUPTED,
    JOBS,
    STATE_FILE,
    Facts,
    Job,
    JobRecord,
    Ran,
)
from tests.test_scheduled import COMMIT, FRIDAY, MONDAY, SATURDAY, SUNDAY, Runs, at, saved, scheduling
from tests.test_subscription_limit import until

BAR_CLOSE = "simulate bar-close"
DAILY = "simulate daily"
# The bar-close algorithms' pre-validations, in turn (the daily kinds' run before the simulations).
PREVALIDATIONS = [
    j for j in JOBS if j.command[0] == "prevalidate" and JOBS.index(j) > JOBS.index(DAILY_SIMULATION)
]
SIMULATIONS_AND_PREVALIDATIONS = [DAILY_SIMULATION, BAR_CLOSE_SIMULATION, *PREVALIDATIONS]


class Takes(Runs):
    """Stand-in runs that take time on the fake clock: each command (its words) until the clock
    reaches its start plus its time in ``took`` (none by default)."""

    def __init__(self, clock: FakeClock, took: Mapping[str, timedelta]) -> None:
        super().__init__(clock)
        self.took = took

    async def __call__(
        self,
        args: Sequence[str],
        timeout: timedelta,
        child: Child | None = None,
    ) -> Ran:
        name = " ".join(args)
        started = self.clock.now()
        self.started.append((name, started))
        await self.clock.sleep_until(started + self.took.get(name, timedelta()))
        self.ended.append(name)
        return self.ran


def waiting(*algorithms: str, simulating: Sequence[str] = ("breiman",), daily: Sequence[str] = ()) -> Facts:
    """Ten recorded days; ``algorithms`` wait for their pre-validation, ``simulating`` (bar-close)
    and ``daily`` for their probation's simulation."""
    return Facts(
        recorded_days=MIN_DAYS,
        waiting=algorithms,
        simulating=tuple(simulating),
        simulating_daily=tuple(daily),
    )


def gave_way(job: str, ahead: str, day: date, last_start: str, limit: str, latest: str, nights: int) -> str:
    return (
        f"예약 작업 {job}({day} 밤): 먼저 돌아 제한 시간({limit})을 다 쓰면 {ahead}의 마지막 시작 "
        f"시각({last_start})을 넘기므로 그 뒤로 미룹니다. 그 뒤 {latest}까지 시작할 수 있으면 그 밤에, "
        f"아니면 다음 밤에 돕니다. 자리를 내준 밤은 {nights}/{GIVE_WAY_NIGHTS}이고, {GIVE_WAY_NIGHTS}밤을 "
        "내준 뒤에는 먼저 돕니다."
    )


def put_off(job: str, day: date, limit: str) -> str:
    return f"예약 작업 {job}({day} 밤): 지금 시작하면 제한 시간({limit})이 05:30을 넘어 다음 밤으로 미룹니다."


def gave_way_on(tmp_path: Path) -> object:
    return saved(tmp_path)["gave_way"]


# The jobs listed


def test_the_simulations_give_way_to_the_pre_validations_only() -> None:
    assert GIVE_WAY_NIGHTS == 2
    for simulation in (DAILY_SIMULATION, BAR_CLOSE_SIMULATION):
        assert simulation.gives_way_to is not None
        assert {j.name for j in JOBS if simulation.gives_way_to(j)} == {
            j.name for j in JOBS if j.command[0] == "prevalidate"
        }
    assert [j.name for j in JOBS if j.gives_way_to is not None] == [
        "simulate-daily-298",
        "simulate-bar-close-298",
    ]
    # Listed as #298 decided; the daily pre-validations before the daily simulation, which enrolls
    # a pass that night, so it never holds them back.
    names = [j.name for j in JOBS]
    assert names.index("prevalidate-cox") < names.index("simulate-daily-298")
    assert names.index("simulate-bar-close-298") < names.index("prevalidate-mandelbrot")


# The nights


async def test_a_pre_validation_that_would_miss_its_last_start_goes_first_and_the_simulation_after_it(
    tmp_path: Path,
) -> None:
    clock = FakeClock(at(FRIDAY, "22:00"))
    runs = Takes(clock, {"prevalidate kalman": timedelta(hours=1, minutes=50)})
    facts = waiting("kalman", "shannon")
    async with scheduling(
        tmp_path, clock.now(), SIMULATIONS_AND_PREVALIDATIONS, runs=runs, facts=facts, clock=clock
    ) as (_, _, told, state):
        clock.set(at(FRIDAY, "22:30"))
        await until(lambda: runs.names() == ["prevalidate kalman"])
        assert told.saying("simulate-bar-close-298") == [
            gave_way("simulate-bar-close-298", "prevalidate-kalman", FRIDAY, "00:30", "5시간", "00:30", 1)
        ]
        assert gave_way_on(tmp_path) == {"simulate-bar-close-298": [FRIDAY.isoformat()]}

        clock.set(at(SATURDAY, "00:20"))  # kalman took 1 h 50 min: the simulation still starts by 00:30
        await until(lambda: runs.ended == ["prevalidate kalman", BAR_CLOSE])
        assert runs.started[1] == (BAR_CLOSE, at(SATURDAY, "00:20"))
        assert state.gave_way == {}  # it ran: the night it gave way counts no more
        assert gave_way_on(tmp_path) == {}


async def test_a_simulation_that_cannot_start_after_it_runs_the_next_night(tmp_path: Path) -> None:
    clock = FakeClock(at(FRIDAY, "22:00"))
    runs = Takes(clock, {"prevalidate kalman": timedelta(hours=2, minutes=20)})
    async with scheduling(
        tmp_path, clock.now(), SIMULATIONS_AND_PREVALIDATIONS, runs=runs, facts=waiting("kalman"), clock=clock
    ) as (_, _, told, _):
        clock.set(at(FRIDAY, "22:30"))
        await until(lambda: runs.names() == ["prevalidate kalman"])
        clock.set(at(SATURDAY, "00:50"))
        await until(lambda: bool(told.saying("지금 시작하면")))
        assert told.saying("simulate-bar-close-298") == [
            gave_way("simulate-bar-close-298", "prevalidate-kalman", FRIDAY, "00:30", "5시간", "00:30", 1),
            put_off("simulate-bar-close-298", FRIDAY, "5시간"),
        ]
        assert runs.names() == ["prevalidate kalman"]
    assert gave_way_on(tmp_path) == {"simulate-bar-close-298": [FRIDAY.isoformat()]}

    # The collector restarts (the 06:00 deploy); the next night no pre-validation is due (kalman ran
    # without a verdict on this code): the simulation runs first thing.
    clock = FakeClock(at(SATURDAY, "06:00"))
    runs = Takes(clock, {})
    async with scheduling(
        tmp_path, clock.now(), SIMULATIONS_AND_PREVALIDATIONS, runs=runs, facts=waiting("kalman"), clock=clock
    ) as (_, _, told, state):
        assert state.gave_way == {"simulate-bar-close-298": [FRIDAY]}
        clock.set(at(SATURDAY, "22:30"))
        await until(lambda: runs.ended == [BAR_CLOSE])
        assert runs.started == [(BAR_CLOSE, at(SATURDAY, "22:30"))]
        assert state.gave_way == {}
        assert not told.saying("자리를 내준")


@pytest.mark.parametrize(
    ("start", "breiman_first"),
    [
        ("22:30", True),  # by the simulation's 5 hours 03:30, past 23:30
        ("23:30", True),  # its last start: it still starts
        ("23:31", False),  # it cannot start anyway: giving way would gain nothing
    ],
)
async def test_a_breiman_has_6_hours_and_must_start_by_23_30(
    tmp_path: Path, start: str, breiman_first: bool
) -> None:
    """The training ends at ``start`` (a collector started inside the night runs its jobs at once)."""
    clock = FakeClock(at(FRIDAY, start))
    runs = Takes(clock, {"prevalidate breiman": timedelta(hours=3)})
    async with scheduling(
        tmp_path,
        clock.now(),
        SIMULATIONS_AND_PREVALIDATIONS,
        runs=runs,
        facts=waiting("breiman"),
        clock=clock,
    ) as (_, _, told, _):
        if breiman_first:
            await until(lambda: runs.names() == ["prevalidate breiman"])
            assert told.saying("자리를 내준") == [
                gave_way(
                    "simulate-bar-close-298", "prevalidate-breiman", FRIDAY, "23:30", "5시간", "00:30", 1
                )
            ]
            return
        await until(lambda: bool(told.saying("prevalidate-breiman")))
        assert runs.names() == [BAR_CLOSE]
        assert told.saying("prevalidate-breiman") == [put_off("prevalidate-breiman", FRIDAY, "6시간")]
        assert not told.saying("자리를 내준")


@pytest.mark.parametrize(
    ("algorithm", "start", "gives_way"),
    [
        ("mandelbrot", "22:30", False),  # 23:30 by its hour, before 00:30
        ("mandelbrot", "23:30", False),  # 00:30 at the latest: the pre-validation still starts
        ("mandelbrot", "23:31", True),
        ("breiman", "22:30", False),  # 23:30 at the latest: just in time
        ("breiman", "22:31", True),
    ],
)
async def test_the_daily_simulation_stays_first_when_both_fit(
    tmp_path: Path, algorithm: str, start: str, gives_way: bool
) -> None:
    """The daily simulation's hour (it took 40 s on 2026-10-20) against a bar-close pre-validation's
    last start: both fit, it goes first, as #298 listed it; else it gives way, and runs once the
    pre-validation is over (it can start until 04:30)."""
    clock = FakeClock(at(FRIDAY, start))
    prevalidation = f"prevalidate {algorithm}"
    runs = Takes(clock, {prevalidation: timedelta(hours=3)})
    facts = waiting(algorithm, simulating=(), daily=("cox",))
    async with scheduling(
        tmp_path, clock.now(), SIMULATIONS_AND_PREVALIDATIONS, runs=runs, facts=facts, clock=clock
    ) as (_, _, told, state):
        if not gives_way:
            await until(lambda: runs.names() == [DAILY, prevalidation])
            assert not told.saying("자리를 내준")
            return
        await until(lambda: runs.names() == [prevalidation])
        last = "23:30" if algorithm == "breiman" else "00:30"
        assert told.saying("자리를 내준") == [
            gave_way("simulate-daily-298", f"prevalidate-{algorithm}", FRIDAY, last, "1시간", "04:30", 1)
        ]
        ended = at(FRIDAY, start) + timedelta(hours=3)
        clock.set(ended)
        await until(lambda: runs.ended == [prevalidation, DAILY])
        assert runs.started[1] == (DAILY, ended)
        assert state.gave_way == {}


async def test_both_simulations_give_way_and_run_after_it_in_the_list_s_order(tmp_path: Path) -> None:
    clock = FakeClock(at(FRIDAY, "23:45"))
    runs = Takes(clock, {"prevalidate mandelbrot": timedelta(minutes=30)})
    facts = waiting("mandelbrot", simulating=("breiman",), daily=("cox",))
    async with scheduling(
        tmp_path, clock.now(), SIMULATIONS_AND_PREVALIDATIONS, runs=runs, facts=facts, clock=clock
    ) as (_, _, told, state):
        await until(lambda: runs.names() == ["prevalidate mandelbrot"])
        assert len(told.saying("자리를 내준")) == 2
        clock.set(at(SATURDAY, "00:15"))
        await until(lambda: runs.ended == ["prevalidate mandelbrot", DAILY, BAR_CLOSE])
        assert state.gave_way == {}


async def test_a_simulation_that_gave_way_on_two_nights_runs_first_on_the_third(tmp_path: Path) -> None:
    """Pre-validations due every night, each past the simulation's last start: it waits two nights,
    then runs first, and that night's pre-validation waits a night as before #376 (2026-10-25)."""
    took = {
        "prevalidate mandelbrot": timedelta(hours=3),
        "prevalidate breiman": timedelta(hours=3),
        BAR_CLOSE: timedelta(hours=2, minutes=22),
    }
    facts = waiting("mandelbrot", "breiman", "kalman")

    async def gives_way(day: date, prevalidation: str, last_start: str, count: int) -> None:
        # The collector restarts every morning: the nights it gave way on are on disk.
        clock = FakeClock(at(day, "06:00"))
        runs = Takes(clock, took)
        async with scheduling(
            tmp_path, clock.now(), SIMULATIONS_AND_PREVALIDATIONS, runs=runs, facts=facts, clock=clock
        ) as (_, _, told, _):
            clock.set(at(day, "22:30"))
            await until(lambda: runs.names() == [prevalidation])
            name = prevalidation.replace(" ", "-")
            assert told.saying("자리를 내준") == [
                gave_way("simulate-bar-close-298", name, day, last_start, "5시간", "00:30", count)
            ]
            clock.set(at(day + timedelta(days=1), "01:30"))
            await until(lambda: bool(told.saying(put_off("simulate-bar-close-298", day, "5시간"))))
            assert runs.names() == [prevalidation]

    await gives_way(FRIDAY, "prevalidate mandelbrot", "00:30", 1)
    await gives_way(SATURDAY, "prevalidate breiman", "23:30", 2)
    assert gave_way_on(tmp_path) == {"simulate-bar-close-298": [FRIDAY.isoformat(), SATURDAY.isoformat()]}

    clock = FakeClock(at(SUNDAY, "06:00"))
    runs = Takes(clock, took)
    async with scheduling(
        tmp_path, clock.now(), SIMULATIONS_AND_PREVALIDATIONS, runs=runs, facts=facts, clock=clock
    ) as (_, _, told, state):
        clock.set(at(SUNDAY, "22:30"))
        await until(lambda: runs.names() == [BAR_CLOSE])
        assert told.saying("먼저 돕니다") == [
            "예약 작업 simulate-bar-close-298(2026-10-18 밤): 사전 검증에 이미 2밤 자리를 내줘 이번에는 "
            "먼저 돕니다. prevalidate-kalman은 마지막 시작 시각(00:30)까지 시작하지 못하면 다음 밤으로 "
            "미뤄집니다."
        ]
        assert state.gave_way == {}
        clock.set(at(MONDAY, "00:52"))  # 2 h 22 min, as on 2026-10-25
        await until(lambda: bool(told.saying(put_off("prevalidate-kalman", SUNDAY, "5시간"))))
        assert runs.names() == [BAR_CLOSE]
    assert gave_way_on(tmp_path) == {}


async def test_the_nights_given_way_count_no_more_once_it_waits_no_more(tmp_path: Path) -> None:
    """A probation that stopped waiting for its simulation (it was retired, say) leaves no count
    behind for the next one."""
    (tmp_path / STATE_FILE).write_text(
        json.dumps({"jobs": {}, "unposted": [], "gave_way": {"simulate-bar-close-298": ["2026-10-15"]}})
    )
    clock = FakeClock(at(FRIDAY, "22:00"))
    runs = Takes(clock, {})
    async with scheduling(
        tmp_path,
        clock.now(),
        SIMULATIONS_AND_PREVALIDATIONS,
        runs=runs,
        facts=waiting(simulating=()),
        clock=clock,
    ) as (_, _, _, state):
        assert state.gave_way == {"simulate-bar-close-298": [date(2026, 10, 15)]}
        clock.set(at(FRIDAY, "22:30"))
        await until(lambda: state.gave_way == {})
        assert runs.started == []
    assert gave_way_on(tmp_path) == {}


async def test_a_job_that_gives_way_to_none_keeps_the_list_s_order(tmp_path: Path) -> None:
    """A long job listed first that gives way to nothing runs first, as every job did before."""
    long = Job(
        name="long",
        due=lambda night: night.record is None,
        command=("long",),
        timeout=timedelta(hours=5),
        targets=BAR_CLOSE_SIMULATION.targets,
    )
    clock = FakeClock(at(FRIDAY, "22:00"))
    runs = Takes(clock, {})
    async with scheduling(
        tmp_path, clock.now(), [long, *PREVALIDATIONS], runs=runs, facts=waiting("kalman"), clock=clock
    ) as (_, _, told, _):
        clock.set(at(FRIDAY, "22:30"))
        await until(lambda: runs.names() == ["long", "prevalidate kalman"])
        assert not told.saying("자리를 내준")


async def test_a_job_a_restart_cut_tonight_is_not_given_way_to(tmp_path: Path) -> None:
    """The night does not run it again (a job that kills the engine would loop): nothing to wait for."""
    later = Job(
        name="later",
        due=lambda night: True,
        command=("prevalidate", "later"),
        timeout=timedelta(hours=5),
        targets=BAR_CLOSE_SIMULATION.targets,
    )
    cut = JobRecord(FRIDAY, INTERRUPTED, at(FRIDAY, "22:30"), COMMIT)
    (tmp_path / STATE_FILE).write_text(json.dumps({"jobs": {"later": cut.as_dict()}, "unposted": []}))
    clock = FakeClock(at(FRIDAY, "23:00"))
    runs = Takes(clock, {})
    async with scheduling(
        tmp_path, clock.now(), [BAR_CLOSE_SIMULATION, later], runs=runs, facts=waiting(), clock=clock
    ) as (_, _, told, _):
        await until(lambda: runs.ended == [BAR_CLOSE])
        assert not told.saying("자리를 내준")
