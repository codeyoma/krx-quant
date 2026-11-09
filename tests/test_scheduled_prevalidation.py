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
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

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
from krx_quant.process_log import Child
from krx_quant.scheduled import (
    AGAIN,
    FAILED,
    FRESH,
    INTERRUPTED,
    JOBS,
    OK,
    PASSED,
    PREVALIDATION_ISSUE,
    REFUSED,
    STATE_FILE,
    Job,
    JobRecord,
    JobState,
    Night,
    PrevalidationFacts,
    Ran,
    Revising,
    Scheduler,
    Target,
    prevalidation_job_name,
    prevalidation_order,
    prevalidation_reports,
    prevalidation_tonight,
    prevalidation_turn,
    read_prevalidation_facts,
    revision_job_name,
    revision_turn,
)
from krx_quant.scheduled.prevalidations import Turn, prevalidations_quiet, revisions_quiet
from krx_quant.touch import TARGET_VERSION
from tests.fake_github import REPO, TOKEN, fake_github
from tests.scheduled_doubles import (
    AT,
    CALENDAR,
    COMMIT,
    FRIDAY,
    SATURDAY,
    SUNDAY,
    THURSDAY,
    Runs,
    Told,
    at,
    faking,
    saved,
    scheduling,
    settle,
)
from tests.test_scheduled import Writes
from tests.test_scheduled_give_way import Takes
from tests.waiting import until

ORDER = ("mandelbrot", "breiman", "kalman", "shannon", "poisson")
EVERYONE: dict[str, Algorithm | KindAlgorithm] = {
    **{a.name: a for a in ALGORITHMS},
    **{a.name: a for a in KIND_ALGORITHMS},
}
# The bar-close algorithms' (the daily kinds', #264, run apart: ``test_daily_prevalidation``), first
# ones (a changed code's, #454, below).
PREVALIDATIONS = [
    j
    for j in JOBS
    if j.command[0] == "prevalidate"
    and j.command[1] not in {a.name for a in DAILY_ALGORITHMS}
    and "--revision" not in j.command
]
REVISIONS = [j for j in JOBS if "--revision" in j.command]
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
) -> Turn:
    facts = PrevalidationFacts(recorded_days=recorded, waiting=tuple(waiting), reports=reports or {})
    records = {prevalidation_job_name(name): record for name, record in (runs or {}).items()}
    return Night(day, CALENDAR.is_trading_day(day), commit, None, records, facts)


def tonight(n: Turn) -> str | None:
    """The algorithm pre-validated tonight, as the jobs' conditions see it: one at most."""
    due = [j.command[1] for j in PREVALIDATIONS if j.due(n)]
    assert len(due) <= 1, due
    assert prevalidation_tonight(n, ORDER) == (due[0] if due else None)
    return due[0] if due else None


# The jobs listed


def test_the_order_is_the_five_then_the_registry_s() -> None:
    registry = [a.name for a in ALGORITHMS] + [a.name for a in KIND_ALGORITHMS]
    # The five, then the registry's others not run before pre-validation: Neyman (#266), the
    # stochastic processes (#518) and the Breimans of the barrier's horizon ladder (#263).
    rest = ("neyman", "samuelson", "ito", "ulam", "bishop", "breiman-15m", "breiman-30m", "breiman-60m")
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
        # The slowest run's pace over 20 days, with room (#546): the other kinds' replay theirs too.
        other_kind = kind_of(EVERYONE[name]) != DIRECTION
        assert listed.timeout == (timedelta(hours=4, minutes=30) if other_kind else timedelta(hours=3))
        assert [t.issue for t in listed.targets] == [204]
        assert [(a.title, a.path) for a in listed.attach] == [
            (f"리포트 state/prevalidations/{name}.json", f"prevalidations/{name}.json")
        ]
        assert listed.label == ("ready-for-agent",)
        # Neyman's result asks the agent to summarise its diagnostics on #283; the others' do not.
        assert ("#283에 요약한다" in listed.targets[0].note) == (name == "neyman")
        assert listed.targets[0].note.startswith("통과(`passed`)하면 엔진이 다음 봉 마감부터 수습으로")


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


