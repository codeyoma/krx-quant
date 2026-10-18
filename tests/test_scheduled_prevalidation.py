"""The scheduled pre-validation (#204): which waiting algorithm is pre-validated tonight, and
what goes on #204.

The conditions are pure functions of the night (``Night``), asked here with nights made by
hand. The runs are stand-ins that write the report ``prevalidate`` would (the replay itself
is ``test_prevalidation.py``'s), except one real command on an empty server: it finds no
recorded day and leaves no report.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path

import aiohttp
import pytest

from krx_quant.algorithms import (
    ALGORITHMS,
    DAILY_ALGORITHMS,
    KIND_ALGORITHMS,
    Algorithm,
    KindAlgorithm,
    kind_of,
)
from krx_quant.clock import FakeClock
from krx_quant.github import GitHub
from krx_quant.kind_registry import kind_named
from krx_quant.kinds import DIRECTION
from krx_quant.prevalidation import MIN_DAYS, REGISTERED_BEFORE, LastReport, code_digest, report_path
from krx_quant.scheduled import (
    AGAIN,
    FAILED,
    FRESH,
    INTERRUPTED,
    JOBS,
    OK,
    STATE_FILE,
    Facts,
    Job,
    JobRecord,
    JobState,
    Night,
    Ran,
    Scheduler,
    prevalidation_job_name,
    prevalidation_order,
    prevalidation_reports,
    prevalidation_tonight,
    prevalidation_turn,
)
from krx_quant.touch import TARGET_VERSION
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
    scheduling,
    settle,
)
from tests.test_subscription_limit import until

ORDER = ("mandelbrot", "breiman", "kalman", "shannon", "poisson")
EVERYONE: dict[str, Algorithm | KindAlgorithm] = {
    **{a.name: a for a in ALGORITHMS},
    **{a.name: a for a in KIND_ALGORITHMS},
}
# The bar-close algorithms' (the daily kinds', #264, run apart: ``test_daily_prevalidation``).
PREVALIDATIONS = [
    j
    for j in JOBS
    if j.command[0] == "prevalidate" and j.command[1] not in {a.name for a in DAILY_ALGORITHMS}
]
NEW = "fedcba9876543210fedc"  # a later deploy's commit
TUESDAY, WEDNESDAY = date(2026, 10, 20), date(2026, 10, 21)


def report(
    *, current: bool = True, passed: bool = False, days: int = MIN_DAYS, made_at: datetime
) -> LastReport:
    return LastReport(made_at=made_at, current=current, passed=passed, days=days)


def ran(day: date, outcome: str = FAILED, commit: str = COMMIT) -> JobRecord:
    """A pre-validation's run, started at 22:30 on ``day``."""
    return JobRecord(day, outcome, at(day, "22:30"), commit, 1 if outcome == FAILED else 0, 600.0)


def night(
    day: date,
    *,
    recorded: int = MIN_DAYS,
    waiting: Sequence[str] = ORDER,
    reports: dict[str, LastReport] | None = None,
    runs: dict[str, JobRecord] | None = None,
    commit: str = COMMIT,
) -> Night:
    facts = Facts(recorded_days=recorded, waiting=tuple(waiting), official=0, reports=reports or {})
    records = {prevalidation_job_name(name): record for name, record in (runs or {}).items()}
    return Night(day, CALENDAR.is_trading_day(day), commit, None, records, facts)


def tonight(n: Night) -> str | None:
    """The algorithm pre-validated tonight, as the jobs' conditions see it: one at most."""
    due = [j.command[1] for j in PREVALIDATIONS if j.due(n)]
    assert len(due) <= 1, due
    assert prevalidation_tonight(n, ORDER) == (due[0] if due else None)
    return due[0] if due else None


# The jobs listed


def test_the_order_is_the_five_then_the_registry_s() -> None:
    registry = [a.name for a in ALGORITHMS] + [a.name for a in KIND_ALGORITHMS]
    # The five, then the registry's others not run before pre-validation: Neyman (#266) and
    # the Breimans of the barrier's horizon ladder (#263).
    rest = ("neyman", "breiman-15m", "breiman-30m", "breiman-60m")
    assert prevalidation_order(registry) == (*ORDER, *rest)
    assert [j.name for j in PREVALIDATIONS] == [f"prevalidate-{name}" for name in (*ORDER, *rest)]
    # A new algorithm gets its job from the registries; one registered before needs none.
    names = ["newton", "zeta", "kalman", "turing", "mandelbrot", "alpha"]
    assert prevalidation_order(names) == ("mandelbrot", "kalman", "zeta", "alpha")
    assert not REGISTERED_BEFORE & set(prevalidation_order(registry))


