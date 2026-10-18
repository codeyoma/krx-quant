"""Neyman: gradient-boosted trees, and a second opinion on each of their up and down calls (#266).

Personality card:
- **Scientist**: Jerzy Neyman.
- **Logic**: meta-labeling (López de Prado 2018, ch. 3). A first model, gradient-boosted trees
  (LightGBM) on Turing's features and the barrier's two, calls up, flat or down. A second, a
  logistic regression, says how likely an up or down call is to be right, from the call's
  own probability (its log odds), flat's, its side, whether the book was known, the
  volatility at the bar close (the barrier's width w, and w over the four-minute volatility
  σ̂₄ it came from), the spread and the time of day. That chance m becomes the called class's
  probability, the rest split between the other two as the first model had them: a call it
  doubts (m well under a half) gives way to flat or the other side. Fewer up and down calls,
  more of them right.
- **Own features**: the barrier's width w and w ÷ σ̂₄ (``BARRIER_FEATURES``, #183), which no
  other direction algorithm reads (the barrier's Breiman, of another kind, does).
- **Learns**: over the last 20 trading days, the first model on all of them; the second on the
  first model's out-of-sample calls. The recorded days and the days with backfilled bars
  only are each cut into four runs of consecutive days; each pair of runs is forecast by a
  first model trained on the other days, the day after each held day left out too (an
  embargo: the next day's previous-day slot return and volatility read the held day; the 5-
  and 20-day slot returns of the days after still hold it, a twentieth to a fifth, a mild
  leak left as it is). A four-minute label
  never crosses a day.
- **Fails**: where trees fail (a market unlike its 20 days), and on days unlike those its
  second model saw; with few out-of-sample days or calls, the second model is the share that
  was right, and nothing more.

It differs from Turing on purpose (#266 review): a first model identical to Turing's would
move with it (a clone, refused at pre-validation). Its first model also reads the barrier's
two features, learns over 20 days, not 60, from another seed.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

import lightgbm as lgb
import numpy as np

from krx_quant.algorithms.contract import Model, Sample
from krx_quant.algorithms.forecasts import UNKNOWN, class_moves, number, with_outcomes
from krx_quant.algorithms.turing import BOOSTING, CLASSES, ROUNDS, Turing
from krx_quant.baselines import Forecast
from krx_quant.cards import TOUCH_LABEL, Card, Meaning, class_move_meanings
from krx_quant.features import BARRIER_SIGMA, BARRIER_WIDTH, FeatureVector
from krx_quant.feeds.base import kst_day
from krx_quant.pricing import Direction, most_likely

TRAINING_DAYS = 20
FEATURES: tuple[str, ...] = (*Turing.features, BARRIER_WIDTH, BARRIER_SIGMA)
FIRST = BOOSTING | {"seed": 1}  # Turing's settings from another seed: trees of its own
FOLDS = 4  # runs of consecutive days (recorded and backfilled apart) the out-of-sample calls come from
EMBARGO = 1  # days after a held day left out of the first model that forecasts it
MIN_CALLS = 200  # out-of-sample up and down calls the second model needs, and
MIN_DAYS = 8  # the days they come from: calls within a day overlap, as the symbols move together
L2 = 1.0  # the second model's ridge on its coefficients (not its intercept)
_FLOOR = 1e-6
_TREES: dict[str, lgb.Booster] = {}  # read once per model version
_KEPT = 4
# What the second model reads of a call: its own probability (log odds), flat's, its side,
# whether the book was known (a recorded day), and the volatility, spread and time of day.
CALL, FLAT, UP, BOOK = "logit_call", "p_flat", "up", "book"
SPREAD = "spread_ticks"
INPUTS = (CALL, FLAT, UP, BOOK, BARRIER_WIDTH, BARRIER_SIGMA, SPREAD, "minutes_since_open")
_INPUT_MEANINGS = {
    CALL: ("1차 모델이 낸 그 방향(상승이나 하락)의 확률의 로그 오즈", "로그 오즈"),
    FLAT: ("1차 모델이 낸 보합 확률", "확률(0–1)"),
    UP: ("그 방향이 상승이면 1, 하락이면 0", "0/1"),
    BOOK: ("봉 마감의 최우선 호가를 알았으면 1(녹화된 날), 몰랐으면 0(백필만 있는 날)", "0/1"),
    BARRIER_WIDTH: ("봉 마감의 장벽 폭 w", "틱"),
    BARRIER_SIGMA: (
        "봉 마감의 장벽 폭 w ÷ 4분 변동성 σ̂₄(1 근처면 변동성이, 1보다 크면 비용 k가 폭을 정함)",
        "배",
    ),
    SPREAD: ("봉 마감의 최우선 호가 스프레드", "틱"),
    "minutes_since_open": ("장 시작 뒤 지난 분", "분"),
}
_FITTED = "밤 학습이 모델을 만들 때 적는다."


def _param_meanings() -> dict[str, Meaning]:
    out: dict[str, Meaning] = {
        "rounds": Meaning(
            f"1차 모델의 부스팅 횟수(학습 설정, 늘 {ROUNDS}). 0이면 학습 전이라 트리가 없다.", "번", _FITTED
        ),
        "meta.intercept": Meaning(
            "2차 모델(로지스틱 회귀)의 절편. 입력이 모두 학습 평균일 때 상승·하락 예측이 맞을 로그 오즈.",
            "로그 오즈",
            _FITTED,
        ),
        "meta.calls": Meaning(
            "2차 모델이 배운 표본 밖 상승·하락 예측 수(1차를 녹화일·백필일 각각 연속한 날 "
            f"{FOLDS}묶음으로 교차 적합). {MIN_CALLS}개보다 적거나 {MIN_DAYS}일보다 적은 날에서 "
            "나왔으면 2차는 절편만(그 맞은 비율)이고, 0이면 2차 없이 1차 그대로 낸다.",
            "개",
            _FITTED,
        ),
        "meta.days": Meaning("그 표본 밖 상승·하락 예측이 나온 날 수.", "일", _FITTED),
        "meta.precision": Meaning(
            "그 표본 밖 상승·하락 예측 중 도달 정답과 같았던 비율(1차만의 방향 정밀도).", "비율(0–1)", _FITTED
        ),
    }
    for name, (text, unit) in _INPUT_MEANINGS.items():
        out[f"meta.coef.{name}"] = Meaning(
            f"2차 모델의 계수: {text}(표준화한 값) 1 표준편차당 맞을 로그 오즈의 변화. 절편만이면 0.",
            "로그 오즈",
            _FITTED,
        )
        out[f"meta.mean.{name}"] = Meaning(
            f"2차 모델이 표준화에 쓴 {text}의 학습 평균. 모르는 값은 이것으로 둔다.", unit, _FITTED
        )
        out[f"meta.scale.{name}"] = Meaning(
            f"2차 모델이 표준화에 쓴 {text}의 학습 표준편차(퍼짐이 없으면 1).", unit, _FITTED
        )
    return out | dict(class_move_meanings(TRAINING_DAYS))


# Its first model's trees are text (the model's blob), not parameters: the API does not show them.
PARAM_MEANINGS: Mapping[str, Meaning] = _param_meanings()


class Neyman:
    name = "neyman"
    # Turing's features, and the barrier's two, its own: register it after Turing.
    features: tuple[str, ...] = FEATURES
    training_days = TRAINING_DAYS
    param_meanings = PARAM_MEANINGS
    card = Card(
        scientist="Jerzy Neyman",
        character="두 번째 의견",
        logic=(
            "meta-labeling(López de Prado 2018, 3장). 1차 모델(LightGBM)이 Turing의 피처와 장벽 폭 w, "
            "w ÷ 4분 변동성 σ̂₄로 상승·보합·하락을 가른다. 2차 모델(로지스틱 회귀)이 1차의 상승·하락 "
            "예측마다 그 예측이 맞을 확률 m을 낸다. 입력은 그 방향 확률의 로그 오즈, 보합 확률, 방향, "
            "호가를 알았는지, w, w ÷ σ̂₄, 스프레드, 장 시작 뒤 지난 분이다. m이 그 방향의 확률이 되고, "
            "나머지는 다른 두 클래스에 1차의 비율대로 나뉜다. 의심스러운 예측은 보합이나 반대쪽에 "
            "자리를 내준다. 상승·하락 예측 수를 줄이고 맞는 비율을 올린다."
        ),
        unique_features=(BARRIER_WIDTH, BARRIER_SIGMA),
        training=(
            "최근 20거래일. 1차는 모든 날로 Turing의 설정(200번 부스팅)에 다른 시드로 학습한다. 2차는 "
            "1차의 표본 밖 예측으로 배운다: 녹화된 날과 백필만 있는 날을 각각 연속한 날 네 묶음으로 "
            "나누고, 묶음마다 나머지 날(그 묶음 각 날의 다음 날은 빼고)로 학습한 1차가 그 묶음을 "
            "예측한다. 그중 상승·하락 예측이 도달 정답과 같았는지(1/0)를 로지스틱 회귀(L2 1, 입력 "
            f"표준화)로 배운다. 표본 밖 상승·하락 예측이 {MIN_CALLS}개보다 적거나 {MIN_DAYS}일보다 "
            "적은 날에서 나왔으면 그 맞은 비율만 쓴다."
        ),
        forecasting=(
            f"1차가 {TOUCH_LABEL}의 세 확률을 낸다. 가장 큰 쪽이 보합이면 그대로다. 상승이나 "
            "하락이면 2차가 그 예측이 맞을 확률 m을 내고, 그 방향의 확률을 m으로 바꾸고 남은 1 − m을 "
            "다른 두 클래스에 1차의 비율대로 나눈다. 문턱은 없다: m이 낮으면 다른 클래스가 더 커져 "
            "예측이 바뀐다. 기대수익은 학습에서 클래스별 평균 4분 변화를 새 확률로 가중한 값이다. "
            "학습 전에는 세 확률을 1/3씩 둔다."
        ),
        failure=(
            "트리가 틀리는 장(학습한 20거래일과 다른 장세)에서 같이 틀린다. 2차가 본 적 없는 "
            "변동성·시간대에서는 맞을 확률을 잘못 잴 수 있다. 표본 밖 예측의 날이 적으면 2차는 평균 "
            "맞은 비율일 뿐이라 거르는 힘이 없다. 1차가 Turing과 비슷한 트리라 예측이 Turing과 "
            "닮을 수 있다(상관 0.9가 넘으면 복제)."
        ),
    )

    def default_model(self) -> Model:
        """No trees before training: nothing to say, and no second opinion."""
        params = {
            "rounds": 0.0,
            "meta.intercept": 0.0,
            "meta.calls": 0.0,
            "meta.days": 0.0,
            "meta.precision": 0.0,
        }
        for name in INPUTS:
            params |= {f"meta.coef.{name}": 0.0, f"meta.mean.{name}": 0.0, f"meta.scale.{name}": 1.0}
        return Model(f"{self.name}-default", params | {f"move.{d.value}": 0.0 for d in CLASSES})

    def predict(self, features: FeatureVector, model: Model) -> Forecast:
        if model.blob is None:
            return UNKNOWN
        trees = _trees(model)
        # In the order the trees were trained on, as Turing's.
        row = np.array([[number(features[n]) for n in trees.feature_name()]], dtype=float)
        return _second_opinion(trees.predict(row, num_threads=1)[0], features, model.params)

    def fit(self, samples: Sequence[Sample]) -> Model:
        scored = with_outcomes(samples)
        x = np.array([[number(s.features[n]) for n in FEATURES] for s in scored], dtype=float)
        y = np.array([CLASSES.index(s.outcome or Direction.FLAT) for s in scored])  # all have one
        final = _boost(x, y)
        out_of_fold = _out_of_fold(scored, x, y)
        params = {"rounds": float(ROUNDS)} | _second_model(scored, out_of_fold) | class_moves(scored)
        # The loss of the forecasts made from the out-of-sample first model (the final one's on a
        # day it never forecast out of sample: fewer than two days).
        first = np.where(np.isnan(out_of_fold), np.asarray(final.predict(x, num_threads=1)), out_of_fold)
        forecasts = [_second_opinion(p, s.features, params) for p, s in zip(first, scored, strict=True)]
        loss = -float(
            np.mean(
                [
                    math.log(max(_probability(f, s.outcome), _FLOOR))
                    for f, s in zip(forecasts, scored, strict=True)
                ]
            )
        )
        return Model.fitted(self.name, params, scored, loss, blob=final.model_to_string())


def _second_opinion(
    trees: Sequence[float] | Any, features: FeatureVector, params: Mapping[str, float]
) -> Forecast:
    """The forecast from the first model's three probabilities (``trees``, down, flat, up): as
    they are when flat is the most likely, or without a second model; else the call's
    probability replaced by the second model's chance m that it is right, the rest 1 − m split
    between the other two as the first model had them."""
    p_down, p_flat, p_up = _normalized(trees)
    call = most_likely(p_up, p_flat, p_down)
    if call is not Direction.FLAT and params["meta.calls"] > 0:
        m = _chance(params, _inputs(p_up, p_flat, p_down, call, features))
        if call is Direction.UP:
            p_up, (p_flat, p_down) = m, _rest(1 - m, p_flat, p_down)
        else:
            p_down, (p_flat, p_up) = m, _rest(1 - m, p_flat, p_up)
    expected = p_up * params["move.up"] + p_flat * params["move.flat"] + p_down * params["move.down"]
    return Forecast(p_up, p_flat, p_down, expected)


def _rest(left: float, a: float, b: float) -> tuple[float, float]:
    """``left`` split between two classes as ``a`` to ``b``; evenly when both are 0."""
    total = a + b
    return (left / 2, left / 2) if total <= 0 else (left * a / total, left * b / total)


def _boost(x: np.ndarray, y: np.ndarray) -> lgb.Booster:
    """The first model on ``x`` and ``y``."""
    data = lgb.Dataset(x, y, feature_name=list(FEATURES), free_raw_data=True)
    return lgb.train(FIRST, data, num_boost_round=ROUNDS)


def folds(scored: Sequence[Sample]) -> list[tuple[np.ndarray, np.ndarray]]:
    """The out-of-sample folds of ``scored``: (held, trained) sample indexes. The recorded days
    (``Sample.reference`` "mid") and the others are each cut into ``FOLDS`` runs of consecutive
    days; fold i holds both i-th runs and trains on every other day but the ``EMBARGO`` days
    after each held one. None with fewer than two days."""
    days = sorted({kst_day(s.bar_close) for s in scored})
    if len(days) < 2:
        return []
    recorded = sorted({kst_day(s.bar_close) for s in scored if s.reference == "mid"})
    others = [d for d in days if d not in set(recorded)]
    runs: list[set[date]] = [set() for _ in range(FOLDS)]
    for group in (recorded, others):
        for i, run in enumerate(np.array_split(np.arange(len(group)), min(FOLDS, len(group)) or 1)):
            runs[i] |= {group[int(j)] for j in run}
    position = {d: i for i, d in enumerate(days)}
    day_of = np.array([position[kst_day(s.bar_close)] for s in scored])
    out = []
    for held_days in runs:
        if not held_days:
            continue
        held = sorted(position[d] for d in held_days)
        embargoed = {h + e for h in held for e in range(1, EMBARGO + 1)} - set(held)
        held_mask = np.isin(day_of, held)
        trained_mask = ~held_mask & ~np.isin(day_of, sorted(embargoed))
        if trained_mask.any():
            out.append((np.flatnonzero(held_mask), np.flatnonzero(trained_mask)))
    return out


def _out_of_fold(scored: Sequence[Sample], x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Each sample's three first-model probabilities (down, flat, up) from a first model that
    never saw its day (``folds``); NaN where no fold holds it."""
    out = np.full((len(scored), len(CLASSES)), np.nan)
    for held, trained in folds(scored):
        out[held] = np.asarray(_boost(x[trained], y[trained]).predict(x[held], num_threads=1))
    return out


