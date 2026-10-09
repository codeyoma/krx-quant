"""For algorithms that forecast a move in ticks: the three classes of a normal
distribution around it, and training by the lowest log loss over a grid of parameters.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence

from krx_quant.algorithms.contract import Model, Sample
from krx_quant.baselines import Forecast
from krx_quant.features import FeatureVector
from krx_quant.pricing import Direction

HORIZON_MINUTES = 4.0
# Never sharper than half a tick: prices move in whole (or half) ticks.
MIN_SPREAD_TICKS = 0.5
# The smallest probability counted in the log loss, so one sure miss does not dominate.
_FLOOR = 1e-6
# Without a flat band or a volatility there is nothing to say.
UNKNOWN = Forecast(1 / 3, 1 / 3, 1 / 3, 0.0)

Params = Mapping[str, float]


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
    return -math.log(max(forecast.probability(outcome), _FLOOR))


def with_outcomes(samples: Sequence[Sample]) -> list[Sample]:
    """The samples that can be trained on; raises ValueError if there are none."""
    scored = [s for s in samples if s.outcome is not None]
    if not scored:
        raise ValueError("no training samples with an outcome")
    return scored


def lowest_log_loss(
    name: str,
    scored: Sequence[Sample],
    grid: Iterable[Params],
    forecast: Callable[[FeatureVector, Params], Forecast],
) -> Model:
    """The model of the parameters in ``grid`` whose forecasts have the lowest mean log loss
    on ``scored``; the first of equals.
    """
    best: tuple[float, Params] | None = None
    for params in grid:
        loss = sum(log_loss(forecast(s.features, params), s.outcome or Direction.FLAT) for s in scored)
        loss /= len(scored)
        if best is None or loss < best[0] - 1e-12:
            best = (loss, params)
    if best is None:
        raise ValueError("an empty grid")
    loss, params = best
    return Model.fitted(name, params, scored, loss)
