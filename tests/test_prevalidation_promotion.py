"""The walk-forward replay as the promotion test (#681): a new bar-close algorithm judged on the
recorded days it replays, as a probation review would judge it, and made active, refused or put on
a probation judged forward the same night.

Input: recorded days of a market that swings up and down every four minutes (the pre-validation
tests' days, ``prevalidation_doubles``), a stand-in that calls them right (``Contrarian``), the
engine's database, and the budget's record of the tests before. The fewest days that pass are
lowered to three (``MIN_DAYS``), so that each run replays four whole sessions (#327). Output: the
report's ``promotion``, the status the engine's database holds and the API lists, the replay kept as
the probation's, the budget's record, and the scheduled jobs' turn.
"""

from __future__ import annotations

import fcntl
import json
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import pytest

from krx_quant import prevalidation
from krx_quant.algorithms import Algorithm
from krx_quant.feeds import KST
from krx_quant.lifecycle import FORWARD, REGISTERED, REPLAYED, Standing, Status, starts_simulation
from krx_quant.market_calendar import MarketCalendar
from krx_quant.predictions import DB_NAME, PredictionStore
from krx_quant.prevalidation import (
    LastReport,
    admits,
    attempts_path,
    budget_lock_path,
    family,
    last_report,
    prevalidate,
    promotion_level,
)
from krx_quant.scheduled import FRESH, PrevalidationFacts, prevalidation_job_name, prevalidation_tonight
from krx_quant.scheduled.core import OK, JobRecord, Night
from krx_quant.scheduled.prevalidations import REJUDGE, prevalidation_turn, to_rejudge
from krx_quant.simulation import WALK_FORWARD
from krx_quant.stats import Lift, weakest_by_day
from krx_quant.stats import Tested as LiftTested
from krx_quant.touch import TARGET_VERSION
from tests.api_schema import check
from tests.prevalidation_doubles import AFTER, ALPHA, DAYS, Contrarian, report, settings, swings
from tests.replay_events import api, at, get_json, run
from tests.test_prevalidation import listed

FOUR = DAYS[:4]  # the first predicted by its defaults (nothing before it): three scored


@pytest.fixture(autouse=True)
def three_days_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prevalidation, "MIN_DAYS", 3)


def statuses(db: Path) -> dict[str, Standing]:
    store = PredictionStore(db)
    try:
        return store.statuses()
    finally:
        store.close()


def attempts(state_dir: Path) -> list[dict[str, Any]]:
    path = attempts_path(state_dir)
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_the_level_shares_the_reviews_level_among_the_budget() -> None:
    assert promotion_level(0.983, 1) == pytest.approx(0.983)
    assert promotion_level(0.983, 10) == pytest.approx(0.9983)


