"""Mandelbrot: big moves follow big moves. The range's first model (#177, #181).

Personality card:
- **Scientist**: Benoit Mandelbrot, who pointed out volatility clustering (1963: large
  changes tend to be followed by large changes, of either sign).
- **Logic**: least squares of the log of the next four minutes' range, log(1 + r) (r the
  range answer, ``ranges.py``), on the mean one-minute range of the last 4, 20 and 60
  minutes and the previous trading day's in the same 30-minute slot (the HAR model's time
  scales, Corsi 2009), the relative volume and, on recorded days, the spread at the bar
  close; every input as log(1 + x), less its training mean (an unknown one is its mean),
  with an intercept for each 30-minute slot of the session (the day's seasonality), by
  ``minutes_since_open``.
- **Own features**: ``range_4``, ``range_20``, ``range_60``, ``range_prev_day``.
- **Learns**: over the last 20 trading days, two regressions: ``book``, with the spread, on
  the bar closes that have it (recorded days); ``bars``, without it, on every bar close.
  Each keeps its residuals' distribution as 101 quantiles (0%, 1%, ..., 100%).
- **Forecasts**: the fitted log range (``book`` where the spread is known and it was
  fitted, else ``bars``) plus the residuals' distribution is the forecast's distribution of
  log(1 + r). Its 10, 50 and 90% quantiles, back in ticks (exp(·) − 1, at least 0), are
  ``q10``, ``q50``, ``q90``; its share below log(1 + k) is ``p_small``, at or above
  log(1 + 2k) ``p_large``, the rest ``p_medium``: the probabilities and the quantiles agree
  (q50 = k exactly when p_small is a half). Before training: a third each, quantiles 0, k
  and 2k. Without k (no reference price at the bar close) it does not predict (``no_book``).
- **Fails**: when volatility turns suddenly (news on a quiet market; a big move that dies
  at once); the residuals are the whole window's, so a day with wilder tails than usual is
  missed.

The residuals' distribution is the quantile function through the 101 stored quantiles,
linear between them (``_quantile``), and its inverse (``_below``): with n residuals, the
quantile at level p is the sorted residuals' value at p·(n − 1), interpolated, as the
range's climate takes its quantiles, so for n − 1 dividing 100 the stored distribution is
the residuals' exactly.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Mapping, Sequence

import numpy as np

from krx_quant.algorithms.contract import (
    Abstention,
    KindSample,
    Model,
    NothingToLearn,
    NotPredicted,
)
from krx_quant.algorithms.forecasts import LOSS_FLOOR
from krx_quant.cards import Card, Meaning, learned
from krx_quant.features import (
    FEATURE_MEANINGS,
    PREVIOUS_DAY_RANGE,
    RANGE_MINUTES,
    FeatureVector,
    range_feature,
)
from krx_quant.kinds import KindForecast
from krx_quant.ranges import QUANTILES, RANGE

NAME = "mandelbrot"
TRAINING_DAYS = 20
RANGES = (*(range_feature(m) for m in RANGE_MINUTES), PREVIOUS_DAY_RANGE)
SPREAD = "spread_ticks"
# The inputs of each regression, as log(1 + x): ``bars`` without the spread, ``book`` with it.
INPUTS: Mapping[str, tuple[str, ...]] = {
    "bars": (*RANGES, "relative_volume"),
    "book": (*RANGES, "relative_volume", SPREAD),
}
SLOT_MINUTES = 30
# The 30-minute slots of a session's prediction window, from the open: bar closes 1–30
# minutes after it are slot 0, ..., the last (376 minutes on a 09:00 open) slot 12.
SLOTS = 13
# The residuals' distribution: its quantiles at 0%, 1%, ..., 100%.
PERCENTS = tuple(range(101))
# An input whose centred values all lie within this of 0 is constant: it says nothing.
_CONSTANT = 1e-12
# A quantile's log(1 + r) is cut here, so that its ticks stay finite whatever the inputs.
_LARGEST_LOG = 700.0


def _param(model: str, *parts: str | int) -> str:
    return ".".join([model, *map(str, parts)])


def _slot_key(model: str, slot: int) -> str:
    return _param(model, "slot", f"{slot:02d}")


def _residual_key(model: str, percent: int) -> str:
    return _param(model, "residual", f"{percent:03d}")


def _param_meanings() -> dict[str, Meaning]:
    when = learned(TRAINING_DAYS)
    out: dict[str, Meaning] = {}
    for model, inputs in INPUTS.items():
        which = (
            "스프레드를 넣은 회귀(`book`, 스프레드가 있는 봉 마감: 녹화된 날)"
            if model == "book"
            else "스프레드를 뺀 회귀(`bars`, 모든 봉 마감)"
        )
        out[_param(model, "n")] = Meaning(
            f"{which}를 맞춘 표본 수(변동폭 정답이 있는 봉 마감). 0이면 그 회귀가 없다: `bars`가 0이면 학습 "
            "전이라 기본 예측(1/3씩, 분위수 0·k·2k)을, `book`이 0이면 늘 `bars`로 낸다.",
            "개",
            when,
        )
        for name in inputs:
            unit = FEATURE_MEANINGS[name].unit
            out[_param(model, "coef", name)] = Meaning(
                f"{which}의 계수: log(1 + `{name}`)이 1 늘 때 log(1 + r)에 더하는 값.",
                f"log(1 + 틱) / log(1 + {unit})",
                when,
            )
            out[_param(model, "mean", name)] = Meaning(
                f"{which}의 학습 표본에서 log(1 + `{name}`)의 평균. 입력은 이 값을 뺀 차이로 넣고, 모르는 "
                "입력은 이 값(차이 0)으로 채운다. 학습 표본에 그 입력이 하나도 없었으면 0이다.",
                f"log(1 + {unit})",
                when,
            )
        for slot in range(SLOTS):
            first, last = SLOT_MINUTES * slot + 1, SLOT_MINUTES * (slot + 1)
            span = f"개장 뒤 {first}분 이후" if slot == SLOTS - 1 else f"개장 뒤 {first}–{last}분"
            out[_slot_key(model, slot)] = Meaning(
                f"{which}에서 {span}의 봉 마감(시간대 {slot})의 절편: 입력이 모두 학습 평균일 때의 "
                "log(1 + r). 학습에 없던 시간대는 가장 가까운(같으면 이른) 시간대의 값이다.",
                "log(1 + 틱)",
                when,
            )
        for percent in PERCENTS:
            out[_residual_key(model, percent)] = Meaning(
                f"{which}의 학습 잔차(log(1 + r) − 회귀값)의 {percent}% 분위수. 101개가 예측 분포를 이룬다.",
                "log(1 + 틱)",
                when,
            )
    return out


PARAM_MEANINGS: Mapping[str, Meaning] = _param_meanings()


def _value(x: float | None) -> float | None:
    """A non-negative input as log(1 + x); None where unknown (none, not finite, negative)."""
    return None if x is None or not math.isfinite(x) or x < 0 else math.log1p(x)


def slot_of(minutes_since_open: float | None) -> int:
    """The 30-minute slot of a bar close by its minutes since the open (``SLOTS``): the first
    without them."""
    if minutes_since_open is None or not math.isfinite(minutes_since_open):
        return 0
    return min(SLOTS - 1, max(0, math.ceil(minutes_since_open / SLOT_MINUTES) - 1))


def _quantiles(values: Sequence[float]) -> list[float]:
    """The quantiles of ``values`` at ``PERCENTS``: the sorted values at p·(n − 1),
    interpolated (exact where p·(n − 1) is whole)."""
    ordered = sorted(values)
    out = []
    for percent in PERCENTS:
        below, part = divmod(percent * (len(ordered) - 1), 100)
        above = min(below + 1, len(ordered) - 1)
        out.append(ordered[below] + (ordered[above] - ordered[below]) * part / 100)
    return out


def _quantile(knots: Sequence[float], level: float) -> float:
    """The distribution's ``level`` quantile: through the stored quantiles, linear between."""
    at = round(level * (len(knots) - 1), 9)
    below = math.floor(at)
    above = min(below + 1, len(knots) - 1)
    return knots[below] + (knots[above] - knots[below]) * (at - below)


