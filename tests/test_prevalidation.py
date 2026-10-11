"""Pre-validation: a new algorithm replayed on the latest recorded days before it runs live.

Input: recorded days, as the collector writes them, of a market that swings up and down
every four minutes, and a registry of stand-in algorithms. Output: the report
(``state/prevalidations/<name>.json``), and what the engine lets run.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant.algorithms import Algorithm, Card, Model, NothingToLearn, RegistrationError, Sample, save_model
from krx_quant.baselines import Forecast
from krx_quant.cli import main
from krx_quant.clock import FakeClock
from krx_quant.config import Settings
from krx_quant.engine import Event, PredictionEngine
from krx_quant.features import FeatureVector
from krx_quant.feeds import KST
from krx_quant.lifecycle import Status
from krx_quant.market_calendar import MarketCalendar
from krx_quant.model_shelf import Shelf
from krx_quant.predictions import DB_NAME, PredictionStore
from krx_quant.prevalidation import admits, code_digest, prevalidate, report_path
from krx_quant.replay import replay_into
from krx_quant.swap import reload_models
from tests.algorithm_doubles import FixedAlgorithm
from tests.replay_events import api, at, book, get_json, run, trade
from tests.test_lifecycle import Outbox
from tests.test_replay import busy_morning
from tests.test_replay_cli import record

# Ten trading days, the fewest that can pass: Monday 14 to Friday 25 September.
DAYS = [d for d in (date(2026, 9, 14) + timedelta(days=i) for i in range(12)) if d.weekday() < 5]
AFTER = datetime.combine(date(2026, 9, 28), time(8), KST)  # the next Monday morning
ALPHA = FixedAlgorithm("alpha", features=("velocity",), unique=("velocity",))


def swings(day: date, end: str = "10:30:00") -> list[Event]:
    """A mid-point four ticks up and down every eight minutes: what went up for four
    minutes comes down the next four. A trade at the ask every ten seconds.
    """
    events: list[Event] = []
    start = when = at("09:00:00", day)
    while when <= at(end, day):
        phase = (when - start) / timedelta(minutes=8) % 1
        level = 4 * (4 * phase - 1 if phase < 0.5 else 3 - 4 * phase)
        mid = 81_250 + 100 * round(level)
        events += [book(when, mid - 50, mid + 50), trade(when + timedelta(seconds=1), mid + 50, 3)]
        when += timedelta(seconds=10)
    return events


class Contrarian:
    """Calls the last ``lag`` minutes undone: right on a market that swings back. Its own
    feature is ``own``; it reads the minutes' moves whatever it is."""

    training_days = 2

    def __init__(self, name: str, own: str = "minute_ticks", lag: int = 4) -> None:
        self.name = name
        self.features: tuple[str, ...] = tuple(dict.fromkeys(("minute_ticks", own)))
        self.card = Card(f"Test {name}", "test", "the last minutes undone", (own,), "nothing")
        self._lag = lag
        self.fitted: list[list[date]] = []  # the days each fit trained on

    def default_model(self) -> Model:
        return Model(f"{self.name}-default", {})

    def fit(self, samples: Sequence[Sample]) -> Model:
        self.fitted.append(sorted({s.bar_close.astimezone(KST).date() for s in samples}))
        return Model.fitted(self.name, {}, [s for s in samples if s.outcome is not None], 0.0)

    def predict(self, features: FeatureVector, model: Model) -> Forecast:
        change = sum(features.series("minute_ticks")[-self._lag :])
        if change >= 2:
            return Forecast(0.1, 0.2, 0.7, -change)
        if change <= -2:
            return Forecast(0.7, 0.2, 0.1, -change)
        return Forecast(0.2, 0.6, 0.2, 0.0)


def settings(tmp_path: Path, days: Sequence[date] = DAYS) -> Settings:
    data = tmp_path / "data"
    for day in days:
        record(data, swings(day), day=day)
    return Settings(
        kis_app_key="",
        kis_app_secret="",
        symbols=("005930",),
        etf_symbols=(),
        index_codes=(),
        us_symbols=(),
        data_dir=data,
        state_dir=tmp_path / "state",
        min_sample=20,
    )


def report(s: Settings, name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((s.state_dir / "prevalidations" / f"{name}.json").read_text())
    return data


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
    assert set(saved["stats"]["lift"]["vs"]) == {"baseline-flat", "baseline-persist", "baseline-random"}
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
    s = settings(tmp_path, DAYS[:9])

    result = await prevalidate(s, "contrarian", algorithms=[ALPHA, Contrarian("contrarian")], now=AFTER)

    assert [c.name for c in result.checks if not c.passed] == ["days"]
    assert "9 recorded days (at least 10" in result.checks[0].detail


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
    async with api(engine) as client:
        body = await get_json(client, "/v1/algorithms")
    return {a["name"]: a["status"]["status"] for a in body["algorithms"]}


async def test_a_new_algorithm_waits_for_its_prevalidation_then_starts_on_probation(tmp_path: Path) -> None:
    s = settings(tmp_path)
    registry: list[Algorithm] = [ALPHA, Contrarian("contrarian")]
    gate = admits(s.state_dir)
    live_day = date(2026, 9, 28)  # the Monday after the ten days
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
        admits=lambda a: a.name == "alpha" or gate(a),
        notify=outbox,
    )

    assert await listed(engine) == {"alpha": "probation"}
    assert engine.waiting() == ["contrarian"]
    assert not engine.store.status_counts("contrarian"), "not run"

    await prevalidate(s, "contrarian", algorithms=registry, now=AFTER)
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
        admits=gate,
    )
    assert engine.waiting() == ["contrarian"]
    path.write_text(json.dumps({"passed": True, "code": "0123456789abcdef"}))  # passed, then the code changed
    assert isinstance(engine.clock, FakeClock)
    await replay_into(engine, engine.clock, [], until=at("09:07:00"))
    assert engine.waiting() == ["contrarian"]
    path.write_text(json.dumps({"passed": True, "code": code_digest(candidate)}))
    await replay_into(engine, engine.clock, [], until=at("09:08:00"))
    assert engine.waiting() == []


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
    assert "contrarian" in alert and "krx-quant prevalidate" in alert
    await run(tmp_path / "live.db", [], until=at("15:45:00", monday), start=at("08:00:00", monday), **options)
    assert len(outbox.sent) == 2, "a restarted engine alerts again"


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

    assert set(await listed(first_five)) == {"newton", "gauss", "granger", "markov", "turing"}
    assert await listed(enrolled) == {"contrarian": "probation"}, "alpha is new: it waits"


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


def test_the_command_reports_a_registry_that_fails_the_registration_check_as_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def refused(*args: Any, **kwargs: Any) -> None:
        raise RegistrationError("twin: the same features as contrarian")

    monkeypatch.setattr("krx_quant.cli.prevalidate", refused)
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))

    assert main(["prevalidate", "twin"]) == 1
    assert "twin: the same features as contrarian" in capsys.readouterr().err


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
