"""Granger: the neighbours' moves that come before the symbol's.

Personality card:
- **Scientist**: Clive Granger.
- **Logic**: a series Granger-causes the symbol when its past moves forecast the symbol's
  next move better than the symbol's own past does alone. Six lead groups are watched:
  the other predicted symbols, KOSPI200 futures, USD futures, the market indices, US
  daytime trades and the KRX-listed US ETFs. Each move is a one-minute log return in the
  series' own standard deviations, 1 to 3 minutes back (``features.Leaders``). The
  four-minute move, in the symbol's own one-minute standard deviations, is a linear
  regression on its own past moves and the kept groups'. The forecast is a normal
  distribution around it, as wide as the regression's residual. A kept group whose move
  is not known (its feed silent, like US daytime trades before 10:00 outside US summer
  time) counts as no move and widens the forecast by its weight.
- **Own features**: the six lead groups' moves.
- **Learns**: over the last 20 trading days, first how many of its own past minutes count,
  then for each group how many minutes back (by BIC), keeping the group only if that
  lowers the BIC of the regression on its own past: a Granger causality test. Then the
  coefficients of what is kept, by least squares. The four-minute moves of consecutive
  bar closes overlap, so the BIC counts a quarter as many independent ones.

Training measures the symbol's own moves on closes, which bounce between the bid and the
ask; a bounce is undone the next minute, so the own coefficients learned lean negative
against the live moves, which are on the mid-point.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import NamedTuple

from krx_quant.algorithms.contract import Card, Model, Sample
from krx_quant.algorithms.forecasts import (
    HORIZON_MINUTES,
    UNKNOWN,
    Params,
    lowest_log_loss,
    normal_forecast,
    with_outcomes,
)
from krx_quant.baselines import Forecast
from krx_quant.features import (
    LEAD_GROUPS,
    LEAD_LAGS,
    FeatureValue,
    FeatureVector,
    lead_feature,
    own_feature,
    solve,
)

OWN = tuple(own_feature(lag) for lag in LEAD_LAGS)
GROUPS = {group: tuple(lead_feature(group, lag) for lag in LEAD_LAGS) for group in LEAD_GROUPS}
COEFFICIENTS = (*OWN, *(name for names in GROUPS.values() for name in names))
# Without a fit: no move expected, and four minutes of a random walk around it, in one
# minute's standard deviations. A fit sets the coefficients it keeps; the rest stay 0.
DEFAULT_PARAMS = {**dict.fromkeys(COEFFICIENTS, 0.0), "residual": math.sqrt(HORIZON_MINUTES)}
# A group seen on fewer bar closes than this is not tested.
MIN_SAMPLES = 100
# Nor is one seen on less than this share of the bar closes that know the symbol's own past:
# the fit keeps only the bar closes that know every chosen group, so a group recorded on a
# few days only (indices and US daytime trades have no backfill) would cut the rest out.
MIN_SHARE = 0.5


class _Row(NamedTuple):
    features: Mapping[str, FeatureValue]
    move: float  # the four-minute move to explain, in the symbol's standard deviations


class Granger:
    name = "granger"
    features: tuple[str, ...] = (*COEFFICIENTS, "volatility", "flat_ticks")
    training_days = 20
    card = Card(
        scientist="Clive Granger",
        character="이웃 관찰",
        logic=(
            "다른 종목, KOSPI200 선물, 달러선물, 지수, 미국 주간거래, 미국 ETF가 1–3분 전에 "
            "움직인 것(각자의 표준편차 단위)으로 4분 뒤 움직임을 회귀한다. 자기 과거만 쓴 "
            "회귀보다 나아지는 묶음만 쓴다."
        ),
        unique_features=tuple(name for names in GROUPS.values() for name in names),
        training=(
            "최근 20거래일 1분봉으로 자기 시차 수를 BIC로 고르고, 묶음마다 시차(1–3분)를 BIC로 "
            "골라 자기 과거만 쓴 회귀보다 BIC를 낮추는 묶음만 남긴다(Granger 인과 검정). "
            "남긴 것의 계수는 최소제곱으로 맞춘다."
        ),
    )

    def default_model(self) -> Model:
        return Model("granger-default", dict(DEFAULT_PARAMS))

    def predict(self, features: FeatureVector, model: Model) -> Forecast:
        return self._forecast(features, model.params)

    def fit(self, samples: Sequence[Sample]) -> Model:
        scored = with_outcomes(samples)
        rows: list[_Row] = []
        for s in scored:
            volatility = s.features["volatility"]
            if volatility and s.change_ticks is not None:
                rows.append(_Row(dict(s.features), s.change_ticks / volatility))
        if not rows:
            raise ValueError("no training samples with a volatility to measure moves in")
        own = _own_lags(rows)
        chosen = list(own)
        for group in LEAD_GROUPS:
            chosen += _granger_causes(rows, own, GROUPS[group])
        fitted = _least_squares(_complete(rows, chosen), chosen)
        if fitted is None:
            raise ValueError("the regression has no solution on these samples")
        coefficients, rss, n = fitted
        params = {**DEFAULT_PARAMS, **dict(zip(chosen, coefficients, strict=True))}
        params["residual"] = math.sqrt(rss / max(n - len(chosen), 1))
        return lowest_log_loss(self.name, scored, [params], self._forecast)

    @staticmethod
    def _forecast(features: FeatureVector, params: Params) -> Forecast:
        k, volatility = features["flat_ticks"], features["volatility"]
        if k is None or volatility is None:
            return UNKNOWN
        move, variance = 0.0, params["residual"] ** 2
        for name in COEFFICIENTS:
            weight = params[name]
            if weight == 0:
                continue
            value = features[name]
            if value is None:
                variance += weight * weight  # a move of about one standard deviation, unknown
            else:
                move += weight * value
        # + 0.0: no move is 0, not −0
        return normal_forecast(volatility * move + 0.0, volatility * math.sqrt(variance), k)


def _own_lags(rows: Sequence[_Row]) -> tuple[str, ...]:
    """How many of the symbol's own past minutes count: the lowest BIC, on the bar closes
    that know all three.
    """
    known = _complete(rows, OWN)
    best = min(range(len(OWN) + 1), key=lambda n: _bic(known, OWN[:n]))
    return OWN[:best]


def _granger_causes(rows: Sequence[_Row], own: tuple[str, ...], group: tuple[str, ...]) -> tuple[str, ...]:
    """The group's minutes back that count, if any: its best number of lags by BIC, kept only
    if it lowers the BIC of the regression on the symbol's own past, on the same bar closes.
    A group known on too few of them is not tested (``MIN_SAMPLES``, ``MIN_SHARE``).
    """
    known = _complete(rows, (*own, *group))
    if len(known) < max(MIN_SAMPLES, MIN_SHARE * len(_complete(rows, own))):
        return ()
    without = _bic(known, own)
    best = min(range(1, len(group) + 1), key=lambda n: _bic(known, (*own, *group[:n])))
    return group[:best] if _bic(known, (*own, *group[:best])) < without else ()


def _complete(rows: Sequence[_Row], names: Sequence[str]) -> list[_Row]:
    return [row for row in rows if all(row.features[name] is not None for name in names)]


def _bic(rows: Sequence[_Row], names: Sequence[str]) -> float:
    """The regression's BIC, counting the overlapping four-minute moves of consecutive bar
    closes as a quarter as many independent ones.
    """
    fitted = _least_squares(rows, names)
    if fitted is None:
        return math.inf
    _, rss, n = fitted
    independent = max(n / HORIZON_MINUTES, 1.0)
    return independent * math.log(max(rss, 1e-300) / n) + len(names) * math.log(independent)


def _least_squares(rows: Sequence[_Row], names: Sequence[str]) -> tuple[list[float], float, int] | None:
    """Coefficients of ``names`` (no intercept), the residual sum of squares and the number
    of rows; None without rows or a solution.
    """
    n, k = len(rows), len(names)
    if n == 0:
        return None
    xs = [[row.features[name] or 0.0 for name in names] for row in rows]
    ys = [row.move for row in rows]
    gram = [[sum(x[i] * x[j] for x in xs) for j in range(k)] for i in range(k)]
    moment = [sum(x[i] * y for x, y in zip(xs, ys, strict=True)) for i in range(k)]
    coefficients = solve(gram, moment) if k else []
    if coefficients is None:
        return None
    rss = sum(
        (y - sum(c * v for c, v in zip(coefficients, x, strict=True))) ** 2
        for x, y in zip(xs, ys, strict=True)
    )
    return coefficients, rss, n
