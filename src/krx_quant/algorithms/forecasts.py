"""For algorithms that forecast a move in ticks: the three classes of a normal
distribution around it, and training by the lowest log loss over a grid of parameters.

What the three classes are follows the target (``KRX_TARGET_VERSION``, #169), which the
features carry (``FeatureVector.touch``) and ``forecasting`` puts in force:

- version 1, the four-minute move: up is the chance the move ends k ticks or more up,
  down k ticks or more down, flat the rest (the endpoint);
- version 2, the touch target (#160): up is the chance the best bid reaches the upper
  target at some time in the four minutes, by the reflection principle for a Brownian
  motion with that drift and spread; the distance is k ticks plus half the spread at the
  bar close (the bid starts half the spread below the mid-point). Down is the chance of
  the ask reaching the lower target times the chance the upper one was not reached (up
  wins when both are, as the label has it; taken as independent), flat the rest.

The algorithms' code is the same for both: only this shared function changes.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar

from krx_quant.algorithms.contract import Model, NothingToLearn, Sample
from krx_quant.baselines import Forecast
from krx_quant.features import FeatureVector, TouchTerms
from krx_quant.pricing import Direction

HORIZON_MINUTES = 4.0
# Never sharper than half a tick: prices move in whole (or half) ticks.
MIN_SPREAD_TICKS = 0.5
# The smallest probability counted in the log loss, so one sure miss does not dominate.
LOSS_FLOOR = 1e-6
# The classes in a forecast's order: up, flat, down.
CLASSES = (Direction.UP, Direction.FLAT, Direction.DOWN)
# Without a flat band or a volatility there is nothing to say.
UNKNOWN = Forecast(1 / 3, 1 / 3, 1 / 3, 0.0)
# How ``normal_forecast`` makes the three probabilities, as the cards say it: of the touch target,
# the default since #170 (with KRX_TARGET_VERSION=1 the endpoint's, which the cards do not tell).
NORMAL_CLASSES = (
    "공통 함수가 이 분포를 4분 동안 그 평균만큼 흐르고 그 폭만큼 퍼지는 움직임(브라운 운동)으로 보고, 도달 "
    "정답의 세 확률로 바꾼다. 상승은 4분 안에 매수 1호가가 위 목표(봉 마감 중간가 + k틱)에 한 번이라도 닿을 "
    "확률로, 반사 원리로 구한다. 거리는 k틱 + 봉 마감 스프레드의 절반이다(매수 1호가는 중간가보다 그만큼 "
    "아래에서 출발한다). 하락은 매도 1호가가 아래 목표(− k틱)에 닿을 확률 × 위에 닿지 않을 확률이다(위·아래 "
    "둘 다 닿으면 상승이라서. 둘을 서로 독립으로 본 근사다). 나머지가 보합이다(k: 보합 범위. 폭은 0.5틱보다 "
    "좁아지지 않는다)."
)

Params = Mapping[str, float]


def param(kind: str, *index: int | str) -> str:
    """A parameter's name: its kind and indices, dotted (``w.up.ofi_1``)."""
    return ".".join([kind, *map(str, index)])


def number(value: float | None) -> float:
    """A feature's value for a numeric array: NaN where unknown or not finite."""
    return math.nan if value is None or not math.isfinite(value) else value


def class_moves(scored: Sequence[Sample]) -> dict[str, float]:
    """Each class's mean move in training, in ticks, as ``move.<class>`` (0 where none is
    known): what a classifier expects of each."""
    out = {}
    for d in Direction:
        moves = [s.change_ticks for s in scored if s.outcome is d and s.change_ticks is not None]
        out[param("move", d.value)] = sum(moves) / len(moves) if moves else 0.0
    return out


# The touch target's terms of the bar close being forecast (target version 2); None: the
# four-minute move (version 1).
_TOUCH: ContextVar[TouchTerms | None] = ContextVar("touch", default=None)


