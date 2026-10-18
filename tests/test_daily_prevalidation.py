"""The daily kinds' algorithms (#264): Cox's walk-forward pre-validation over the KRX days, the
engine's gate, its night, its scheduled job, its commands and its 08:50 forecast, on made-up
histories."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from krx_quant.algorithms import DAILY_ALGORITHMS, Model, save_model
from krx_quant.algorithms.cox import FEATURES, Cox
from krx_quant.cli import main
from krx_quant.clock import FakeClock
from krx_quant.config import Settings
from krx_quant.daily import StoredDays
from krx_quant.daily_features import (
    DAILY_FEATURES,
    DailyFiles,
    LiveFeatures,
    input_versions,
    pending_inputs,
    samples,
)
from krx_quant.daily_training import WALK_MIN_DAYS, DailyTrainer, prevalidate_daily
from krx_quant.engine import PredictionEngine
from krx_quant.feeds.base import KST
from krx_quant.kinds import DIRECTION_KIND, KindForecast
from krx_quant.lifecycle import Status
from krx_quant.market_calendar import MarketCalendar
from krx_quant.nightly import Night
from krx_quant.open_to_close import OPEN_TO_CLOSE
from krx_quant.predictions import PredictionStore
from krx_quant.prevalidation import admits, code_digest, history_path, last_report, report_path
from krx_quant.replay import replay_into
from krx_quant.scheduled import (
    FAILED,
    JOBS,
    OK,
    Facts,
    JobRecord,
    prevalidation_job_name,
)
from krx_quant.scheduled import (
    Night as JobNight,
)
from tests.daily_doubles import SYMBOLS, made_history

FEE = 0.015 / 100
LAST = date(2026, 10, 16)
NOW = datetime(2026, 10, 16, 22, 30, tzinfo=KST)
DAYS = 1_300  # 550 days out of sample: above WALK_MIN_DAYS


def settings(
    tmp_path: Path, signal: float, days: int = DAYS, seed: int = 264, krx_history: bool = True
) -> Settings:
    made_history(
        tmp_path / "data", tmp_path / "state", LAST, days, signal=signal, seed=seed, krx_history=krx_history
    )
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


def test_a_model_that_reads_the_night_passes_the_walk_forward_and_the_gate_lets_it_in(tmp_path: Path) -> None:
    s = settings(tmp_path, signal=0.6)
    cox = DAILY_ALGORITHMS[0]
    assert not admits(s.state_dir)(cox), "no report yet"

    result = prevalidate_daily(s, "cox", now=NOW)

    assert result.passed, result.checks
    checks = {c.name: c for c in result.checks}
    assert set(checks) == {"days", "features", "budget", "correlation", "stats"}
    assert len(result.days) >= WALK_MIN_DAYS
    assert "run 1 on these days, by trading day at 95.00%" in checks["stats"].detail
    report = json.loads(report_path(s.state_dir, "cox").read_text())
    assert (report["kind"], report["target_version"], report["passed"], report["db"]) == (
        "open_to_close",
        1,
        True,
        None,
    )
    assert report["walk_forward"]["folds"] == len(report["models"])
    assert set(report["walk_forward"]["by_symbol"]) == set(SYMBOLS)
    assert set(report["stats"]["lift"]["vs"]) == {"otc-flat", "otc-climate", "otc-persist"}
    assert set(report["stats"]["extra"]) == {"n", "pnl", "long", "short", "pnl_inverse", "short_inverse"}
    # The inputs it was judged on (#297): each feature's version, and how many samples had it.
    assert report["inputs"] == input_versions(FEATURES)
    assert report["inputs"]["foreign_net"] == report["inputs"]["institution_net"] == 2
    walk = report["walk_forward"]
    assert set(walk["present"]) == set(FEATURES) and walk["samples"] >= max(walk["present"].values())
    assert walk["present"]["foreign_net"] > 0 and walk["present"]["weekday"] == walk["samples"]
    assert admits(s.state_dir)(cox), "passed with the code it runs, on its kind's answer version"
    found = last_report(s.state_dir, cox)
    assert found is not None and found.judged and found.current

    again = prevalidate_daily(s, "cox", now=NOW + timedelta(days=1))
    assert (again.earlier_runs, again.earlier_on_these_days) == (1, 1)
    assert (
        "run 2 on these days, by trading day at 97.50%" in {c.name: c for c in again.checks}["stats"].detail
    )
    assert len(history_path(s.state_dir, "cox").read_text().splitlines()) == 2


def test_a_report_on_older_inputs_waits_for_a_new_run_and_the_job_runs_it(tmp_path: Path) -> None:
    """A report from before the flows' version 2 (#297: KRX's net buys before NXT opened) has no
    ``inputs``: it was on version 1 of each. Cox reads the flows, so the gate waits for a new
    pre-validation, and the scheduled job runs it on the next night; a report on the versions
    Cox's features have now lets it in."""
    state = tmp_path / "state"
    cox = DAILY_ALGORITHMS[0]
    report = {
        "algorithm": "cox",
        "code": code_digest(cox),
        "prevalidated_at": NOW.isoformat(),
        "passed": True,
        "days": [LAST.isoformat()] * 600,
        "target_version": OPEN_TO_CLOSE.answer_version,
        "kind": "open_to_close",
    }
    path = report_path(state, "cox")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(report))

    assert not admits(state)(cox), "judged on the flows' version 1"
    found = last_report(state, cox)
    assert found is not None and found.passed and not found.current and not found.judged
    [job] = [j for j in JOBS if j.name == "prevalidate-cox"]
    ran = JobRecord(LAST, OK, NOW - timedelta(minutes=30), "abc", 0, 600.0)
    assert job.due(job_night(LAST + timedelta(days=1), ("cox",), found, ran)), "on the same commit too"

    path.write_text(json.dumps({**report, "inputs": input_versions(FEATURES)}))
    assert admits(state)(cox)
    found = last_report(state, cox)
    assert found is not None and found.current and found.judged
    assert not job.due(job_night(LAST + timedelta(days=1), ("cox",), found, ran))
    # A version 1 feature in the report is as good as none listed: only raised ones count.
    path.write_text(json.dumps({**report, "inputs": {"us_sox": 1, "foreign_net": 2, "institution_net": 2}}))
    assert admits(state)(cox)


