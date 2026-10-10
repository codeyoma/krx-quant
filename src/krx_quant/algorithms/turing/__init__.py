"""Turing: every feature at once, and a learner that finds which of them matter.

Personality card:
- **Scientist**: Alan Turing.
- **Logic**: gradient-boosted trees (LightGBM) classify the next four minutes as up, flat
  or down from every number the Feature Store has: the other algorithms' features, and its
  own: the time of day, the trading days to the KOSPI200 options expiry, and the day's flows
  (program trading, foreign member firms, brokers' estimates of foreign and institutional
  buying). A feature not known (a flow on a backfilled day) is left missing, and the trees
  learn where missing ones go. The expected move is the classes' mean moves in training,
  weighted by their probabilities.
- **Own features**: the time of day, the options expiry, and the flows.
- **Learns**: over the last 60 trading days, a fixed number of boosting rounds, the same
  trees for the same samples (one thread, a fixed seed).

This is the one-minute version: order book features come once ten trading days of quotes
are recorded (#16). The series features (``minute_ticks``) are left to Markov.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import lightgbm as lgb
import numpy as np

from krx_quant.algorithms.contract import Card, Model, Sample
from krx_quant.algorithms.forecasts import UNKNOWN, with_outcomes
from krx_quant.baselines import Forecast
from krx_quant.features import FEATURES, SERIES_FEATURES, FeatureVector
from krx_quant.pricing import Direction

CLASSES = (Direction.DOWN, Direction.FLAT, Direction.UP)  # the trees' labels 0, 1, 2
OWN = (
    "minutes_since_open",
    "days_to_expiry",
    "program_net",
    "member_foreign_net",
    "investor_foreign",
    "investor_institution",
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
    "seed": 0,
    "verbose": -1,
}
_FLOOR = 1e-6
_TREES: dict[str, lgb.Booster] = {}  # read once per model version
_KEPT = 4


class Turing:
    name = "turing"
    # Every number the Feature Store has, at registration: register Turing last.
    features: tuple[str, ...] = tuple(n for n in FEATURES if n not in SERIES_FEATURES)
    training_days = 60
    card = Card(
        scientist="Alan Turing",
        character="만능 학습",
        logic=(
            "LightGBM 그래디언트 부스팅 트리로 4분 뒤 상승·보합·하락을 분류한다. 피처 저장소의 모든 숫자 피처"
            "(다른 알고리즘의 피처 포함)에 시간대, 옵션 만기까지 남은 거래일, 프로그램매매·외국계 회원사·"
            "투자자별 추정 흐름을 더한다. 모르는 피처(백필만 있는 날의 흐름)는 비워 두고 트리가 다룬다."
        ),
        unique_features=OWN,
        training=(
            "최근 60거래일 롤링으로 LightGBM을 정해진 횟수(200번) 부스팅한다. "
            "같은 표본이면 같은 트리가 나온다."
        ),
    )

    def default_model(self) -> Model:
        """No trees before training: nothing to say."""
        return Model(f"{self.name}-default", {"rounds": 0.0} | {f"move.{d.value}": 0.0 for d in CLASSES})

    def predict(self, features: FeatureVector, model: Model) -> Forecast:
        if model.blob is None:
            return UNKNOWN
        row = np.array([[_number(features[n]) for n in self.features]], dtype=float)
        p_down, p_flat, p_up = _normalized(_trees(model).predict(row, num_threads=1)[0])
        params = model.params
        expected = p_up * params["move.up"] + p_flat * params["move.flat"] + p_down * params["move.down"]
        return Forecast(p_up, p_flat, p_down, expected)

    def fit(self, samples: Sequence[Sample]) -> Model:
        scored = with_outcomes(samples)
        x = np.array([[_number(s.features[n]) for n in self.features] for s in scored], dtype=float)
        y = np.array([CLASSES.index(s.outcome) for s in scored if s.outcome is not None])
        data = lgb.Dataset(x, y, feature_name=list(self.features), free_raw_data=True)
        booster = lgb.train(BOOSTING, data, num_boost_round=ROUNDS)
        probabilities = np.asarray(booster.predict(x, num_threads=1), dtype=float)
        loss = -float(np.mean(np.log(np.maximum(probabilities[np.arange(len(y)), y], _FLOOR))))
        params = {"rounds": float(ROUNDS)} | _moves(scored)
        return Model.fitted(self.name, params, scored, loss, blob=booster.model_to_string())


def _moves(scored: Sequence[Sample]) -> dict[str, float]:
    """Each class's mean move in training, in ticks (0 where none is known)."""
    out = {}
    for d in CLASSES:
        moves = [s.change_ticks for s in scored if s.outcome is d and s.change_ticks is not None]
        out[f"move.{d.value}"] = sum(moves) / len(moves) if moves else 0.0
    return out


def _trees(model: Model) -> lgb.Booster:
    trees = _TREES.get(model.version)
    if trees is None:
        if len(_TREES) >= _KEPT:
            _TREES.clear()
        trees = _TREES[model.version] = lgb.Booster(model_str=model.blob)
    return trees


def _number(value: float | None) -> float:
    return math.nan if value is None or not math.isfinite(value) else value


def _normalized(p: Sequence[float]) -> tuple[float, float, float]:
    total = float(sum(p))
    down, flat, up = (float(v) / total for v in p)
    return down, flat, up