def quiet(n: Turn) -> tuple[str, ...]:
    """The algorithms that may be pre-validated second on a quiet night, as the jobs' conditions on
    one see them (#546), in turn."""
    due = tuple(j.command[1] for j in PREVALIDATIONS if j.quiet is not None and j.quiet(n))
    assert prevalidations_quiet(n, ORDER) == due
    return due


def test_a_quiet_night_asks_for_a_second_after_exactly_one_whatever_came_of_it() -> None:
    assert quiet(night(FRIDAY)) == (), "none ran tonight: the night's turn picks"
    for outcome in (OK, FAILED):
        one = {"mandelbrot": ran(FRIDAY, outcome)}
        assert tonight(night(FRIDAY, runs=one)) is None
        assert quiet(night(FRIDAY, runs=one)) == ("breiman", "kalman", "shannon", "poisson"), "in turn"
    # A restart cut it (it may have taken the engine down with it): no second tonight.
    assert quiet(night(FRIDAY, runs={"mandelbrot": ran(FRIDAY, INTERRUPTED)})) == ()
    two = {"mandelbrot": ran(FRIDAY), "breiman": ran(FRIDAY)}
    assert quiet(night(FRIDAY, runs=two)) == (), "never more than two"
    # A changed code's run tonight is the night's one too (#454).
    n = night(FRIDAY)
    revised = replace(n, records={revision_job_name("turing"): ran(FRIDAY)})
    assert tonight(revised) is None and quiet(revised) == ORDER
    assert quiet(replace(revised, records={**revised.records, **night(FRIDAY, runs=one).records})) == ()
    # Those never tried before those tried again on new code, as in the night's turn.
    again = {name: ran(THURSDAY) for name in ORDER[1:]}  # no verdict, on the code before NEW
    deployed = {"mandelbrot": ran(FRIDAY, commit=NEW)}
    assert quiet(night(FRIDAY, runs={**deployed, **again}, commit=NEW)) == ORDER[1:]
    retried = {**deployed, **again, "poisson": ran(THURSDAY, INTERRUPTED)}  # cut by a restart: fresh
    assert quiet(night(FRIDAY, runs=retried, commit=NEW)) == ("poisson",)
    assert quiet(night(FRIDAY, runs={**one, **again})) == (), "the same code, no verdict: not again"


def test_a_changed_code_may_be_the_quiet_night_s_second_ahead_of_the_waiting() -> None:
    changed = {"neyman": revising(MIN_DAYS)}
    first = {prevalidation_job_name("mandelbrot"): ran(FRIDAY, OK)}
    n = revision_night(FRIDAY, changed, runs=first)
    assert revisions_quiet(n, [j.command[1] for j in REVISIONS]) == ("neyman",)
    assert [j.command[1] for j in REVISIONS if j.quiet is not None and j.quiet(n)] == ["neyman"]
    assert prevalidations_quiet(n, ORDER) == ORDER[1:], "asked after it, in the list's order"
    both = revision_night(FRIDAY, changed, runs={**first, revision_job_name("neyman"): ran(FRIDAY, OK)})
    assert revisions_quiet(both, [j.command[1] for j in REVISIONS]) == ()
    assert prevalidations_quiet(both, ORDER) == ()


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

    async def __call__(
        self,
        args: Sequence[str],
        timeout: timedelta,
        child: Child | None = None,
    ) -> Ran:
        assert args[0] == "prevalidate"
        name = args[1]
        path = report_path(self.state_dir, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(refusal(EVERYONE[name], self.clock.now()), indent=2))
        self.started.append((name, self.clock.now()))
        return Ran(1, f"{name}: refused ({MIN_DAYS} recorded days)\n  stats: FAILED - weakest lift\n", "")


