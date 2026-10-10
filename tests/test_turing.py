"""Turing's own features on snapshots made by hand, and its learning on made-up samples.

Input: one-minute bars with the day's flows (program trading, foreign member firms,
investor estimates) and its calendar (a Snapshot), or samples whose outcome follows a rule
of the features. Output: the feature values, and what the fitted model forecasts.
"""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant.algorithms import Model, Sample
from krx_quant.algorithms.turing import Turing
from krx_quant.contracts import days_to_expiry
from krx_quant.engine import Event
from krx_quant.features import FEATURES, FeatureVector, Flows, Snapshot
from krx_quant.feeds import KST
from krx_quant.market_calendar import MarketCalendar
from krx_quant.pricing import Direction
from krx_quant.reference import Bar
from tests.replay_events import at, flows_morning, run
from tests.test_algorithms_replay import predictions_of
from tests.test_replay import busy_morning

TURING = Turing()
DAY = date(2026, 10, 12)  # a Monday; the October options expire on Thursday the 8th
OPEN = datetime.combine(DAY, time(9), KST)


def bar(m: int, close: float, volume: int) -> Bar:
    at = OPEN + timedelta(minutes=m)
    return Bar(at, close, close, close, close, volume, 1, at, at)


def snapshot(flows: Flows, *, minutes: int = 30, expiry_days: int | None = 3) -> Snapshot:
    bars = [bar(m, 10_000.0, 100) for m in range(minutes)]  # 100 shares, 1,000,000 won a minute
    close = OPEN + timedelta(minutes=minutes)
    return Snapshot.at(
        "005930", close, bars, 10_000.0, 0.00015, session_open=OPEN, flows=flows, expiry_days=expiry_days
    )


def value(name: str, s: Snapshot) -> float | None:
    v = FEATURES[name](s)
    assert not isinstance(v, tuple)
    return v


def test_the_flows_are_shares_of_what_traded() -> None:
    flows = Flows(
        program_now=3_000_000.0,
        program_before=2_000_000.0,  # 1,000,000 won bought by programs in the last 5 minutes
        foreign_member_now=-150.0,
        foreign_member_before=100.0,  # foreign firms sold 250 shares net
        foreign_estimate=600.0,
        institution_estimate=-300.0,
    )
    s = snapshot(flows)

    assert value("program_net", s) == pytest.approx(1_000_000 / 5_000_000)
    assert value("member_foreign_net", s) == pytest.approx(-250 / 500)
    assert value("investor_foreign", s) == pytest.approx(600 / 3_000)  # of the day's 3,000 shares
    assert value("investor_institution", s) == pytest.approx(-300 / 3_000)


def test_a_flow_not_received_is_unknown_and_one_new_today_counts_from_zero() -> None:
    s = snapshot(Flows(program_now=500_000.0))

    assert value("program_net", s) == pytest.approx(500_000 / 5_000_000)
    assert value("member_foreign_net", s) is None
    assert value("investor_foreign", s) is None


def test_the_time_of_day_and_the_trading_days_to_the_options_expiry() -> None:
    s = snapshot(Flows(), minutes=75, expiry_days=0)

    assert value("minutes_since_open", s) == 75.0
    assert value("days_to_expiry", s) == 0.0
    calendar = MarketCalendar({date(2026, 11, 11): False})  # a holiday before November's
    assert days_to_expiry(date(2026, 10, 8), calendar) == 0
    assert days_to_expiry(DAY, calendar) == 22, "to Thursday 12 November, the 11th not trading"


def features(**values: float | None) -> FeatureVector:
    known = dict.fromkeys(TURING.features) | values
    return FeatureVector(known, TURING.features)


def made_up(n: int, seed: int = 0) -> list[Sample]:
    """Programs buying (selling) push the price up (down) four minutes later; the rest is noise."""
    rng = random.Random(seed)
    out = []
    t = datetime.combine(date(2026, 9, 1), time(9, 1), KST)
    for i in range(n):
        program = rng.uniform(-1, 1)
        noise = rng.gauss(0, 0.3)
        move = 3 * program + noise
        outcome = Direction.UP if move > 1 else Direction.DOWN if move < -1 else Direction.FLAT
        day = t + timedelta(days=i // 300, minutes=i % 300)
        f = features(program_net=program, flat_ticks=1.0, volatility=rng.uniform(0.5, 2))
        out.append(Sample("005930", day, f, outcome, move))
    return out


def test_turing_learns_which_way_the_programs_push() -> None:
    model = TURING.fit(made_up(3000))

    assert model.version.startswith("turing-2026") and model.blob
    buying = TURING.predict(features(program_net=0.8, flat_ticks=1.0, volatility=1.0), model)
    selling = TURING.predict(features(program_net=-0.8, flat_ticks=1.0, volatility=1.0), model)
    still = TURING.predict(features(program_net=0.0, flat_ticks=1.0, volatility=1.0), model)
    assert buying.p_up > 0.8 and selling.p_down > 0.8 and still.p_flat > 0.6
    assert buying.expected_ticks > 1 > -1 > selling.expected_ticks
    # Saved and read back, the model forecasts the same.
    again = Model.from_json(model.to_json())
    assert TURING.predict(features(program_net=0.8, flat_ticks=1.0, volatility=1.0), again) == buying


def test_the_same_samples_give_the_same_model() -> None:
    assert TURING.fit(made_up(1000)).version == TURING.fit(made_up(1000)).version


def a_morning(end: str) -> list[Event]:
    return sorted([*busy_morning(end=end), *flows_morning(end=end)], key=lambda e: e[1]["received_at"])


def trained() -> Model:
    """Turing fitted on made-up samples where the programs push the price."""
    return TURING.fit(made_up(3000))


async def test_turing_predicts_and_is_scored_on_the_prediction_path(tmp_path: Path) -> None:
    model = trained()

    engine = await run(
        tmp_path / "engine.db", a_morning("10:00:00"), until=at("10:00:00"), models={"turing": model}
    )

    turing = await predictions_of(engine, "turing")
    assert len(turing) == 60 and {p["model_version"] for p in turing} == {model.version}
    assert {p["status"] for p in turing} == {"ok"}
    assert sum(p["outcome"] is not None for p in turing) == 56
    assert len({p["p_up"] for p in turing}) > 1, "the forecasts follow the programs"


async def test_what_comes_after_a_bar_close_never_changes_its_forecast(tmp_path: Path) -> None:
    model = trained()
    cut = at("09:40:00")
    everything = a_morning("10:00:00")
    until_then = [e for e in everything if e[1]["received_at"] < cut]

    full = await run(tmp_path / "full.db", everything, until=at("10:00:00"), models={"turing": model})
    short = await run(tmp_path / "short.db", until_then, until=at("09:40:30"), models={"turing": model})

    def at_cut(engine_predictions: list[dict[str, Any]]) -> dict[str, Any]:
        [p] = [p for p in engine_predictions if p["bar_close"] == "2026-10-12T09:40:00+09:00"]
        return {k: p[k] for k in ("p_up", "p_flat", "p_down", "expected_ticks")}

    assert at_cut(await predictions_of(full, "turing")) == at_cut(await predictions_of(short, "turing"))
