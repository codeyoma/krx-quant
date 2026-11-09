"""Ulam (#518): paths drawn from the session's minutes and read as the label reads bars, the
blocks, the drift's share, the fallback, and training on made-up days.

Input: features by hand (the pools of minutes), or days of bars turned into samples. Output:
the three probabilities, the model.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from krx_quant.algorithms.contract import Model
from krx_quant.algorithms.forecasts import UNKNOWN, forecasting, ordered_forecast
from krx_quant.algorithms.ulam import MIN_POOL, PATHS, SMOOTHING, Ulam, draws
from krx_quant.features import FeatureVector, TouchTerms, Value
from krx_quant.market_calendar import MarketCalendar
from krx_quant.training import samples
from tests.synthetic_bars import random_walk, trending

FEE = 0.015 / 100
DAYS = [date(2026, 9, 14) + timedelta(days=i) for i in range(5)]
ALL = (PATHS + SMOOTHING) / (PATHS + 3 * SMOOTHING)  # every path in one class
NONE = SMOOTHING / (PATHS + 3 * SMOOTHING)


def features(
    changes: list[float], highs: list[float], lows: list[float], *, touch: bool = True, k: float = 2.0
) -> FeatureVector:
    values: dict[str, Value] = {
        "minute_ticks": tuple(changes),
        "minute_highs": tuple(highs),
        "minute_lows": tuple(lows),
        "volatility": 1.0,
        "flat_ticks": k,
    }
    return FeatureVector(values, Ulam.features, TouchTerms(0.5) if touch else None)


def model(block: float = 2.0, drift: float = 0.0, scale: float = 1.0) -> Model:
    return Model("ulam-test", {"block": block, "drift": drift, "scale": scale})


def test_a_minute_s_high_reaches_the_target_though_its_close_does_not() -> None:
    f = Ulam().predict(features([0.0] * 20, [3.0] * 20, [0.0] * 20), model())

    assert f.p_up == pytest.approx(ALL)
    assert f.expected_ticks == 0.0


def test_up_wins_when_a_path_reaches_both_targets() -> None:
    f = Ulam().predict(features([0.0] * 20, [3.0] * 20, [-3.0] * 20), model())

    assert (f.p_up, f.p_down) == pytest.approx((ALL, NONE))


def test_down_is_a_low_at_the_lower_target_without_a_high_at_the_upper() -> None:
    f = Ulam().predict(features([-1.0] * 20, [0.0] * 20, [-1.0] * 20), model(drift=1.0))

    assert f.p_down == pytest.approx(ALL)  # four minutes of −1: −3 by the last minute's start
    assert f.expected_ticks == pytest.approx(-4.0)


def test_the_drift_s_share_is_taken_out_of_every_minute() -> None:
    rising = features([1.0] * 20, [1.0] * 20, [0.0] * 20)

    kept = Ulam().predict(rising, model(drift=1.0))
    removed = Ulam().predict(rising, model(drift=0.0))

    assert kept.p_up == pytest.approx(ALL)
    assert removed.p_flat == pytest.approx(ALL)  # every minute 0, high 0, low −1
    assert removed.expected_ticks == pytest.approx(0.0)


def test_the_four_minute_move_is_where_the_path_ends() -> None:
    f = Ulam().predict(features([1.0] * 20, [5.0] * 20, [-5.0] * 20, touch=False), model(drift=1.0))

    assert f.p_up == pytest.approx(ALL)  # ends +4, at or past k = 2


def test_a_block_carries_on_the_minute_before() -> None:
    pick = np.array([[0.0, 0.5, 0.5, 0.5]])
    stay = np.zeros((1, 4))

    assert draws(np.array(10), 1e9, pick, stay).tolist() == [[0, 1, 2, 3]]
    assert draws(np.array(10), 1.0, pick, stay).tolist() == [[0, 5, 5, 5]]  # b = 1: always new
    assert draws(np.array(3), 1e9, np.array([[0.9, 0, 0, 0]]), stay).tolist() == [[2, 0, 1, 2]]  # a circle


def test_too_few_minutes_fall_back_on_the_volatility() -> None:
    short = features([1.0] * (MIN_POOL - 1), [1.0] * (MIN_POOL - 1), [0.0] * (MIN_POOL - 1))
    with forecasting(short):
        assert Ulam().predict(short, model()) == ordered_forecast(0.0, 2.0, 2.0)
    unknown = FeatureVector(
        {"minute_ticks": (), "minute_highs": (), "minute_lows": (), "volatility": None, "flat_ticks": 2.0},
        Ulam.features,
    )
    assert Ulam().predict(unknown, model()) == UNKNOWN


def test_the_same_features_give_the_same_forecast() -> None:
    rng = np.random.default_rng(3)
    changes = rng.integers(-2, 3, 30).astype(float)
    f = features(list(changes), list(np.maximum(changes, 0) + 1), list(np.minimum(changes, 0) - 1))

    assert Ulam().predict(f, model()) == Ulam().predict(f, model())


def fit(make_day: object) -> Model:
    assert callable(make_day)
    bar_days = {("005930", d): make_day(d, seed=i) for i, d in enumerate(DAYS)}
    training = samples(bar_days, MarketCalendar(dict.fromkeys(DAYS, True)), Ulam.features, FEE)
    return Ulam().fit(training)


def test_a_day_that_keeps_going_up_is_fitted_with_its_drift() -> None:
    m = fit(trending)

    assert m.version.startswith("ulam-20260918-")
    assert m.params["drift"] == 1.0


def test_training_on_the_touch_target_is_the_same_every_time() -> None:
    first = fit(random_walk)

    assert first.target_version == 3
    assert fit(random_walk).version == first.version