def _below(knots: Sequence[float], x: float) -> float:
    """The distribution's share below ``x`` (P(e < x)): the inverse of ``_quantile``. Where the
    stored quantiles hold a value over a span of levels (a mass at it), its first level."""
    if x <= knots[0]:
        return 0.0
    if x > knots[-1]:
        return 1.0
    i = bisect.bisect_left(knots, x)  # knots[i − 1] < x ≤ knots[i]
    low, high = knots[i - 1], knots[i]
    return (i - 1 + (x - low) / (high - low)) / (len(knots) - 1)


def default_forecast(k: float) -> KindForecast:
    """Before training: every class as likely, quantiles 0, k and 2k ticks."""
    return KindForecast((1 / 3, 1 / 3, 1 / 3), {"q10": 0.0, "q50": k, "q90": 2 * k})


def forecast(fitted: float, knots: Sequence[float], k: float) -> KindForecast:
    """The forecast of a fitted log range and the residuals' distribution (``knots``) at the
    flat band ``k``: its classes' probabilities and its quantiles, from that one distribution."""
    small = _below(knots, math.log1p(k) - fitted)
    not_large = _below(knots, math.log1p(2 * k) - fitted)
    quantiles = {
        key: max(0.0, math.expm1(min(fitted + _quantile(knots, level), _LARGEST_LOG)))
        for key, level in QUANTILES
    }
    return KindForecast((small, not_large - small, 1 - not_large), quantiles)


