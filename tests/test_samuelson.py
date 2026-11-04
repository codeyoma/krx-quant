"""Samuelson (#518): a geometric Brownian motion's targets in log prices, the touch target in the
label's order, the drift's shrinking, and training on made-up days.

Input: features by hand, or days of bars turned into samples. Output: the forecast, the model.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pytest

from krx_quant.algorithms.forecasts import UNKNOWN, reach_probability, stay_probability
from krx_quant.algorithms.samuelson import Samuelson, gbm_forecast, log_targets
from krx_quant.features import FeatureVector, TouchTerms
from krx_quant.market_calendar import MarketCalendar
from krx_quant.training import samples
from tests.synthetic_bars import random_walk, trending

FEE = 0.015 / 100
DAYS = [date(2026, 9, 14) + timedelta(days=i) for i in range(5)]
TICK_BP = 1e4 * math.log1p(100 / 81_400)


def features(
    drift: float | None, sigma: float | None = 12.0, minutes: float = 120, touch: bool = True
) -> FeatureVector:
    values = {
        "session_drift": drift,
        "session_sigma": sigma,
        "tick_bp": TICK_BP,
        "minutes_since_open": minutes,
        "flat_ticks": 2.0,
    }
    return FeatureVector(values, Samuelson.features, TouchTerms(0.5) if touch else None)


def test_the_targets_in_log_prices_put_down_a_little_farther_than_up() -> None:
    up, down = log_targets(2.5, TICK_BP)

    share = 100 / 81_400
    assert up == pytest.approx(1e4 * math.log(1 + 2.5 * share))
    assert down == pytest.approx(-1e4 * math.log(1 - 2.5 * share))
    assert down > up


def test_the_touch_target_is_in_the_label_s_order() -> None:
    f = gbm_forecast(3.0, 25.0, TICK_BP, 2.0, 0.5)
    up, down = log_targets(2.5, TICK_BP)

    assert f.p_up == pytest.approx(reach_probability(up, 3.0, 25.0))
    assert f.p_flat == pytest.approx(stay_probability(down, up, 3.0, 25.0))
    assert f.p_up + f.p_flat + f.p_down == pytest.approx(1.0)
    # Taking the two targets as independent would shrink down by the chance of reaching up.
    assert f.p_down > reach_probability(down, -3.0, 25.0) * (1 - f.p_up)


def test_the_strip_s_survival_matches_brownian_paths() -> None:
    rng = np.random.default_rng(7)
    paths, steps = 20_000, 1_000
    x = np.zeros(paths)
    inside = np.ones(paths, dtype=bool)
    for _ in range(steps):
        x += 1.0 / steps + 2.0 * math.sqrt(1 / steps) * rng.standard_normal(paths)
        inside &= (x > -2.5) & (x < 2.0)
    # Watched at a thousand steps, the paths stay as the continuous motion does between targets
    # 0.5826·σ·√Δt farther out (Broadie, Glasserman & Kou 1997).
    farther = 0.5826 * 2.0 * math.sqrt(1 / steps)
    assert stay_probability(2.5 + farther, 2.0 + farther, 1.0, 2.0) == pytest.approx(inside.mean(), abs=0.01)
    driftless = sum(
        4 / math.pi * (-1) ** n / (2 * n + 1) * math.exp(-((2 * n + 1) ** 2) * math.pi**2 * 4 / (8 * 6.25))
        for n in range(200)
    )
    assert stay_probability(2.5, 2.5, 0.0, 2.0) == pytest.approx(driftless, abs=1e-9)
    assert stay_probability(2.5, 2.0, 1.0, 2.0) == pytest.approx(stay_probability(2.0, 2.5, -1.0, 2.0))


def test_the_drift_is_shrunk_by_the_minutes_behind_it() -> None:
    model = Samuelson().default_model()  # w = 0.5, n₀ = 120
    early = Samuelson().predict(features(1.0, minutes=0), model)
    later = Samuelson().predict(features(1.0, minutes=480), model)

    assert early.p_up == pytest.approx(Samuelson().predict(features(0.0), model).p_up)
    assert later.p_up > early.p_up and later.p_down < early.p_down
    assert later.expected_ticks > 0


def test_the_expected_move_is_the_lognormal_mean_in_ticks() -> None:
    # E[S₄ ÷ S₀ − 1] = e^(ν + σ²/2) − 1 with ν and σ as log returns (bp ÷ 10⁴), over a tick's share.
    share = math.expm1(TICK_BP / 1e4)

    still = gbm_forecast(0.0, 24.0, TICK_BP, 2.0, 0.5)
    falling = gbm_forecast(-10.0, 24.0, TICK_BP, 2.0, 0.5)

    assert still.expected_ticks == pytest.approx(math.expm1((24e-4) ** 2 / 2) / share)
    assert 0 < still.expected_ticks < 0.01  # σ²/2 alone moves the mean a hair, not ticks
    assert falling.expected_ticks == pytest.approx(math.expm1(-10e-4 + (24e-4) ** 2 / 2) / share)
    assert falling.expected_ticks < 0


def test_the_four_minute_move_ends_past_the_targets() -> None:
    f = Samuelson().predict(features(0.0, touch=False), Samuelson().default_model())
    up, down = log_targets(2.0, TICK_BP)
    spread = 12.0 * 2  # σ √4

    assert f.p_up == pytest.approx(0.5 * math.erfc((up / spread) / math.sqrt(2)))
    assert f.p_down == pytest.approx(0.5 * math.erfc((down / spread) / math.sqrt(2)))


def test_unknown_drift_or_sigma_says_nothing() -> None:
    model = Samuelson().default_model()

    assert Samuelson().predict(features(None), model) == UNKNOWN
    assert Samuelson().predict(features(1.0, sigma=None), model) == UNKNOWN


def fit(make_day: object) -> dict[str, float]:
    assert callable(make_day)
    bar_days = {("005930", d): make_day(d, seed=i) for i, d in enumerate(DAYS)}
    training = samples(bar_days, MarketCalendar(dict.fromkeys(DAYS, True)), Samuelson.features, FEE)
    model = Samuelson().fit(training)
    assert model.version.startswith("samuelson-20260918-")
    return dict(model.params)


def test_a_day_that_keeps_going_up_is_fitted_with_its_drift() -> None:
    assert fit(trending)["weight"] >= 1.0


def test_a_random_walk_is_fitted_too() -> None:
    params = fit(random_walk)

    assert set(params) == {"weight", "prior", "scale"}
