"""Itô (#518): the grid's chain against the closed forms of a Brownian motion, the skew, the
variance clock, and training on made-up days.

Input: forecast terms or features by hand, or days of bars turned into samples. Output: the
three probabilities, the model.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pytest

from krx_quant.algorithms.forecasts import UNKNOWN, normal_forecast, reach_probability, stay_probability
from krx_quant.algorithms.ito import SPEEDS, Ito, Terms, chains, clock, probabilities
from krx_quant.features import FeatureVector, TouchTerms
from krx_quant.market_calendar import MarketCalendar
from krx_quant.training import samples
from tests.synthetic_bars import random_walk

FEE = 0.015 / 100
DAYS = [date(2026, 9, 14) + timedelta(days=i) for i in range(5)]


def solved(
    distance: float, sigma: float, *, touch: bool = True, up: float = 1.0, down: float = 1.0
) -> np.ndarray:
    """Up, flat and down for σ a minute held for four minutes."""
    t = Terms(distance, touch, sigma * sigma, sigma * sigma, up, down, max(2 * sigma * 1.25, 0.5))
    return np.asarray(probabilities(t, chains(t, 1.0), np.array([clock(t.fast, t.slow, 0.0, 1.0)]))[0])


@pytest.mark.parametrize(("distance", "sigma"), [(2.5, 1.0), (2.5, 3.0), (1.5, 0.3), (3.5, 8.0)])
def test_without_skew_the_chain_is_a_brownian_motion(distance: float, sigma: float) -> None:
    p_up, p_flat, p_down = solved(distance, sigma)
    spread = 2 * sigma

    assert p_up == pytest.approx(reach_probability(distance, 0.0, spread), abs=3e-3)
    assert p_flat == pytest.approx(stay_probability(distance, distance, 0.0, spread), abs=3e-3)
    assert p_up + p_flat + p_down == pytest.approx(1.0)
    end = solved(distance, sigma, touch=False)
    normal = normal_forecast(0.0, spread, distance)
    assert end == pytest.approx([normal.p_up, normal.p_flat, normal.p_down], abs=3e-3)


def test_the_label_s_order_gives_down_more_than_independence_would() -> None:
    p_up, _, p_down = solved(2.5, 1.0)

    assert p_down == pytest.approx(p_up, abs=3e-3)  # no skew: the two sides alike
    assert p_down > reach_probability(2.5, 0.0, 2.0) * (1 - p_up) + 0.03


def test_the_wider_side_is_reached_more_often() -> None:
    p_up, _, p_down = solved(2.5, 1.0, up=0.8, down=1.17)

    assert p_down > p_up


def test_the_variance_clock_runs_from_the_fast_variance_to_the_slow() -> None:
    assert clock(4.0, 1.0, 0.0, 1.0) == pytest.approx(16.0)  # κ = 0: the fast variance stays
    assert clock(4.0, 1.0, 1e3, 1.0) == pytest.approx(4.0, abs=0.01)  # κ large: the slow at once
    assert clock(4.0, 1.0, 0.3, 1.0) == pytest.approx(4 + 3 * (1 - math.exp(-1.2)) / 0.3)
    assert clock(0.0, 0.0, 0.3, 1.0) == 0.25  # never under half a tick's square
    assert SPEEDS[0] == 0.0


def features(**values: float | None) -> FeatureVector:
    known: dict[str, float | None] = {
        "semivol_up": 1.0,
        "semivol_down": 1.0,
        "parkinson_5": 1.0,
        "volatility": 1.0,
        "flat_ticks": 2.0,
    }
    return FeatureVector(known | values, Ito.features, TouchTerms(0.5))


def test_the_forecast_is_a_martingale_s() -> None:
    model = Ito().default_model()
    skewed = Ito().predict(features(semivol_up=0.6, semivol_down=1.4), model)

    assert skewed.expected_ticks == 0.0
    assert skewed.p_down > skewed.p_up
    assert Ito().predict(features(volatility=None), model) == UNKNOWN
    # A side that has not moved still diffuses (a tenth of σ̄), so the chain stays defined.
    assert math.isfinite(Ito().predict(features(semivol_up=0.0), model).p_up)


def test_training_picks_a_point_of_the_grid() -> None:
    bar_days = {("005930", d): random_walk(d, seed=i) for i, d in enumerate(DAYS)}
    training = samples(
        bar_days, MarketCalendar(dict.fromkeys(DAYS, True)), Ito.features, FEE, target_version=3
    )

    model = Ito().fit(training)

    assert model.version.startswith("ito-20260918-")
    assert model.target_version == 3
    assert Ito().fit(training).version == model.version
