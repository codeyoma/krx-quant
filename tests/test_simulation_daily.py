"""Simulated probation of a daily kind's algorithm (#298): the latest 20 trading days replayed as
if it had been on probation, each day's model fitted on the days before it, judged at once.

Input: made-up KRX days, US closes and supply (``daily_doubles``), with a planted signal (the
day's open→close leans against the night's SOX move) or none, and the engine's database.
Output: the simulation's predictions, apart from the live ones, the status the review at once
leaves, the engine's later reviews and stages counting those days, and the scheduled job.
"""

from __future__ import annotations

import json
import random
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant.algorithms import DAILY_ALGORITHMS, Model, NothingToLearn
from krx_quant.algorithms.cox import FEATURES
from krx_quant.candles import krx_days_path, read_krx_days, write_krx_days
from krx_quant.cards import Card, Meaning
from krx_quant.cli import main
from krx_quant.clock import FakeClock
from krx_quant.config import Settings
from krx_quant.daily import JUDGING_HELD, DailySample
from krx_quant.daily_features import input_versions
from krx_quant.engine import PredictionEngine
from krx_quant.features import FeatureVector
from krx_quant.feeds.base import KST
from krx_quant.kinds import DIRECTION_KIND, Answer, KindForecast
from krx_quant.lifecycle import REGISTERED, Status
from krx_quant.market_calendar import MarketCalendar
from krx_quant.open_to_close import CLIMATE, FLAT_NAME, OPEN_TO_CLOSE, PERSIST
from krx_quant.predictions import DB_NAME, NewKindPrediction, PredictionStore
from krx_quant.prevalidation import code_digest, report_path
from krx_quant.scheduled import FAILED, JOBS, OK, Facts, JobRecord, Night, prevalidation_job_name
from krx_quant.simulation import DAILY_DAYS, SimulationRun, report, simulate_daily
from krx_quant.special import Kind as SpecialKind
from krx_quant.special import Window
from tests.daily_doubles import SYMBOLS, made_history
from tests.replay_events import api, get_json

FEE = 0.015 / 100
LAST = date(2026, 10, 16)  # a Friday: the last made-up KRX day
NIGHT = datetime(2026, 10, 16, 23, 0, tzinfo=KST)  # that night's scheduled jobs
CALENDAR = MarketCalendar()


# The hold on open_to_close's judging (#264, ``daily.JUDGING_HELD``), as deployed; the tests below
# judge its algorithms as the engine will once its close is fixed, unless one puts it back.
HELD = dict(JUDGING_HELD)


@pytest.fixture(autouse=True)
def judged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("krx_quant.daily.JUDGING_HELD", {})


def settings(tmp_path: Path, signal: float, days: int = 420, seed: int = 298) -> Settings:
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


def passed_report(s: Settings, name: str = "cox") -> None:
    """A passed pre-validation of the code and inputs Cox runs now: the engine's gate admits it."""
    cox = DAILY_ALGORITHMS[0]
    path = report_path(s.state_dir, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "algorithm": name,
                "code": code_digest(cox),
                "prevalidated_at": NIGHT.isoformat(),
                "passed": True,
                "days": [LAST.isoformat()] * 600,
                "target_version": OPEN_TO_CLOSE.answer_version,
                "kind": OPEN_TO_CLOSE.name,
                "inputs": input_versions(FEATURES),
            }
        )
    )


def store_of(s: Settings) -> PredictionStore:
    return PredictionStore(s.state_dir / DB_NAME)


class AgainstTheNight:
    """A made-up daily algorithm: down after a night the SOX rose, up after one it fell. Its fits
    are kept (the samples' last day), to see what each day was trained on."""

    training_days = 100
    param_meanings: Mapping[str, Meaning] = {}

    def __init__(self, name: str = "against") -> None:
        self.name = name
        self.kind = OPEN_TO_CLOSE.name
        self.features: tuple[str, ...] = ("us_sox",)
        self.card = Card("Test", "test", "test", ("us_sox",), "test", "test", "test")
        self.fitted: list[date] = []  # the last day of each fit's samples

    def default_model(self) -> Model:
        return Model(f"{self.name}-default", {})

    def fit(self, samples: Sequence[DailySample]) -> Model:
        last = max(s.day for s in samples)
        self.fitted.append(last)
        return Model(f"{self.name}-{last:%Y%m%d}", {}, trained_until=last, target_version=1)

    def predict(self, features: FeatureVector, model: Model) -> KindForecast:
        sox = features["us_sox"]
        if sox is None:
            return KindForecast((1 / 3, 1 / 3, 1 / 3))
        return KindForecast((0.1, 0.2, 0.7) if sox > 0 else (0.7, 0.2, 0.1))


