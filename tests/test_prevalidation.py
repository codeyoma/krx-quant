"""Pre-validation: a new algorithm replayed on the latest recorded days before it runs live.

Input: recorded days, as the collector writes them, of a market that swings up and down
every four minutes, and a registry of stand-in algorithms. Output: the report
(``state/prevalidations/<name>.json``), and what the engine lets run.
"""

from __future__ import annotations

import json
import re
import sqlite3
import tracemalloc
import weakref
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from krx_quant import engine as engine_module
from krx_quant import lifecycle, prevalidation, stats
from krx_quant.algorithms import Algorithm, Model, NothingToLearn, RegistrationError, Sample, save_model
from krx_quant.algorithms.cox5d import Cox5d
from krx_quant.baselines import TOUCH_BASELINES
from krx_quant.cli import main
from krx_quant.clock import FakeClock
from krx_quant.config import Settings
from krx_quant.consensus import fit as fit_weights
from krx_quant.engine import PredictionEngine
from krx_quant.feeds import KST
from krx_quant.footprint import give_back
from krx_quant.kind_stats import entry as kind_entry
from krx_quant.kind_stats import kind_scoring, scored_of_kind
from krx_quant.kind_stats import split as kind_split
from krx_quant.lifecycle import DIRECTION_SCORING, Standing, Status
from krx_quant.market_calendar import MarketCalendar
from krx_quant.model_shelf import Shelf
from krx_quant.predictions import DB_NAME, KIND_REVIEWED, REVIEWED, PredictionStore, read_statuses
from krx_quant.prevalidation import (
    LastReport,
    PrevalidationRunning,
    Refusal,
    _checks,
    _held,
    _stats,
    admits,
    code_digest,
    earlier_runs,
    history_path,
    last_report,
    left_out,
    prevalidate,
    refusals,
    report_path,
    seed_history,
    tried_level,
)
from krx_quant.replay import recorded_events, replay_into, replay_recorded
from krx_quant.stats import Lift
from krx_quant.swap import reload_models
from krx_quant.touch import TARGET_VERSION
from tests.algorithm_doubles import FixedAlgorithm
from tests.kind_doubles import DRIFT, TEST_KIND, FixedKindAlgorithm
from tests.lifecycle_doubles import Outbox
from tests.prevalidation_doubles import AFTER, ALPHA, DAYS, Contrarian, refuse, report, settings, swings
from tests.replay_doubles import busy_morning, stored
from tests.replay_events import FEE, api, at, get_json, run


# Ten recorded days are the fewest that pass (``MIN_DAYS``), each a whole session replayed:
# about 10 s on a laptop, and past the 30 s default (``pyproject.toml``) on a loaded CI runner
# (#327). The other tests here replay fewer.
@pytest.mark.timeout(120)
async def test_an_algorithm_that_beats_the_baselines_on_its_own_features_passes(tmp_path: Path) -> None:
    s = settings(tmp_path)
    candidate = Contrarian("contrarian")

    result = await prevalidate(s, "contrarian", algorithms=[ALPHA, candidate], now=AFTER)

    assert result.passed
    saved = report(s, "contrarian")
    assert saved["passed"] is True and saved["days"] == [d.isoformat() for d in DAYS]
    assert {c["check"]: c["passed"] for c in saved["checks"]} == {
        "days": True,
        "features": True,
        "budget": True,
        "correlation": True,
        "stats": True,
    }
    assert saved["timed_out"] == 0
    assert saved["stats"]["n"] > 200 and saved["stats"]["lift"]["low"] > 0
    # The touch target's four baselines, the default (#170).
    assert set(saved["stats"]["lift"]["vs"]) == {
        "baseline-flat",
        "baseline-persist",
        "baseline-random",
        "baseline-climate",
    }
    assert saved["target_version"] == TARGET_VERSION
    assert set(saved["correlations"]) <= {"alpha"}, "alpha never changes its mind: nothing to correlate"


async def test_an_algorithm_moving_with_another_is_refused_as_a_clone(tmp_path: Path) -> None:
    s = settings(tmp_path, DAYS[:3])
    copycat = Contrarian("copycat", own="vwap_gap")  # reads vwap_gap, calls as contrarian does

    result = await prevalidate(s, "copycat", algorithms=[ALPHA, Contrarian("contrarian"), copycat], now=AFTER)

    assert not result.passed
    checks = {c.name: c for c in result.checks}
    assert not checks["correlation"].passed and "contrarian" in checks["correlation"].detail
    assert result.correlations["contrarian"] == pytest.approx(1.0)
    assert checks["stats"].passed, "as good as the one it copies"
    assert report(s, "copycat")["passed"] is False


async def test_an_algorithm_over_its_time_budget_is_refused(tmp_path: Path) -> None:
    s = replace(settings(tmp_path, DAYS[:1]), algorithm_seconds=0.02)
    slow = FixedAlgorithm("slow", features=("vwap_z",), unique=("vwap_z",), seconds=0.05)

    result = await prevalidate(s, "slow", algorithms=[ALPHA, slow], now=AFTER)

    checks = {c.name: c for c in result.checks}
    assert not checks["budget"].passed and result.statuses["timed_out"] > 0
    assert report(s, "slow")["timed_out"] == result.statuses["timed_out"]


async def test_an_algorithm_no_better_than_a_baseline_is_refused(tmp_path: Path) -> None:
    s = settings(tmp_path, DAYS[:3])
    flat = FixedAlgorithm("flat", features=("vwap_z",), unique=("vwap_z",))  # always flat, as a baseline

    result = await prevalidate(s, "flat", algorithms=[ALPHA, flat], now=AFTER)

    checks = {c.name: c for c in result.checks}
    assert not checks["stats"].passed
    assert result.stats.lifts["baseline-flat"].lift.value == 0, "the same calls"
    assert checks["budget"].passed and checks["correlation"].passed


async def test_too_few_scored_predictions_to_judge_are_refused(tmp_path: Path) -> None:
    s = replace(settings(tmp_path, DAYS[:1]), min_sample=500)

    result = await prevalidate(s, "contrarian", algorithms=[ALPHA, Contrarian("contrarian")], now=AFTER)

    checks = {c.name: c for c in result.checks}
    assert not checks["stats"].passed and "too few to judge" in checks["stats"].detail


async def test_fewer_than_ten_recorded_days_never_pass(tmp_path: Path) -> None:
    # Three days replayed, not nine: each is a whole session (#327). Nine, one short, are judged
    # below on what the three gave: only the days are counted again.
    s = settings(tmp_path, DAYS[:3])
    candidate = Contrarian("contrarian")

    result = await prevalidate(s, "contrarian", algorithms=[ALPHA, candidate], now=AFTER)

    assert [c.name for c in result.checks if not c.passed] == ["days"]
    assert "3 recorded days (at least 10" in result.checks[0].detail
    nine = _checks(candidate, DAYS[:9], result.statuses, result.correlations, result.stats, s.min_sample)
    assert [c.name for c in nine if not c.passed] == ["days"]
    assert "9 recorded days (at least 10" in nine[0].detail


async def test_a_registry_failing_the_registration_check_is_not_replayed(tmp_path: Path) -> None:
    s = settings(tmp_path, DAYS[:1])
    twin = FixedAlgorithm("twin", features=("minute_ticks",), unique=("minute_ticks",))

    with pytest.raises(RegistrationError, match="the same features as contrarian"):
        await prevalidate(s, "twin", algorithms=[Contrarian("contrarian"), twin], now=AFTER)

    assert not (s.state_dir / "prevalidations" / "twin.json").exists()


async def test_each_day_is_predicted_by_a_model_trained_only_on_the_days_before_it(tmp_path: Path) -> None:
    s = settings(tmp_path, DAYS[:4])
    candidate = Contrarian("contrarian")
    # Thursday evening, before 21:00: Thursday's recording is not archived, so not replayed.
    thursday_evening = datetime.combine(DAYS[3], time(20), KST)

    result = await prevalidate(s, "contrarian", algorithms=[ALPHA, candidate], now=thursday_evening)

    assert result.days == DAYS[:3]
    assert candidate.fitted == [[DAYS[0]], [DAYS[0], DAYS[1]]], "two days at most: its training_days"
    versions = report(s, "contrarian")["models"]
    assert versions[DAYS[0].isoformat()] == "contrarian-default", "nothing before the first day"
    assert versions[DAYS[1].isoformat()].startswith("contrarian-20260914-")
    assert versions[DAYS[2].isoformat()].startswith("contrarian-20260915-")


