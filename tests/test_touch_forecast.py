"""The shared normal forecast with the touch target (#160, #169), on values worked out by hand.

Input: a move normally distributed around its drift μ with standard deviation s over the
four minutes (ticks), the flat band k, and half the spread at the bar close. Output: the
three probabilities.

Hand values use the standard normal table: Φ(1) = 0.841345, Φ(1.25) = 0.894350,
Φ(0.75) = 0.773373, Φ(1.75) = 0.959941; e^1.25 = 3.490343.

- Reaching a above the start: 1 − Φ((a − μ)/s) + e^(2μa/s²)·Φ((−a − μ)/s); without drift
  2·(1 − Φ(a/s)), twice the chance of ending there (the reflection principle).
- The distance is k ticks plus half the spread: the bid starts that far below the mid-point.
- p_up = P(upper), p_down = P(lower)·(1 − p_up), p_flat the rest.
"""

from __future__ import annotations

import math

import pytest

from krx_quant.algorithms.forecasts import forecasting, normal_forecast, reach_probability
from krx_quant.baselines import Forecast
from krx_quant.features import DEFAULT_HALF_SPREAD, FeatureVector, TouchTerms


def touching(move: float, sigma: float, k: float, half_spread: float) -> Forecast:
    """``normal_forecast`` of the touch target, as the engine runs it for a bar close."""
    with forecasting(FeatureVector({}, (), TouchTerms(half_spread))):
        return normal_forecast(move, sigma, k)


def test_without_drift_reaching_is_twice_as_likely_as_ending_there() -> None:
    # a = 2, s = 2: ending 2 ticks up 1 − Φ(1) = 0.158655; reaching it on the way 0.317311.
    ending = normal_forecast(0.0, 2.0, 2.0).p_up  # the four-minute move (target version 1)
    assert ending == pytest.approx(0.158655, abs=1e-6)
    assert reach_probability(2.0, 0.0, 2.0) == pytest.approx(0.317311, abs=1e-6)
    assert reach_probability(2.0, 0.0, 2.0) == pytest.approx(2 * ending)
    assert touching(0.0, 2.0, 2.0, 0.0).p_up == pytest.approx(2 * ending)


def test_the_distance_is_k_ticks_plus_half_the_spread() -> None:
    # k = 2 and a one-tick spread: a = 2.5, s = 2. P = 2·(1 − Φ(1.25)) = 0.211300 each way.
    f = touching(0.0, 2.0, 2.0, 0.5)
    assert f.p_up == pytest.approx(0.211300, abs=1e-6)
    assert f.p_down == pytest.approx(0.211300 * (1 - 0.211300), abs=1e-6)  # 0.166652
    assert f.p_flat == pytest.approx(0.622048, abs=1e-6)
    assert f.expected_ticks == 0.0
    # A wider spread: further to go, less likely.
    assert touching(0.0, 2.0, 2.0, 1.0).p_up < f.p_up


def test_a_drift_up_makes_the_upper_target_likelier_and_the_lower_less() -> None:
    # μ = 1, s = 2, a = 2.5.
    # P(upper) = 1 − Φ(0.75) + e^1.25·Φ(−1.75) = 0.226627 + 3.490343 × 0.040059 = 0.366447.
    # P(lower) = 1 − Φ(1.75) + e^−1.25·Φ(−0.75) = 0.040059 + 0.286505 × 0.226627 = 0.104989.
    f = touching(1.0, 2.0, 2.0, 0.5)
    assert f.p_up == pytest.approx(0.366447, abs=1e-5)
    assert f.p_down == pytest.approx(0.104989 * (1 - 0.366447), abs=1e-5)  # 0.066516
    assert f.p_flat == pytest.approx(0.567037, abs=1e-5)
    assert f.expected_ticks == 1.0, "the expected move stays the four-minute one"


@pytest.mark.parametrize("move", [-40.0, -3.0, -0.5, 0.0, 0.7, 2.5, 6.0, 40.0])
@pytest.mark.parametrize("sigma", [0.1, 0.5, 2.0, 9.0])
def test_the_three_add_up_to_one(move: float, sigma: float) -> None:
    f = touching(move, sigma, 2.0, 0.5)
    assert all(0 <= p <= 1 for p in (f.p_up, f.p_flat, f.p_down))
    assert math.isclose(f.p_up + f.p_flat + f.p_down, 1.0, abs_tol=1e-12)


def test_a_strong_drift_reaches_its_side_without_overflowing() -> None:
    # e^(2μa/s²) alone would overflow: μ = 40, a = 2.5, s = 0.5 gives e^800.
    up = touching(40.0, 0.5, 2.0, 0.5)
    down = touching(-40.0, 0.5, 2.0, 0.5)
    assert up.p_up == pytest.approx(1.0) and up.p_down == pytest.approx(0.0)
    assert down.p_down == pytest.approx(1.0) and down.p_up == pytest.approx(0.0, abs=1e-12)


def test_no_spread_is_narrower_than_half_a_tick() -> None:
    assert touching(0.0, 0.0, 2.0, 0.5) == touching(0.0, 0.5, 2.0, 0.5)


def test_without_the_touch_terms_the_forecast_is_of_the_four_minute_move() -> None:
    plain = FeatureVector({}, ())
    with forecasting(plain):
        f = normal_forecast(1.0, 2.0, 2.0)
    assert f.p_up == pytest.approx(1 - 0.691462, abs=1e-6)  # 1 − Φ(0.5)
    assert f.p_down == pytest.approx(1 - 0.933193, abs=1e-6)  # Φ(−1.5)


def test_the_terms_default_to_a_one_tick_spread() -> None:
    assert TouchTerms().half_spread == DEFAULT_HALF_SPREAD == 0.5
    assert TouchTerms.at(None, 100).half_spread == 0.5
    assert TouchTerms.at((81_200.0, 81_400.0), 100).half_spread == 1.0
    # A vector made from another keeps its terms.
    terms = TouchTerms(0.75)
    assert FeatureVector(FeatureVector({"flat_ticks": 2}, ("flat_ticks",), terms), ()).touch == terms