def enroll(s: Settings, name: str, at: datetime = NIGHT - timedelta(hours=14)) -> datetime:
    """``name`` on probation since ``at``, as the engine enrolls it (``registered``)."""
    store = store_of(s)
    try:
        store.add_status(name, at, None, Status.PROBATION, REGISTERED)
    finally:
        store.close()
    return at


def test_cox_on_a_planted_signal_is_enrolled_simulated_and_active_the_same_night(tmp_path: Path) -> None:
    """Cox's pre-validation passed tonight: the command enrolls it (the engine would at its next
    start), replays the latest 20 trading days, each with a model fitted on the days before it,
    and judges it at once on the review of day 20. On a planted signal it passes: active from
    now, the change marked simulated."""
    s = settings(tmp_path, signal=4.0)
    passed_report(s)

    run = simulate_daily(s, now=NIGHT)

    assert run.enrolled == ["cox"]
    [sim] = run.simulations
    assert len(sim.days) == DAILY_DAYS and sim.days[-1] == LAST
    assert sim.training["cadence"] == "daily" and len(sim.training["models"]) == DAILY_DAYS
    assert sim.verdict is not None and sim.verdict.review_day == 20 and sim.verdict.enough
    assert sim.status is Status.ACTIVE, sim.verdict
    store = store_of(s)
    try:
        assert store.statuses()["cox"].status is Status.ACTIVE
        entered, changed = store.status_history("cox")
        assert (entered["status"], entered["reason"], entered["simulated"]) == (
            "probation",
            REGISTERED,
            False,
        )
        assert changed["simulated"] is True
        assert changed["reason"].startswith(
            f"simulated probation {sim.days[0]}..{LAST} (20 days): 20 days on"
        )
        # Each day's model was the night before's: trained up to the trading day before it.
        models = sim.training["models"]
        assert all(v.startswith("cox-") for v in models.values())
        assert models[LAST.isoformat()].startswith(f"cox-{sim.days[-2]:%Y%m%d}-")
        # Nothing reached the live tables: the leaderboard and the statistics never see it.
        assert store.kind_scored(OPEN_TO_CLOSE.name, 1, sim.days, None, True) == []
        assert store.to_simulate(["cox"]) == [], "simulated once"
    finally:
        store.close()
    lines = report(run, 0.983)
    assert lines[0] == "enrolled on probation (pre-validation passed): cox"
    assert any("review of day 20: passed, active from now" in line for line in lines)

    again = simulate_daily(s, now=NIGHT + timedelta(days=1))
    assert (again.enrolled, again.simulations) == ([], []), "nothing waits: its probation was simulated"


def test_noise_stays_on_probation_and_the_live_days_count_on_from_day_21(tmp_path: Path) -> None:
    s = settings(tmp_path, signal=0.0, seed=7)
    against = AgainstTheNight()
    enroll(s, against.name)

    [sim] = simulate_daily(s, algorithms=[against], now=NIGHT).simulations

    assert sim.status is Status.PROBATION and sim.verdict is not None
    assert sim.verdict.change is None and sim.verdict.review_day == 20
    store = store_of(s)
    try:
        [entered] = store.status_history(against.name)
        assert entered["status"] == "probation"
    finally:
        store.close()
    lines = report(SimulationRun([], [sim]), 0.983)
    assert any("not passed, stays on probation; live days count on from day 21" in line for line in lines)