class Mandelbrot:
    name = NAME
    kind = RANGE.name
    features: tuple[str, ...] = (*RANGES, "relative_volume", SPREAD, "minutes_since_open", "flat_ticks")
    training_days = TRAINING_DAYS
    param_meanings = PARAM_MEANINGS
    card = Card(
        scientist="Benoit Mandelbrot",
        character="변동성 군집",
        logic=(
            "다음 4분 변동폭 r(봉 마감부터 4분 동안 통합 중간가의 최고 − 최저, 틱)의 로그 log(1 + r)를 최근 "
            "4·20·60분 1분봉의 (고가 − 저가) 평균과 전 거래일 같은 30분 시간대의 평균(HAR 방식의 여러 시간 "
            "척도, Corsi 2009), 상대 거래량, 봉 마감 스프레드(녹화된 날만)로 최소제곱 회귀한다. 입력은 모두 "
            "log(1 + x)로 넣고, 개장부터 센 30분 시간대마다 절편을 따로 둔다(장중 계절성). 큰 움직임 뒤에 큰 "
            "움직임이 온다는 변동성 군집(Mandelbrot 1963)을 쓴다."
        ),
        unique_features=RANGES,
        training=(
            "최근 20거래일의 변동폭 정답(녹화일은 호가의 중간가, 그 전 날은 1분봉 규칙)으로 회귀 "
            "둘을 맞춘다: 스프레드를 넣은 회귀(스프레드가 있는 봉 마감, 곧 녹화일)와 뺀 회귀(모든 "
            "봉 마감). 입력은 학습 평균을 뺀 차이로 넣고, 모르는 입력(전 거래일 백필이 없는 날, "
            "개장 첫 10분의 상대 거래량 등)은 평균으로 채운다. 회귀마다 학습 잔차의 분포를 1% 간격 "
            "분위수 101개로 남긴다."
        ),
        forecasting=(
            "봉 마감에 스프레드를 알면 스프레드 회귀로, 모르면 뺀 회귀로 log(1 + r)의 회귀값을 "
            "낸다. 회귀값에 그 회귀의 학습 잔차 분포를 더한 것이 log(1 + r)의 예측 분포다. 분위수 "
            "q10·q50·q90은 그 분포의 10·50·90% 값을 exp(·) − 1로 틱으로 돌린 것이다(0 아래는 0). "
            "작음 확률은 log(1 + k)보다 작을 확률, 큼 확률은 log(1 + 2k) 이상일 확률, 보통은 "
            "나머지다. 확률과 분위수가 같은 분포에서 나와 서로 맞는다(q50이 k이면 작음 확률이 "
            "50%다). 학습 전에는 세 구간 1/3씩, 분위수 0·k·2k틱이다. 봉 마감에 기준가가 없어 k를 "
            "모르면 예측하지 않는다(no_book)."
        ),
        failure=(
            "변동성이 갑자기 바뀔 때 틀린다: 조용하던 장에 뉴스나 지표 발표로 갑자기 커질 때(지난 "
            "범위가 작아 작게 본다), 큰 움직임 뒤 곧바로 잦아들 때. 잔차 분포는 학습 20거래일 "
            "전체의 것이라 유난히 거친 날의 꼬리를 놓친다. 1분봉 날의 정답은 1분봉 규칙의 "
            "근사값이라 녹화일의 중간가 r과 조금 다르다."
        ),
    )

    def default_model(self) -> Model:
        """No regression before training: the defaults predict."""
        return Model(f"{self.name}-default", {_param(m, "n"): 0.0 for m in INPUTS})

    def fit(self, samples: Sequence[KindSample]) -> Model:
        """Both regressions on the samples with a range answer; ``book`` only where the spread
        is known (left out, ``book.n`` 0, without any). NothingToLearn without an answer."""
        answered = [s for s in samples if s.label is not None and s.answer is not None and _r(s) is not None]
        if not answered:
            raise NothingToLearn("no range answers to learn from")
        params = _regression("bars", answered)
        booked = [s for s in answered if _value(s.features[SPREAD]) is not None]
        params |= _regression("book", booked) if booked else {_param("book", "n"): 0.0}
        # Its mean log loss on them (as the nightly training's): the training data's.
        losses = [
            -math.log(max(f.probabilities[RANGE.index(s.label)], LOSS_FLOOR))
            for s in answered
            if s.label is not None and (f := _predicted(s.features, params)) is not None
        ]
        return Model.fitted(self.name, params, answered, sum(losses) / len(losses) if losses else 0.0)

    def predict(self, features: FeatureVector, model: Model) -> KindForecast:
        made = _predicted(features, model.params)
        if made is None:
            raise NotPredicted(Abstention.NO_BOOK)
        return made