async def test_a_pass_makes_a_new_algorithm_active_that_night_judged_on_days_each_trained_before(
    tmp_path: Path,
) -> None:
    # One test a family of days may hold: held to the reviews' 98.3%, as three days can pass (at the
    # default budget's 99.83%, three days' t quantile is 18).
    s = replace(settings(tmp_path, FOUR), promotion_budget=1)
    candidate = Contrarian("contrarian")
    registry: list[Algorithm] = [ALPHA, candidate]

    result = await prevalidate(s, "contrarian", algorithms=registry, now=AFTER)

    # Walk-forward: each day predicted by a model fitted on days before it only, never on it or later.
    assert candidate.fitted == [[FOUR[0]], [FOUR[0], FOUR[1]], [FOUR[1], FOUR[2]]]
    for fitted, day in zip(candidate.fitted, FOUR[1:], strict=True):
        assert max(fitted) < day, f"{day} was predicted by a model that saw it or a later day"
    assert result.passed
    saved = report(s, "contrarian")
    promotion = saved["promotion"]
    assert promotion["outcome"] == "active" and promotion["tested"]
    assert promotion["level"] == pytest.approx(promotion_level(s.review_confidence, s.promotion_budget))
    assert (promotion["attempt"], promotion["budget"], promotion["family"]) == (1, 1, [])
    assert promotion["judged_days"] == [d.isoformat() for d in FOUR[1:]], "the first is skipped"
    assert [(d["training_days"], d["recorded"], d["scored"]) for d in promotion["days"]] == [
        (0, 0, False),
        (1, 1, True),
        (2, 2, True),
        (2, 2, True),
    ], "the training-day count of each day replayed"
    assert promotion["weakest"]["low"] > 0 and promotion["enough"]
    assert {c["check"]: c["passed"] for c in saved["checks"]}["promotion"] is True

    # Active in the engine's database the same night, its replay kept as its probation's.
    db = s.state_dir / DB_NAME
    store = PredictionStore(db)
    try:
        standing = store.statuses()["contrarian"]
        assert standing.status is Status.ACTIVE and standing.reason.startswith(f"{REPLAYED} {FOUR[1]}..")
        [entered, made] = store.status_history("contrarian")
        assert (entered["status"], entered["reason"]) == ("probation", FORWARD)
        assert made["simulated"] is True
        record = store.simulations()["contrarian"]
        assert record.cadence == WALK_FORWARD and list(record.days) == FOUR[1:]
        assert record.detail["review"]["status"] == "active"
        kept = store.sim_scored(record.run or 0, FOUR[1:], None, False)
        assert {r["algorithm"] for r in kept} == {
            "contrarian",
            "baseline-flat",
            "baseline-persist",
            "baseline-random",
            "baseline-climate",
        }
        assert store.scored_predictions(FOUR, None, True) == [], "nothing in the live tables"
    finally:
        store.close()
    [attempt] = attempts(s.state_dir)
    assert (attempt["algorithm"], attempt["outcome"]) == ("contrarian", "active")
    assert attempt["days"] == promotion["judged_days"]
    last = last_report(s.state_dir, candidate)
    assert last is not None and last.promotion == "active"

    # The API lists it active (#681), and the engine runs it from its next bar close (its database on
    # the touch target already, as the server's).
    live_day = date(2026, 9, 28)
    store = PredictionStore(db)
    try:
        store.mark_target(TARGET_VERSION, AFTER)
    finally:
        store.close()
    gate = admits(s.state_dir, s.target_version)
    engine = await run(
        db,
        swings(live_day, end="09:30:00"),
        until=at("09:30:00", live_day),
        calendar=MarketCalendar({live_day: True}),
        algorithms=registry,
        kind_algorithms=(),
        admits=lambda a: a.name == "alpha" or gate(a),
    )
    assert (await listed(engine))["contrarian"] == "active"
    async with api(engine) as client:
        body = await get_json(client, "/v1/algorithms")
    check("algorithms", body)  # its replay listed as its probation's simulation (#681), in the contract
    [shown] = [a for a in body["algorithms"] if a["name"] == "contrarian"]
    assert shown["simulation"]["cadence"] == WALK_FORWARD and shown["simulation"]["review"][
        "level"
    ] == pytest.approx(0.983)
    assert engine.store.recent("005930", 10, "contrarian"), "run from the next bar close"


@dataclass(frozen=True)
class _Count:
    n: int


@dataclass(frozen=True)
class _Measured:
    """What the promotion test measures (``stats.Lifted``), set by the test: one baseline's lift by
    trading day."""

    low: float
    algorithm: str = "contrarian"
    summary: _Count = _Count(300)
    lifts: dict[str, LiftTested] = field(default_factory=dict)

    def __post_init__(self) -> None:
        by_day = Lift(3, 0.01, self.low, 0.04, 0.2)
        self.lifts["baseline-flat"] = LiftTested(Lift(300, 0.05, 0.01, 0.09, 0.01), by_day=by_day)

    @property
    def weakest_by_day(self) -> Lift | None:
        return weakest_by_day(self.lifts)


def measuring(monkeypatch: pytest.MonkeyPatch, low: float) -> None:
    monkeypatch.setattr(prevalidation, "_promotion_stats", lambda *args: _Measured(low))


