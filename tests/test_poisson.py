"""Poisson: its own features through the Replay Harness, its discretized Hawkes process on
made-up arrivals, and its predictions on the prediction path within the time budget.

Input: trades with their aggressor side, size and receipt time, or samples whose arrivals
excite each other as a Hawkes process does. Output: the feature values an algorithm sees
at each bar close, what the fitted model forecasts, what the engine stores, and how long
it took.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from krx_quant.algorithms import NothingToLearn, Sample
from krx_quant.algorithms.forecasts import UNKNOWN
from krx_quant.algorithms.poisson import ARRIVALS, EXCITATIONS, Poisson
from krx_quant.engine import Event
from krx_quant.features import SIDES, FeatureVector, Leaders
from krx_quant.feeds import KST
from krx_quant.pricing import Direction
from krx_quant.training import recorded_samples
from tests.algorithm_doubles import FixedAlgorithm
from tests.replay_events import CALENDAR, DAY, FEE, at, book, run, trade, with_sides
from tests.test_algorithms_replay import predictions_of
from tests.test_replay import busy_morning

OWN = (
    "buy_excitation_10",
    "buy_excitation_60",
    "sell_excitation_10",
    "sell_excitation_60",
    "buy_arrivals_1",
    "sell_arrivals_1",
)


async def test_each_side_s_trades_excite_by_their_size_and_fade(tmp_path: Path) -> None:
    events = [
        trade(at("08:59:50"), 81_300, 500, side="buy"),  # before the open: not the day's
        book(at("09:00:00"), 81_200, 81_300),
        trade(at("09:00:30"), 81_300, 10, side="buy"),
        trade(at("09:00:50"), 81_200, 30, side="sell"),
        trade(at("09:01:00"), 81_300, 90, side="buy"),  # at the bar close: the next minute's
    ]
    probe = FixedAlgorithm("probe", features=OWN, unique=OWN)

    await run(tmp_path / "engine.db", events, until=at("09:02:00"), algorithms=[probe])

    at_0901, at_0902 = probe.seen
    # The day's mean size before 09:01 is 20: the buy weighs 0.5, the sell 1.5.
    buy, sell = 0.5, 1.5
    assert at_0901["buy_excitation_10"] == pytest.approx(buy * math.exp(-30 / 10))
    assert at_0901["buy_excitation_60"] == pytest.approx(buy * math.exp(-30 / 60))
    assert at_0901["sell_excitation_10"] == pytest.approx(sell * math.exp(-10 / 10))
    assert at_0901["sell_excitation_60"] == pytest.approx(sell * math.exp(-10 / 60))
    assert at_0901["buy_arrivals_1"] == pytest.approx(buy) and at_0901["sell_arrivals_1"] == pytest.approx(
        sell
    )
    # By 09:02 the 09:01 buy of 90 is in: a mean of 130 / 3. Each excitation fades from its age.
    mean = 130 / 3
    assert at_0902["buy_excitation_60"] == pytest.approx((10 * math.exp(-90 / 60) + 90 * math.exp(-1)) / mean)
    assert at_0902["sell_excitation_60"] == pytest.approx(30 * math.exp(-70 / 60) / mean)
    assert at_0902["buy_arrivals_1"] == pytest.approx(90 / mean) and at_0902["sell_arrivals_1"] == 0.0


async def test_without_any_trade_s_side_the_arrivals_are_unknown(tmp_path: Path) -> None:
    events = [book(at("09:00:00"), 81_200, 81_300), trade(at("09:00:30"), 81_300, 10)]  # no side
    probe = FixedAlgorithm("probe", features=OWN, unique=OWN)

    await run(tmp_path / "engine.db", events, until=at("09:01:00"), algorithms=[probe])

    assert set(probe.seen[0].values()) == {None}


POISSON = Poisson()
# Each side's next four minutes: a baseline, set off by its own fast excitation and a little
# by the other side's; the slow excitations set off nothing.
TRUE_INTENSITIES = {
    "buy": (2.0, 1.5, 0.0, 0.3, 0.0),
    "sell": (2.0, 0.3, 0.0, 1.5, 0.0),
}  # base, then EXCITATIONS


def vector(**values: float | None) -> FeatureVector:
    return FeatureVector(dict.fromkeys(POISSON.features) | values, POISSON.features)


def made_up(n: int, seed: int = 0) -> list[Sample]:
    """Samples ten minutes apart, each with its excitations, and the four minutes after it
    carrying the arrivals that the true intensities drew (all in the fourth)."""
    rng = np.random.default_rng(seed)
    out: list[Sample] = []
    start = datetime.combine(date(2026, 9, 1), time(9, 1), KST)
    for i in range(n):
        x = rng.exponential(1.0, size=len(EXCITATIONS))
        drawn = {
            side: rng.poisson(TRUE_INTENSITIES[side][0] + float(np.dot(TRUE_INTENSITIES[side][1:], x)))
            for side in SIDES
        }
        lean = drawn["buy"] - drawn["sell"]
        outcome = Direction.UP if lean > 2 else Direction.DOWN if lean < -2 else Direction.FLAT
        t = start + timedelta(minutes=10 * i)
        out.append(
            Sample(
                "005930",
                t,
                vector(**dict(zip(EXCITATIONS, map(float, x), strict=True))),
                outcome,
                float(lean),
            )
        )
        for step in range(1, 5):
            last = {
                a: float(drawn[side]) if step == 4 else 0.0 for a, side in zip(ARRIVALS, SIDES, strict=True)
            }
            out.append(Sample("005930", t + timedelta(minutes=step), vector(**last), None, None))
    return out


def test_maximum_likelihood_finds_the_baselines_and_what_excites_each_side() -> None:
    model = POISSON.fit(made_up(3000))

    assert model.version.startswith("poisson-2026")
    for side in SIDES:
        base, *excites = TRUE_INTENSITIES[side]
        assert model.params[f"base.{side}"] == pytest.approx(base, rel=0.15)
        for excitation, true in zip(EXCITATIONS, excites, strict=True):
            learned = model.params[f"excite.{side}.{excitation}"]
            assert learned == pytest.approx(true, abs=0.2), (side, excitation)


def test_the_side_more_excited_leans_the_forecast_its_way() -> None:
    model = POISSON.fit(made_up(3000))
    quiet = dict.fromkeys(EXCITATIONS, 0.0)

    buying = POISSON.predict(vector(**quiet | {"buy_excitation_10": 4.0}), model)
    selling = POISSON.predict(vector(**quiet | {"sell_excitation_10": 4.0}), model)
    still = POISSON.predict(vector(**quiet), model)

    assert buying.p_up > 0.5 > buying.p_down and selling.p_down > 0.5 > selling.p_up
    assert still.p_up == pytest.approx(still.p_down, abs=0.1) and still.p_flat > 0.5, "the baselines, learned"
    assert buying.expected_ticks > 0 > selling.expected_ticks


def test_the_same_samples_give_the_same_model() -> None:
    assert POISSON.fit(made_up(300)).version == POISSON.fit(made_up(300)).version


def test_without_the_trades_sides_poisson_neither_learns_nor_leans() -> None:
    unsided = [replace(s, features=vector()) for s in made_up(20)]

    with pytest.raises(NothingToLearn, match="trades' sides"):
        POISSON.fit(unsided)
    assert POISSON.predict(vector(), POISSON.default_model()) == UNKNOWN


def a_sided_morning(end: str = "10:00:00") -> list[Event]:
    """The busy morning, its trades buying in runs and selling in runs."""
    return with_sides(busy_morning(end=end), run=20)


async def test_poisson_predicts_well_within_its_time_budget_and_is_scored(tmp_path: Path) -> None:
    model = POISSON.fit(
        recorded_samples(
            a_sided_morning("11:00:00"), CALENDAR, DAY, ("005930",), POISSON.features, FEE, Leaders()
        )
    )

    engine = await run(
        tmp_path / "engine.db", a_sided_morning(), until=at("10:00:00"), models={"poisson": model}
    )

    poisson = await predictions_of(engine, "poisson")
    assert len(poisson) == 60 and {p["status"] for p in poisson} == {"ok"}, "none over its time budget"
    assert {p["model_version"] for p in poisson} == {model.version}
    assert sum(p["outcome"] is not None for p in poisson) == 56
    assert engine.cpu_use().expected["poisson"] < 0.1, "seconds a bar close, as the engine timed it"


async def test_what_comes_after_a_bar_close_never_changes_poisson_s_forecast(tmp_path: Path) -> None:
    model = POISSON.fit(made_up(1000))
    cut = at("09:40:00")
    everything = a_sided_morning()
    until_then = [e for e in everything if e[1]["received_at"] < cut]

    full = await run(tmp_path / "full.db", everything, until=at("10:00:00"), models={"poisson": model})
    short = await run(tmp_path / "short.db", until_then, until=at("09:40:30"), models={"poisson": model})

    def at_cut(predictions: list[dict[str, Any]]) -> dict[str, Any]:
        [p] = [p for p in predictions if p["bar_close"] == cut.isoformat()]
        return {k: p[k] for k in ("p_up", "p_flat", "p_down", "expected_ticks")}

    assert at_cut(await predictions_of(full, "poisson")) == at_cut(await predictions_of(short, "poisson"))