def test_each_job_prevalidates_its_algorithm_and_posts_on_204() -> None:
    for listed in PREVALIDATIONS:
        name = listed.command[1]
        assert listed.command == ("prevalidate", name)
        assert listed.timeout == timedelta(hours=6 if name.startswith("breiman") else 5)
        assert [t.issue for t in listed.targets] == [204]
        assert [(a.title, a.path) for a in listed.attach] == [
            (f"리포트 state/prevalidations/{name}.json", f"prevalidations/{name}.json")
        ]
        assert listed.label == ("ready-for-agent",)


# The conditions


def test_nothing_until_ten_trading_days_are_recorded() -> None:
    assert tonight(night(FRIDAY, recorded=MIN_DAYS - 1)) is None
    assert tonight(night(FRIDAY, recorded=MIN_DAYS)) == "mandelbrot"
    assert prevalidation_turn(night(FRIDAY, recorded=MIN_DAYS - 1), "mandelbrot") is None


def test_only_an_algorithm_the_engine_waits_for() -> None:
    assert tonight(night(FRIDAY, waiting=("kalman", "breiman"))) == "breiman"
    assert tonight(night(FRIDAY, waiting=("poisson",))) == "poisson"
    assert tonight(night(FRIDAY, waiting=())) is None
    assert tonight(night(FRIDAY, waiting=("newton",))) is None  # runs without a report


@pytest.mark.parametrize("outcome", [OK, FAILED, INTERRUPTED])
def test_one_a_night_whatever_came_of_it(outcome: str) -> None:
    # A run to its end left its verdict; one a restart cut, none.
    left = {} if outcome == INTERRUPTED else {"mandelbrot": report(made_at=at(FRIDAY, "23:10"))}
    tried = {"mandelbrot": ran(FRIDAY, outcome)}
    assert tonight(night(FRIDAY, runs=tried, reports=left)) is None
    # The next night, the next one; or mandelbrot again if a restart cut it.
    assert tonight(night(SATURDAY, runs=tried, reports=left)) == (
        "mandelbrot" if outcome == INTERRUPTED else "breiman"
    )


def test_a_verdict_on_the_code_it_runs_now_is_not_asked_for_again() -> None:
    tried = {"mandelbrot": ran(FRIDAY)}
    for verdict in (report(made_at=at(FRIDAY, "23:10")), report(passed=True, made_at=at(FRIDAY, "23:10"))):
        # A deploy that leaves its code as it was does not ask again; a pass still waiting
        # (no bar close since: a weekend) is not run again either.
        for commit in (COMMIT, NEW):
            n = night(SATURDAY, runs=tried, reports={"mandelbrot": verdict}, commit=commit)
            assert prevalidation_turn(n, "mandelbrot") is None
            assert tonight(n) == "breiman"


def test_new_code_or_a_new_target_asks_again_in_its_turn() -> None:
    # Refused on Friday; its code (or the target) has changed since: the report is not current.
    stale = {"mandelbrot": report(current=False, made_at=at(FRIDAY, "23:10"))}
    tried = {"mandelbrot": ran(FRIDAY), "breiman": ran(THURSDAY)}
    stale["breiman"] = report(made_at=at(THURSDAY, "23:10"))  # breiman's verdict stands
    for commit in (NEW, COMMIT):  # the target is a setting: no new commit for it
        n = night(SATURDAY, runs=tried, reports=stale, commit=commit)
        assert prevalidation_turn(n, "mandelbrot") == FRESH
        assert tonight(n) == "mandelbrot"  # ahead of kalman, never tried


def test_a_look_on_fewer_days_is_no_verdict() -> None:
    look = {"mandelbrot": report(days=5, made_at=at(THURSDAY, "19:00"))}  # run by hand early
    assert tonight(night(FRIDAY, reports=look)) == "mandelbrot"
    # The job left one itself (it counted the days otherwise): not again on the same code.
    # A report's time is its run's start.
    left = {"mandelbrot": report(days=5, made_at=at(FRIDAY, "22:31"))}
    assert tonight(night(SATURDAY, reports=left, runs={"mandelbrot": ran(FRIDAY)})) == "breiman"