async def test_a_fail_refuses_a_new_algorithm_and_enrolls_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = settings(tmp_path, FOUR)
    measuring(monkeypatch, -0.01)

    result = await prevalidate(s, "contrarian", algorithms=[ALPHA, Contrarian("contrarian")], now=AFTER)

    assert not result.passed
    assert [c.name for c in result.checks if not c.passed] == ["promotion"]
    saved = report(s, "contrarian")
    assert saved["promotion"]["outcome"] == "refused" and saved["promotion"]["tested"]
    assert "low -0.0100 at 99.83%" in saved["promotion"]["detail"]
    assert statuses(s.state_dir / DB_NAME) == {}
    assert [a["outcome"] for a in attempts(s.state_dir)] == ["refused"], "it counts against the budget"
    assert not admits(s.state_dir, s.target_version)(Contrarian("contrarian"))


async def test_the_budget_is_read_judged_and_recorded_under_one_lock_every_algorithm_shares(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #684 review: two pre-validations of other algorithms on the same days at once could each read
    # an empty family and both pass as the first test. While one judges, the budget's lock is held.
    s = settings(tmp_path, FOUR)
    held: list[bool] = []

    def judged(*args: Any) -> _Measured:
        with budget_lock_path(s.state_dir).open("a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held.append(True)
            else:
                held.append(False)
        return _Measured(0.01)

    monkeypatch.setattr(prevalidation, "_promotion_stats", judged)

    await prevalidate(s, "contrarian", algorithms=[ALPHA, Contrarian("contrarian")], now=AFTER)

    assert held == [True], "another process would wait for it"
    [attempt] = attempts(s.state_dir)
    assert attempt["outcome"] == "active", "recorded before the lock is let go"


async def test_with_the_days_budget_spent_it_is_not_tested_and_goes_on_probation_judged_forward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = settings(tmp_path, FOUR)
    measuring(monkeypatch, 0.01)  # it would pass
    path = attempts_path(s.state_dir)
    path.parent.mkdir(parents=True)
    earlier = [
        # Ten tests before on any of its days, of other algorithms and an earlier version of it.
        *(
            {
                "algorithm": f"other{i}",
                "code": "c",
                "at": "x",
                "outcome": "refused",
                "days": [FOUR[2].isoformat()],
            }
            for i in range(9)
        ),
        {
            "algorithm": "contrarian",
            "code": "old",
            "at": "x",
            "outcome": "refused",
            "days": [FOUR[3].isoformat()],
        },
        # Not on its days: not of its family.
        {"algorithm": "other", "code": "c", "at": "x", "outcome": "active", "days": ["2026-08-03"]},
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in earlier))

    result = await prevalidate(s, "contrarian", algorithms=[ALPHA, Contrarian("contrarian")], now=AFTER)

    assert result.passed, "not refused: judged forward"
    promotion = report(s, "contrarian")["promotion"]
    assert promotion["outcome"] == "forward" and not promotion["tested"] and promotion["attempt"] is None
    assert "budget is spent (10 of 10 tests judged them)" in promotion["detail"]
    assert len(promotion["family"]) == 10
    assert [(v["algorithm"], v["code"]) for v in promotion["earlier_versions"]] == [("contrarian", "old")]
    standing = statuses(s.state_dir / DB_NAME)["contrarian"]
    assert standing.status is Status.PROBATION and standing.reason.startswith(f"{FORWARD}: not tested")
    assert not starts_simulation(standing.reason, bar_close=True), "its live days judge it, not a replay"
    assert len(attempts(s.state_dir)) == len(earlier), "not tested: not counted"
    assert len(family(s.state_dir, FOUR[1:])) == 10


async def test_too_few_days_with_a_trained_model_leave_it_on_probation_judged_forward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(prevalidation, "MIN_DAYS", 4)  # the four days pass the checks; three are scored
    s = settings(tmp_path, FOUR)

    result = await prevalidate(s, "contrarian", algorithms=[ALPHA, Contrarian("contrarian")], now=AFTER)

    assert result.passed
    promotion = report(s, "contrarian")["promotion"]
    assert promotion["outcome"] == "forward"
    assert "3 days with a trained model, fewer than 4" in promotion["detail"]
    assert statuses(s.state_dir / DB_NAME)["contrarian"].status is Status.PROBATION


def on_probation(s: Any, name: str, reason: str = REGISTERED) -> datetime:
    since = datetime.combine(FOUR[-1], time(8), KST)
    store = PredictionStore(s.state_dir / DB_NAME)
    try:
        store.add_status(name, since, None, Status.PROBATION, reason)
    finally:
        store.close()
    return since


async def test_one_on_a_probation_from_before_is_judged_again_and_made_active_or_left_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = settings(tmp_path, FOUR)
    since = on_probation(s, "contrarian")
    measuring(monkeypatch, -0.01)

    failed = await prevalidate(s, "contrarian", algorithms=[ALPHA, Contrarian("contrarian")], now=AFTER)

    assert failed.passed, "not refused: it runs, on probation"
    assert report(s, "contrarian")["promotion"]["outcome"] == "forward"
    assert report(s, "contrarian")["promotion"]["on_probation"] is True
    assert statuses(s.state_dir / DB_NAME)["contrarian"] == Standing(Status.PROBATION, since)

    measuring(monkeypatch, 0.01)
    # Its checks as on a first run (a rerun's stats are held to a stricter bound, #262): the promotion
    # test is what this judges.
    monkeypatch.setattr(prevalidation, "earlier_runs", lambda *args: (0, 0))
    await prevalidate(s, "contrarian", algorithms=[ALPHA, Contrarian("contrarian")], now=AFTER)

    standing = statuses(s.state_dir / DB_NAME)["contrarian"]
    assert standing.status is Status.ACTIVE and standing.reason.startswith(REPLAYED)
    assert "test 2 of 10 on these days" in standing.reason, "the first counted against the budget"
    store = PredictionStore(s.state_dir / DB_NAME)
    try:
        assert store.simulations()["contrarian"].stint == since, "kept as the probation's it was on"
    finally:
        store.close()


async def test_one_the_engine_runs_with_another_status_is_not_judged(tmp_path: Path) -> None:
    s = settings(tmp_path, FOUR)
    on_probation(s, "contrarian", "code changed")

    result = await prevalidate(s, "contrarian", algorithms=[ALPHA, Contrarian("contrarian")], now=AFTER)

    assert result.promotion is None and "promotion" not in report(s, "contrarian")
    assert statuses(s.state_dir / DB_NAME)["contrarian"].reason == "code changed"
    assert attempts(s.state_dir) == []


# The scheduled jobs (#681): the probations begun before are judged again first.

AT = datetime.combine(date(2026, 11, 11), time(22, 30), KST)


def last(promotion: str | None = None) -> LastReport:
    return LastReport(made_at=AT, current=True, passed=True, days=20, promotion=promotion)


def test_the_probations_begun_before_with_no_promotion_test_are_judged_again() -> None:
    since = datetime.combine(date(2026, 11, 2), time(9), KST)
    standings = {
        "neyman": Standing(Status.PROBATION, since, REGISTERED),
        "ito": Standing(Status.PROBATION, since, REGISTERED),  # judged already
        "ulam": Standing(Status.PROBATION, since, "code changed"),
        "bishop": Standing(Status.ACTIVE, since, "set"),
        "turing": Standing(Status.PROBATION, since, REGISTERED),  # registered before: no job
    }
    reports = {name: last() for name in standings}
    reports["ito"] = last("forward")
    registry = [Contrarian(name) for name in ("turing", "neyman", "ito", "ulam", "bishop", "samuelson")]

    assert to_rejudge(standings, registry, reports) == ("neyman",)


def test_one_judged_again_goes_first_once_a_deploy_brings_the_test() -> None:
    order = ("mandelbrot", "neyman")
    facts = PrevalidationFacts(recorded_days=20, waiting=("mandelbrot",), rejudging=("neyman",))
    before = JobRecord(date(2026, 11, 3), OK, at("22:30:00", date(2026, 11, 3)), "old", 0, 600.0)
    night = Night(AT.date(), True, "new", None, {prevalidation_job_name("neyman"): before}, facts)

    assert prevalidation_turn(night, "neyman") == REJUDGE
    assert prevalidation_turn(night, "mandelbrot") == FRESH
    assert prevalidation_tonight(night, order) == "neyman"
    ran = JobRecord(AT.date(), OK, AT, "new", 0, 600.0)
    again = Night(AT.date(), True, "new", None, {prevalidation_job_name("neyman"): ran}, facts)
    assert prevalidation_turn(again, "neyman") is None, "it ran on this code: another deploy may"
