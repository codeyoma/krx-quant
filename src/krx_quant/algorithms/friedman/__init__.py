"""Friedman: every daily input at once, and trees that find which of them matter. A candidate of
the daily kind ``open_to_close`` (#698).

Personality card:
- **Scientist**: Jerome H. Friedman, whose 2001 "Greedy function approximation: a gradient
  boosting machine" set out gradient boosting.
- **Logic**: gradient-boosted trees (LightGBM, multiclass) of the day's class on every daily
  feature (``daily_features.DAILY_FEATURES``: Cox's, Hurst's lookbacks,
  Lorenz's drawdown and range) and its own: the last five days' foreign and institution net buys.
  Small trees (at most ``num_leaves`` 7, ``min_data_in_leaf`` 100) and a strong L2 penalty: a day
  is one sample a symbol, and the inputs are weak. A missing input stays missing, and the trees
  learn where it goes.
- **Own features**: ``foreign_net_5``, ``institution_net_5``.
- **Learns**: on the last ``TRAINING_DAYS`` (2,450) trading days of every symbol pooled
  (``daily_window``). The number of rounds is where the log loss on the last 20% of the days
  stopped falling for ``PATIENCE`` rounds (of at most ``MAX_ROUNDS``), boosting on the first 80%;
  the trees are then grown again on all of them for that many rounds. One thread and a fixed
  seed: the same trees for the same samples.
- **Forecasts**: the trees' three probabilities. Before training, a third each.
- **Fails**: where ten years of days are too few for trees (Cox's card: they fit noise a linear
  model cannot), and outside the values they saw.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from typing import Any

import lightgbm as lgb
import numpy as np

from krx_quant.algorithms.contract import Model, TrainedOn
from krx_quant.algorithms.daily_window import Window, log_loss, window
from krx_quant.cards import Card, Meaning
from krx_quant.daily import DailySample
from krx_quant.features import FeatureVector
from krx_quant.kinds import KindForecast
from krx_quant.open_to_close import CLASSES, OPEN_TO_CLOSE

NAME = "friedman"
TRAINING_DAYS = 2_450
OWN: tuple[str, ...] = ("foreign_net_5", "institution_net_5")
# Every daily feature at its registration (#698), written out: a feature added later is not its own
# until its code says so (and its digest changes with it).
FEATURES: tuple[str, ...] = (
    "us_sox",
    "us_nvda",
    "us_mu",
    "vix",
    "foreign_net",
    "institution_net",
    "oc_1",
    "cc_1",
    "gap_1",
    "range_1",
    "oc_5",
    "vol_ratio",
    "sigma",
    "climate_up",
    "climate_down",
    "premarket_gap",
    "weekday",
    "month_end",
    "month_start",
    "night_futures",
    "cc_5",
    "cc_20",
    "cc_60",
    "drawdown",
    "range_5",
    "foreign_net_5",
    "institution_net_5",
)
MAX_ROUNDS = 400
PATIENCE = 30
FALLBACK_ROUNDS = 50  # too few days to hold out: a fixed small number
BOOSTING: dict[str, Any] = {
    "objective": "multiclass",
    "num_class": len(CLASSES),
    "learning_rate": 0.03,
    "num_leaves": 7,
    "max_depth": 3,
    "min_data_in_leaf": 100,
    "feature_fraction": 0.7,
    "bagging_fraction": 0.7,
    "bagging_freq": 1,
    "lambda_l2": 10.0,
    # The same trees for the same samples, and one core of the server's two.
    "num_threads": 1,
    "deterministic": True,
    "force_col_wise": True,  # with deterministic: no layout chosen by timing
    "seed": 0,
    "verbose": -1,
}
_KEPT = 4
_TREES: dict[str, lgb.Booster] = {}  # read once per model version
_LEARNED = (
    f"밤 학습이 최근 {TRAINING_DAYS}거래일(약 10년)의 KRX 일봉으로 매일 다시 정한다. 새 모델은 다음 거래일 "
    "08:00 교체 때부터 쓴다. 학습 전에는 기본값이다."
)
# Its trees are text (the model's blob), not parameters: the API does not show them.
PARAM_MEANINGS: Mapping[str, Meaning] = {
    "rounds": Meaning(
        f"부스팅 횟수: 학습 기간 앞 80%로 키우며 뒤 20%의 log loss가 {PATIENCE}번 동안 더 줄지 않은 곳(최대 "
        f"{MAX_ROUNDS}). 그 횟수로 전 기간에 다시 키운다.",
        "번",
        _LEARNED,
    ),
    "samples": Meaning("학습한 표본 수(종목 × 날).", "개", _LEARNED),
    "validation_loss": Meaning("고른 횟수의 검증 log loss(뒤 20%, 1/3씩이면 1.0986).", "nats", _LEARNED),
}


def _matrix(samples: Sequence[DailySample], features: Sequence[str]) -> np.ndarray:
    rows = [[_number(s.features[f]) for f in features] for s in samples]
    return np.array(rows, dtype=float).reshape(len(samples), len(features))


def _number(value: object) -> float:
    if not isinstance(value, int | float) or not math.isfinite(float(value)):
        return math.nan
    return float(value)


class Friedman:
    name = NAME
    kind = OPEN_TO_CLOSE.name
    answer_version = OPEN_TO_CLOSE.answer_version
    features: tuple[str, ...] = FEATURES
    training_days = TRAINING_DAYS
    param_meanings: Mapping[str, Meaning] = PARAM_MEANINGS
    code_from: tuple[type, ...] = (Window,)  # its window and split (``daily_window``)
    card = Card(
        scientist="Jerome H. Friedman",
        character="모든 하루 입력과 그중 무엇이 중요한지 찾는 트리",
        logic=(
            "LightGBM 그래디언트 부스팅 트리(다중 분류)로 그날 방향(상승·보합·하락)을 분류한다. 그보다 먼저 "
            "등록된 하루 한 번 피처를 모두 읽는다(`cox`의 것, `hurst`의 5·20·60일 움직임, `lorenz`의 고점 "
            "대비 하락과 범위). 고유 피처는 최근 5거래일 외국인·기관 순매수다. 하루가 종목당 표본 하나이고 "
            "입력이 약해서 트리를 작게(잎 7개, 잎마다 표본 100개 이상) 두고 L2 벌점을 크게 준다. 모르는 "
            "입력은 비워 두고 트리가 다룬다."
        ),
        unique_features=OWN,
        training=(
            f"최근 {TRAINING_DAYS}거래일(약 10년) 세 종목의 날을 한데 모은다. 학습 기간 앞 80%로 키우며 뒤 "
            f"20%의 log loss가 {PATIENCE}번 동안 더 줄지 않은 곳(최대 {MAX_ROUNDS}번)을 부스팅 횟수로 정하고"
            "(여러 세션 창이면 그 사이를 비운다), 그 횟수로 전 기간에 다시 키운다. 스레드 하나, 고정 seed라 "
            "같은 표본이면 같은 트리다."
        ),
        forecasting="트리의 세 확률이다(합이 1이 되게 나눈다). 학습 전에는 1/3씩이다.",
        failure=(
            "10년의 날이 트리에는 적다(`cox`의 카드: 선형 모델이 못 외우는 잡음을 트리는 외운다). 학습에서 "
            "본 값의 범위 밖은 외삽하지 못한다."
        ),
    )

    def default_model(self) -> Model:
        return Model(f"{self.name}-default", {})

    def fit(self, samples: Sequence[DailySample]) -> Model:
        w = window(samples, self.training_days)
        y = np.array([CLASSES.index(label) for label in w.labels])
        x = _matrix(w.samples, self.features)
        names = list(self.features)
        rounds, validation = FALLBACK_ROUNDS, math.nan
        if w.splits(BOOSTING["min_data_in_leaf"] * 2):
            fitted = lgb.Dataset(x[w.early], y[w.early], feature_name=names, free_raw_data=True)
            held = lgb.Dataset(x[w.late], y[w.late], reference=fitted, free_raw_data=True)
            trial = lgb.train(
                BOOSTING,
                fitted,
                num_boost_round=MAX_ROUNDS,
                valid_sets=[held],
                callbacks=[lgb.early_stopping(PATIENCE, verbose=False)],
            )
            rounds = max(1, trial.best_iteration or MAX_ROUNDS)
            p = np.asarray(trial.predict(x[w.late], num_iteration=rounds, num_threads=1), dtype=float)
            validation = log_loss(p, y[w.late])
        data = lgb.Dataset(x, y, feature_name=names, free_raw_data=True)
        booster = lgb.train(BOOSTING, data, num_boost_round=rounds)
        p = np.asarray(booster.predict(x, num_threads=1), dtype=float)
        loss = log_loss(p, y)
        params: dict[str, float] = {"rounds": float(rounds), "samples": float(len(y))}
        if math.isfinite(validation):
            params["validation_loss"] = validation
        blob = booster.model_to_string()
        first, last = w.samples[0].day, w.samples[-1].day
        digest = hashlib.sha256(f"{blob}|{first}|{last}|{len(y)}".encode()).hexdigest()[:6]
        return Model(
            version=f"{self.name}-{last:%Y%m%d}-{digest}",
            params=params,
            trained=f"{first}..{last}, {len(y)} samples, {rounds} rounds, log loss {loss:.4f}",
            trained_until=last,
            blob=blob,
            training=TrainedOn(first, last, len(y), loss),
            target_version=self.answer_version,
        )

    def predict(self, features: FeatureVector, model: Model) -> KindForecast:
        if model.blob is None:
            return KindForecast((1 / 3, 1 / 3, 1 / 3))
        trees = _trees(model)
        # In the order the trees were trained on: features registered since are not theirs.
        row = np.array([[_number(features[n]) for n in trees.feature_name()]], dtype=float)
        p = [float(v) for v in trees.predict(row, num_threads=1)[0]]
        total = sum(p)
        up, flat, down = (v / total for v in p)
        return KindForecast((up, flat, down))


def _trees(model: Model) -> lgb.Booster:
    trees = _TREES.get(model.version)
    if trees is None:
        if len(_TREES) >= _KEPT:
            _TREES.clear()
        trees = _TREES[model.version] = lgb.Booster(model_str=model.blob)
    return trees
