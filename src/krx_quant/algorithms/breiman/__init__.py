"""Breiman: a forest of trees on the barrier's answers. The barrier's first model (#178, #184).

Personality card:
- **Scientist**: Leo Breiman, who built classification and regression trees (CART, 1984),
  bagging (1996) and random forests (2001): many trees, each on a part of the data, that
  vote. López de Prado pairs the triple barrier's answers with such an ensemble (*Advances in
  Financial Machine Learning*, 2018, ch. 3 and 6).
- **Logic**: gradient-boosted trees (LightGBM, as Turing's) classify the four minutes after a
  bar close as up first, timeout or down first (the barrier's answer, ``barriers.py``) from
  the numbers Turing reads (every number of the Feature Store but the series and the range's:
  the flows, the book's order flow, the tape's sides and arrivals, the options' implied
  volatility, the time of day), and its own: the barrier's width at the bar close
  (``barrier_width``) and that width over the volatility it came from (``barrier_sigma``).
  A feature not known (the book on a backfilled day) is left missing, and the trees learn
  where missing ones go.
- **Own features**: ``barrier_width``, ``barrier_sigma``.
- **Learns**: over the last 20 trading days, on the barrier's answers (recorded days by the
  quotes, the others by the bar rule; windows without a class left out), Turing's boosting:
  200 rounds, the same trees for the same samples (one thread, a fixed seed).
- **Forecasts**: the trees' three probabilities, made to sum to 1; a third each before
  training. Without a width (no reference price at the bar close) it does not predict
  (``no_book``): there are no barriers to reach.
- **Fails**: in a market unlike its 20 training days; trees do not reach past the values
  they saw.

Its trees are the model's blob, as Turing's: a model trained before reads the features it was
trained on, in their order.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import lightgbm as lgb
import numpy as np

from krx_quant.algorithms.contract import Abstention, KindSample, Model, NothingToLearn, NotPredicted
from krx_quant.algorithms.forecasts import LOSS_FLOOR, number
from krx_quant.barriers import BARRIER
from krx_quant.cards import Card, Meaning, learned
from krx_quant.features import (
    BARRIER_SIGMA,
    BARRIER_WIDTH,
    FEATURES,
    RANGE_FEATURES,
    SERIES_FEATURES,
    FeatureVector,
)
from krx_quant.kinds import KindForecast

NAME = "breiman"
TRAINING_DAYS = 20
CLASSES = BARRIER.classes  # the trees' labels 0, 1, 2: up_first, timeout, down_first
OWN = (BARRIER_WIDTH, BARRIER_SIGMA)
# Turing's boosting (#184): the same settings, kept here so that its code says what it runs.
ROUNDS = 200
BOOSTING: dict[str, Any] = {
    "objective": "multiclass",
    "num_class": len(CLASSES),
    "learning_rate": 0.05,
    "num_leaves": 15,
    "min_data_in_leaf": 50,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 1.0,
    # The same trees for the same samples, and one core of the server's two.
    "num_threads": 1,
    "deterministic": True,
    "force_col_wise": True,  # with deterministic: no layout chosen by timing
    "seed": 0,
    "verbose": -1,
}
_TREES: dict[str, lgb.Booster] = {}  # read once per model version
_KEPT = 4
EVEN = KindForecast((1 / 3, 1 / 3, 1 / 3))  # before training: nothing to say


def _share(label: str) -> str:
    return f"share.{label}"


# Its trees are text (the model's blob), not parameters: the API does not show them.
PARAM_MEANINGS: Mapping[str, Meaning] = {
    "rounds": Meaning(
        f"부스팅 횟수(학습 설정, 늘 {ROUNDS}). 0이면 학습 전이라 트리가 없다.",
        "번",
        "밤 학습이 모델을 만들 때 적는다.",
    ),
    **{
        _share(label): Meaning(
            f"학습 표본(클래스가 있는 장벽 정답) 중 정답이 {title}이었던 비율. 학습 구간의 기본 비율을 "
            "보여 주는 값이고, 예측은 트리만으로 낸다. 학습 전에는 0이다.",
            "비율",
            learned(TRAINING_DAYS),
        )
        for label, title in zip(CLASSES, BARRIER.class_titles, strict=True)
    },
}


class Breiman:
    name = NAME
    kind = BARRIER.name
    # The numbers Turing reads (every number of the Feature Store but the series and the
    # range's, #181), and its own, the barrier's (#183).
    features: tuple[str, ...] = tuple(n for n in FEATURES if n not in SERIES_FEATURES | RANGE_FEATURES)
    training_days = TRAINING_DAYS
    param_meanings = PARAM_MEANINGS
    card = Card(
        scientist="Leo Breiman",
        character="트리 앙상블",
        logic=(
            "LightGBM 그래디언트 부스팅 트리로 봉 마감 뒤 4분 동안 위·아래 장벽 중 어느 쪽에 먼저 닿을지(위 "
            "먼저·시간 만료·아래 먼저, 장벽 정답)를 분류한다. Turing이 읽는 피처 저장소의 숫자 피처(프로그램"
            "매매·외국계 회원사 흐름, 호가의 주문 흐름, 체결의 방향과 도착, 옵션 내재변동성, 시간대, 최우선 "
            "호가 포함)에 그 봉 마감의 장벽 폭 w(`barrier_width`)와 폭 ÷ 4분 변동성(`barrier_sigma`)을 "
            "더한다. 삼중 장벽 정답에 트리 앙상블을 학습하는 López de Prado의 짝이다. 모르는 피처(백필만 "
            "있는 날의 호가·체결)는 비워 두고 트리가 다룬다."
        ),
        unique_features=OWN,
        training=(
            "최근 20거래일의 장벽 정답(녹화된 날은 호가의 정답, 그 전 날은 1분봉 규칙)으로 LightGBM을 "
            "Turing과 같은 설정으로 200번 부스팅한다(학습률 0.05, 잎 15개, 잎당 최소 표본 50, 피처·표본 "
            "80%, L2 1). 클래스가 없는 정답(채점할 수 없는 창, 1분봉 날에 같은 분에 양쪽이 닿은 창)과 "
            "녹화일의 VI 중 봉 마감, 특수 상황 창은 뺀다. 한 스레드와 고정 시드라 같은 표본이면 같은 트리가 "
            "나온다."
        ),
        forecasting=(
            "장벽 정답으로 직접 학습한 LightGBM 다중 분류 트리가 위 먼저·시간 만료·아래 먼저의 세 확률을 "
            "낸다(합이 1이 되게 나눈다). 모르는 피처는 비워 두고, 트리가 학습에서 정한 쪽으로 보낸다. 학습 "
            "전(트리 없음)에는 1/3씩이다. 봉 마감에 기준가가 없어 폭을 모르면 장벽이 없으니 예측하지 "
            "않는다(no_book)."
        ),
        failure=(
            "학습한 20거래일과 다른 장세(국면 전환, 큰 이벤트가 있는 날)에서 틀리기 쉽다. 트리는 학습에서 본 "
            "값의 범위 밖을 외삽하지 못한다. 1분봉 날의 정답은 1분봉 규칙의 근사라 녹화일의 호가 정답과 "
            "조금 다르고, 그 날에는 호가·체결 피처가 비어 있어서 녹화가 쌓일수록 그 피처의 몫이 바뀐다."
        ),
    )

    def default_model(self) -> Model:
        """No trees before training: a third each."""
        return Model(f"{self.name}-default", {"rounds": 0.0} | {_share(c): 0.0 for c in CLASSES})

    def predict(self, features: FeatureVector, model: Model) -> KindForecast:
        if features[BARRIER_WIDTH] is None:
            raise NotPredicted(Abstention.NO_BOOK)  # no reference price: no barriers
        if model.blob is None:
            return EVEN
        trees = _trees(model)
        # In the order the trees were trained on: features registered since are not theirs.
        row = np.array([[number(features[n]) for n in trees.feature_name()]], dtype=float)
        return KindForecast(_normalized(trees.predict(row, num_threads=1)[0]))

    def fit(self, samples: Sequence[KindSample]) -> Model:
        """The trees of the samples with a barrier class (NothingToLearn without any)."""
        answered = [s for s in samples if s.label is not None]
        if not answered:
            raise NothingToLearn("no barrier answers to learn from")
        x = np.array([[number(s.features[n]) for n in self.features] for s in answered], dtype=float)
        y = np.array([CLASSES.index(s.label or "") for s in answered])  # all have one
        data = lgb.Dataset(x, y, feature_name=list(self.features), free_raw_data=True)
        booster = lgb.train(BOOSTING, data, num_boost_round=ROUNDS)
        probabilities = np.asarray(booster.predict(x, num_threads=1), dtype=float)
        loss = -float(np.mean(np.log(np.maximum(probabilities[np.arange(len(y)), y], LOSS_FLOOR))))
        params = {"rounds": float(ROUNDS)} | {
            _share(c): float(np.mean(y == i)) for i, c in enumerate(CLASSES)
        }
        return Model.fitted(self.name, params, answered, loss, blob=booster.model_to_string())


def _trees(model: Model) -> lgb.Booster:
    trees = _TREES.get(model.version)
    if trees is None:
        if len(_TREES) >= _KEPT:
            _TREES.clear()
        trees = _TREES[model.version] = lgb.Booster(model_str=model.blob)
    return trees


def _normalized(p: Sequence[float]) -> tuple[float, float, float]:
    total = float(sum(p))
    up, timeout, down = (float(v) / total for v in p)
    return up, timeout, down