def test_a_run_before_the_krx_history_is_stored_records_the_old_version_and_the_job_waits(
    tmp_path: Path,
) -> None:
    """#297 review: if the keeper has not stored KRX's net buys before NXT (both of the day's
    attempts failed), a pre-validation reads the old flows, so its report says version 1, never
    the registry's 2, and the gate does not take it. The scheduled job waits for the history;
    once the keeper has stored it, a new run records version 2 and is current."""
    s = settings(tmp_path, signal=0.6, days=900, krx_history=False)
    cox = DAILY_ALGORITHMS[0]
    assert pending_inputs(FEATURES, s.state_dir, s.symbols) == ["foreign_net", "institution_net"]

    prevalidate_daily(s, "cox", now=NOW)

    report = json.loads(report_path(s.state_dir, "cox").read_text())
    assert (report["inputs"]["foreign_net"], report["inputs"]["institution_net"]) == (1, 1)
    found = last_report(s.state_dir, cox)
    assert found is not None and not found.current and not found.judged
    assert not admits(s.state_dir)(cox)
    [job] = [j for j in JOBS if j.name == "prevalidate-cox"]
    ran = JobRecord(LAST, OK, NOW - timedelta(minutes=30), "abc", 1, 60.0)
    pending = ("foreign_net", "institution_net")
    night = JobNight(
        LAST + timedelta(days=1), True, "abc", ran, {job.name: ran}, Facts(0, ("cox",), 0, {"cox": found})
    )
    assert job.due(night), "its data is in: it runs"
    assert not job.due(replace(night, facts=replace(night.facts, pending_inputs=pending))), "it waits"

    # The keeper's later run stores the history: a new run is on version 2.
    made_history(s.data_dir, s.state_dir, LAST, 900, signal=0.6, seed=264)
    assert pending_inputs(FEATURES, s.state_dir, s.symbols) == []
    prevalidate_daily(s, "cox", now=NOW + timedelta(days=1))
    report = json.loads(report_path(s.state_dir, "cox").read_text())
    assert report["inputs"] == input_versions(FEATURES)
    found = last_report(s.state_dir, cox)
    assert found is not None and found.current


