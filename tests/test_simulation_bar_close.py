"""Simulated probation of a bar-close algorithm (#298): the latest recorded days (10 to 20)
replayed as if it had been on probation, trained once before them, judged at once.

Input: recorded days of a market that swings up and down every four minutes (as the
pre-validation tests record them), backfilled one-minute bars of the days before, stand-in
algorithms, and the engine's database. Output: the simulation's predictions, apart from the live
ones, the status the review at once leaves, the clone check on the simulated days, the restart on
a new target, the engine's stage, and the scheduled job.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from krx_quant import simulation
from krx_quant.backfill import BAR_SCHEMA, MINUTE_BARS_TR, bars_key
from krx_quant.cli import main
from krx_quant.clock import FakeClock
from krx_quant.config import Settings
from krx_quant.engine import PredictionEngine
from krx_quant.feeds import KST
from krx_quant.kinds import DIRECTION_KIND
from krx_quant.lifecycle import REGISTERED, Status, target_changed
from krx_quant.market_calendar import MarketCalendar
from krx_quant.predictions import DB_NAME, PredictionStore
from krx_quant.scheduled import FAILED, JOBS, OK, Facts, JobRecord, Night
from krx_quant.simulation import BAR_CLOSE_DAYS, SimulationRun, report, simulate_bar_close
from krx_quant.touch import TARGET_VERSION
from tests.algorithm_doubles import FixedAlgorithm, spend
from tests.test_prevalidation import Contrarian, swings
from tests.test_replay_cli import record

SYMBOL = "005930"
ALPHA = FixedAlgorithm("alpha", features=("velocity",), unique=("velocity",))


def weekdays(first: date, n: int) -> list[date]:
    days: list[date] = []
    d = first
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


# Two backfilled days, then the recorded ones from Monday 14 September 2026.
BACKFILLED = [date(2026, 9, 10), date(2026, 9, 11)]
RECORDED = weekdays(date(2026, 9, 14), 22)
# A simulation replays ten recorded days at least (``simulation.MIN_DAYS``), each a whole session:
# 10-15 s on a laptop, and past the 30 s default (``pyproject.toml``) on a loaded CI runner (#327).
TEN_DAYS = pytest.mark.timeout(120)


def backfill(data: Path, day: date) -> None:
    """One-minute bars of ``day`` from 09:00 to 10:30, swinging four ticks up and down every eight
    minutes, as the recorded days do."""
    rows = []
    start = datetime.combine(day, time(9), KST)
    for i in range(91):
        phase = (i / 8) % 1
        level = 4 * (4 * phase - 1 if phase < 0.5 else 3 - 4 * phase)
        close = 81_300 + 100 * round(level)
        rows.append(
            {
                "symbol": SYMBOL,
                "bar_time": start + timedelta(minutes=i),
                "venue": "UN",
                "open": close,
                "high": close + 100,
                "low": close - 100,
                "close": close,
                "volume": 1_000,
                "cum_value": 0,
            }
        )
    path = data / bars_key(day, MINUTE_BARS_TR, SYMBOL)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=BAR_SCHEMA), path)


def settings(
    tmp_path: Path, recorded: Sequence[date], backfilled: Sequence[date] = BACKFILLED, end: str = "10:30:00"
) -> Settings:
    """Recorded days of swings until ``end`` (a short morning keeps a long replay quick), and
    ``backfilled`` days before them."""
    data = tmp_path / "data"
    for day in backfilled:
        backfill(data, day)
    for day in recorded:
        record(data, swings(day, end=end), day=day)
    return Settings(
        kis_app_key="",
        kis_app_secret="",
        symbols=(SYMBOL,),
        etf_symbols=(),
        index_codes=(),
        us_symbols=(),
        data_dir=data,
        state_dir=tmp_path / "state",
        min_sample=20,
    )


def night_after(days: Sequence[date]) -> datetime:
    """The night of the last of ``days``: its recording archived, the scheduled jobs running."""
    return datetime.combine(days[-1], time(23), KST)


def on_probation(s: Settings, *names: str, reason: str = REGISTERED, at: datetime | None = None) -> datetime:
    """``names`` on probation since ``at`` (default: the morning before the last recorded day)."""
    at = at or datetime.combine(date(2026, 9, 14), time(8), KST)
    store = PredictionStore(s.state_dir / DB_NAME)
    try:
        for name in names:
            store.add_status(name, at, None, Status.PROBATION, reason)
    finally:
        store.close()
    return at


def statuses(s: Settings) -> dict[str, Any]:
    store = PredictionStore(s.state_dir / DB_NAME)
    try:
        return {name: store.status_history(name) for name in store.statuses()}
    finally:
        store.close()


@TEN_DAYS
async def test_ten_recorded_days_are_simulated_trained_once_before_them_and_judged_on_day_10(
    tmp_path: Path,
) -> None:
    days = RECORDED[:10]
    s = settings(tmp_path, days)
    contrarian = Contrarian("contrarian")
    on_probation(s, "contrarian")

    run = await simulate_bar_close(
        s, algorithms=[ALPHA, contrarian], kind_algorithms=[], now=night_after(days)
    )

    assert contrarian.fitted == [BACKFILLED], "trained once, on its two days before the first replayed"
    [sim] = run.simulations
    assert (sim.days, run.recorded_days) == (days, 10)
    assert sim.training["cadence"] == "bar_close" and sim.training["last_day"] == BACKFILLED[-1].isoformat()
    assert sim.training["days"] == {"bars": 2}
    assert sim.verdict is not None and sim.verdict.review_day == 10 and sim.verdict.enough
    assert sim.status is Status.ACTIVE, sim.verdict
    [entered, changed] = statuses(s)["contrarian"]
    assert changed["simulated"] is True
    assert changed["reason"].startswith(f"simulated probation {days[0]}..{days[-1]} (10 days): 10 days on")
    store = PredictionStore(s.state_dir / DB_NAME)
    try:
        assert store.scored_predictions(days, None, True) == [], "nothing in the live tables"
        rows = store.sim_scored(1, days, None, False)
        assert {r["algorithm"] for r in rows} == {
            "contrarian",
            "baseline-flat",
            "baseline-persist",
            "baseline-random",
            "baseline-climate",
        }, "alpha was not on a probation that waits: not replayed"
        assert {r["model_version"] for r in rows if r["algorithm"] == "contrarian"} == {sim.training["model"]}
        assert {r["model_target"] for r in rows if r["algorithm"] == "contrarian"} == {TARGET_VERSION}
        assert store.to_simulate(["contrarian"]) == []
    finally:
        store.close()
    lines = report(run, s.review_confidence)
    assert any(f"trained once before them ({sim.training['model']}" in line for line in lines)
    assert any("review of day 10: passed, active from now" in line for line in lines)


# Twenty days replayed (two runs of ten): about 11 s on a laptop, twice that on CI's runners,
# near the 30 s default (``pyproject.toml``).
@pytest.mark.timeout(120)
async def test_twenty_of_twenty_two_recorded_days_are_simulated_and_judged_on_day_20(tmp_path: Path) -> None:
    s = settings(tmp_path, RECORDED, end="09:40:00")
    contrarian = Contrarian("contrarian")
    on_probation(s, "contrarian")

    run = await simulate_bar_close(s, algorithms=[contrarian], kind_algorithms=[], now=night_after(RECORDED))

    [sim] = run.simulations
    assert len(sim.days) == BAR_CLOSE_DAYS and sim.days == RECORDED[2:]
    assert contrarian.fitted == [RECORDED[:2]], "once, on the two recorded days before the first replayed"
    assert sim.training["days"] == {"recorded": 2}
    assert sim.verdict is not None and sim.verdict.review_day == 20


async def test_with_fewer_than_ten_recorded_days_nothing_is_simulated_and_probation_goes_on_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    days = RECORDED[:9]
    s = settings(tmp_path, days)
    on_probation(s, "contrarian")

    run = await simulate_bar_close(
        s, algorithms=[Contrarian("contrarian")], kind_algorithms=[], now=night_after(days)
    )

    assert (run.simulations, run.recorded_days) == ([], 9)
    assert report(run, 0.983) == ["9 recorded days, fewer than 10: no simulation; the probations go on live"]
    store = PredictionStore(s.state_dir / DB_NAME)
    try:
        assert store.to_simulate(["contrarian"]) == ["contrarian"], "it still waits, for the 10th day"
        assert store.simulations() == {}
    finally:
        store.close()


async def test_one_that_learns_nothing_before_the_days_is_not_simulated_and_goes_on_live(
    tmp_path: Path,
) -> None:
    days = RECORDED[:10]
    s = settings(tmp_path, days, backfilled=[])
    on_probation(s, "contrarian")

    [sim] = (
        await simulate_bar_close(
            s, algorithms=[Contrarian("contrarian")], kind_algorithms=[], now=night_after(days)
        )
    ).simulations

    assert sim.days == [] and sim.why is not None and "nothing to learn before 2026-09-14" in sim.why
    assert statuses(s)["contrarian"][-1]["status"] == "probation"
    store = PredictionStore(s.state_dir / DB_NAME)
    try:
        assert store.to_simulate(["contrarian"]) == [], "kept as not simulated: not tried every night"
        assert store.simulated("direction", 1) == {}
    finally:
        store.close()


# Twenty days replayed (two runs of ten): about 11 s on a laptop, twice that on CI's runners,
# near the 30 s default (``pyproject.toml``).
@pytest.mark.timeout(120)
async def test_no_replayed_day_is_predicted_with_what_came_after_it(tmp_path: Path) -> None:
    """The model learns only the days before the first replayed one, and a later recorded day
    that changes leaves the earlier days' predictions as they were."""
    days = RECORDED[:10]
    first = settings(tmp_path / "a", days, end="09:40:00")
    on_probation(first, "contrarian")
    await simulate_bar_close(
        first, algorithms=[Contrarian("contrarian")], kind_algorithms=[], now=night_after(days)
    )

    other = settings(tmp_path / "b", days[:9], end="09:40:00")
    record(other.data_dir, swings(days[9], end="09:20:00"), day=days[9])  # the last day is another
    on_probation(other, "contrarian")
    await simulate_bar_close(
        other, algorithms=[Contrarian("contrarian")], kind_algorithms=[], now=night_after(days)
    )

    def outputs(s: Settings) -> dict[tuple[str, str], Any]:
        store = PredictionStore(s.state_dir / DB_NAME)
        try:
            rows = store.sim_scored(1, days[:9], None, True)
        finally:
            store.close()
        return {(r["bar_close"], r["algorithm"]): (r["p_up"], r["p_down"]) for r in rows}

    assert outputs(first) and outputs(first) == outputs(other)