async def test_in_turn_one_a_night_two_on_a_quiet_one_each_refusal_on_204_with_its_report(
    tmp_path: Path,
) -> None:
    """The runs take no time: every night is quiet, and a second follows the first (#546)."""
    clock = FakeClock(at(FRIDAY, "22:00"))
    runs = Prevalidates(clock, tmp_path)  # the state file's directory is the state directory

    def facts() -> PrevalidationFacts:  # read each night, as the collector does
        return PrevalidationFacts(
            MIN_DAYS, ORDER, prevalidation_reports(tmp_path, list(EVERYONE.values()), TARGET_VERSION)
        )

    def told(lines: int) -> Callable[[], bool]:
        return lambda: len(lines_told) == lines

    nights = [FRIDAY, FRIDAY, SATURDAY, SATURDAY, SUNDAY]
    async with fake_github() as hub:
        hub.labels = {204: {"ready-for-human"}}
        async with scheduling(
            tmp_path,
            at(FRIDAY, "22:00"),
            faking(PREVALIDATIONS, {read_prevalidation_facts: facts}),
            runs=runs,
            github=hub,
            clock=clock,
        ) as (_, _, lines_told, state):
            for count, day in zip((2, 4, 5), (FRIDAY, SATURDAY, SUNDAY), strict=True):
                clock.set(at(day, "22:30"))
                await until(told(count))  # their results' lines
            assert runs.names() == list(ORDER)  # in turn, two a quiet night
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
        "**예약 작업 `prevalidate-mandelbrot`** · 2026-10-16 밤 · 거부 · 커밋 `0123456789ab` · "
        "종료 코드 1 · 0초",
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
        "예약 작업 prevalidate-mandelbrot(2026-10-16 밤): 거부, 종료 코드 1, 0초. 댓글: "
    )
    # A refusal failed, as its exit code 1 always did to the conditions: they are as they were.
    assert {(r.outcome, r.verdict) for r in state.jobs.values()} == {(FAILED, REFUSED)}


@pytest.mark.parametrize(
    ("took", "second", "saturday"),
    [
        # 23:30: breiman's 4 h 30 min still ends by 05:30; kalman's turn the next night.
        (timedelta(hours=1), "prevalidate breiman", "prevalidate kalman"),
        # 01:30: past breiman's last start (01:00), not kalman's (02:30): the first that fits.
        (timedelta(hours=3), "prevalidate kalman", "prevalidate breiman"),
        # 02:31: none fits; the next night's turn is breiman's, in turn.
        (timedelta(hours=4, minutes=1), None, "prevalidate breiman"),
    ],
)
async def test_a_quiet_night_s_second_pre_validation_follows_every_other_job_if_it_ends_by_05_30(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, took: timedelta, second: str | None, saturday: str
) -> None:
    """Input: mandelbrot's run in its turn taking ``took`` from 22:30, a job listed after it, and
    breiman and kalman waiting too. Output (#546): once that job ran, the first of the waiting in turn
    whose time limit still ends by 05:30 runs; two at most."""
    later: Job[None] = Job(
        name="later",
        due=lambda n: n.record is None or n.record.day != n.day,  # once a night
        command=("later",),
        timeout=timedelta(minutes=30),
        targets=(Target(999),),
    )
    caplog.set_level("INFO", logger="krx_quant.scheduled")
    clock = FakeClock(at(FRIDAY, "22:00"))
    runs = Takes(clock, {"prevalidate mandelbrot": took})
    facts = PrevalidationFacts(MIN_DAYS, ("mandelbrot", "breiman", "kalman"))
    async with scheduling(
        tmp_path,
        clock.now(),
        faking([*PREVALIDATIONS, later], {read_prevalidation_facts: facts}),
        runs=runs,
        clock=clock,
    ) as (_, _, _, _):
        clock.set(at(FRIDAY, "22:30"))
        await until(lambda: runs.names() == ["prevalidate mandelbrot"])
        clock.set(at(FRIDAY, "22:30") + took)
        await until(lambda: "later" in runs.ended)
        clock.set(at(SATURDAY, "22:30"))  # the next night's turn marks the end of this one
        await until(lambda: saturday in [name for name, when in runs.started if when >= clock.now()])
    friday = [name for name, when in runs.started if when < at(SATURDAY, "22:30")]
    assert friday == ["prevalidate mandelbrot", "later", *([second] if second else [])]
    passed = {
        r.getMessage().split(":")[0] for r in caplog.records if "due on the quiet night" in r.getMessage()
    }
    if second is None:
        assert passed == {"scheduled job prevalidate-breiman", "scheduled job prevalidate-kalman"}
    elif second.endswith("kalman"):
        assert passed == {"scheduled job prevalidate-breiman"}  # asked before kalman, too late