def test_no_day_is_predicted_with_what_came_on_or_after_it(tmp_path: Path) -> None:
    """Each day's model is fitted on the samples up to the trading day before it, and a day's
    prediction does not move when its own KRX day candle and every later one change."""
    s = settings(tmp_path / "a", signal=0.0, seed=11)
    against = AgainstTheNight()
    enroll(s, against.name)
    [sim] = simulate_daily(s, algorithms=[against], now=NIGHT).simulations
    market = sorted({d.day for symbol in SYMBOLS for d in made_days(s, symbol)})
    assert against.fitted == [max(m for m in market if m < d) for d in sim.days]

    # The same history, but every candle from the 10th simulated day on moves.
    other = settings(tmp_path / "b", signal=0.0, seed=11)
    cut = sim.days[9]
    for symbol in SYMBOLS:
        rows = read_krx_days(other.data_dir, symbol)
        krx_days_path(other.data_dir, symbol).unlink()
        write_krx_days(
            other.data_dir,
            symbol,
            [
                {**r, "open": round(r["open"] * 0.97), "close": round(r["close"] * 1.05)}
                if r["date"] >= cut
                else r
                for r in rows
            ],
        )
    enroll(other, against.name)
    [moved] = simulate_daily(other, algorithms=[AgainstTheNight()], now=NIGHT).simulations
    assert moved.days == sim.days
    first, second = predictions(s, against.name), predictions(other, against.name)
    before = {k: v for k, v in first.items() if k[1] < cut.isoformat()}
    assert len(before) == 9 * len(SYMBOLS)
    assert before == {k: v for k, v in second.items() if k[1] < cut.isoformat()}
    assert first.keys() == second.keys()


def made_days(s: Settings, symbol: str) -> list[Any]:
    from krx_quant.daily import StoredDays

    return StoredDays(s.data_dir).days(symbol)


def predictions(s: Settings, name: str) -> dict[tuple[str, str], Any]:
    """``name``'s simulated outputs, by (symbol, day)."""
    store = store_of(s)
    try:
        rows = store._db.execute(
            "SELECT symbol, bar_close, output FROM sim_kind_predictions WHERE algorithm = ?", (name,)
        ).fetchall()
    finally:
        store.close()
    return {(r[0], r[1][:10]): json.loads(r[2]) for r in rows}


def test_a_circuit_breaker_makes_a_simulated_day_special(tmp_path: Path) -> None:
    s = settings(tmp_path, signal=0.0, seed=3)
    against = AgainstTheNight()
    enroll(s, against.name)
    store = store_of(s)
    try:
        start = datetime.combine(LAST, time(10, 0), KST)
        store.keep_special(
            Window(SpecialKind.CIRCUIT_BREAKER, None, start, start + timedelta(minutes=20)), start
        )
    finally:
        store.close()
    simulate_daily(s, algorithms=[against], now=NIGHT)
    store = store_of(s)
    try:
        rows = store._db.execute(
            "SELECT substr(bar_close, 1, 10), MAX(answer_special) FROM sim_kind_predictions GROUP BY 1"
        ).fetchall()
    finally:
        store.close()
    special = {day for day, flag in rows if flag}
    assert special == {LAST.isoformat()}


def add_live_day(store: PredictionStore, name: str, day: date, rng: random.Random) -> None:
    """A live day: ``name`` right on every symbol, the baselines always flat."""
    made, answered = [], []
    for symbol in SYMBOLS:
        label = rng.choice(OPEN_TO_CLOSE.classes)
        right = {f"p_{c}": float(c == label) for c in OPEN_TO_CLOSE.classes}
        flat = {"p_up": 0.0, "p_flat": 1.0, "p_down": 0.0}
        at = OPEN_TO_CLOSE.predicted_at(day)
        for who, version, output in (
            (name, f"{name}-live", right),
            (FLAT_NAME, "v1", flat),
            (CLIMATE, "v1", flat),
            (PERSIST, "v1", flat),
        ):
            made.append(NewKindPrediction(symbol, who, version, at, at, output))
        answered.append((symbol, at, Answer(label, None, {}), False))
    store.add_kind_predictions(OPEN_TO_CLOSE.name, made)
    store.add_kind_answers(OPEN_TO_CLOSE.name, 1, answered, scored_at=at + timedelta(hours=12))