@contextmanager
def forecasting(features: FeatureVector) -> Iterator[None]:
    """Forecasts made inside are of ``features``' target: the touch target when they carry
    its terms (target version 2), else the four-minute move."""
    token = _TOUCH.set(features.touch)
    try:
        yield
    finally:
        _TOUCH.reset(token)


def _normal_cdf(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def _upper_tail(z: float) -> float:
    """1 − Φ(z), accurate far in the tail."""
    return 0.5 * math.erfc(z / math.sqrt(2))


def _log_cdf(z: float) -> float:
    """ln Φ(z); far in the lower tail, where Φ underflows, from its asymptotic series."""
    if z > -30:
        return math.log(0.5 * math.erfc(-z / math.sqrt(2)))
    return -0.5 * z * z - math.log(-z) - 0.5 * math.log(2 * math.pi) + math.log1p(-1 / z**2 + 3 / z**4)


def reach_probability(distance: float, drift: float, sigma: float) -> float:
    """The chance that a Brownian motion with ``drift`` and standard deviation ``sigma`` over
    the horizon reaches ``distance`` above its start at some time in it (the reflection
    principle): 1 − Φ((a − μ)/s) + exp(2μa/s²)·Φ((−a − μ)/s); without drift 2·(1 − Φ(a/s)),
    twice the chance of ending there. A distance of 0 or less is reached at once.
    """
    if distance <= 0:
        return 1.0
    a, mu, s = distance, drift, sigma
    # The reflected term in logs: its exponential alone overflows for a strong drift.
    reflected = 2 * mu * a / (s * s) + _log_cdf((-a - mu) / s)
    return min(1.0, _upper_tail((a - mu) / s) + (math.exp(reflected) if reflected > -745 else 0.0))


def normal_forecast(move: float, spread: float, flat_ticks: float) -> Forecast:
    """Up, flat and down for a move normally distributed around ``move`` with standard
    deviation ``spread`` (both in ticks), against the flat band ``flat_ticks``: of where the
    move ends (target version 1), or of reaching the targets on the way (version 2, the
    terms ``forecasting`` put in force).
    """
    sigma = max(spread, MIN_SPREAD_TICKS)
    touch = _TOUCH.get()
    if touch is not None:
        distance = flat_ticks + touch.half_spread
        p_up = reach_probability(distance, move, sigma)
        p_down = reach_probability(distance, -move, sigma) * (1 - p_up)
    else:
        p_up = 1 - _normal_cdf((flat_ticks - move) / sigma)
        p_down = _normal_cdf((-flat_ticks - move) / sigma)
    p_flat = max(0.0, 1 - p_up - p_down)
    total = p_up + p_flat + p_down
    return Forecast(p_up / total, p_flat / total, p_down / total, move)


def log_loss(forecast: Forecast, outcome: Direction) -> float:
    return -math.log(max(forecast.probability(outcome), LOSS_FLOOR))


def with_outcomes(samples: Sequence[Sample]) -> list[Sample]:
    """The samples that can be trained on; raises NothingToLearn if there are none."""
    scored = [s for s in samples if s.outcome is not None]
    if not scored:
        raise NothingToLearn("no training samples with an outcome")
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
    touching = any(s.features.touch is not None for s in scored)
    for params in grid:
        if touching:  # each sample's forecast of its own target (#169)
            loss = 0.0
            for s in scored:
                token = _TOUCH.set(s.features.touch)  # as ``forecasting``, without its overhead
                try:
                    loss += log_loss(forecast(s.features, params), s.outcome or Direction.FLAT)
                finally:
                    _TOUCH.reset(token)
        else:
            loss = sum(log_loss(forecast(s.features, params), s.outcome or Direction.FLAT) for s in scored)
        loss /= len(scored)
        if best is None or loss < best[0] - 1e-12:
            best = (loss, params)
    if best is None:
        raise ValueError("an empty grid")
    loss, params = best
    return Model.fitted(name, params, scored, loss)