# The live engine, with the gate the collector gives it.


async def listed(engine: PredictionEngine) -> dict[str, str]:
    """Each registered algorithm's state in the API: its status if it runs, ``waiting`` if not (#561)."""
    async with api(engine) as client:
        body = await get_json(client, "/v1/algorithms")
    return {a["name"]: a["state"] for a in body["algorithms"]}


async def test_a_new_algorithm_waits_for_its_prevalidation_then_starts_on_probation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The gate, not the day count: two replayed days pass here, as ten would (#327).
    monkeypatch.setattr(prevalidation, "MIN_DAYS", 2)
    s = settings(tmp_path, DAYS[:2])
    registry: list[Algorithm] = [ALPHA, Contrarian("contrarian")]
    gate = admits(s.state_dir, s.target_version)  # the engine's target, the touch target (#170)
    live_day = date(2026, 9, 28)  # the Monday after the recorded days
    events = swings(live_day, end="10:00:00")
    morning = [e for e in events if e[1]["received_at"] < at("09:20:00", live_day)]
    later = [e for e in events if e[1]["received_at"] >= at("09:20:00", live_day)]
    outbox = Outbox()

    engine = await run(
        tmp_path / "live.db",
        morning,
        until=at("09:20:00", live_day),
        calendar=MarketCalendar({live_day: True}),
        algorithms=registry,
        kind_algorithms=(),  # the direction's: the range's Mandelbrot would wait too (#181)
        admits=lambda a: a.name == "alpha" or gate(a),
        notify=outbox,
    )

    assert await listed(engine) == {"alpha": "probation", "contrarian": "waiting"}
    assert engine.waiting() == ["contrarian"]
    assert not engine.store.status_counts("contrarian"), "not run"

    assert (await prevalidate(s, "contrarian", algorithms=registry, now=AFTER)).passed
    # Trained (at night, or by hand) while it waited: it starts on that model.
    trained = Model("contrarian-20260925-abcdef", {}, None, DAYS[-1])
    save_model(s.state_dir, "contrarian", trained)
    assert reload_models(engine, Shelf(s.state_dir))
    assert isinstance(engine.clock, FakeClock)
    await replay_into(engine, engine.clock, later, until=at("15:45:00", live_day))

    assert await listed(engine) == {"alpha": "probation", "contrarian": "probation"}
    made = engine.store.recent("005930", 1000, "contrarian")
    assert made[-1]["bar_close"] == at("09:21:00", live_day).isoformat(), "from the next bar close"
    assert {p["model_version"] for p in made} == {trained.version}
    assert outbox.sent == [], "nothing waits at the review"


async def test_a_report_counts_only_for_the_code_that_passed(tmp_path: Path) -> None:
    candidate = Contrarian("contrarian")
    path = report_path(tmp_path / "state", "contrarian")
    path.parent.mkdir(parents=True)
    path.write_text("[]")  # not a report: it waits, and the engine goes on
    gate = admits(tmp_path / "state")

    engine = await run(
        tmp_path / "live.db",
        busy_morning(end="09:05:00"),
        until=at("09:05:00"),
        algorithms=[candidate],
        kind_algorithms=(),
        admits=gate,
    )
    assert engine.waiting() == ["contrarian"]
    on_target = {"passed": True, "target_version": TARGET_VERSION}
    path.write_text(json.dumps({**on_target, "code": "0123456789abcdef"}))  # passed, then the code changed
    assert isinstance(engine.clock, FakeClock)
    await replay_into(engine, engine.clock, [], until=at("09:07:00"))
    assert engine.waiting() == ["contrarian"]
    path.write_text(json.dumps({**on_target, "code": code_digest(candidate)}))
    await replay_into(engine, engine.clock, [], until=at("09:08:00"))
    assert engine.waiting() == []


async def test_a_wait_is_told_once_for_the_code_and_target_it_waits_on_across_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#508: the engine starts again every trading day (the 06:00 deploy, 07:20). The review tells a
    waiting algorithm once for the code and target it waits on (``runs``: ``prevalidation_waiting``),
    not at each start; told again on new code or a new target, and at the next review when Telegram
    did not take it. The alert says the night's scheduled jobs pre-validate it and where the result
    goes, and asks for no command."""
    candidate = Contrarian("contrarian")
    gate = admits(tmp_path / "state")  # no report: it waits
    days = [date(2026, 9, 28) + timedelta(days=i) for i in range(5)]  # Monday to Friday

    async def start(day: date, down: bool = False) -> list[str]:
        """An engine started at 07:20 of ``day``: what its review at 15:40 tells of the wait."""
        outbox = Outbox()
        outbox.down = down
        engine = await run(
            tmp_path / "live.db",
            [],
            until=at("15:45:00", day),
            start=at("07:20:00", day),
            calendar=MarketCalendar({day: True}),
            algorithms=[ALPHA, candidate],
            kind_algorithms=(),
            admits=lambda a: a.name == "alpha" or gate(a),
            notify=outbox,
        )
        assert engine.waiting() == ["contrarian"]
        engine.store.close()
        return [m for m in outbox.sent if m.startswith("사전 검증 대기")]

    [lost] = await start(days[0], down=True)
    [told] = await start(days[0] + timedelta(days=7))
    assert told == lost, "Telegram did not take it: told at the next review"
    assert told == (
        "사전 검증 대기: contrarian. 검증을 통과해야 돌기 시작합니다. 봉 마감 알고리즘은 녹화가 10거래일 "
        "이상이면 밤 학습 뒤 예약 작업이 한 밤에 하나씩(시간이 남는 밤에는 둘) 차례로 검증합니다. 결과는 "
        "#204에 댓글로 올라옵니다. 손으로 돌릴 것은 없습니다."
    )
    assert "krx-quant prevalidate" not in told
    assert await start(days[1] + timedelta(days=7)) == [], "a restart: told already"

    monkeypatch.setattr(prevalidation, "code_digest", lambda a: "0123456789abcdef")  # a new deploy
    assert await start(days[2] + timedelta(days=7)) == [told], "new code: told again"
    assert await start(days[3] + timedelta(days=7)) == []

    store = PredictionStore(tmp_path / "live.db")
    keys = [json.loads(r.detail) for r in store.runs(engine_module.WAITING_RUN, at("00:00:00", days[0]))]
    store.close()
    assert [(k["code"], k["target_version"]) for k in keys] == [
        (code_digest(candidate), TARGET_VERSION),
        ("0123456789abcdef", TARGET_VERSION),
    ]


async def test_the_engine_logs_each_admission_once(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    # #456: the line Loki shows when the gate lets a new algorithm in, once, not at each bar close.
    caplog.set_level("INFO", logger="krx_quant.engine")
    candidate = Contrarian("contrarian")
    path = report_path(tmp_path / "state", "contrarian")
    path.parent.mkdir(parents=True)
    gate = admits(tmp_path / "state")

    engine = await run(
        tmp_path / "live.db",
        busy_morning(end="09:05:00"),
        until=at("09:05:00"),
        algorithms=[ALPHA, candidate],
        kind_algorithms=(),
        admits=lambda a: a.name == "alpha" or gate(a),
    )
    assert engine.waiting() == ["contrarian"]
    path.write_text(
        json.dumps({"passed": True, "code": code_digest(candidate), "target_version": TARGET_VERSION})
    )
    assert isinstance(engine.clock, FakeClock)
    await replay_into(engine, engine.clock, [], until=at("09:09:00"))
    assert engine.waiting() == []
    assert engine.store.status_history("contrarian")[-1]["status"] == "probation"

    admitted = [r.getMessage() for r in caplog.records if "admitted on probation" in r.getMessage()]
    assert admitted == [
        f"alpha: admitted on probation through the pre-validation gate (code {code_digest(ALPHA)})",
        f"contrarian: admitted on probation through the pre-validation gate (code {code_digest(candidate)})",
    ]


async def test_an_engine_without_the_gate_logs_no_admission(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A replay runs every algorithm without a gate (#456): nothing passed, so nothing is told.
    caplog.set_level("INFO", logger="krx_quant.engine")
    await run(tmp_path / "replay.db", [], until=at("09:02:00"), algorithms=[ALPHA], kind_algorithms=())
    assert not [r for r in caplog.records if "admitted on probation" in r.getMessage()]


def test_a_report_counts_only_for_the_target_it_was_scored_on(tmp_path: Path) -> None:
    candidate = Contrarian("contrarian")
    path = report_path(tmp_path / "state", "contrarian")
    path.parent.mkdir(parents=True)
    passed = {"passed": True, "code": code_digest(candidate)}
    path.write_text(json.dumps(passed))  # before #169: the four-minute move
    assert not admits(tmp_path / "state")(candidate), "not judged on the touch target"
    path.write_text(json.dumps({**passed, "target_version": 2}))  # #170 to #187: crossed quotes dropped
    assert not admits(tmp_path / "state")(candidate), "not judged on its version 3"
    path.write_text(json.dumps({**passed, "target_version": TARGET_VERSION}))
    assert admits(tmp_path / "state")(candidate)


def test_the_scheduled_job_reads_the_latest_report_as_the_engine_does(tmp_path: Path) -> None:
    # Whether the code an algorithm runs now has been judged (#204): ``admits``'s test of the
    # code and the target, and a refusal on fewer than ten days is a look, not a verdict.
    candidate = Contrarian("contrarian")
    state = tmp_path / "state"
    assert last_report(state, candidate) is None
    path = report_path(state, "contrarian")
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert last_report(state, candidate) is None
    made = datetime(2026, 10, 23, 23, 10, tzinfo=KST)
    ten = [d.isoformat() for d in DAYS]
    refused = {
        "code": code_digest(candidate),
        "prevalidated_at": made.isoformat(),
        "passed": False,
        "days": ten,
        "target_version": TARGET_VERSION,
    }
    path.write_text(json.dumps(refused))
    judged = last_report(state, candidate)
    assert judged == LastReport(made_at=made, current=True, passed=False, days=10) and judged.judged
    path.write_text(json.dumps({**refused, "target_version": 2}))
    on_another_target = last_report(state, candidate)
    assert on_another_target is not None and not on_another_target.current and not on_another_target.judged
    path.write_text(json.dumps({**refused, "code": "0123456789abcdef"}))
    other_code = last_report(state, candidate, target_version=TARGET_VERSION)
    assert other_code is not None and not other_code.current
    path.write_text(json.dumps({**refused, "days": ten[:5]}))
    look = last_report(state, candidate, target_version=TARGET_VERSION)
    assert look is not None and look.current and not look.judged
    before_204 = {"passed": True, "code": code_digest(candidate), "target_version": TARGET_VERSION}
    path.write_text(json.dumps(before_204))
    assert last_report(state, candidate) == LastReport(made_at=None, current=True, passed=True, days=0)


def test_a_refusal_is_of_a_report_that_judged_the_code_and_target_it_runs(tmp_path: Path) -> None:
    # #310: refused, not waiting, only when a new run is not due: a pass, a report of other code or
    # target, or a look on fewer than ten days is none. Each refused report is its own key.
    candidate = Contrarian("contrarian")
    state = tmp_path / "state"
    refused_by = refusals(state, target_version=TARGET_VERSION)
    assert refused_by(candidate) is None, "no report"
    path = report_path(state, "contrarian")
    path.parent.mkdir(parents=True)
    ten = [d.isoformat() for d in DAYS]
    refused = {
        "code": code_digest(candidate),
        "prevalidated_at": datetime(2026, 10, 23, 23, 10, tzinfo=KST).isoformat(),
        "passed": False,
        "days": ten,
        "target_version": TARGET_VERSION,
        "checks": [
            {"check": "days", "passed": True, "detail": "10 recorded days"},
            {"check": "correlation", "passed": False, "detail": "highest with alpha: r = 0.950"},
            {"check": "stats", "passed": False, "detail": "weakest lift -0.0100"},
        ],
    }
    path.write_text(json.dumps(refused))
    refusal = refused_by(candidate)
    assert refusal is not None
    assert refusal.why == "correlation: highest with alpha: r = 0.950; stats: weakest lift -0.0100"
    path.write_text(json.dumps({**refused, "target_version": 2}))
    assert refused_by(candidate) is None, "on another target: a new run is due"
    path.write_text(json.dumps({**refused, "code": "0123456789abcdef"}))
    assert refused_by(candidate) is None, "of other code"
    path.write_text(json.dumps({**refused, "days": ten[:5]}))
    assert refused_by(candidate) is None, "a look"
    path.write_text(json.dumps({**refused, "passed": True}))
    assert refused_by(candidate) is None, "passed"
    path.write_text(json.dumps({**refused, "prevalidated_at": "2026-10-24T23:10:00+09:00"}))
    again = refused_by(candidate)
    assert again is not None and again.judged != refusal.judged, "a run again, told again"
    report_path(state, "newton").write_text(json.dumps(refused))
    assert refused_by(Contrarian("newton")) is None, "registered before pre-validation: it runs"


def test_left_out_is_a_refusal_of_the_code_it_has_by_an_algorithm_the_engine_does_not_run(
    tmp_path: Path,
) -> None:
    # #544: what the night does not train and a pre-validation does not replay beside its candidate.
    state = tmp_path / "state"
    since = datetime(2026, 10, 1, 9, tzinfo=KST)
    candidate, newton, daily = Contrarian("contrarian"), Contrarian("newton"), Cox5d()
    for algorithm in (candidate, newton, daily):
        refuse(state, algorithm)

    def out(statuses: dict[str, Status]) -> Callable[[Any], Refusal | None]:
        return left_out(state, {n: Standing(s, since) for n, s in statuses.items()}, TARGET_VERSION)

    assert out({})(candidate) == refusals(state, TARGET_VERSION)(candidate), "not enrolled: refused"
    assert out({"contrarian": Status.RETIRED})(candidate) is not None, "retired: as a refused revision"
    for running in (Status.PROBATION, Status.ACTIVE, Status.BENCH, Status.CLONE):
        assert out({"contrarian": running})(candidate) is None, f"{running.value}: the engine runs it"
    assert out({})(newton) is None, "registered before pre-validation: it runs without a report"
    assert out({"newton": Status.RETIRED})(newton) is not None, "retired by its revision's refusal"
    assert out({"cox-5d": Status.PROBATION})(daily) is not None, "a daily one on probation waits for a pass"
    assert out({"cox-5d": Status.ACTIVE})(daily) is None, "judged live already: it runs"
    assert left_out(state, {}, 1)(candidate) is None, "of another target: a new run is due"
    refuse(state, candidate, code="0123456789abcdef")  # its code changed since
    assert out({})(candidate) is None, "of other code: it trains again and waits for a new run"


def test_the_statuses_are_read_beside_the_engine_without_writing(tmp_path: Path) -> None:
    # #544: the night's training and a pre-validation read them as the engine keeps them.
    db = tmp_path / "state" / DB_NAME
    assert read_statuses(db) == {}, "no engine DB: none"
    store = PredictionStore(db)
    store.add_status("contrarian", at("09:00:00"), None, Status.PROBATION, "registered")
    store.add_status("contrarian", at("15:40:00"), Status.PROBATION, Status.RETIRED, "refused")
    assert read_statuses(db) == store.statuses(), "beside the engine, while it writes"
    store.close()
    assert read_statuses(db)["contrarian"].status is Status.RETIRED
    assert not db.with_name(db.name + "-wal").exists(), "nothing written"


# Two pre-validations of three recorded days each, about 6 s each on a laptop: past the 30 s default
# (``pyproject.toml``) on a loaded CI runner (#327).
@pytest.mark.timeout(120)
async def test_a_refused_algorithm_the_engine_does_not_run_is_not_replayed_beside_a_candidate(
    tmp_path: Path,
) -> None:
    # #544: copycat calls as contrarian does, a clone of it (above). contrarian's report refused the
    # code it has, and the engine retired it: copycat is replayed without it, and so is not refused as
    # its clone. A run of contrarian again by hand replays it all the same, beside copycat.
    s = settings(tmp_path, DAYS[:3])
    contrarian, copycat = Contrarian("contrarian"), Contrarian("copycat", own="vwap_gap")
    registry: list[Algorithm] = [ALPHA, contrarian, copycat]
    refuse(s.state_dir, contrarian)
    store = PredictionStore(s.state_dir / DB_NAME)
    store.add_status("contrarian", at("15:40:00", DAYS[0]), None, Status.RETIRED, "refused")
    store.close()

    result = await prevalidate(s, "copycat", algorithms=registry, now=AFTER)

    assert {c.name: c.passed for c in result.checks}["correlation"], "no contrarian to copy"
    assert "contrarian" not in result.correlations
    replayed = predictors(s, "copycat")
    assert {"alpha", "copycat"} <= replayed and "contrarian" not in replayed, "neither loaded nor run"
    again = await prevalidate(s, "contrarian", algorithms=registry, now=AFTER)
    assert "contrarian" in predictors(s, "contrarian"), "the candidate runs whatever its report says"
    assert again.correlations["copycat"] == pytest.approx(1.0), "beside the algorithms the engine runs"


def predictors(s: Settings, name: str) -> set[str]:
    """What predicted in ``name``'s pre-validation: the algorithms replayed among them."""
    with sqlite3.connect(s.state_dir / f"prevalidation-{name}.sqlite3") as db:
        return {r[0] for r in db.execute("SELECT DISTINCT algorithm FROM predictions")}


async def test_an_algorithm_waiting_is_alerted_once_after_the_engine_starts(tmp_path: Path) -> None:
    thursday, friday, monday = date(2026, 9, 17), date(2026, 9, 18), date(2026, 9, 21)
    outbox = Outbox()
    options: dict[str, Any] = {
        "calendar": MarketCalendar({thursday: True, friday: True, monday: True}),
        "algorithms": [ALPHA, Contrarian("contrarian")],
        "admits": lambda a: a.name == "alpha",
        "notify": outbox,
    }
    events = [*swings(thursday, end="09:30:00"), *swings(friday, end="09:30:00")]

    await run(
        tmp_path / "live.db", events, until=at("15:45:00", friday), start=at("08:00:00", thursday), **options
    )

    [alert] = outbox.sent  # at Thursday's review; not again at Friday's
    assert "contrarian" in alert and "krx-quant prevalidate" not in alert, "the night's jobs run it (#508)"
    await run(tmp_path / "live.db", [], until=at("15:45:00", monday), start=at("08:00:00", monday), **options)
    assert outbox.sent == [alert], "a restarted engine does not alert again on the same code (#508)"


async def test_the_algorithms_registered_before_and_those_enrolled_run_without_a_report(
    tmp_path: Path,
) -> None:
    gate = admits(tmp_path / "state")  # no report at all
    events = busy_morning(end="09:05:00")

    first_five = await run(tmp_path / "five.db", events, until=at("09:05:00"), admits=gate)
    enrolled_db = tmp_path / "enrolled.db"
    store = PredictionStore(enrolled_db)
    # Enrolled by an earlier engine.
    store.add_status("contrarian", at("08:00:00"), None, Status.PROBATION, "registered")
    store.close()
    enrolled = await run(
        enrolled_db, events, until=at("09:05:00"), algorithms=[ALPHA, Contrarian("contrarian")], admits=gate
    )

    states = await listed(first_five)
    assert {name for name, state in states.items() if state != "waiting"} == {
        "newton",
        "gauss",
        "granger",
        "markov",
        "turing",
    }
    enrolled_states = await listed(enrolled)
    assert (enrolled_states["alpha"], enrolled_states["contrarian"]) == ("waiting", "probation"), (
        "alpha is new: it waits"
    )
    assert {s for n, s in enrolled_states.items() if n not in {"alpha", "contrarian"}} == {"waiting"}, (
        "the other kinds' new ones too"
    )


def test_the_command_reports_and_never_touches_the_live_engine_s_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    s = settings(tmp_path, DAYS[:2])
    s.state_dir.mkdir()
    live = s.state_dir / DB_NAME
    store = PredictionStore(live)
    store.add_status("newton", at("08:00:00"), None, Status.ACTIVE, "by hand")
    store.close()
    before = live.read_bytes()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_DATA_DIR", str(s.data_dir))
    monkeypatch.setenv("KRX_STATE_DIR", str(s.state_dir))
    monkeypatch.setenv("KRX_SYMBOLS", "005930")

    code = main(["prevalidate", "newton", "--days", "1"])

    out = capsys.readouterr().out
    assert code in (0, 1) and out.startswith("newton: ")
    assert "(1 recorded days, 2026-09-15..2026-09-15)" in out
    assert all(f"  {check}: " in out for check in ("features", "budget", "correlation", "stats"))
    assert "registered before pre-validation" in out
    assert live.read_bytes() == before, "the live engine's database is never written"
    assert (s.state_dir / "prevalidation-newton.sqlite3").exists()
    assert report(s, "newton")["days"] == ["2026-09-15"]
    # #262: every run is kept, and a run on days replayed before says so.
    assert report(s, "newton")["earlier_runs"] == 0
    assert "run before" not in out
    main(["prevalidate", "newton", "--days", "1"])
    again = capsys.readouterr().out
    assert "run before: 1, 1 of them on any of these days" in again
    assert (report(s, "newton")["earlier_runs"], report(s, "newton")["earlier_runs_on_these_days"]) == (1, 1)
    assert len(history_path(s.state_dir, "newton").read_text().splitlines()) == 2


def test_earlier_runs_count_those_on_any_of_the_days(tmp_path: Path) -> None:
    path = history_path(tmp_path, "x")
    path.parent.mkdir(parents=True)
    lines = [
        {"days": ["2026-09-14", "2026-09-15"]},
        {"days": ["2026-09-16"]},
        "not json",
        {"days": ["2026-09-15", "2026-09-16"]},
    ]
    path.write_text("\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines) + "\n")
    assert earlier_runs(tmp_path, "x", [date(2026, 9, 15)]) == (3, 2)
    assert earlier_runs(tmp_path, "x", [date(2026, 9, 17)]) == (3, 0)
    assert earlier_runs(tmp_path, "y", [date(2026, 9, 15)]) == (0, 0)


@dataclass(frozen=True)
class _Counted:
    n: int


@dataclass(frozen=True)
class _Judged:
    """A candidate's stats as the stats check reads them (``stats.Lifted``)."""

    weakest_lift: float | None
    day_low: float | None = None
    algorithm: str = "x"
    summary: _Counted = _Counted(300)
    lifts: dict[str, stats.Tested] = field(
        default_factory=lambda: {"baseline-flat": stats.Tested(Lift(300, 0.05, 0.01, 0.09, 0.01))}
    )
    lift_low: float | None = None
    weakest_by_day: Lift | None = None


def test_a_rerun_on_the_same_days_is_held_to_a_bonferroni_bound_by_trading_day() -> None:
    # #262: the N-th run on any of these days needs the weakest lift by trading day's lower
    # bound above 0 at 1 − 0.05/N; the first needs the weakest lift above 0, as before.
    assert [tried_level(n) for n in (1, 2, 4)] == [
        pytest.approx(0.95),
        pytest.approx(0.975),
        pytest.approx(0.9875),
    ]
    first = _stats(_Judged(0.05), 20)
    assert first.passed and "run" not in first.detail
    second = _stats(_Judged(0.05), 20, tries=2, strict_low=0.004)
    assert second.passed
    assert "run 2 on these days, held to 97.50% (Bonferroni)" in second.detail
    assert "weakest lower bound by trading day 0.0040" in second.detail
    assert not _stats(_Judged(0.05), 20, tries=2, strict_low=-0.002).passed
    assert not _stats(_Judged(-0.01), 20, tries=2, strict_low=0.004).passed  # the lift itself still counts
    unpaired = _stats(_Judged(0.05), 20, tries=3, strict_low=None)
    assert not unpaired.passed and "fewer than two days paired" in unpaired.detail
    assert "too few to judge" in _stats(_Judged(0.05), 500, tries=2, strict_low=0.004).detail


async def test_a_rerun_records_its_stats_and_is_judged_by_trading_day(tmp_path: Path) -> None:
    s = settings(tmp_path, DAYS[:3])  # judged by trading day on three as on ten (#327)
    history = history_path(s.state_dir, "contrarian")
    history.parent.mkdir(parents=True)
    # A run before on two of these days: this one is the second on them.
    history.write_text(
        json.dumps({"days": [DAYS[0].isoformat(), DAYS[1].isoformat()], "passed": False}) + "\n"
    )

    result = await prevalidate(s, "contrarian", algorithms=[ALPHA, Contrarian("contrarian")], now=AFTER)

    check = next(c for c in result.checks if c.name == "stats")
    assert "run 2 on these days, held to 97.50% (Bonferroni)" in check.detail
    bound = float(check.detail.rsplit("weakest lower bound by trading day ", 1)[1].split(" ")[0])
    assert check.passed == (bound > 0 and (result.stats.weakest_lift or 0) > 0)
    assert bound <= (result.stats.day_low or 0), "a wider interval: a lower bound no higher than at 95%"
    last = json.loads(history.read_text().splitlines()[-1])
    assert last["weakest_lift"] == pytest.approx(result.stats.weakest_lift)
    assert last["day_low"] == pytest.approx(result.stats.day_low)


def test_a_report_from_before_the_history_counts_as_a_run(tmp_path: Path) -> None:
    report = report_path(tmp_path, "z")
    report.parent.mkdir(parents=True)
    earlier = {"prevalidated_at": "2026-10-10T22:30:00+09:00", "code": "abc", "passed": False}
    report.write_text(json.dumps({**earlier, "days": ["2026-09-15"]}))
    seed_history(tmp_path, "z")
    assert earlier_runs(tmp_path, "z", [date(2026, 9, 15)]) == (1, 1)
    seed_history(tmp_path, "z")  # once only: the history exists now
    assert earlier_runs(tmp_path, "z", [date(2026, 9, 15)]) == (1, 1)


def test_the_command_reports_a_registry_that_fails_the_registration_check_as_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def refused(*args: Any, **kwargs: Any) -> None:
        raise RegistrationError("twin: the same features as contrarian")

    monkeypatch.setattr("krx_quant.prevalidation.prevalidate", refused)  # imported as the command runs
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))

    assert main(["prevalidate", "twin"]) == 1
    assert "twin: the same features as contrarian" in capsys.readouterr().err