# What came of a run (#440): a refusal is the command's verdict, not a failure, though its exit
# code is 1 as an error's is.

MANDELBROT = next(j for j in PREVALIDATIONS if j.name == "prevalidate-mandelbrot")
TOLD = "예약 작업 prevalidate-mandelbrot(2026-10-16 밤): "
# Its comment's first line: what came of it, and the exit code.
HEADING = (
    "**예약 작업 `prevalidate-mandelbrot`** · 2026-10-16 밤 · {} · 커밋 `0123456789ab` · 종료 코드 {} · 0초"
)


async def ended(state_dir: Path, ran: Ran, report: str | None = None) -> tuple[str, str, JobRecord]:
    """mandelbrot's pre-validation run once, on Friday night from 22:30, ending as ``ran`` and
    writing ``report`` as its report while it runs (None: none): the line told without the
    comments' addresses, its comment's first line, its record."""
    clock = FakeClock(at(FRIDAY, "22:00"))
    path = report_path(state_dir, "mandelbrot")
    path.parent.mkdir(parents=True, exist_ok=True)
    runs = Writes(clock, ran, {} if report is None else {path: report})
    async with fake_github() as hub:
        async with scheduling(
            state_dir,
            at(FRIDAY, "22:00"),
            faking([MANDELBROT], {read_prevalidation_facts: PrevalidationFacts(MIN_DAYS, ORDER)}),
            runs=runs,
            github=hub,
            clock=clock,
        ) as (_, _, told, state):
            clock.set(at(FRIDAY, "22:30"))
            await until(lambda: bool(told))
    [body] = hub.bodies(PREVALIDATION_ISSUE)
    return told[0].split(" 댓글: ")[0], body.splitlines()[0], state.jobs[MANDELBROT.name]


async def test_each_end_of_a_pre_validation_is_told_in_words_of_its_own(tmp_path: Path) -> None:
    refused = refusal(EVERYONE["mandelbrot"], at(FRIDAY, "22:30"))  # made as the run starts: its own
    ends = {
        "passed": (Ran(0, "mandelbrot: passed\n", ""), json.dumps({**refused, "passed": True})),
        "refused": (Ran(1, "mandelbrot: refused\n", ""), json.dumps(refused)),
        "raised": (Ran(1, "", "Traceback (most recent call last): ...\nKeyError: 'x'\n"), None),
        "timed_out": (Ran(-9, "half\n", "", timed_out=True), None),
        "killed": (Ran(-9, "", ""), None),  # the kernel's out-of-memory killer (breiman, 2026-10-27)
    }
    seen = {name: await ended(tmp_path / name, ran, report) for name, (ran, report) in ends.items()}

    assert {name: told for name, (told, _, _) in seen.items()} == {
        "passed": TOLD + "통과, 종료 코드 0, 0초.",
        "refused": TOLD + "거부, 종료 코드 1, 0초.",
        "raised": TOLD + "예외, 종료 코드 1, 0초.",
        "timed_out": TOLD + "시간 초과, 제한 시간 4시간 30분을 넘어 멈춤, 0초.",
        "killed": TOLD + "신호로 죽음, 종료 코드 -9(SIGKILL), 0초.",
    }
    assert {name: heading for name, (_, heading, _) in seen.items()} == {
        "passed": HEADING.format("통과", "0"),
        "refused": HEADING.format("거부", "1"),
        "raised": HEADING.format("예외", "1"),
        "timed_out": HEADING.format("시간 초과", "-9(SIGKILL)"),
        "killed": HEADING.format("신호로 죽음", "-9(SIGKILL)"),
    }
    # The record keeps how the run ended and what it judged apart.
    assert {name: (r.outcome, r.verdict) for name, (_, _, r) in seen.items()} == {
        "passed": (OK, PASSED),
        "refused": (FAILED, REFUSED),
        "raised": (FAILED, None),
        "timed_out": (FAILED, None),
        "killed": (FAILED, None),
    }
    jobs = saved(tmp_path / "refused")["jobs"]
    assert isinstance(jobs, dict) and jobs["prevalidate-mandelbrot"]["verdict"] == REFUSED  # as saved