@TEN_DAYS
async def test_the_clone_check_reads_the_simulated_days(tmp_path: Path) -> None:
    """Two algorithms simulated together that call alike: the later one is a clone of the earlier,
    on their simulated days."""
    days = RECORDED[:10]
    s = settings(tmp_path, days)
    on_probation(s, "contrarian", "copycat")
    copycat = Contrarian("copycat", own="vwap_gap")

    run = await simulate_bar_close(
        s, algorithms=[Contrarian("contrarian"), copycat], kind_algorithms=[], now=night_after(days)
    )

    found = {sim.algorithm: sim for sim in run.simulations}
    assert found["copycat"].status is Status.CLONE
    changed = statuses(s)["copycat"][-1]
    assert changed["reason"].startswith(f"simulated probation {days[0]}..{days[-1]} (10 days): p_up - p_down")
    assert "moves with contrarian (r = 1.000)" in changed["reason"] and changed["simulated"] is True
    assert found["contrarian"].status is Status.ACTIVE


@TEN_DAYS
async def test_a_restart_on_a_new_target_is_simulated(tmp_path: Path) -> None:
    """An algorithm active on an old target goes back on probation when the engine first opens
    the database on a new one (#187); that probation is simulated."""
    days = RECORDED[:10]
    s = settings(tmp_path, days)
    morning = datetime.combine(days[-1], time(6), KST)
    store = PredictionStore(s.state_dir / DB_NAME)
    store.add_status("contrarian", morning - timedelta(days=30), None, Status.ACTIVE, "set")
    store.close()
    contrarian = Contrarian("contrarian")
    engine = PredictionEngine(
        PredictionStore(s.state_dir / DB_NAME),
        MarketCalendar(),
        (SYMBOL,),
        s.fee_rate,
        FakeClock(morning),
        algorithms=[contrarian],
        kinds=(DIRECTION_KIND,),
        kind_algorithms=(),
        target_version=TARGET_VERSION,
    )
    standing = engine.store.statuses()["contrarian"]
    assert standing.status is Status.PROBATION and standing.reason == target_changed(
        "direction", TARGET_VERSION
    )
    assert engine.to_simulate() == (["contrarian"], [])

    [sim] = (
        await simulate_bar_close(
            s, algorithms=[Contrarian("contrarian")], kind_algorithms=[], now=night_after(days)
        )
    ).simulations
    assert sim.days == days and sim.status is Status.ACTIVE
    restarted, changed = statuses(s)["contrarian"][-2:]
    assert restarted["reason"] == target_changed("direction", TARGET_VERSION)
    assert changed["reason"].startswith("simulated probation") and changed["status"] == "active"
    assert "contrarian" not in await engine.stages(), "active: no stage"
    engine.store.close()