def test_a_model_on_noise_is_refused_on_its_lift_by_trading_day(tmp_path: Path) -> None:
    s = settings(tmp_path, signal=0.0, seed=7)
    result = prevalidate_daily(s, "cox", now=NOW)
    stats = {c.name: c for c in result.checks}["stats"]
    assert not result.passed and not stats.passed, stats.detail
    assert not admits(s.state_dir)(DAILY_ALGORITHMS[0])


def test_too_short_a_history_is_refused_on_its_days(tmp_path: Path) -> None:
    s = settings(tmp_path, signal=0.6, days=900)
    result = prevalidate_daily(s, "cox", now=NOW)
    assert not {c.name: c for c in result.checks}["days"].passed
    with pytest.raises(ValueError, match="unknown daily algorithm"):
        prevalidate_daily(s, "nobody", now=NOW)


def test_the_night_refits_cox_on_the_krx_days_and_always_promotes_it(tmp_path: Path) -> None:
    s = settings(tmp_path, signal=0.6, days=900)
    trainer = DailyTrainer(DailyFiles(s.data_dir, s.state_dir), SYMBOLS, FEE)
    report, model = trainer.train(Cox(), OPEN_TO_CLOSE, LAST)
    assert model is not None and model.trained_until == LAST
    assert (report["kind"], report["cadence"], report["promoted"], report["target_version"]) == (
        "open_to_close",
        "daily",
        True,
        1,
    )
    assert report["challenger"]["version"] == model.version
    assert report["training_days"][1] == LAST.isoformat()
    assert report["recent"]["days"] == 60 and report["recent"]["loss"] < 1.0986
    line = Night(LAST, "2026-10-16T21:40:00+09:00", {"cox": model.version}, [report]).summary()
    assert "open_to_close 정답 표본: KRX 일봉" in line and "최근 60일 표본 밖 log loss" in line
    assert "- cox 승격: refitted on the KRX days up to 2026-10-16" in line


def job_night(
    day: date, waiting: tuple[str, ...], report: object = None, record: JobRecord | None = None
) -> JobNight:
    reports = {} if report is None else {"cox": report}
    records = {} if record is None else {prevalidation_job_name("cox"): record}
    return JobNight(day, True, "abc", record, records, Facts(0, waiting, 0, reports))  # type: ignore[arg-type]


def test_the_scheduled_walk_forward_does_not_wait_for_recorded_days_nor_its_turn() -> None:
    [job] = [j for j in JOBS if j.name == "prevalidate-cox"]
    assert job.command == ("prevalidate", "cox")
    assert {t.issue for t in job.targets} == {204, 264}
    names = [j.name for j in JOBS]
    assert names.index("prevalidate-cox") < names.index("prevalidate-mandelbrot")
    day = date(2026, 10, 19)
    assert job.due(job_night(day, ("cox",))), "no recorded day needed: waiting, never ran"
    assert not job.due(job_night(day, ())), "not waiting: running already"
    ran = JobRecord(day - timedelta(days=1), FAILED, NOW, "abc", 1, 9.0)
    assert not job.due(job_night(day, ("cox",), record=ran)), "ran on this code without a verdict"
    assert job.due(job_night(day, ("cox",), record=JobRecord(day, OK, NOW, "older", 1, 9.0))), "new code"