async def test_it_trains_on_samples_that_say_their_day_was_recorded_as_the_nightly_training_does(
    tmp_path: Path,
) -> None:
    class Tracing(Contrarian):  # what a fold-cutting algorithm (Neyman) reads: which days were recorded
        references: list[set[str | None]] = []

        def fit(self, samples: Sequence[Sample]) -> Model:
            self.references.append({s.reference for s in samples})
            return super().fit(samples)

    s = settings(tmp_path, DAYS[:3])
    candidate = Tracing("tracing")

    result = await prevalidate(s, "tracing", algorithms=[ALPHA, candidate], now=AFTER)

    assert result.days == DAYS[:3]
    assert candidate.references == [{"mid"}, {"mid"}], "recorded days, as the nightly training says"
    model = report(s, "tracing")["models"][DAYS[2].isoformat()]
    assert model.startswith("tracing-20260915-"), "versions do not read the reference"


# A replayed day's memory line (#383): the resident memory it starts with, after its training and
# after its replay, each with its peak; ``?`` where a figure cannot be read (macOS has no VmRSS).
_MB = r"(\d+\.\d|\?)"
MEMORY_LINE = (
    rf"{_MB} MB at the start; training {_MB} MB \(peak {_MB}\); replay {_MB} MB \(peak {_MB}\); "
    r"peaks since (each phase began|the process started)"
)