@TEN_DAYS
async def test_the_engine_s_stage_counts_the_simulated_days_and_the_next_review_is_day_20(
    tmp_path: Path,
) -> None:
    days = RECORDED[:10]
    s = replace(settings(tmp_path, days), min_sample=100_000)  # never enough: it stays on probation
    on_probation(s, "contrarian")
    [sim] = (
        await simulate_bar_close(
            s, algorithms=[Contrarian("contrarian")], kind_algorithms=[], now=night_after(days)
        )
    ).simulations
    assert sim.status is Status.PROBATION and sim.verdict is not None and not sim.verdict.enough
    marked = PredictionStore(s.state_dir / DB_NAME)
    marked.mark_target(TARGET_VERSION, night_after(days))  # on the target already: no restart
    marked.close()
    engine = PredictionEngine(
        PredictionStore(s.state_dir / DB_NAME),
        MarketCalendar(),
        (SYMBOL,),
        s.fee_rate,
        FakeClock(night_after(days) + timedelta(days=3)),
        algorithms=[Contrarian("contrarian")],
        kinds=(DIRECTION_KIND,),
        kind_algorithms=(),
        target_version=TARGET_VERSION,
        min_sample=s.min_sample,
    )
    stage = (await engine.stages())["contrarian"]
    assert (stage.days, stage.simulated, stage.next_review, stage.counted_from) == (10, 10, 20, None)
    engine.store.close()