class Source:
    def days(self, symbol: str) -> list[Any]:
        return []


def no_features(algorithm: Any, symbol: str, day: date, past: Sequence[Any], band: Any) -> FeatureVector:
    return FeatureVector({}, ())


def engine_on(s: Settings, clock: FakeClock, algorithms: Sequence[Any]) -> PredictionEngine:
    return PredictionEngine(
        store_of(s),
        CALENDAR,
        SYMBOLS,
        FEE,
        clock,
        algorithms=(),
        kinds=(DIRECTION_KIND,),
        kind_algorithms=(),
        daily=Source(),
        daily_algorithms=algorithms,
        daily_features=no_features,
        daily_feature_names={"us_sox"},
    )


async def test_the_engine_counts_the_simulated_days_first_then_the_live_days_to_day_40(
    tmp_path: Path,
) -> None:
    """A simulation that did not pass leaves 20 days counted: the stage says so, its next review is
    day 40, and the engine's review of day 40 judges the 20 simulated days and the 20 live days
    after them together (the simulation's rows on its days, the live ones after)."""
    s = settings(tmp_path, signal=0.0, seed=7)
    against = AgainstTheNight()
    enroll(s, against.name)
    [sim] = simulate_daily(s, algorithms=[against], now=NIGHT).simulations
    assert sim.status is Status.PROBATION
    live = [d for d in (LAST + timedelta(days=i) for i in range(1, 60)) if CALENDAR.is_trading_day(d)][:21]
    store = store_of(s)
    store.record_models([(against.name, f"{against.name}-live", 1)])
    rng = random.Random(1)
    clock = FakeClock(datetime.combine(live[0], time(16), KST))
    e = engine_on(s, clock, [against])
    stage = (await e.stages())[against.name]
    assert (stage.days, stage.simulated, stage.next_review, stage.counted_from) == (20, 20, 40, None)
    for i, day in enumerate(live[:20]):
        add_live_day(store, against.name, day, rng)
        clock.set(datetime.combine(live[i + 1], time(15, 39), KST))
        await e._review(datetime.combine(live[i + 1], time(15, 40), KST))
        if i < 19:
            assert e.store.statuses()[against.name].status is Status.PROBATION, f"day {21 + i}"
    changed = e.store.status_history(against.name)[-1]
    assert changed["status"] == "active", changed
    assert changed["reason"].startswith("40 days on probation; weakest lift low")
    assert changed["reason"].endswith("; 20 of the days simulated") and changed["simulated"] is True
    store.close()


async def test_the_live_leaderboard_never_reads_the_simulated_days(tmp_path: Path) -> None:
    s = settings(tmp_path, signal=4.0)
    passed_report(s)
    simulate_daily(s, now=NIGHT)
    clock = FakeClock(datetime.combine(LAST + timedelta(days=3), time(16), KST))
    e = engine_on(s, clock, [])
    async with api(e) as client:
        board = await get_json(client, "/v1/leaderboard?kind=open_to_close&period=20d")
    names = {x["algorithm"] for x in [*board["ranked"], *board["unranked"], *board["baselines"]]}
    assert "cox" not in names, "no live prediction of cox: the simulation's are not the leaderboard's"


def night_of(day: date, record: JobRecord | None, records: Mapping[str, JobRecord], **facts: Any) -> Night:
    return Night(day, True, "abc", record, dict(records), Facts(**facts))


