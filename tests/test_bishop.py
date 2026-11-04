"""Bishop (#518): the mixture density network's input, its training by likelihood, the mixed
forecast, and training on made-up days.

Input: features by hand, rows made up, or days of bars turned into samples. Output: the
network's likelihood, the three probabilities, the model.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pytest

from krx_quant.algorithms.bishop import (
    INPUTS,
    WINDOW,
    Bishop,
    _gradients,
    _initial,
    inputs,
    mixed_forecast,
    train,
)
from krx_quant.algorithms.contract import NothingToLearn
from krx_quant.algorithms.forecasts import UNKNOWN, forecasting, ordered_forecast
from krx_quant.features import FeatureVector, TouchTerms
from krx_quant.market_calendar import MarketCalendar
from krx_quant.training import samples
from tests.synthetic_bars import random_walk

FEE = 0.015 / 100
DAYS = [date(2026, 9, 14) + timedelta(days=i) for i in range(5)]


def features(volatility: float | None = 2.0, minutes: int = 10) -> FeatureVector:
    values = {
        "minute_ticks": (2.0,) * minutes,
        "minute_highs": (4.0,) * minutes,
        "minute_lows": (-2.0,) * minutes,
        "minute_volumes": (1.0,) * minutes,
        "volatility": volatility,
        "flat_ticks": 2.0,
    }
    return FeatureVector(values, Bishop.features, TouchTerms(0.5))


def test_the_input_is_the_bar_shapes_in_volatilities_the_latest_last() -> None:
    x = inputs(features(minutes=10))

    assert x is not None and x.shape == (INPUTS,)
    assert x[: WINDOW - 10].tolist() == [0.0] * (WINDOW - 10)  # the minutes before the open
    assert x[WINDOW - 10 : WINDOW].tolist() == [1.0] * 10  # +2 ticks over a volatility of 2
    assert x[2 * WINDOW - 1] == 2.0 and x[3 * WINDOW - 1] == -1.0
    assert x[4 * WINDOW - 1] == pytest.approx(math.log(2))  # ln(1 + 1)
    assert x[-1] == pytest.approx(10 / WINDOW)
    assert inputs(features(volatility=None)) is None


def test_training_raises_the_likelihood_of_moves_the_bars_foretell() -> None:
    rng = np.random.default_rng(1)
    x = rng.normal(0, 1, (2000, INPUTS))
    y = np.where(x[:, WINDOW - 1] > 0, 1.0, -1.0) + 0.3 * rng.normal(0, 1, 2000)
    before, _ = _gradients(_initial(np.random.default_rng(0)), x, y)

    net = train(x, y)

    after, _ = _gradients(net, x, y)
    assert after < before - 0.5, (before, after)
    assert all(np.array_equal(net[k], train(x, y)[k]) for k in net)  # the same rows, the same net


def test_without_a_net_it_is_one_normal_distribution_about_0() -> None:
    f = features()
    x = inputs(f)
    assert x is not None
    with forecasting(f):
        assert mixed_forecast(None, x, 4.0, 2.0) == ordered_forecast(0.0, 4.0, 2.0)
        assert Bishop().predict(f, Bishop().default_model()) == ordered_forecast(0.0, 4.0, 2.0)
    assert Bishop().predict(features(volatility=None), Bishop().default_model()) == UNKNOWN


def test_a_trained_model_is_the_same_every_time_and_predicts_a_distribution() -> None:
    bar_days = {("005930", d): random_walk(d, seed=i) for i, d in enumerate(DAYS)}
    training = samples(
        bar_days, MarketCalendar(dict.fromkeys(DAYS, True)), Bishop.features, FEE, target_version=3
    )

    model = Bishop().fit(training)

    assert model.version.startswith("bishop-20260918-") and model.blob is not None
    assert Bishop().fit(training).version == model.version
    for s in training[:20]:
        with forecasting(s.features):
            f = Bishop().predict(s.features, model)
        assert f.p_up + f.p_flat + f.p_down == pytest.approx(1.0)


def test_too_few_samples_teach_nothing() -> None:
    bar_days = {("005930", DAYS[0]): random_walk(DAYS[0])}
    few = samples(bar_days, MarketCalendar({DAYS[0]: True}), Bishop.features, FEE)[:50]

    with pytest.raises(NothingToLearn):
        Bishop().fit(few)