def _second_model(scored: Sequence[Sample], out_of_fold: np.ndarray) -> dict[str, float]:
    """The second model's parameters (``meta.*``), fitted on the first model's out-of-sample up
    and down calls; the share right alone (the intercept) with fewer than ``MIN_CALLS`` of them
    or ``MIN_DAYS`` days they come from; no second model (``meta.calls`` 0) without any."""
    rows: list[list[float]] = []
    right: list[float] = []
    days: set[date] = set()
    for p, sample in zip(out_of_fold, scored, strict=True):
        if np.isnan(p).any():
            continue
        p_down, p_flat, p_up = _normalized(p)
        call = most_likely(p_up, p_flat, p_down)
        if call is Direction.FLAT:
            continue
        rows.append(_inputs(p_up, p_flat, p_down, call, sample.features))
        right.append(float(sample.outcome is call))
        days.add(kst_day(sample.bar_close))
    params: dict[str, float] = {"meta.calls": float(len(right)), "meta.days": float(len(days))}
    params["meta.precision"] = sum(right) / len(right) if right else 0.0
    data = np.array(rows, dtype=float).reshape(len(rows), len(INPUTS))
    means = [float(np.nanmean(c)) if np.isfinite(c).any() else 0.0 for c in data.T]
    scales = []
    for c in data.T:
        known = c[np.isfinite(c)]
        spread = float(np.std(known)) if known.size else 0.0
        scales.append(spread if spread > 0 else 1.0)
    if len(right) >= MIN_CALLS and len(days) >= MIN_DAYS:
        standard = np.nan_to_num((data - np.array(means)) / np.array(scales), nan=0.0)
        weights = _logistic(standard, np.array(right))
    else:  # the share right, and nothing more (none: ``meta.calls`` 0 turns the second model off)
        share = min(max(params["meta.precision"], _FLOOR), 1 - _FLOOR)
        weights = np.zeros(len(INPUTS) + 1)
        weights[0] = math.log(share / (1 - share)) if right else 0.0
    params["meta.intercept"] = float(weights[0])
    for name, w, mean, scale in zip(INPUTS, weights[1:], means, scales, strict=True):
        params |= {f"meta.coef.{name}": float(w), f"meta.mean.{name}": mean, f"meta.scale.{name}": scale}
    return params