def _r(sample: KindSample) -> float | None:
    r = None if sample.answer is None else sample.answer.path.get("r")
    return None if r is None else float(r)


def _regression(model: str, samples: Sequence[KindSample]) -> dict[str, float]:
    """One regression's parameters (``model``: its inputs, ``INPUTS``) fitted on ``samples``."""
    inputs = INPUTS[model]
    raw = np.array(
        [[np.nan if (v := _value(s.features[n])) is None else v for n in inputs] for s in samples],
        dtype=float,
    ).reshape(len(samples), len(inputs))
    known = ~np.isnan(raw)
    means = np.array(
        [float(raw[known[:, j], j].mean()) if known[:, j].any() else 0.0 for j in range(len(inputs))]
    )
    centred = np.where(known, raw - means, 0.0)
    for j in range(len(inputs)):
        if np.all(np.abs(centred[:, j]) < _CONSTANT):
            centred[:, j] = 0.0  # a constant input: its coefficient stays 0
    slots = [slot_of(s.features["minutes_since_open"]) for s in samples]
    seen = sorted(set(slots))
    dummies = np.array([[1.0 if slot == c else 0.0 for c in seen] for slot in slots]).reshape(
        len(samples), len(seen)
    )
    design = np.hstack([centred, dummies])
    y = np.array([math.log1p(r) for s in samples if (r := _r(s)) is not None], dtype=float)
    solution, *_ = np.linalg.lstsq(design, y, rcond=None)
    residuals = y - design @ solution
    params = {_param(model, "n"): float(len(samples))}
    for j, name in enumerate(inputs):
        params[_param(model, "coef", name)] = float(solution[j])
        params[_param(model, "mean", name)] = float(means[j])
    fitted_slots = dict(zip(seen, (float(v) for v in solution[len(inputs) :]), strict=True))
    for slot in range(SLOTS):
        nearest = min(seen, key=lambda c: (abs(c - slot), c))
        params[_slot_key(model, slot)] = fitted_slots[nearest]
    for percent, value in zip(PERCENTS, _quantiles([float(e) for e in residuals]), strict=True):
        params[_residual_key(model, percent)] = value
    return params


def _predicted(features: FeatureVector, params: Mapping[str, float]) -> KindForecast | None:
    """The forecast of ``params`` from ``features``: ``book`` where the spread is known and it
    was fitted, else ``bars``; the defaults before training; None without k."""
    k = features["flat_ticks"]
    if k is None or not math.isfinite(k) or k <= 0:
        return None
    model = (
        "book"
        if _value(features[SPREAD]) is not None and params.get(_param("book", "n"), 0.0) > 0
        else "bars"
    )
    if params.get(_param(model, "n"), 0.0) <= 0:
        return default_forecast(k)
    fitted = params[_slot_key(model, slot_of(features["minutes_since_open"]))]
    for name in INPUTS[model]:
        value = _value(features[name])
        if value is not None:
            fitted += params[_param(model, "coef", name)] * (value - params[_param(model, "mean", name)])
    knots = [params[_residual_key(model, p)] for p in PERCENTS]
    return forecast(fitted, knots, k)
