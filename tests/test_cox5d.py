"""Cox-5d (#310): Cox on the five sessions of ``open_to_close_5d``. Its own fit purges the windows
between its 80/20 split; its pre-validation measures the overlap's variance ratio and the review
days the rule gives it. Until those days were fixed in code, the kind's algorithms were held out of
probation (``DailyKind.review_hold``, kept for a later kind); now one that passes goes on probation
on 60, 120 and 180 days, and cox-5d, refused (2026-10-23), does not run."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from krx_quant.algorithms import DAILY_ALGORITHMS, Model
from krx_quant.algorithms import cox5d as cox5d_module
from krx_quant.algorithms.cox import FEATURES, LAMBDAS, OWN, VALIDATION_SHARE, Cox
from krx_quant.algorithms.cox import _fit as cox_fit
from krx_quant.algorithms.cox5d import Cox5d
from krx_quant.cards import Card, Meaning
from krx_quant.clock import FakeClock
from krx_quant.config import Settings
from krx_quant.daily import DailySample, overlap_review
from krx_quant.daily_features import DailyFiles, input_versions, samples
from krx_quant.daily_training import prevalidate_daily
from krx_quant.engine import Admits, PredictionEngine
from krx_quant.features import FeatureVector
from krx_quant.feeds.base import KST
from krx_quant.kinds import DIRECTION_KIND, KindForecast
from krx_quant.lifecycle import REGISTERED, Status
from krx_quant.market_calendar import MarketCalendar
from krx_quant.open_to_close_5d import OPEN_TO_CLOSE_5D
from krx_quant.predictions import DB_NAME, PredictionStore
from krx_quant.prevalidation import code_digest, report_path
from krx_quant.scheduled import OK, Facts, JobRecord, Night, daily_prevalidation_jobs, prevalidation_job_name
from krx_quant.simulation import simulate_daily
from tests.daily_doubles import SYMBOLS, made_history

FEE = 0.015 / 100
LAST = date(2026, 10, 16)
NIGHT = datetime(2026, 10, 16, 23, 0, tzinfo=KST)
CALENDAR = MarketCalendar()
# The kind as it runs (the dividend probe's result lifted its gate, #338), its review days fixed from
# the measured variance ratio: 60, 120 and 180 (#310).
RUNNING = OPEN_TO_CLOSE_5D
# The hold as it was before the days were fixed (``DailyKind.review_hold``, #339): kept for a later
# kind of windows of several sessions, so its behaviour is still tested, on a held copy.
HOLD = "its review days are fixed from the variance ratio its first pre-validation measures (#310): not yet"
HELD = replace(OPEN_TO_CLOSE_5D, review_hold=HOLD)


def settings(tmp_path: Path, days: int, signal: float = 0.0, seed: int = 310) -> Settings:
    made_history(tmp_path / "data", tmp_path / "state", LAST, days, signal=signal, seed=seed)
    return Settings(
        kis_app_key="",
        kis_app_secret="",
        symbols=SYMBOLS,
        etf_symbols=(),
        index_codes=(),
        us_symbols=(),
        data_dir=tmp_path / "data",
        state_dir=tmp_path / "state",
    )


def test_cox_5d_is_cox_on_the_five_sessions_and_its_code_is_cox_s_too() -> None:
    assert [a.name for a in DAILY_ALGORITHMS] == ["cox", "cox-5d"]
    algorithm = DAILY_ALGORITHMS[1]
    assert isinstance(algorithm, Cox5d) and algorithm.kind == "open_to_close_5d"
    assert algorithm.features == FEATURES and algorithm.card.unique_features == OWN
    assert algorithm.training_days == Cox.training_days
    # Its digest covers its package, then Cox's: a change to Cox's code asks for a new verdict.
    root = Path(cox5d_module.__file__).parent
    cox_root = Path(__file__).parents[1] / "src" / "krx_quant" / "algorithms" / "cox"
    digest = hashlib.sha256()
    for files_root in (root, None, cox_root):
        if files_root is None:
            digest.update(b"\0code_from\0")
            continue
        for f in sorted(files_root.rglob("*.py")):
            digest.update(f.relative_to(files_root).as_posix().encode())
            digest.update(f.read_bytes())
    assert code_digest(algorithm) == digest.hexdigest()[:16]
    own = hashlib.sha256()
    for f in sorted(cox_root.rglob("*.py")):
        own.update(f.relative_to(cox_root).as_posix().encode())
        own.update(f.read_bytes())
    assert code_digest(Cox()) == own.hexdigest()[:16], "Cox's own digest is as it was"


def test_its_lambda_is_chosen_on_the_windows_ended_before_the_held_out_days(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = settings(tmp_path, 700)
    found = [
        x
        for x in samples(
            RUNNING, DailyFiles(s.data_dir, s.state_dir), SYMBOLS, FEATURES, FEE, calendar=CALENDAR
        )
        if x.ended_by(LAST)
    ]
    rows: list[int] = []
    real = cox_fit

    def counting(x: np.ndarray, y: np.ndarray, penalty: float) -> np.ndarray:
        rows.append(len(x))
        return real(x, y, penalty)

    monkeypatch.setattr(cox5d_module, "_fit", counting)
    model = Cox5d().fit(found)
    labelled = [x for x in found if x.label is not None]
    days = sorted({x.day for x in labelled})
    cut = days[int(len(days) * (1 - VALIDATION_SHARE))]
    early = [x for x in labelled if x.ended_before(cut)]
    purged = [x for x in labelled if x.day < cut and not x.ended_before(cut)]
    assert {x.day for x in purged} == set(days[days.index(cut) - 4 : days.index(cut)]), "the four days before"
    assert rows[: len(LAMBDAS)] == [len(early)] * len(LAMBDAS), "λ fitted on the ended windows only"
    assert rows[len(LAMBDAS)] == len(labelled), "then every window, with the λ chosen"
    assert model.version.startswith("cox-5d-") and model.target_version == OPEN_TO_CLOSE_5D.answer_version


@pytest.mark.timeout(180)  # a ten-year-like walk-forward of Cox: thirteen fits of 750 days
def test_the_pre_validation_measures_the_overlap_and_the_review_days_it_gives(tmp_path: Path) -> None:
    s = settings(tmp_path, 1_000, signal=1.0)
    result = prevalidate_daily(s, "cox-5d", kinds=[RUNNING])
    saved = json.loads(report_path(s.state_dir, "cox-5d").read_text())
    overlap = saved["walk_forward"]["overlap"]
    assert (overlap["sessions"], overlap["lag"]) == (5, 8)
    assert overlap["independent_days"] == overlap["days"] // 5 and overlap["days"] == len(result.days) - 4
    ratios = overlap["variance_ratio"]
    assert set(ratios) == {"otc5-flat", "otc5-climate", "otc5-persist"}
    assert all(r is not None and r > 0 for r in ratios.values())
    assert overlap["variance_ratio_max"] == max(ratios.values())
    assert set(overlap["baselines_variance_ratio"]) == {
        "otc5-climate - otc5-flat",
        "otc5-persist - otc5-flat",
    }
    rule = overlap_review(overlap["variance_ratio_max"], 5)
    assert overlap["review_days"] == {
        "variance_ratio": overlap["variance_ratio_max"],
        "first": rule.first,
        "probation_days": list(rule.rules.probation_days),
        "bench_days": list(rule.rules.bench_days),
        "recent_days": rule.recent_days,
        "min_sample": rule.min_sample,
    }
    assert [p["phase"] for p in overlap["phases"]] == [0, 1, 2, 3, 4]
    assert sum(p["days"] for p in overlap["phases"]) == overlap["days"], "every day in one phase"
    assert saved["stats"]["overlap_sessions"] == 5, "its gate on the overlap-corrected bound"
    from krx_quant.cli import _overlap_lines

    lines = _overlap_lines(overlap).splitlines()
    assert lines[0].startswith("  overlap: 5 sessions, Newey-West lag 8,")
    assert lines[1].startswith("  variance ratio by baseline: otc5-flat ") and "; largest " in lines[1]
    assert lines[3] == (
        f"  review days by the rule (#310): {list(rule.rules.probation_days)} (recent {rule.recent_days},"
        f" at least {rule.min_sample} scored)"
    )
    assert [line.split(":")[0] for line in lines[4:]] == [f"  phase {p}" for p in range(5)]
    print(f"synthetic cox-5d walk-forward: VR {ratios}, between {overlap['baselines_variance_ratio']}")
    print(f"review days by the rule: {overlap['review_days']}")


class Stand:
    """A made-up daily algorithm of the five sessions: its fits are kept."""

    training_days = 100
    param_meanings: Mapping[str, Meaning] = {}

    def __init__(self, kind: str = "open_to_close_5d") -> None:
        self.name = "stand"
        self.kind = kind
        self.features: tuple[str, ...] = ("us_sox",)
        self.card = Card("Test", "test", "test", ("us_sox",), "test", "test", "test")
        self.fits: list[list[DailySample]] = []

    def default_model(self) -> Model:
        return Model("stand-default", {})

    def fit(self, samples: Sequence[DailySample]) -> Model:
        self.fits.append(list(samples))
        last = max(x.day for x in samples)
        return Model(f"stand-{last:%Y%m%d}", {}, trained_until=last, target_version=1)

    def predict(self, features: FeatureVector, model: Model) -> KindForecast:
        sox = features["us_sox"]
        return KindForecast((1 / 3, 1 / 3, 1 / 3) if sox is None else (0.6, 0.2, 0.2))


def passed(s: Settings, algorithm: Stand | Cox5d) -> None:
    path = report_path(s.state_dir, algorithm.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "algorithm": algorithm.name,
                "code": code_digest(algorithm),
                "prevalidated_at": NIGHT.isoformat(),
                "passed": True,
                "days": [LAST.isoformat()] * 600,
                "target_version": OPEN_TO_CLOSE_5D.answer_version,
                "kind": OPEN_TO_CLOSE_5D.name,
                "inputs": input_versions(algorithm.features),
            }
        )
    )


async def test_a_passed_algorithm_of_a_held_kind_waits_for_its_review_days(tmp_path: Path) -> None:
    """#310: a kind's review days are fixed in code from the variance ratio its first pre-validation
    measures; while they are held, one that passed is neither run nor enrolled nor simulated."""
    assert HELD.as_dict([])["daily"]["review_hold"] == HOLD
    s = settings(tmp_path, 420)
    algorithm = Stand()
    passed(s, algorithm)
    clock = FakeClock(datetime(2026, 10, 19, 7, 20, tzinfo=KST))
    from krx_quant.daily import StoredDays

    engine = PredictionEngine(
        PredictionStore(s.state_dir / DB_NAME),
        CALENDAR,
        SYMBOLS,
        FEE,
        clock,
        algorithms=(),
        kinds=(DIRECTION_KIND,),
        kind_algorithms=(),
        daily=StoredDays(s.data_dir),
        daily_kinds=(HELD,),
        daily_algorithms=[algorithm],
        daily_features=lambda a, sym, d, p, b: FeatureVector(dict.fromkeys(a.features), a.features),
        daily_feature_names={"us_sox"},
        admits=lambda a: True,
    )
    assert engine.daily_algorithms == () and "stand" not in engine.store.statuses()
    engine.store.close()
    run = simulate_daily(s, algorithms=[algorithm], kinds=[HELD], now=NIGHT)
    assert run.enrolled == [] and run.simulations == []
    # Enrolled by hand (an operator's mistake), its simulation waits for the days too.
    store = PredictionStore(s.state_dir / DB_NAME)
    store.add_status("stand", NIGHT - timedelta(hours=14), None, Status.PROBATION, REGISTERED)
    store.close()
    run = simulate_daily(s, algorithms=[algorithm], kinds=[HELD], now=NIGHT)
    assert run.held == {"stand": HOLD} and algorithm.fits == []


def test_its_pre_validation_posts_to_310_and_its_simulation_job_waits_for_the_days() -> None:
    from krx_quant.scheduled import _daily_simulation_due

    [job] = daily_prevalidation_jobs(["cox-5d"], {"cox-5d": FEATURES}, {"cox-5d": "open_to_close_5d"})
    assert [t.issue for t in job.targets] == [204, 310]
    assert "분산 배율" in job.targets[1].note and "60·120·180일로 고정했다" in job.targets[1].note
    [cox_job] = daily_prevalidation_jobs(["cox"], {"cox": FEATURES}, {"cox": "open_to_close"})
    assert [t.issue for t in cox_job.targets] == [204, 264]
    passed_tonight = {
        prevalidation_job_name("cox-5d"): JobRecord(LAST, OK, NIGHT, "c", 0, 60.0),
    }
    night = Night(LAST, True, "c", None, passed_tonight, Facts(simulating_daily=("cox-5d",)))
    assert not _daily_simulation_due([Cox5d()], [HELD])(night), "held: not enrolled, not simulated"
    assert _daily_simulation_due([Cox5d()], [RUNNING])(night), "its days fixed: a pass is simulated"


def test_once_its_days_are_fixed_it_is_simulated_on_its_first_review_day_of_ended_windows(
    tmp_path: Path,
) -> None:
    review = overlap_review(1.0, 5)  # D₁ = 40
    kind = replace(
        RUNNING,
        review_rules=review.rules,
        recent_days=review.recent_days,
        min_sample=review.min_sample,
    )
    s = settings(tmp_path, 420)
    algorithm = Stand()
    passed(s, algorithm)
    store = PredictionStore(s.state_dir / DB_NAME)
    store.add_status("stand", NIGHT - timedelta(hours=14), None, Status.PROBATION, REGISTERED)
    store.close()

    run = simulate_daily(s, algorithms=[algorithm], kinds=[kind], now=NIGHT)

    [sim] = run.simulations
    assert len(sim.days) == 40, "its first review day: D₁"
    days = sorted({x.day for fit in algorithm.fits for x in fit} | set(sim.days))
    for replayed, fit in zip(sim.days, algorithm.fits, strict=True):
        night = max(d for d in days if d < replayed)
        assert fit and all(x.ended_by(night) for x in fit), "only windows ended by the night before"
    assert sim.days[-1] == date(2026, 10, 12), "the latest window to end by Friday began on Monday"


def _engine(s: Settings, algorithm: Stand | Cox5d, clock: FakeClock, admitted: Admits) -> PredictionEngine:
    from krx_quant.daily import StoredDays

    return PredictionEngine(
        PredictionStore(s.state_dir / DB_NAME),
        CALENDAR,
        SYMBOLS,
        FEE,
        clock,
        algorithms=(),
        kinds=(DIRECTION_KIND,),
        kind_algorithms=(),
        daily=StoredDays(s.data_dir),
        daily_kinds=(RUNNING,),
        daily_algorithms=[algorithm],
        daily_features=lambda a, sym, d, p, b: FeatureVector(dict.fromkeys(a.features), a.features),
        daily_feature_names=set(algorithm.features),
        admits=admitted,
    )


def test_a_passing_algorithm_of_the_kind_goes_on_probation_judged_on_60_120_180(tmp_path: Path) -> None:
    """#310: the hold is lifted with the days fixed. An algorithm of the kind whose pre-validation
    passes is run on probation by the engine, and its simulated probation (#298) replays the latest
    60 ended windows, judged at once on its first review day, 60."""
    from krx_quant.prevalidation import admits

    s = settings(tmp_path, 420)
    algorithm = Stand()
    passed(s, algorithm)
    clock = FakeClock(datetime(2026, 10, 16, 7, 20, tzinfo=KST))
    engine = _engine(s, algorithm, clock, admits(s.state_dir))
    assert engine.daily_algorithms == (algorithm,) and engine.waiting() == [] and engine.held() == []
    standing = engine.store.statuses()["stand"]
    assert (standing.status, standing.reason) == (Status.PROBATION, REGISTERED)
    engine.store.close()

    run = simulate_daily(s, algorithms=[algorithm], kinds=[RUNNING], now=NIGHT)

    assert run.enrolled == [] and run.held == {}, "the engine enrolled it; nothing holds it"
    [sim] = run.simulations
    assert len(sim.days) == 60, "the kind's first review day"
    assert sim.verdict is not None and sim.verdict.review_day == 60
    assert sim.days[-1] == date(2026, 10, 12), "ended windows only: the latest begun on Monday"


def test_cox_5d_refused_is_not_on_probation_and_nothing_starts_it(tmp_path: Path) -> None:
    """#310: cox-5d's pre-validation was refused (2026-10-23: weakest lift 0.0204, lower bound by
    trading day −0.0072 at 95%). With the hold lifted, the engine still does not run it (it waits for
    a pre-validation, as any algorithm refused); ``simulate daily`` neither enrolls nor simulates
    it; the simulation job is not due; and its scheduled pre-validation does not run again on the
    same code."""
    from krx_quant.prevalidation import admits, last_report
    from krx_quant.scheduled import FAILED, _daily_simulation_due

    s = settings(tmp_path, 420)
    algorithm = Cox5d()
    passed(s, algorithm)
    path = report_path(s.state_dir, "cox-5d")
    path.write_text(json.dumps({**json.loads(path.read_text()), "passed": False}))
    clock = FakeClock(datetime(2026, 10, 16, 7, 20, tzinfo=KST))
    engine = _engine(s, algorithm, clock, admits(s.state_dir))
    assert engine.daily_algorithms == () and "cox-5d" not in engine.store.statuses()
    assert engine.waiting() == ["cox-5d"] and engine.held() == [], "refused: waits, not held"
    engine.store.close()

    run = simulate_daily(s, algorithms=[algorithm], kinds=[RUNNING], now=NIGHT)
    assert (run.enrolled, run.simulations, run.held) == ([], [], {})

    refused_tonight = {prevalidation_job_name("cox-5d"): JobRecord(LAST, FAILED, NIGHT, "c", 1, 63.0)}
    report = last_report(s.state_dir, algorithm)
    assert report is not None and report.current and not report.passed and report.judged
    facts = Facts(waiting=("cox-5d",), reports={"cox-5d": report})
    night = Night(LAST, True, "c", None, refused_tonight, facts)
    assert not _daily_simulation_due([algorithm], [RUNNING])(night)
    [job] = daily_prevalidation_jobs(["cox-5d"], {"cox-5d": FEATURES}, {"cox-5d": "open_to_close_5d"})
    later = Night(LAST + timedelta(days=3), True, "d", None, refused_tonight, facts)
    assert not job.due(later), "judged on the code it runs: not again"


def test_the_pre_validation_says_when_the_rule_gives_longer_days_than_the_kind_s() -> None:
    """#310 "다음 알고리즘": a later algorithm's report measures the ratio again; the command prints
    the kind's fixed days under the rule's and says when the rule's are longer."""
    from krx_quant.cli import _overlap_lines

    def overlap(first: int) -> dict[str, object]:
        review = overlap_review({60: 2.22, 80: 3.0}[first], 5)
        assert review.first == first
        return {
            "sessions": 5,
            "lag": 8,
            "days": 1698,
            "independent_days": 339,
            "variance_ratio": {"otc5-flat": 2.0},
            "variance_ratio_max": 2.0,
            "baselines_variance_ratio": {},
            "review_days": {
                "first": review.first,
                "probation_days": list(review.rules.probation_days),
                "recent_days": review.recent_days,
                "min_sample": review.min_sample,
            },
            "phases": [],
        }

    same = _overlap_lines(overlap(60), RUNNING).splitlines()
    assert same[-1] == "  review days of open_to_close_5d: [60, 120, 180]"
    longer = _overlap_lines(overlap(80), RUNNING).splitlines()
    assert longer[-2].startswith("  review days by the rule (#310): [80, 160, 240]")
    assert longer[-1] == (
        "  review days of open_to_close_5d: [60, 120, 180]"
        " - the rule gives longer ones: open an issue to fix them again (#310)"
    )
    assert not any("review days of" in line for line in _overlap_lines(overlap(60)).splitlines())