def _verdict(result: prevalidation.Prevalidation) -> dict[str, Any]:
    """The report as saved, without when it was made and where its database is."""
    saved = result.as_dict()
    for key in ("prevalidated_at", "db"):
        saved.pop(key)
    return saved


# Two pre-validations of three days, each a whole session: about 16 s on a laptop, past the 30 s
# default (``pyproject.toml``) on a loaded CI runner (#327).
@pytest.mark.timeout(120)
async def test_each_replayed_day_logs_its_memory_and_lets_its_engine_go_before_the_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Input: three recorded days. Output: a memory line a replayed day (#383) and one for the
    verdict (#546), and each day's engine gone, and the memory it freed handed back, before the next
    day is trained; the same report as when the engines were kept, as before #383."""
    engines: list[weakref.ref[PredictionEngine]] = []
    alive_at_fits: list[int] = []
    given_back: list[int] = []

    async def kept(*args: Any, **kwargs: Any) -> PredictionEngine:
        engine = await replay_recorded(*args, **kwargs)
        engines.append(weakref.ref(engine))
        return engine

    class Looking(Contrarian):  # counts the replays' engines still alive when it is trained
        def fit(self, samples: Sequence[Sample]) -> Model:
            alive_at_fits.append(sum(e() is not None for e in engines))
            return super().fit(samples)

    monkeypatch.setattr(prevalidation, "replay_recorded", kept)

    def counted() -> None:
        given_back.append(len(engines))
        give_back()

    monkeypatch.setattr(prevalidation, "give_back", counted)
    caplog.set_level("INFO", logger="krx_quant.prevalidation")
    s = settings(tmp_path / "now", DAYS[:3])

    result = await prevalidate(s, "looking", algorithms=[ALPHA, Looking("looking")], now=AFTER)

    assert alive_at_fits == [0, 0], "trained on days 2 and 3 with no replay's engine left"
    assert given_back == [1, 2, 3], "after each day's replay"
    lines = [r.getMessage() for r in caplog.records if ": memory on " in r.getMessage()]
    assert [line.split(": ")[1] for line in lines] == [
        f"memory on {d} (day {i} of 3)" for i, d in enumerate(DAYS[:3], 1)
    ]
    assert all(re.fullmatch(rf"looking: memory on \S+ \(day \d of 3\): {MEMORY_LINE}", x) for x in lines)
    # And the verdict's, which reads every replayed day at once (#546).
    [verdict] = [r.getMessage() for r in caplog.records if ": memory of the verdict " in r.getMessage()]
    assert re.fullmatch(
        rf"looking: memory of the verdict on 3 days: {_MB} MB at the start; verdict {_MB} MB \(peak {_MB}\); "
        r"peaks since (each phase began|the process started)",
        verdict,
    )

    # As before #383: each day's engine kept until the next day's replay ended, nothing handed back.
    held: list[PredictionEngine] = []

    async def as_before(settings: Settings, day: date, leaders: Any, **options: Any) -> None:
        events = recorded_events(settings.data_dir, day, settings.symbols, leaders)
        engine = await kept(settings, day, events, **options)
        engine.store.close()
        held[:] = [engine]

    monkeypatch.setattr(prevalidation, "give_back", lambda: None)
    monkeypatch.setattr(prevalidation, "_replay_day", as_before)
    engines.clear()
    alive_at_fits.clear()
    before = await prevalidate(
        settings(tmp_path / "before", DAYS[:3]), "looking", algorithms=[ALPHA, Looking("looking")], now=AFTER
    )
    assert alive_at_fits == [1, 1], "the previous day's engine was still alive"
    assert _verdict(before) == _verdict(result), "the same replay, the same verdict"


def read_as_before_487(monkeypatch: pytest.MonkeyPatch) -> None:
    """The status reviews and the consensus refit read as before #487: every predictor's whole
    rows (``scored_predictions``, ``kind_scored``), the refit's too."""

    def whole(store: PredictionStore, days: Sequence[date], special: bool, algorithms: Any = None) -> Any:
        return store.scored_predictions(days, None, special)

    def whole_of_kind(
        store: PredictionStore, kind: str, version: int, days: Sequence[date], special: bool
    ) -> Any:
        return store.kind_scored(kind, version, days, None, special)

    monkeypatch.setattr(PredictionStore, "reviewed", whole)
    monkeypatch.setattr(PredictionStore, "kind_reviewed", whole_of_kind)


def replayed_rows(db: Path) -> dict[str, list[tuple[Any, ...]]]:
    """What a replay stored that its reviews and consensus decide (#487): every prediction with its
    outcome (the consensus's too), every kind's, the status changes and the consensus weights."""
    conn = sqlite3.connect(db)
    try:
        return {
            "predictions": stored(db),
            "kind_predictions": conn.execute(
                "SELECT kind, symbol, algorithm, model_version, bar_close, displayed_at, status, output"
                " FROM kind_predictions ORDER BY kind, bar_close, symbol, algorithm"
            ).fetchall(),
            "statuses": conn.execute(
                "SELECT algorithm, at, previous, status, reason FROM statuses ORDER BY id"
            ).fetchall(),
            "weights": conn.execute(
                "SELECT * FROM consensus_weights ORDER BY version, symbol, slot, algorithm"
            ).fetchall(),
        }
    finally:
        conn.close()


async def test_a_review_reads_the_fields_it_judges_by_in_the_order_stored_and_judges_the_same(
    tmp_path: Path,
) -> None:
    """Input: two replayed days of the direction and of the drift. Output (#487): what a status
    review reads (``reviewed``, ``kind_reviewed``) is each whole row's fields the review reads, in the
    same order, each string repeated across rows kept once, in a fraction of the memory; and the
    stats it judges each algorithm by are those of the whole rows."""
    s = settings(tmp_path, DAYS[:2])
    steady = FixedKindAlgorithm("steady", features=("vwap_gap",), unique=("vwap_gap",))
    result = await prevalidate(
        s,
        "steady",
        algorithms=[ALPHA, Contrarian("contrarian")],
        kind_algorithms=[steady],
        kinds=(TEST_KIND,),
        now=AFTER,
    )
    store = PredictionStore(result.db or Path())
    try:
        days = store.prediction_days(DAYS[1], 10)
        touch = {b.name for b in TOUCH_BASELINES}
        drift = {b.name for b in TEST_KIND.baselines}
        for special in (True, False):
            whole, read = _traced(partial(store.scored_predictions, days, None, special))
            slim, held = _traced(partial(store.reviewed, days, special))
            assert slim == [{k: r[k] for k in REVIEWED} for r in whole] and len(slim) > 1000
            assert held < read / 4, f"{held / len(slim):.0f} B a row, {read / len(whole):.0f} B a whole one"
            assert len({id(r["touch_path"]) for r in slim}) == len({r["touch_path"] for r in slim})
            assert store.reviewed(days, special, ["alpha"]) == [r for r in slim if r["algorithm"] == "alpha"]
            assert store.reviewed(days, special, []) == []
            for name in {r["algorithm"] for r in whole} - touch:
                assert DIRECTION_SCORING.measure(name, slim, touch, FEE, 0.95) == _whole_measure(
                    name, whole, touch
                )

            whole = store.kind_scored(DRIFT, TEST_KIND.answer_version, days, None, special)
            slim = store.kind_reviewed(DRIFT, TEST_KIND.answer_version, days, special)
            assert slim == [{k: r[k] for k in KIND_REVIEWED} for r in whole] and len(slim) > 100
            assert len({id(r["answer_path"]) for r in slim}) == len(
                {json.dumps(r["answer_path"]) for r in slim}
            )
            scoring = kind_scoring(TEST_KIND)
            for name in {r["algorithm"] for r in whole} - drift:
                as_before = kind_split(scored_of_kind(whole, TEST_KIND), drift)
                expected = kind_entry(name, as_before[0].get(name, []), as_before[1], TEST_KIND, 0.95)
                assert scoring.measure(name, slim, drift, FEE, 0.95) == expected
    finally:
        store.close()


def _traced(read: Callable[[], list[dict[str, Any]]]) -> tuple[list[dict[str, Any]], int]:
    """What ``read`` returns, and the memory it holds (tracemalloc)."""
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        rows = read()
        return rows, tracemalloc.get_traced_memory()[0] - before
    finally:
        tracemalloc.stop()


def _whole_measure(name: str, rows: list[dict[str, Any]], baselines: set[str]) -> stats.Lifted:
    """The direction's stats of ``name`` as the review took them before #487: from every
    predictor's whole rows."""
    algorithms, scored = stats.split(stats.scored_by_algorithm(rows), baselines)
    return stats.entry(name, algorithms.get(name, []), scored, FEE, 0.95)


# Two pre-validations of five days, each a whole session, traced (tracemalloc): about 50 s on a
# laptop, past the 30 s default (``pyproject.toml``) on a loaded CI runner (#327).
@pytest.mark.timeout(240)
async def test_a_replayed_day_s_review_holds_a_fraction_of_what_it_did_and_decides_the_same(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Input: five recorded days, probation judged from its second day on, so that the candidate is
    made active on the fourth and the consensus has a member on the fifth. Output (#487): the end of
    each replayed day (the review, which judges the days before it too, and the consensus refit)
    holds the fields it reads of each prediction, and the refit its members' only (none before the
    first): what it holds grows by under a third of what it did a replayed day, when it read every
    predictor's whole rows. The same report, predictions (the consensus's too), status changes and
    weights as then."""

    async def traced(
        root: Path,
    ) -> tuple[prevalidation.Prevalidation, list[int], list[tuple[set[str], set[str]]]]:
        """The run's report, each replayed day's end's peak above what it started with, and the
        algorithms of the rows each refit read with its members."""
        peaks: list[int] = []
        refits: list[tuple[set[str], set[str]]] = []
        review = PredictionEngine._review

        async def measured(self: PredictionEngine, at: datetime) -> None:
            tracemalloc.start()  # what it allocates, at most at once
            try:
                await review(self, at)
                peaks.append(tracemalloc.get_traced_memory()[1])
            finally:
                tracemalloc.stop()

        def fitted(rows: Sequence[dict[str, Any]], members: Sequence[str], *args: Any) -> Any:
            refits.append(({r["algorithm"] for r in rows}, set(members)))
            return fit_weights(rows, members, *args)

        monkeypatch.setattr(PredictionEngine, "_review", measured)
        monkeypatch.setattr(engine_module, "fit_weights", fitted)
        s = replace(settings(root, DAYS[:5]), probation_review_days=(2, 3, 4))
        algorithms: list[Algorithm] = [ALPHA, Contrarian("contrarian"), Contrarian("looking", own="vwap_gap")]
        try:
            result = await prevalidate(s, "looking", algorithms=algorithms, now=AFTER)
        finally:
            monkeypatch.setattr(PredictionEngine, "_review", review)
            monkeypatch.setattr(engine_module, "fit_weights", fit_weights)
        return result, peaks, refits

    now, peaks, refits = await traced(tmp_path / "now")
    read_as_before_487(monkeypatch)
    before, peaks_before, refits_before = await traced(tmp_path / "before")

    assert [members for _, members in refits] == [set()] * 3 + [{"looking"}] * 2, "active on the fourth day"
    assert [read for read, _ in refits] == [set()] * 3 + [{"looking"}] * 2, "its members' rows only"
    assert refits_before[0][0] >= {"alpha", "contrarian", "looking", "baseline-flat"}, "before: every one's"
    grown, grown_before = peaks[-1] - peaks[0], peaks_before[-1] - peaks_before[0]
    assert grown < grown_before / 3, (
        f"{grown / 2**20:.2f} MB over four days, {grown_before / 2**20:.2f} MB before"
    )
    assert _verdict(now) == _verdict(before)
    assert replayed_rows(now.db or Path()) == replayed_rows(before.db or Path())


class ReadAsBefore546:
    """A review's reads before #546 (``lifecycle._Read``): each set of days read apart, every
    predictor's rows with special ones and without, each kept until the review ended."""

    of = None  # the stats read every predictor's rows

    def __init__(self, rows: Any, baselines: Any, target_version: int) -> None:
        self._rows, self._baselines, self._target = rows, baselines, target_version
        self._read: dict[tuple[tuple[date, ...], bool], list[dict[str, Any]]] = {}

    def __call__(self, chosen: Sequence[date], special: bool) -> list[dict[str, Any]]:
        key = (tuple(chosen), special)
        if key not in self._read:
            self._read[key] = stats.on_target(self._rows(chosen, special), self._baselines, self._target)
        return self._read[key]


def read_as_before_546(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reviews, their clone check and the verdict read as before #546: each set of days apart
    (``ReadAsBefore546``), the clone check every predictor's leans from the rows of all its days at
    once, the verdict every predictor's whole rows."""

    def correlated(judge: Any, name: str, others: Sequence[str], days: Sequence[date]) -> dict[str, float]:
        every = lifecycle.leans(judge.rows(days, True), judge.scoring.lean)
        return lifecycle.correlated(name, others, [every])

    def whole(verdict: Any) -> Any:
        def judged(*args: Any, **kwargs: Any) -> Any:
            with monkeypatch.context() as m:
                read_as_before_487(m)
                return verdict(*args, **kwargs)

        return judged

    monkeypatch.setattr(lifecycle, "_Read", ReadAsBefore546)
    monkeypatch.setattr(lifecycle._Judge, "correlated", correlated)
    monkeypatch.setattr(prevalidation, "_judge", whole(prevalidation._judge))
    monkeypatch.setattr(prevalidation, "_judge_kind", whole(prevalidation._judge_kind))


def on_target_models(s: Settings, algorithms: Sequence[Algorithm]) -> None:
    """The models of the target the nights would have saved for ``algorithms`` (their defaults,
    marked so): the reviews of a replay count them from its first day, as the server's are."""
    for a in algorithms:
        model = replace(a.default_model(), version=f"{a.name}-night", target_version=TARGET_VERSION)
        save_model(s.state_dir, a.name, model)


# Two pre-validations of six days, each a whole session, traced (tracemalloc): about 60 s on a
# laptop, past the 30 s default (``pyproject.toml``) on a loaded CI runner (#327).
@pytest.mark.timeout(240)
async def test_a_replayed_day_s_review_holds_about_as_much_whatever_the_days_before_it_and_decides_the_same(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Input: six recorded days, the running algorithms on models of the target (counted from the
    first day, as the server's are; the candidate from its second, and a clone of one of them), probation
    judged on days 2 to 4. Output (#546): what the end of a replayed day holds at most grows by under a
    fifth of what it did over the days, when each set of days the review asked for kept every
    predictor's rows (the clone check's with special predictions too); the verdict holds under half.
    The same report, predictions (the consensus's too), status changes and weights as then."""

    async def traced(root: Path) -> tuple[prevalidation.Prevalidation, list[int], list[int]]:
        """The run's report, each replayed day's end's peak, and the verdict's."""
        peaks: list[int] = []
        verdicts: list[int] = []
        review, judge = PredictionEngine._review, prevalidation._judge

        async def measured(self: PredictionEngine, at: datetime) -> None:
            tracemalloc.start()  # what it allocates, at most at once
            try:
                await review(self, at)
                peaks.append(tracemalloc.get_traced_memory()[1])
            finally:
                tracemalloc.stop()

        def judged(*args: Any, **kwargs: Any) -> prevalidation.Prevalidation:
            tracemalloc.start()
            try:
                result = judge(*args, **kwargs)
                verdicts.append(tracemalloc.get_traced_memory()[1])
                return result
            finally:
                tracemalloc.stop()

        monkeypatch.setattr(PredictionEngine, "_review", measured)
        monkeypatch.setattr(prevalidation, "_judge", judged)
        s = replace(settings(root, DAYS[:6]), probation_review_days=(2, 3, 4))
        running: list[Algorithm] = [
            ALPHA,
            Contrarian("contrarian"),
            Contrarian("slow", own="depth_imbalance", lag=6),
        ]
        on_target_models(s, running)
        try:
            result = await prevalidate(
                s, "looking", algorithms=[*running, Contrarian("looking", own="vwap_gap")], now=AFTER
            )
        finally:
            monkeypatch.setattr(PredictionEngine, "_review", review)
            monkeypatch.setattr(prevalidation, "_judge", judge)
        return result, peaks, verdicts

    now, peaks, verdict = await traced(tmp_path / "now")
    read_as_before_546(monkeypatch)
    before, peaks_before, verdict_before = await traced(tmp_path / "before")

    conn = sqlite3.connect(now.db or Path())
    try:
        candidates = {
            r[0] for r in conn.execute("SELECT algorithm FROM runs WHERE kind = 'retire_candidate'")
        }
        changed = conn.execute(
            "SELECT algorithm, previous, status FROM statuses WHERE previous IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    assert candidates == {"alpha", "contrarian", "slow"}, "the running ones counted from the first day"
    assert ("looking", "probation", "clone") in changed, "the clone check read the running ones' leans"
    grown, grown_before = peaks[-1] - peaks[0], peaks_before[-1] - peaks_before[0]
    assert grown < grown_before / 5, (
        f"{grown / 2**20:.2f} MB over five days, {grown_before / 2**20:.2f} MB before"
    )
    assert verdict[0] < verdict_before[0] / 2, (
        f"{verdict[0] / 2**20:.2f} MB, {verdict_before[0] / 2**20:.2f} before"
    )
    assert _verdict(now) == _verdict(before)
    assert replayed_rows(now.db or Path()) == replayed_rows(before.db or Path())


async def test_the_stats_rows_of_some_predictors_a_day_at_a_time_are_those_of_one_read_in_its_order(
    tmp_path: Path,
) -> None:
    """Input: a replay's database of three days. Output (#546): a review's rows of some predictors
    (``_Read.of``), read a day at a time, are those of one read of every day, in its order; with
    special predictions, every read is the store's."""
    s = settings(tmp_path, DAYS[:3])
    running: list[Algorithm] = [ALPHA, Contrarian("contrarian")]
    on_target_models(s, running)
    result = await prevalidate(
        s, "looking", algorithms=[*running, Contrarian("looking", own="vwap_gap")], now=AFTER
    )
    store = PredictionStore(result.db or Path())
    try:
        days = store.prediction_days(DAYS[2], 10)
        baselines = {b.name for b in TOUCH_BASELINES}
        read = lifecycle._Read(store.reviewed, baselines, TARGET_VERSION)
        every = stats.on_target(store.reviewed(days, False), baselines, TARGET_VERSION)
        for predictors in ({"alpha"}, {"looking", *baselines}, {"contrarian", "alpha", *baselines}):
            for chosen in (days, days[:2], days[1:]):
                kept = {d.isoformat() for d in chosen}
                expected = [r for r in every if r["algorithm"] in predictors and r["bar_close"][:10] in kept]
                assert read.of(chosen, predictors) == expected and len(expected) > 100
        assert read(days, False) == every
        assert read(days[1:], True) == stats.on_target(
            store.reviewed(days[1:], True), baselines, TARGET_VERSION
        )
    finally:
        store.close()


async def test_a_day_with_nothing_the_algorithm_can_learn_from_is_predicted_by_its_defaults(
    tmp_path: Path,
) -> None:
    class Picky(Contrarian):  # learns from nothing it is given (a book algorithm on backfilled days)
        def fit(self, samples: Sequence[Sample]) -> Model:
            raise NothingToLearn("no training samples with the book")

    s = settings(tmp_path, DAYS[:2])

    result = await prevalidate(s, "picky", algorithms=[ALPHA, Picky("picky")], now=AFTER)

    assert set(result.models.values()) == {"picky-default"}


async def test_any_other_error_in_training_fails_the_pre_validation(tmp_path: Path) -> None:
    class Broken(Contrarian):  # a defect, not a gap in the data
        def fit(self, samples: Sequence[Sample]) -> Model:
            raise ValueError("math domain error")

    s = settings(tmp_path, DAYS[:2])

    with pytest.raises(ValueError, match="math domain error"):
        await prevalidate(s, "broken", algorithms=[ALPHA, Broken("broken")], now=AFTER)

    assert not report_path(s.state_dir, "broken").exists(), "no report: the engine never starts it"


async def test_with_the_touch_target_it_trains_replays_and_is_judged_on_the_touch_label(
    tmp_path: Path,
) -> None:
    class Watched(Contrarian):  # keeps what it was trained on
        def fit(self, samples: Sequence[Sample]) -> Model:
            self.samples = list(samples)
            return super().fit(samples)

    s = replace(settings(tmp_path, DAYS[:3]), target_version=TARGET_VERSION)
    candidate = Watched("contrarian")

    result = await prevalidate(s, "contrarian", algorithms=[ALPHA, candidate], now=AFTER)

    # Trained on the touch target: its samples carry the terms of its forecast.
    assert candidate.samples and all(x.features.touch is not None for x in candidate.samples)
    assert set(result.stats.lifts) == {
        "baseline-flat",
        "baseline-persist",
        "baseline-random",
        "baseline-climate",
    }
    assert result.db is not None
    with sqlite3.connect(result.db) as db:
        # Judged on the touch label: every prediction of it with a label and basis A's mid-points.
        [(labelled,)] = db.execute(
            "SELECT COUNT(*) FROM predictions p JOIN outcomes o ON o.prediction_id = p.id"
            " JOIN touches t ON t.symbol = p.symbol AND t.bar_close = p.bar_close"
            " WHERE p.algorithm = 'contrarian' AND p.status = 'ok' AND t.class IS NOT NULL"
            " AND o.mid_close IS NOT NULL AND o.mid_target IS NOT NULL AND o.special = 0"
        )
        versions = dict(
            db.execute(
                "SELECT substr(bar_close, 1, 10), model_version FROM predictions"
                " WHERE algorithm = 'baseline-climate' GROUP BY 1"
            )
        )
        persist = {
            v
            for (v,) in db.execute(
                "SELECT model_version FROM predictions WHERE algorithm = 'baseline-persist'"
            )
        }
    assert result.stats.summary.n == labelled > 0
    # The climate table, refitted before each day on the days before it.
    assert versions[str(DAYS[0])] == "baseline-climate-default"
    assert versions[str(DAYS[1])].startswith(f"baseline-climate-{DAYS[0]:%Y%m%d}-")
    assert versions[str(DAYS[2])].startswith(f"baseline-climate-{DAYS[1]:%Y%m%d}-")
    assert persist == {"v2"}


async def test_a_second_pre_validation_of_the_same_algorithm_waits_for_the_first(tmp_path: Path) -> None:
    """Input: kalman's pre-validation under way (its lock held, #204: the scheduled job), and
    its database. Output: a second one (by hand) stops at once and leaves the database; once
    the first ends, it goes on (here: no recorded day)."""
    s = settings(tmp_path, days=[])
    db = s.state_dir / "prevalidation-kalman.sqlite3"
    db.parent.mkdir(parents=True)
    db.write_text("the first run's")

    with _held(s.state_dir, "kalman"):
        with pytest.raises(PrevalidationRunning, match="kalman runs already"):
            await prevalidate(s, "kalman")
        assert db.read_text() == "the first run's"

    with pytest.raises(FileNotFoundError):
        await prevalidate(s, "kalman")


# An algorithm's diagnostics (#283, ``contract.Diagnostics``): measured, reported, never judged.


class _Measures:
    """Stand-in diagnostics: the algorithm's fit, counted; each replayed day's samples, kept.
    ``broken``: the step that raises."""

    def __init__(self, algorithm: Contrarian, broken: str | None) -> None:
        self.algorithm = algorithm
        self.broken = broken
        self.fits = 0
        self.days: list[tuple[date, str, set[date]]] = []  # day, model version, the samples' days
        self.references: set[str | None] = set()  # the reference prices the days' samples name

    def _check(self, step: str) -> None:
        if self.broken == step:
            raise RuntimeError(f"{step} broke")

    def fit(self, samples: Sequence[Sample]) -> Model:
        self._check("fit")
        self.fits += 1
        return self.algorithm.fit(samples)

    def day(self, day: date, model: Model, samples: Sequence[Sample]) -> None:
        self._check("day")
        self.days.append((day, model.version, {s.bar_close.astimezone(KST).date() for s in samples}))
        self.references |= {s.reference for s in samples}

    def report(self) -> dict[str, Any]:
        self._check("report")
        return {"fits": self.fits, "days": [d.isoformat() for d, _, _ in self.days]}

    def lines(self) -> list[str]:
        return [f"{self.algorithm.name} diagnostics: {len(self.days)} days"]


class Measured(Contrarian):
    """A Contrarian that offers diagnostics."""

    def __init__(self, name: str, broken: str | None = None) -> None:
        super().__init__(name)
        self.measures = _Measures(self, broken)

    def diagnostics(self) -> _Measures:
        self.measures._check("start")
        return self.measures


def _checks_of(result: prevalidation.Prevalidation) -> list[tuple[str, bool, str]]:
    return [(c.name, c.passed, c.detail) for c in result.checks]


async def test_an_algorithm_s_diagnostics_are_reported_and_never_judged(tmp_path: Path) -> None:
    plain_settings = settings(tmp_path / "plain", DAYS[:3])
    plain = await prevalidate(
        plain_settings, "contrarian", algorithms=[ALPHA, Contrarian("contrarian")], now=AFTER
    )
    s = settings(tmp_path / "measured", DAYS[:3])
    candidate = Measured("contrarian")

    result = await prevalidate(s, "contrarian", algorithms=[ALPHA, candidate], now=AFTER)

    assert _checks_of(result) == _checks_of(plain), "the same checks, the same verdict"
    assert result.models == plain.models, "its diagnostics' fit gave the algorithm's models"
    measures = candidate.measures
    assert measures.fits == len(candidate.fitted) == 2, "the walk-forward trained it through them"
    # Each replayed day, with the model that predicted it and that day's samples.
    assert [(d, v) for d, v, _ in measures.days] == list(result.models.items())
    assert all(days == {d} for d, _, days in measures.days)
    assert measures.references == {"mid"}, "recorded days, named so, as the nightly training reads them"
    assert report(s, "contrarian")["diagnostics"] == {"fits": 2, "days": [d.isoformat() for d in DAYS[:3]]}
    assert result.diagnostic_lines == ("contrarian diagnostics: 3 days",)
    assert "diagnostics" not in report(plain_settings, "contrarian"), "none offered, none reported"


@pytest.mark.parametrize("broken", ["start", "fit", "day", "report"])
async def test_diagnostics_that_fail_leave_the_run_and_its_verdict_as_they_were(
    tmp_path: Path, broken: str
) -> None:
    plain = await prevalidate(
        settings(tmp_path / "plain", DAYS[:2]),
        "contrarian",
        algorithms=[ALPHA, Contrarian("contrarian")],
        now=AFTER,
    )
    s = settings(tmp_path / "measured", DAYS[:2])

    result = await prevalidate(s, "contrarian", algorithms=[ALPHA, Measured("contrarian", broken)], now=AFTER)

    assert _checks_of(result) == _checks_of(plain)
    assert result.models == plain.models, "the algorithm's own fit trained it once they failed"
    error = report(s, "contrarian")["diagnostics"]["error"]
    assert error.startswith(f"{broken}") and error.endswith(f"RuntimeError: {broken} broke")
    assert result.diagnostic_lines == (f"contrarian diagnostics: not measured ({error})",)


def test_the_command_prints_neyman_s_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    s = settings(tmp_path, DAYS[:3])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_DATA_DIR", str(s.data_dir))
    monkeypatch.setenv("KRX_STATE_DIR", str(s.state_dir))
    monkeypatch.setenv("KRX_SYMBOLS", "005930")

    main(["prevalidate", "neyman", "--days", "2"])

    out = capsys.readouterr().out
    assert "\nneyman diagnostics (#283; reported, not judged): 2 replayed days predicted by trees" in out
    assert "\n  flips 005930: " in out and "\n  first model 005930, p_up - p_down " in out
    measured = report(s, "neyman")["diagnostics"]
    assert (measured["issue"], measured["judged"]) == (283, False)
    flips = measured["flips"]["by_symbol"]["005930"]
    assert flips["scored"] == measured["first_model"]["by_symbol"]["005930"]["n"]["live"] > 0
    assert flips["calls"] == flips["kept"] + flips["to_flat"] + flips["flipped"]