def night_of(day: date, record: JobRecord | None, **facts: Any) -> Night:
    return Night(
        day, True, "abc", record, {} if record is None else {"simulate-bar-close-298": record}, Facts(**facts)
    )


def test_the_scheduled_job_waits_for_ten_recorded_days_and_runs_before_the_pre_validations() -> None:
    [job] = [j for j in JOBS if j.name == "simulate-bar-close-298"]
    assert job.command == ("simulate", "bar-close")
    assert {t.issue for t in job.targets} == {298, 204}
    assert job.timeout == timedelta(hours=5)
    names = [j.name for j in JOBS]
    assert names.index("simulate-daily-298") < names.index("simulate-bar-close-298")
    assert names.index("simulate-bar-close-298") < names.index("prevalidate-mandelbrot")
    day = date(2026, 10, 22)
    start = datetime.combine(day, time(23), KST)
    waiting = ("newton", "gauss")
    assert not job.due(night_of(day, None, recorded_days=9, simulating=waiting)), "9 recorded days"
    assert job.due(night_of(day, None, recorded_days=10, simulating=waiting))
    assert not job.due(night_of(day, None, recorded_days=10)), "nothing waits"
    ran = JobRecord(day - timedelta(days=1), OK, start, "abc", 0, 3600.0)
    assert job.due(night_of(day, ran, recorded_days=11, simulating=("kalman",))), "a new probation waits"
    assert not job.due(night_of(day, replace(ran, day=day), recorded_days=11, simulating=("kalman",)))
    failed = replace(ran, outcome=FAILED)
    assert not job.due(night_of(day, failed, recorded_days=11, simulating=waiting)), "failed on this code"
    assert job.due(night_of(day, replace(failed, commit="older"), recorded_days=11, simulating=waiting))
    cut = replace(ran, outcome="interrupted")
    assert job.due(night_of(day, cut, recorded_days=11, simulating=waiting)), "a restart cut it"


def test_the_command_simulates_the_bar_close_probations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    days = RECORDED[:9]
    s = settings(tmp_path, days)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_DATA_DIR", str(s.data_dir))
    monkeypatch.setenv("KRX_STATE_DIR", str(s.state_dir))
    monkeypatch.setenv("KRX_SYMBOLS", SYMBOL)
    on_probation(s, "newton")
    assert main(["simulate", "bar-close"]) == 0
    out = capsys.readouterr().out
    assert out == (
        "simulated probation (#298), bar-close:\n"
        "9 recorded days, fewer than 10: no simulation; the probations go on live\n"
    )


def test_nothing_waiting_on_ten_recorded_days_says_so() -> None:
    assert report(SimulationRun(recorded_days=12), 0.983) == ["no probation waits for its simulation"]