def test_the_review_days_never_take_a_ratio_below_one() -> None:
    """#339 review: every baseline's gains alternating day by day make every ratio < 1; the report
    keeps them, but the review days are the rule's at 1 (the intervals never shrink below the
    independent ones, ``stats.overlap_t_test``): 40, not 20."""
    from krx_quant.daily_training import overlap_report
    from krx_quant.kinds import KindScored

    def scored(name: str, day: int, right: bool) -> KindScored:
        at = datetime(2026, 1, 5, 8, 50, tzinfo=KST) + timedelta(days=day)
        return KindScored(name, "v1", "005930", at, (0.0, 1.0, 0.0), 1 if right else 0, {}, {})

    mine = [scored("alg", d, d % 2 == 0) for d in range(200)]
    baselines = {
        name: [scored(name, d, d % 2 == 1) for d in range(200)]
        for name in ("otc5-flat", "otc5-climate", "otc5-persist")
    }
    report = overlap_report(RUNNING, mine, baselines)
    assert all(r is not None and r < 1 for r in report["variance_ratio"].values())
    assert report["variance_ratio_max"] < 1, "kept as measured"
    assert report["review_days"]["variance_ratio"] == 1.0
    assert (
        report["review_days"]["first"] == 40 and overlap_review(report["variance_ratio_max"], 5).first == 20
    )