def test_a_look_by_hand_after_the_job_asks_for_a_verdict_again() -> None:
    """Friday's job refused mandelbrot on 10 days; on Saturday someone looked again by hand on
    5 days, on the same code, and that report replaced the verdict: Sunday asks again."""
    look = {"mandelbrot": report(days=5, made_at=at(SATURDAY, "10:00"))}
    refused = {"mandelbrot": ran(FRIDAY, OK)}
    assert tonight(night(SUNDAY, reports=look, runs=refused)) == "mandelbrot"
    # A look started while the job ran (it could not: the lock) would not count.
    during = {"mandelbrot": report(days=5, made_at=at(FRIDAY, "22:35"))}
    assert tonight(night(SUNDAY, reports=during, runs=refused)) == "breiman"


def test_a_run_that_left_no_verdict_waits_for_a_deploy_then_for_the_others() -> None:
    # Friday's mandelbrot crashed (or hit its time limit): no report since it started.
    crashed = {"mandelbrot": ran(FRIDAY)}
    older = {"mandelbrot": report(current=False, made_at=at(THURSDAY, "19:00"))}  # not this run's
    for reports in ({}, older):
        assert prevalidation_turn(night(SATURDAY, runs=crashed, reports=reports), "mandelbrot") is None
        assert (
            prevalidation_turn(night(SATURDAY, runs=crashed, reports=reports, commit=NEW), "mandelbrot")
            == AGAIN
        )
    # After a deploy it runs again, but after those not tried yet.
    assert tonight(night(SATURDAY, runs=crashed, commit=NEW)) == "breiman"
    judged = {name: report(made_at=at(THURSDAY, "23:10")) for name in ORDER[1:]}
    assert tonight(night(SATURDAY, runs=crashed, reports=judged, commit=NEW)) == "mandelbrot"
    assert tonight(night(SATURDAY, runs=crashed, reports=judged)) is None  # the same code: nothing


# The jobs run


def refusal(algorithm: Algorithm | KindAlgorithm, made_at: datetime) -> dict[str, object]:
    """A report as ``prevalidate`` writes it: refused, on the code and target it runs now (the
    engine's touch target, ``TARGET_VERSION``; a kind's answer version)."""
    kind = None if kind_of(algorithm) == DIRECTION else kind_named(kind_of(algorithm))
    days = [(date(2026, 10, 1) + timedelta(days=i)).isoformat() for i in range(MIN_DAYS)]
    return {
        "algorithm": algorithm.name,
        "code": code_digest(algorithm),
        "prevalidated_at": made_at.isoformat(),
        "passed": False,
        "days": days,
        "checks": [{"check": "stats", "passed": False, "detail": "weakest lift -0.0100 over 900 scored"}],
        "target_version": TARGET_VERSION if kind is None else kind.answer_version,
    }


class Prevalidates(Runs):
    """A stand-in ``prevalidate <name>``: writes a refusal in the state directory, as the
    command would, and prints its lines."""

    def __init__(self, clock: FakeClock, state_dir: Path) -> None:
        super().__init__(clock)
        self.state_dir = state_dir

    async def __call__(self, args: Sequence[str], timeout: timedelta) -> Ran:
        assert args[0] == "prevalidate"
        name = args[1]
        path = report_path(self.state_dir, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(refusal(EVERYONE[name], self.clock.now()), indent=2))
        self.started.append((name, self.clock.now()))
        return Ran(1, f"{name}: refused ({MIN_DAYS} recorded days)\n  stats: FAILED - weakest lift\n", "")


