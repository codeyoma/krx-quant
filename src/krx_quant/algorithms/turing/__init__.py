"""Turing: every feature at once, and a learner that finds which of them matter.

Personality card:
- **Scientist**: Alan Turing.
- **Logic**: gradient-boosted trees (LightGBM) classify the next four minutes as up, flat
  or down from every number the Feature Store has: the other algorithms' features (program
  trading and foreign member firms' flows, the book's order flow, the tape's sides and
  arrivals, the options' implied volatility), and its own. A feature not known (the book on
  a backfilled day) is left missing, and the trees learn where missing ones go. The expected
  move is the classes' mean moves in training, weighted by their probabilities.
- **Own features**: the time of day, the options expiry, the investor estimates, the
  best quotes: their spread in ticks, the balance of their sizes and how those sizes
  changed over the last minute (#55), and the same 10-minute slot's return on the days
  before (``SLOT_RETURN_FEATURES``, #265: the previous day's, the last 5's and 20's means;
  Breiman reads them too).
- **Learns**: over the last 60 trading days, a fixed number of boosting rounds, the same
  trees for the same samples (one thread, a fixed seed).
- **Fails**: in a market unlike its 60 training days; trees do not reach past the values
  they saw.

It began as the one-minute version (#16); the book's, the tape's and the options' features
came once recordings had piled up (#55). A model trained before reads the features it was
trained on, in their order. The series features (``minute_ticks``, ``microprice_ticks``,
``kospi200f_moves``) are left to Markov and Kalman, the range's own features
(``RANGE_FEATURES``, #181) to the range's Mandelbrot, and the barrier's (``BARRIER_FEATURES``,
#183) to the barrier's algorithms: they came after Turing, which learns the direction as it
did.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import lightgbm as lgb
import numpy as np

from krx_quant.algorithms.contract import Model, Sample
from krx_quant.algorithms.forecasts import UNKNOWN, class_moves, number, with_outcomes
from krx_quant.baselines import Forecast
from krx_quant.cards import TOUCH_LABEL, Card, Meaning, class_move_meanings
from krx_quant.features import (
    BARRIER_FEATURES,
    FEATURES,
    RANGE_FEATURES,
    SERIES_FEATURES,
    SLOT_RETURN_DAYS,
    FeatureVector,
    slot_return_feature,
)
from krx_quant.pricing import Direction

CLASSES = (Direction.DOWN, Direction.FLAT, Direction.UP)  # the trees' labels 0, 1, 2
# Program trading and foreign member firms' flows were its own until Shannon (#26), which
# comes before it, read them too.
OWN = (
    "minutes_since_open",
    "days_to_expiry",
    "investor_foreign",
    "investor_institution",
    "spread_ticks",
    "top_imbalance",
    "top_size_change",
    *(slot_return_feature(d) for d in SLOT_RETURN_DAYS),
)
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
_FLOOR = 1e-6
_TREES: dict[str, lgb.Booster] = {}  # read once per model version
_KEPT = 4
TRAINING_DAYS = 60
# Its trees are text (the model's blob), not parameters: the API does not show them.
PARAM_MEANINGS: Mapping[str, Meaning] = {
    "rounds": Meaning(
        f"부스팅 횟수(학습 설정, 늘 {ROUNDS}). 0이면 학습 전이라 트리가 없다.",
        "번",
        "밤 학습이 모델을 만들 때 적는다.",
    ),
    **class_move_meanings(TRAINING_DAYS),
}


class Turing:
    name = "turing"
    # Every number the Feature Store has, at registration, but the range's (#181) and the
    # barrier's (#183): register Turing last.
    features: tuple[str, ...] = tuple(
        n for n in FEATURES if n not in SERIES_FEATURES | RANGE_FEATURES | BARRIER_FEATURES
    )
    training_days = TRAINING_DAYS
    param_meanings = PARAM_MEANINGS
    card = Card(
        scientist="Alan Turing",
        character="만능 학습",
        logic=(
            "LightGBM 그래디언트 부스팅 트리로 4분 안의 상승·보합·하락(도달 정답)을 분류한다. 피처 저장소의 "
            "모든 숫자 "
            "피처(다른 알고리즘의 프로그램매매·외국계 회원사 흐름, 호가의 주문 흐름, 체결의 방향과 도착, "
            "옵션 내재변동성 포함)에 시간대, 옵션 만기까지 남은 거래일, 투자자별 추정 흐름, 최우선 호가의 "
            "스프레드·잔량 균형·잔량 변화, 앞선 거래일들의 같은 10분 시간대 수익률(전일, 5일·20일 평균, "
            "#265)을 더한다. 모르는 피처(백필만 있는 날의 호가)는 비워 두고 트리가 다룬다."
        ),
        unique_features=OWN,
        training=(
            "최근 60거래일 롤링으로 LightGBM을 정해진 횟수(200번) 부스팅한다. "
            "같은 표본이면 같은 트리가 나온다."
        ),
        forecasting=(
            f"{TOUCH_LABEL}으로 직접 학습한 LightGBM 다중 분류 트리가 세 확률을 "
            "낸다(합이 1이 되게 나눈다). 모르는 피처는 비워 두고, 트리가 학습에서 정한 쪽으로 보낸다. "
            "기대수익은 학습에서 클래스별 평균 4분 변화를 세 확률로 가중한 값이다. 학습 전(트리 없음)에는 세 "
            "확률을 1/3씩 둔다."
        ),
        failure=(
            "학습한 60거래일과 다른 장세(국면 전환, 큰 이벤트가 있는 날)에서 틀리기 쉽다. 트리는 학습에서 본 "
            "값의 범위 밖을 외삽하지 못한다. 녹화 전 날에는 호가·체결 피처가 비어 있어서, 녹화가 쌓일수록 그 "
            "피처의 몫이 바뀐다."
        ),
    )

    def default_model(self) -> Model:
        """No trees before training: nothing to say."""
        return Model(f"{self.name}-default", {"rounds": 0.0} | {f"move.{d.value}": 0.0 for d in CLASSES})

    def predict(self, features: FeatureVector, model: Model) -> Forecast:
        if model.blob is None:
            return UNKNOWN
        trees = _trees(model)
        # In the order the trees were trained on: features registered since are not theirs.
        row = np.array([[number(features[n]) for n in trees.feature_name()]], dtype=float)
        p_down, p_flat, p_up = _normalized(trees.predict(row, num_threads=1)[0])
        params = model.params
        expected = p_up * params["move.up"] + p_flat * params["move.flat"] + p_down * params["move.down"]
        return Forecast(p_up, p_flat, p_down, expected)

    def fit(self, samples: Sequence[Sample]) -> Model:
        scored = with_outcomes(samples)
        x = np.array([[number(s.features[n]) for n in self.features] for s in scored], dtype=float)
        y = np.array([CLASSES.index(s.outcome or Direction.FLAT) for s in scored])  # all have one
        data = lgb.Dataset(x, y, feature_name=list(self.features), free_raw_data=True)
        booster = lgb.train(BOOSTING, data, num_boost_round=ROUNDS)
        probabilities = np.asarray(booster.predict(x, num_threads=1), dtype=float)
        loss = -float(np.mean(np.log(np.maximum(probabilities[np.arange(len(y)), y], _FLOOR))))
        params = {"rounds": float(ROUNDS)} | class_moves(scored)
        return Model.fitted(self.name, params, scored, loss, blob=booster.model_to_string())


def _trees(model: Model) -> lgb.Booster:
    trees = _TREES.get(model.version)
    if trees is None:
        if len(_TREES) >= _KEPT:
            _TREES.clear()
        trees = _TREES[model.version] = lgb.Booster(model_str=model.blob)
    return trees


def _normalized(p: Sequence[float]) -> tuple[float, float, float]:
    total = float(sum(p))
    down, flat, up = (float(v) / total for v in p)
    return down, flat, up