@TEN_DAYS
async def test_a_kind_s_algorithm_is_simulated_on_its_kind_s_answers_against_its_baselines(
    tmp_path: Path,
) -> None:
    from tests.kind_doubles import DRIFT, TEST_KIND, DriftFrequency

    days = RECORDED[:10]
    s = settings(tmp_path, days)
    on_probation(s, "frequency")
    frequency = DriftFrequency("frequency", "velocity")

    [sim] = (
        await simulate_bar_close(
            s, algorithms=[], kind_algorithms=[frequency], kinds=[TEST_KIND], now=night_after(days)
        )
    ).simulations

    assert (sim.kind, sim.days) == (DRIFT, days) and frequency.fitted_on and frequency.fitted_on[0] > 0
    assert len(frequency.fitted_on) == 1, "trained once"
    assert sim.verdict is not None and sim.verdict.review_day == 10
    store = PredictionStore(s.state_dir / DB_NAME)
    try:
        assert store.sim_predictors(1, DRIFT) == {"frequency", "drift-held", "drift-persist"}
        rows = store.sim_kind_scored(1, DRIFT, TEST_KIND.answer_version, days, None, True)
        assert rows and {r["model_target"] for r in rows if r["algorithm"] == "frequency"} == {1}
        assert {r["answer_class"] for r in rows} <= set(TEST_KIND.classes)
        assert store.kind_scored(DRIFT, 1, days, None, True) == [], "nothing in the live tables"
        assert set(store.simulated(DRIFT, 1)) == {"frequency"}
    finally:
        store.close()


async def test_the_nightly_facts_tell_the_bar_close_probations_waiting(tmp_path: Path) -> None:
    from krx_quant.scheduled import read_facts

    days = RECORDED[:10]
    s = settings(tmp_path, days)
    on_probation(s, "contrarian")
    marked = PredictionStore(s.state_dir / DB_NAME)
    marked.mark_target(TARGET_VERSION, night_after(days))
    marked.close()
    clock = FakeClock(night_after(days))
    engine = PredictionEngine(
        PredictionStore(s.state_dir / DB_NAME),
        MarketCalendar(),
        (SYMBOL,),
        s.fee_rate,
        clock,
        algorithms=[Contrarian("contrarian")],
        kinds=(DIRECTION_KIND,),
        kind_algorithms=(),
        target_version=TARGET_VERSION,
    )

    facts = await read_facts(s, MarketCalendar(), clock, engine)

    assert (facts.simulating, facts.simulating_daily, facts.recorded_days) == (("contrarian",), (), 10)
    engine.store.close()


class SlowContrarian(Contrarian):
    """The contrarian, ``seconds`` a bar close (``spend``): a cost the CPU budget counts."""

    def __init__(self, name: str, seconds: float) -> None:
        super().__init__(name)
        self.seconds = seconds

    def predict(self, features: Any, model: Any) -> Any:
        spend(self.seconds)
        return super().predict(features, model)


async def test_the_replay_shares_the_cpu_budget_with_the_live_algorithms_as_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixed_costs: None
) -> None:
    """#300 review: live, an active algorithm registered before a candidate takes its share of the
    CPU budget, and the candidate, registered last, drops out when the two are over it. The replay
    runs that algorithm beside it (its predictions not kept), so the candidate drops out there too;
    with it retired, the candidate runs at every bar close."""
    monkeypatch.setattr(simulation, "MIN_DAYS", 1)  # one replayed day keeps the timing test short
    days = RECORDED[:1]

    async def predicted(root: Path, heavy_status: Status) -> tuple[int, int]:
        s = replace(settings(root, days), algorithm_seconds=1.0, cpu_budget_seconds=0.0055)
        store = PredictionStore(s.state_dir / DB_NAME)
        store.add_status("heavy", night_after(days) - timedelta(days=30), None, heavy_status, "set")
        store.close()
        on_probation(s, "candidate")
        heavy = FixedAlgorithm("heavy", features=("velocity",), unique=("velocity",), seconds=0.004)
        await simulate_bar_close(
            s,
            algorithms=[heavy, SlowContrarian("candidate", 0.003)],
            kind_algorithms=[],
            now=night_after(days),
        )
        replay = PredictionStore(s.state_dir / "simulation-bar-close.sqlite3")
        kept = PredictionStore(s.state_dir / DB_NAME)
        try:
            skipped = replay.runs_by_algorithm(
                "cpu_budget", night_after(days) - timedelta(days=2), night_after(days)
            )
            rows = kept._db.execute(
                "SELECT algorithm, COUNT(*) FROM sim_predictions GROUP BY algorithm"
            ).fetchall()
        finally:
            replay.close()
            kept.close()
        counted = {r[0]: r[1] for r in rows}
        assert "heavy" not in counted, "beside the candidate for the budget only: not kept"
        return counted.get("candidate", 0), skipped.get("candidate", 0)

    shared, skipped = await predicted(tmp_path / "shared", Status.ACTIVE)
    alone, never = await predicted(tmp_path / "alone", Status.RETIRED)
    assert skipped > 0 and never == 0
    assert shared < 0.8 * alone, (shared, alone)