async def test_one_algorithm_a_night_in_turn_each_refusal_on_204_with_its_report(tmp_path: Path) -> None:
    clock = FakeClock(at(FRIDAY, "22:00"))
    runs = Prevalidates(clock, tmp_path)  # the state file's directory is the state directory

    def facts() -> Facts:  # read each night, as the collector does
        return Facts(
            MIN_DAYS, ORDER, 0, prevalidation_reports(tmp_path, list(EVERYONE.values()), TARGET_VERSION)
        )

    def told(lines: int) -> Callable[[], bool]:
        return lambda: len(lines_told) == lines

    nights = [FRIDAY, SATURDAY, SUNDAY, MONDAY, TUESDAY]
    async with fake_github() as hub:
        hub.labels = {204: {"ready-for-human"}}
        async with scheduling(
            tmp_path, at(FRIDAY, "22:00"), PREVALIDATIONS, runs=runs, github=hub, facts=facts, clock=clock
        ) as (_, _, lines_told, state):
            for count, day in enumerate(nights, start=1):
                clock.set(at(day, "22:30"))
                await until(told(count))  # its result's line
            assert runs.names() == list(ORDER)  # one a night, in turn
            assert [state.jobs[f"prevalidate-{n}"].day for n in ORDER] == nights
            clock.set(at(WEDNESDAY, "22:30"))
            await settle()
            assert runs.names() == list(ORDER)  # all refused on the code they run: none again

            # kalman's code changes in a deploy: its refusal no longer stands, and it runs again.
            path = report_path(tmp_path, "kalman")
            path.write_text(json.dumps({**json.loads(path.read_text()), "code": "0" * 16}))
            clock.set(at(THURSDAY + timedelta(days=7), "22:30"))
            await until(told(6))
            assert runs.names()[-1] == "kalman"

    bodies = hub.bodies(204)
    assert len(bodies) == 6 and hub.labels == {204: {"ready-for-agent"}}
    first = bodies[0]
    assert first.splitlines()[:4] == [
        "**예약 작업 `prevalidate-mandelbrot`** · 2026-10-16 밤 · 커밋 `0123456789ab` · 종료 코드 1 · 0초",
        "",
        "다시 돌리기: `docker compose run --rm engine prevalidate mandelbrot`",
        "통과(`passed`)하면 엔진이 다음 봉 마감부터 수습으로 돌리고, 수습이 시작된 그 밤 "
        "`simulate-bar-close-298`이 최근 녹화일로 수습을 시뮬레이션해 바로 판정한다(#298). "
        "거부(`refused`)되면 같은 코드·정답 버전으로는 다시 돌지 않고, 고친 코드가 배포되면 다시 돈다.",
    ]
    assert (
        f"```\nmandelbrot: refused ({MIN_DAYS} recorded days)\n  stats: FAILED - weakest lift\n```" in first
    )
    folded = first.split("<details><summary>리포트 state/prevalidations/mandelbrot.json</summary>\n\n```\n")[
        1
    ]
    assert json.loads(folded.split("\n```")[0]) == refusal(EVERYONE["mandelbrot"], at(FRIDAY, "22:30"))
    assert lines_told[0].startswith(
        "예약 작업 prevalidate-mandelbrot(2026-10-16 밤): 실패, 종료 코드 1, 0초. 댓글: "
    )


@pytest.fixture
def empty_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An engine's data and state directories with nothing recorded, for the real command."""
    monkeypatch.chdir(tmp_path)  # no .env
    monkeypatch.setenv("KRX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


async def test_the_job_runs_the_real_command(empty_server: Path) -> None:
    state = JobState.load(empty_server / "state" / STATE_FILE)
    [mandelbrot] = [j for j in PREVALIDATIONS if j.name == "prevalidate-mandelbrot"]

    async def facts() -> Facts:  # as if ten days were recorded: the command finds none
        return Facts(MIN_DAYS, ORDER)

    async def run_night(day: date, jobs: Sequence[Job]) -> None:
        async with aiohttp.ClientSession() as session:
            scheduler = Scheduler(
                jobs,
                AT,
                CALENDAR,
                state,
                GitHub(session, TOKEN, REPO, hub.url),
                Told(),
                FakeClock(at(day, "23:00")),
                commit=COMMIT,
                facts=facts,
            )
            await asyncio.wait_for(scheduler.run_night(day), 25)

    async with fake_github() as hub:
        await run_night(FRIDAY, [mandelbrot])
        [body] = hub.bodies(204)
        assert "종료 코드 1" in body.splitlines()[0]
        assert "`state/prevalidations/mandelbrot.json`은 이번 실행이 쓰지 않아 붙이지 않는다." in body
        assert "<details><summary>stderr</summary>" in body and "no recorded day" in body
        assert state.jobs["prevalidate-mandelbrot"].outcome == FAILED
        # No verdict on this code: not again until a deploy; breiman's turn the next night.
        await run_night(SATURDAY, [mandelbrot])
        assert len(hub.bodies(204)) == 1