def test_the_scheduled_job_runs_after_a_daily_pass_or_while_a_probation_waits() -> None:
    [job] = [j for j in JOBS if j.name == "simulate-daily-298"]
    assert job.command == ("simulate", "daily")
    assert {t.issue for t in job.targets} == {298, 264}
    names = [j.name for j in JOBS]
    assert (
        names.index("prevalidate-cox")
        < names.index("simulate-daily-298")
        < names.index("prevalidate-mandelbrot")
    )
    day = date(2026, 10, 20)
    start = datetime.combine(day, time(23), KST)
    assert not job.due(night_of(day, None, {})), "nothing waits"
    passed = {prevalidation_job_name("cox"): JobRecord(day, OK, start, "abc", 0, 60.0)}
    assert job.due(night_of(day, None, passed)), "cox passed tonight: enrolled and simulated at once"
    refused = {prevalidation_job_name("cox"): JobRecord(day, FAILED, start, "abc", 1, 60.0)}
    assert not job.due(night_of(day, None, refused))
    assert job.due(night_of(day, None, {}, simulating_daily=("cox",))), "a probation waits"
    tonight = JobRecord(day, OK, start, "abc", 0, 30.0)
    assert not job.due(night_of(day, tonight, passed)), "once a night"
    failed = JobRecord(day - timedelta(days=1), FAILED, start, "abc", 1, 30.0)
    assert not job.due(night_of(day, failed, {}, simulating_daily=("cox",))), "failed on this code"
    assert job.due(night_of(day, replace(failed, commit="older"), {}, simulating_daily=("cox",)))
    assert job.due(night_of(day, failed, passed)), "a new pass asks again"


def test_while_open_to_close_is_held_its_probation_waits_unsimulated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """open_to_close's judging is held (#264): the command still enrolls Cox (the engine would)
    but neither simulates nor judges it; its probation still waits for its simulation, which is
    made once the hold is lifted. The scheduled job is not due meanwhile, even the night Cox
    passed."""
    monkeypatch.setattr("krx_quant.daily.JUDGING_HELD", HELD)
    s = settings(tmp_path, signal=4.0)
    passed_report(s)
    run = simulate_daily(s, now=NIGHT)
    assert run.enrolled == ["cox"]
    assert run.simulations == []
    assert run.held == {"cox": HELD[OPEN_TO_CLOSE.name]}
    lines = report(run, 0.983)
    assert lines[1].startswith("cox: its simulation waits - its close may be the after-market's")
    store = store_of(s)
    try:
        assert store.statuses()["cox"].status is Status.PROBATION
        assert store.to_simulate(["cox"]) == ["cox"], "still waits for its simulation"
        assert store.simulations() == {}
    finally:
        store.close()

    [job] = [j for j in JOBS if j.name == "simulate-daily-298"]
    day = date(2026, 10, 20)
    passed = {
        prevalidation_job_name("cox"): JobRecord(day, OK, datetime.combine(day, time(23), KST), "abc", 0, 60)
    }
    assert not job.due(night_of(day, None, passed)), "held: not the night it passed"
    assert not job.due(night_of(day, None, {}, simulating_daily=("cox",))), "held: not while it waits"

    monkeypatch.setattr("krx_quant.daily.JUDGING_HELD", {})
    assert job.due(night_of(day, None, {}, simulating_daily=("cox",))), "lifted: due"
    [sim] = simulate_daily(s, now=NIGHT + timedelta(days=1)).simulations
    assert sim.algorithm == "cox" and len(sim.days) == DAILY_DAYS