async def test_an_exit_1_without_a_whole_report_of_its_own_is_a_failure_not_a_refusal(tmp_path: Path) -> None:
    candidate = EVERYONE["mandelbrot"]
    earlier = json.dumps(refusal(candidate, at(THURSDAY, "23:10")))  # an earlier run's refusal
    left = report_path(tmp_path / "left", "mandelbrot")
    left.parent.mkdir(parents=True)
    left.write_text(earlier)  # and this run leaves it as it was
    timeless = {k: v for k, v in refusal(candidate, at(FRIDAY, "22:30")).items() if k != "prevalidated_at"}
    passing = json.dumps({**refusal(candidate, at(FRIDAY, "22:30")), "passed": True})
    exits = {
        "none": None,  # no report at all (no recorded day, a lock held, a crash before it)
        "left": None,
        "stale": earlier,  # written while it ran, but made before it started
        "timeless": json.dumps(timeless),
        "torn": '{"passed": false, "prevalidated_at": "2026-10-16T22:30',
        "passing": passing,  # a pass with exit 1: something after it failed
    }
    crashed = Ran(1, "", "Traceback (most recent call last): ...\n")
    seen = {name: await ended(tmp_path / name, crashed, report) for name, report in exits.items()}
    assert {name: told for name, (told, _, _) in seen.items()} == dict.fromkeys(
        exits, TOLD + "예외, 종료 코드 1, 0초."
    )
    assert {name: heading for name, (_, heading, _) in seen.items()} == dict.fromkeys(
        exits, HEADING.format("예외", "1")
    )
    assert {(r.outcome, r.verdict) for _, _, r in seen.values()} == {(FAILED, None)}


