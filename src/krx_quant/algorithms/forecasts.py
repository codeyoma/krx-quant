"""For algorithms that forecast a move in ticks: the three classes of a normal
distribution around it, and the log loss they are trained on.
"""

from __future__ import annotations

import math

from krx_quant.baselines import Forecast
from krx_quant.pricing import Direction

# Never sharper than half a tick: prices move in whole (or half) ticks.
MIN_SPREAD_TICKS = 0.5
# The smallest probability counted in the log loss, so one sure miss does not dominate.
_FLOOR = 1e-6
# Without a flat band or a volatility there is nothing to say.
UNKNOWN = Forecast(1 / 3, 1 / 3, 1 / 3, 0.0)


def _normal_cdf(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def normal_forecast(move: float, spread: float, flat_ticks: float) -> Forecast:
    """Up, flat and down for a move normally distributed around ``move`` with standard
    deviation ``spread`` (both in ticks), against the flat band ``flat_ticks``.
    """
    sigma = max(spread, MIN_SPREAD_TICKS)
    p_up = 1 - _normal_cdf((flat_ticks - move) / sigma)
    p_down = _normal_cdf((-flat_ticks - move) / sigma)
    p_flat = max(0.0, 1 - p_up - p_down)
    total = p_up + p_flat + p_down
    return Forecast(p_up / total, p_flat / total, p_down / total, move)


def log_loss(forecast: Forecast, outcome: Direction) -> float:
    p = {Direction.UP: forecast.p_up, Direction.FLAT: forecast.p_flat, Direction.DOWN: forecast.p_down}
    return -math.log(max(p[outcome], _FLOOR))