def test_the_command_prints_what_it_did(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    s = settings(tmp_path, signal=4.0)
    passed_report(s)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_DATA_DIR", str(s.data_dir))
    monkeypatch.setenv("KRX_STATE_DIR", str(s.state_dir))
    monkeypatch.setenv("KRX_SYMBOLS", ",".join(SYMBOLS))
    assert main(["simulate", "daily"]) == 0
    out = capsys.readouterr().out
    assert out.startswith(
        "simulated probation (#298), daily:\nenrolled on probation (pre-validation passed): cox"
    )
    assert "cox (open_to_close): simulated" in out and "20 days, refitted before each day" in out
    assert main(["simulate", "daily"]) == 0
    assert "no probation waits for its simulation" in capsys.readouterr().out


def test_a_new_algorithm_and_a_new_target_start_a_simulated_probation_a_command_does_not(
    tmp_path: Path,
) -> None:
    from krx_quant.lifecycle import starts_simulation, target_changed

    assert starts_simulation(REGISTERED)
    assert starts_simulation(target_changed("direction", 3)) and starts_simulation(
        target_changed("barrier", 2)
    )
    assert starts_simulation(
        "the target changed to the touch target (#170): probation restarts with a model trained on it"
    )
    assert not starts_simulation("unretired by command")
    assert not starts_simulation("no longer moves with an earlier algorithm")
    store = PredictionStore(tmp_path / DB_NAME)
    try:
        t = datetime(2026, 10, 18, 6, 0, tzinfo=KST)
        store.add_status("a", t, None, Status.PROBATION, REGISTERED)
        store.add_status("b", t, None, Status.ACTIVE, "set")
        store.change_status("b", t, Status.ACTIVE, Status.PROBATION, target_changed("direction", 3))
        store.add_status("c", t, None, Status.RETIRED, "set")
        store.change_status("c", t, Status.RETIRED, Status.PROBATION, "unretired by command")
        assert store.to_simulate(["a", "b", "c", "d"]) == ["a", "b"]
        store.add_simulation("a", t, OPEN_TO_CLOSE.name, None, t, {"why": "test"})
        assert store.to_simulate(["a", "b"]) == ["b"], "a probation is simulated once, or kept as not"
    finally:
        store.close()


class Flaky(AgainstTheNight):
    """Learns nothing on the nights ``fails`` names (the last day of their samples)."""

    def __init__(self, name: str, fails: set[date] | None = None) -> None:
        super().__init__(name)
        self.fails = fails

    def fit(self, samples: Sequence[DailySample]) -> Model:
        last = max(s.day for s in samples)
        if self.fails is None or last in self.fails:
            raise NothingToLearn(f"nothing on {last}")
        return super().fit(samples)


def test_a_night_that_learns_nothing_keeps_the_model_before_it_as_live(tmp_path: Path) -> None:
    """#298 review: live, a failed night leaves the night before's model installed; so does the
    replay, and every simulated day is predicted by a model of the kind's answers."""
    s = settings(tmp_path, signal=0.0, seed=5)
    probe = AgainstTheNight("probe")
    enroll(s, probe.name)
    [first] = simulate_daily(s, algorithms=[probe], now=NIGHT).simulations
    nights = probe.fitted  # the night before each simulated day
    flaky = Flaky("flaky", fails={nights[4]})
    enroll(s, flaky.name)

    [sim] = simulate_daily(s, algorithms=[flaky], now=NIGHT).simulations

    models = sim.training["models"]
    assert models[first.days[4].isoformat()] == models[first.days[3].isoformat()], "day 5: day 4's model"
    assert models[first.days[5].isoformat()] == f"flaky-{nights[5]:%Y%m%d}"
    store = store_of(s)
    try:
        targets = store._db.execute(
            "SELECT DISTINCT model_target FROM sim_kind_predictions WHERE algorithm = 'flaky'"
        ).fetchall()
    finally:
        store.close()
    assert [tuple(t) for t in targets] == [(1,)], "no day by a target-less default"


def test_one_whose_first_night_learns_nothing_is_not_simulated(tmp_path: Path) -> None:
    s = settings(tmp_path, signal=0.0, seed=5)
    enroll(s, "barren")

    [sim] = simulate_daily(s, algorithms=[Flaky("barren")], now=NIGHT).simulations

    assert sim.days == [] and sim.why is not None and sim.why.startswith("nothing to learn before")
    store = store_of(s)
    try:
        assert store.statuses()["barren"].status is Status.PROBATION
        assert store.to_simulate(["barren"]) == [], "kept as not simulated: its probation goes on live"
        assert store.simulated(OPEN_TO_CLOSE.name, 1) == {}
    finally:
        store.close()


async def test_the_nightly_facts_read_the_engine_s_database_on_its_thread(tmp_path: Path) -> None:
    """#298 review: the scheduler reads the facts in the collector's event loop; a read of the
    engine's database in a worker thread would raise (its connection belongs to the loop's thread),
    and every fact would be lost for the night. A daily probation waiting is among them."""
    from krx_quant.scheduled import read_facts

    s = settings(tmp_path, signal=0.0, seed=5)
    against = AgainstTheNight()
    enroll(s, against.name)
    clock = FakeClock(NIGHT)
    e = engine_on(s, clock, [against])

    facts = await read_facts(s, CALENDAR, clock, e)

    assert facts.simulating_daily == (against.name,)
    e.store.close()