def test_a_state_saved_before_the_verdicts_is_read_as_it_was(tmp_path: Path) -> None:
    poisson = {  # as the scheduler saved a run before #440: no verdict
        "day": "2026-10-16",
        "outcome": "failed",
        "started_at": "2026-10-16T22:30:00+09:00",
        "commit": COMMIT,
        "exit_code": 1,
        "seconds": 3600.0,
        "timed_out": False,
        "comments": {"204": "https://github.com/x/y/issues/204#issuecomment-1"},
    }
    before = {"jobs": {"prevalidate-poisson": poisson}, "unposted": [], "gave_way": {}}
    (tmp_path / STATE_FILE).write_text(json.dumps(before))
    state = JobState.load(tmp_path / STATE_FILE)
    assert not (tmp_path / f"{STATE_FILE}.bad").exists()
    record = state.jobs["prevalidate-poisson"]
    assert (record.outcome, record.exit_code, record.verdict, record.comments) == (
        FAILED,
        1,
        None,
        {204: "https://github.com/x/y/issues/204#issuecomment-1"},
    )
    # Its turn reads it as before (#204): it ran on this code, and no report judged it.
    assert prevalidation_turn(night(SATURDAY, runs={"poisson": record}), "poisson") is None
    state.save()
    assert saved(tmp_path)["jobs"] == {"prevalidate-poisson": {**poisson, "verdict": None}}


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

    facts = PrevalidationFacts(MIN_DAYS, ORDER)  # as if ten days were recorded: the command finds none

    async def run_night(day: date, jobs: Sequence[Job[Any]]) -> None:
        async with aiohttp.ClientSession() as session:
            scheduler = Scheduler(
                faking(jobs, {read_prevalidation_facts: facts}),
                AT,
                CALENDAR,
                state,
                GitHub(session, TOKEN, REPO, hub.url),
                Told(),
                FakeClock(at(day, "23:00")),
                commit=COMMIT,
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


# A running algorithm's changed code (#454): pre-validated on the days after the change, first in
# the night's one turn.

CHANGED_ON = date(2026, 10, 5)  # a Monday, the week before the nights here
CHANGED = at(CHANGED_ON, "06:00")  # the deploy that changed it: a revision's boundary


def revising(
    recorded: int, made_at: datetime | None = None, *, days: int = MIN_DAYS, passed: bool = False
) -> Revising:
    """A changed revision from CHANGED_ON's morning with ``recorded`` days recorded since, its report made
    at ``made_at`` for it if given (on the code it runs, on ``days`` days, ``passed`` or not)."""
    found = None if made_at is None else report(made_at=made_at, days=days, passed=passed)
    return Revising(CHANGED, CHANGED_ON, recorded, found)


def revision_night(
    day: date,
    changed: dict[str, Revising],
    *,
    waiting: Sequence[str] = ORDER,
    runs: dict[str, JobRecord] | None = None,
    commit: str = COMMIT,
) -> Turn:
    """A night with ``changed`` revisions, ``waiting`` algorithms and ``runs`` by job name."""
    facts = PrevalidationFacts(recorded_days=30, waiting=tuple(waiting), revising=changed)
    return Night(day, CALENDAR.is_trading_day(day), commit, None, dict(runs or {}), facts)


def revised_tonight(n: Turn) -> str | None:
    """The changed code pre-validated tonight, as the jobs' conditions see it: one at most, and
    then no first pre-validation."""
    due = [j.command[1] for j in REVISIONS if j.due(n)]
    assert len(due) <= 1, due
    return due[0] if due else None


def test_every_bar_close_algorithm_has_a_revision_job_turing_too() -> None:
    registry = [a.name for a in ALGORITHMS] + [a.name for a in KIND_ALGORITHMS]
    assert [j.command for j in REVISIONS] == [("prevalidate", name, "--revision") for name in registry]
    assert {"turing", "newton", "neyman"} <= {j.command[1] for j in REVISIONS}
    for job in REVISIONS:
        name = job.command[1]
        assert job.name == revision_job_name(name)
        # As its first pre-validation's (#546).
        other_kind = kind_of(EVERYONE[name]) != DIRECTION
        assert job.timeout == (timedelta(hours=4, minutes=30) if other_kind else timedelta(hours=3))
        assert [t.issue for t in job.targets] == [PREVALIDATION_ISSUE]
        assert job.report == f"prevalidations/{name}.json"
        assert ("#283에 요약한다" in job.targets[0].note) == (name == "neyman")
    names = [j.name for j in JOBS]
    # After the bar-close simulation (it gives way to them as to any pre-validation), before the
    # first pre-validations: its algorithm runs already and cannot be made active before it passes.
    assert names.index("simulate-bar-close-298") < names.index(REVISIONS[0].name)
    assert names.index(REVISIONS[-1].name) < names.index(PREVALIDATIONS[0].name)
    [simulation] = [j for j in JOBS if j.name == "simulate-bar-close-298"]
    assert simulation.gives_way_to is not None and all(simulation.gives_way_to(j) for j in REVISIONS)


def test_a_changed_code_is_pre_validated_once_ten_days_are_recorded_after_the_change() -> None:
    assert revised_tonight(revision_night(FRIDAY, {"turing": revising(MIN_DAYS - 1)})) is None
    assert revision_turn(revision_night(FRIDAY, {"turing": revising(MIN_DAYS - 1)}), "turing") is None
    assert revised_tonight(revision_night(FRIDAY, {"turing": revising(MIN_DAYS)})) == "turing"
    assert revised_tonight(revision_night(FRIDAY, {})) is None, "no changed code"
    # Two in the registries' order: newton first, turing the next night.
    both = {"turing": revising(MIN_DAYS), "newton": revising(MIN_DAYS)}
    assert revised_tonight(revision_night(FRIDAY, both)) == "newton"
    ran_newton = {revision_job_name("newton"): ran(FRIDAY, OK)}
    left = {**both, "newton": revising(MIN_DAYS, at(FRIDAY, "23:10"))}  # its verdict
    assert revised_tonight(revision_night(FRIDAY, left, runs=ran_newton)) is None, "one a night"
    assert revised_tonight(revision_night(SATURDAY, left, runs=ran_newton)) == "turing"


def test_it_takes_the_night_s_one_turn_before_the_algorithms_waiting() -> None:
    changed = {"neyman": revising(MIN_DAYS)}
    n = revision_night(FRIDAY, changed)
    assert revised_tonight(n) == "neyman"
    assert prevalidation_tonight(n, ORDER) == "mandelbrot", "asked before it ran"
    after = revision_night(FRIDAY, changed, runs={revision_job_name("neyman"): ran(FRIDAY, OK)})
    assert prevalidation_tonight(after, ORDER) is None, "it ran tonight: the waiting wait a night"
    # A first pre-validation already ran tonight (a restart at night): the changed code waits too.
    first = revision_night(FRIDAY, changed, runs={prevalidation_job_name("mandelbrot"): ran(FRIDAY, OK)})
    assert revised_tonight(first) is None


def test_a_changed_code_s_verdict_is_not_asked_again_a_run_without_one_waits_for_a_deploy() -> None:
    judged = {"turing": revising(MIN_DAYS, at(FRIDAY, "23:10"))}
    tried = {revision_job_name("turing"): ran(FRIDAY, OK)}
    for commit in (COMMIT, NEW):
        assert revision_turn(revision_night(SATURDAY, judged, runs=tried, commit=commit), "turing") is None
    # A look on fewer days by hand since is no verdict: the next night asks for one again.
    look = {"turing": revising(MIN_DAYS, at(SATURDAY, "10:00"), days=5)}
    assert revision_turn(revision_night(SUNDAY, look, runs=tried), "turing") == FRESH
    # It crashed: no report for this revision. The same code waits; a deploy asks again, after the
    # changed codes not tried yet.
    crashed = {revision_job_name("turing"): ran(FRIDAY)}
    none = {"turing": revising(MIN_DAYS), "newton": revising(MIN_DAYS)}
    tried_newton = {**crashed, revision_job_name("newton"): ran(THURSDAY, OK)}
    none["newton"] = revising(MIN_DAYS, at(THURSDAY, "23:10"))
    assert revision_turn(revision_night(SATURDAY, none, runs=crashed), "turing") is None
    assert revision_turn(revision_night(SATURDAY, none, runs=crashed, commit=NEW), "turing") == AGAIN
    assert revised_tonight(revision_night(SATURDAY, none, runs=tried_newton, commit=NEW)) == "turing"
    # A run before this revision began was an earlier revision's: this one is fresh.
    earlier = {revision_job_name("turing"): ran(CHANGED_ON - timedelta(days=3))}
    assert (
        revision_turn(revision_night(SATURDAY, {"turing": revising(MIN_DAYS)}, runs=earlier), "turing")
        == FRESH
    )