async def test_a_passed_algorithm_held_for_its_review_days_is_not_said_to_wait_after_a_restart(
    tmp_path: Path,
) -> None:
    """#339 review: once its pre-validation passed, an algorithm of a kind holding its review days
    is held, not waiting for a pre-validation: a restart's review says it is held, never "검증을
    통과해야 돌기 시작합니다"."""
    from krx_quant.daily import StoredDays
    from krx_quant.replay import replay_into
    from tests.test_lifecycle import Outbox

    s = settings(tmp_path, 420)
    monday = date(2026, 10, 19)

    def engine(outbox: Outbox, start: datetime) -> tuple[PredictionEngine, FakeClock]:
        clock = FakeClock(start)
        return PredictionEngine(
            PredictionStore(s.state_dir / DB_NAME),
            CALENDAR,
            SYMBOLS,
            FEE,
            clock,
            algorithms=(),
            kinds=(DIRECTION_KIND,),
            kind_algorithms=(),
            daily=StoredDays(s.data_dir),
            daily_kinds=(HELD,),
            daily_algorithms=[Stand()],
            daily_features=lambda a, sym, d, p, b: FeatureVector(dict.fromkeys(a.features), a.features),
            daily_feature_names={"us_sox"},
            admits=lambda a: True,  # its report passed
            notify=outbox,
        ), clock

    for day in (monday, monday + timedelta(days=1)):  # the second: a restart the next morning
        outbox = Outbox()
        e, clock = engine(outbox, datetime.combine(day, datetime.min.time(), KST).replace(hour=7, minute=20))
        assert e.waiting() == [] and e.held() == ["stand"] and e.daily_algorithms == ()
        review = datetime.combine(day, datetime.min.time(), KST).replace(hour=15, minute=45)
        await replay_into(e, clock, [], until=review)
        about = [m for m in outbox.sent if "stand" in m]
        assert len(about) == 1 and about[0].startswith("판정일 보류: stand."), "one alert, the held one"
        assert "사전 검증 대기" not in about[0]
        e.store.close()