async def test_the_engine_forecasts_cox_at_0850_once_it_passed(tmp_path: Path) -> None:
    s = settings(tmp_path, signal=0.6, days=900)
    monday = date(2026, 10, 19)
    trainer = DailyTrainer(DailyFiles(s.data_dir, s.state_dir), SYMBOLS, FEE)
    _, model = trainer.train(Cox(), OPEN_TO_CLOSE, LAST)
    assert model is not None
    calendar = MarketCalendar()
    clock = FakeClock(datetime.combine(monday, time(7, 20), KST))
    engine = PredictionEngine(
        PredictionStore(tmp_path / "e.sqlite3"),
        calendar,
        SYMBOLS,
        FEE,
        clock,
        algorithms=(),
        kinds=(DIRECTION_KIND,),
        kind_algorithms=(),
        models={"cox": model},
        daily=StoredDays(s.data_dir),
        daily_algorithms=DAILY_ALGORITHMS,
        daily_features=LiveFeatures(OPEN_TO_CLOSE, s.data_dir, s.state_dir, calendar, SYMBOLS, FEE),
        daily_feature_names=DAILY_FEATURES,
    )
    assert engine.store.statuses()["cox"].status is Status.PROBATION
    await replay_into(engine, clock, [], until=datetime.combine(monday, time(8, 51), KST))
    rows = [r for symbol in SYMBOLS for r in engine.store.kind_recent("open_to_close", 1, symbol, 5, "cox")]
    assert len(rows) == 3 and {r["status"] for r in rows} == {"ok"}
    assert {r["model_version"] for r in rows} == {model.version}
    for r in rows:
        assert OPEN_TO_CLOSE.invalid(KindForecast(OPEN_TO_CLOSE.probabilities(r["output"]))) is None


def test_the_commands_fit_and_prevalidate_cox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = settings(tmp_path, signal=0.6)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_DATA_DIR", str(s.data_dir))
    monkeypatch.setenv("KRX_STATE_DIR", str(s.state_dir))
    monkeypatch.setenv("KRX_SYMBOLS", ",".join(SYMBOLS))
    assert main(["fit", "cox"]) == 0
    saved = Model.from_json((s.state_dir / "models" / "cox.json").read_text())
    assert saved.version.startswith("cox-20261016-")
    assert main(["prevalidate", "cox"]) == 0
    assert json.loads(report_path(s.state_dir, "cox").read_text())["passed"] is True
    save_model(s.state_dir, "cox", saved)
    assert samples(OPEN_TO_CLOSE, DailyFiles(s.data_dir, s.state_dir), SYMBOLS, FEATURES, FEE, first=LAST)


def test_the_night_trains_cox_last_and_puts_it_on_the_promotion_list(tmp_path: Path) -> None:
    from krx_quant.nightly import train_night
    from krx_quant.object_store import LocalObjectStore
    from tests.test_nightly import DAYS as NIGHT_DAYS
    from tests.test_nightly import FINISHED, archive_bars

    root = tmp_path / "store"
    for d in NIGHT_DAYS:
        archive_bars(root, d)
    night_day = NIGHT_DAYS[-1]
    made_history(tmp_path / "data", tmp_path / "state", night_day, 900, signal=0.6)
    night = train_night(
        LocalObjectStore(root),
        tmp_path / "cache",
        symbols=("005930",),
        fee_rate=FEE,
        algorithms=[],
        kind_algorithms=[],
        kinds=(DIRECTION_KIND,),
        now=lambda: FINISHED,
        daily=DailyTrainer(DailyFiles(tmp_path / "data", tmp_path / "state"), SYMBOLS, FEE),
    )
    assert list(night.promoted) == ["cox"]
    [report] = [r for r in night.reports if r["algorithm"] == "cox"]
    assert (report["cadence"], report["day"]) == ("daily", night_day.isoformat())
    stored = json.loads((root / f"models/cox/{night.promoted['cox']}.json").read_text())
    assert stored["target_version"] == 1