def _logistic(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """A logistic regression's intercept and coefficients by Newton's method with step
    halving, ``L2`` on the coefficients: the same samples, the same weights."""
    design = np.column_stack([np.ones(len(y)), x])
    penalty = L2 * np.eye(design.shape[1])
    penalty[0, 0] = 0.0

    def objective(w: np.ndarray) -> float:
        z = np.clip(design @ w, -30, 30)
        return float(np.sum(np.logaddexp(0, z) - y * z) + 0.5 * w @ penalty @ w)

    w = np.zeros(design.shape[1])
    current = objective(w)
    for _ in range(100):
        p = 1 / (1 + np.exp(-np.clip(design @ w, -30, 30)))
        gradient = design.T @ (p - y) + penalty @ w
        hessian = design.T @ (design * (p * (1 - p))[:, None]) + penalty
        step = np.linalg.solve(hessian + 1e-9 * np.eye(len(w)), gradient)
        scale = 1.0
        while scale > 1e-6 and objective(w - scale * step) > current:
            scale /= 2
        w -= scale * step
        current = objective(w)
        if float(np.max(np.abs(scale * step))) < 1e-10:
            break
    return w


def _inputs(
    p_up: float, p_flat: float, p_down: float, call: Direction, features: FeatureVector
) -> list[float]:
    """What the second model reads of an up or down ``call`` (``INPUTS``): NaN where unknown."""
    up = call is Direction.UP
    p_call = min(max(p_up if up else p_down, _FLOOR), 1 - _FLOOR)
    spread = number(features[SPREAD])
    return [
        math.log(p_call / (1 - p_call)),
        p_flat,
        1.0 if up else 0.0,
        0.0 if math.isnan(spread) else 1.0,
        number(features[BARRIER_WIDTH]),
        number(features[BARRIER_SIGMA]),
        spread,
        number(features["minutes_since_open"]),
    ]


def _chance(params: Mapping[str, float], values: Sequence[float]) -> float:
    """The second model's chance that a call with ``values`` is right; an unknown value at its
    training mean."""
    z = params["meta.intercept"]
    for name, v in zip(INPUTS, values, strict=True):
        if math.isfinite(v):
            standard = (v - params[f"meta.mean.{name}"]) / params[f"meta.scale.{name}"]
            z += params[f"meta.coef.{name}"] * standard
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))


def _probability(forecast: Forecast, outcome: Direction | None) -> float:
    return {Direction.UP: forecast.p_up, Direction.FLAT: forecast.p_flat, Direction.DOWN: forecast.p_down}[
        outcome or Direction.FLAT
    ]


def _trees(model: Model) -> lgb.Booster:
    trees = _TREES.get(model.version)
    if trees is None:
        if len(_TREES) >= _KEPT:
            _TREES.clear()
        trees = _TREES[model.version] = lgb.Booster(model_str=model.blob)
    return trees


def _normalized(p: Sequence[float] | Any) -> tuple[float, float, float]:
    total = float(sum(p))
    down, flat, up = (float(v) / total for v in p)
    return down, flat, up
