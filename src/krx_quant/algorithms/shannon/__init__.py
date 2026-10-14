"""Shannon: what the order book and the tape say about who is in a hurry.

Personality card:
- **Scientist**: Claude Shannon.
- **Logic**: a multinomial logistic regression of the next four minutes (up, flat, down)
  on the order flow imbalance at the best quotes over the last minute and five, the ten
  levels' size imbalance at the bar close, the trades' aggressor imbalance and its
  entropy, NXT's share of the volume, and the day's program trading and foreign member
  firms' flows. Each input is standardized; an unknown one is its mean. The expected move
  is the classes' mean moves in training, weighted by their probabilities.
- **Own features**: the order flow imbalance and the entropy of the trades' sides.
- **Learns**: L1-penalized regression over the last 20 trading days' recorded books and
  trades (accelerated proximal gradient). The penalty is the one, of a few, whose model
  trained on the earlier trading days has the lowest log loss on the latest ones, about
  20% of the samples, whole days only: a four-minute outcome never spans the two. Then it
  is trained on all of them. The model keeps the penalty and the days it was validated on.
- **Fails**: when the book and the tape do not lead the price (spoofed quotes, split
  orders, a market moved by the index or news).

The book and the trades' sides are recorded only (backfilled days have neither): on a
day with backfilled bars only it does not learn, and at a bar close without the book it
does not predict (``no_book``).
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date

import numpy as np

from krx_quant.algorithms.contract import Abstention, Model, NothingToLearn, NotPredicted, Sample
from krx_quant.algorithms.forecasts import CLASSES, class_moves, lowest_log_loss, number, param
from krx_quant.baselines import Forecast
from krx_quant.cards import CLASS_NAMES, TOUCH_LABEL, Card, Meaning, class_move_meanings, learned
from krx_quant.features import FEATURE_MEANINGS, FeatureVector
from krx_quant.feeds.base import kst_day

INPUTS = (
    "ofi_1",
    "ofi_5",
    "depth_imbalance",
    "trade_imbalance",
    "trade_entropy",
    "nxt_share",
    "program_net",
    "member_foreign_net",
)
# L1 penalties tried, strongest first: of equals, the sparser model is kept.
PENALTIES = (0.1, 0.03, 0.01, 0.003, 0.001)
VALIDATION_SHARE = 0.2
# Fewer samples than this, or a single trading day: no validation split; the middle penalty.
MIN_SPLIT = 100
ROUNDS = 1000
_TOLERANCE = 1e-8
_MIN_SD = 1e-9


def default_params() -> dict[str, float]:
    """Before any training: no input matters, every class as likely, no move expected."""
    # The days the penalty was validated on (YYYYMMDD; 0 and none: not validated).
    params = {"penalty": 0.0, "validation_from": 0.0, "validation_to": 0.0, "validation_days": 0.0}
    for f in INPUTS:
        params[param("mean", f)] = 0.0
        params[param("sd", f)] = 1.0
    for c in CLASSES:
        params[param("bias", c.value)] = 0.0
        params[param("move", c.value)] = 0.0
        for f in INPUTS:
            params[param("w", c.value, f)] = 0.0
    return params


TRAINING_DAYS = 20


def _param_meanings() -> dict[str, Meaning]:
    when = f"{learned(TRAINING_DAYS)} 녹화된 날(호가·체결)로만 배운다."
    unvalidated = "0이면 검증하지 않았다(표본이 100개 미만이거나 날이 하루뿐이었거나 학습 전)."
    out = {
        "penalty": Meaning(
            "고른 L1 강도: 가중치 절댓값에 거는 벌점. 0.1, 0.03, 0.01, 0.003, 0.001 중에서 최근 거래일 "
            "검증의 로그손실로 고른다. 0이면 학습 전이다.",
            "벌점 계수",
            when,
        ),
        "validation_from": Meaning(
            f"L1 강도를 고를 때 검증에 둔 첫 거래일(YYYYMMDD를 숫자로). {unvalidated}", "날짜", when
        ),
        "validation_to": Meaning(
            f"L1 강도를 고를 때 검증에 둔 마지막 거래일(YYYYMMDD를 숫자로). {unvalidated}", "날짜", when
        ),
        "validation_days": Meaning(f"L1 강도를 고를 때 검증에 둔 거래일 수. {unvalidated}", "거래일", when),
    }
    for f in INPUTS:
        unit = FEATURE_MEANINGS[f].unit
        out[param("mean", f)] = Meaning(
            f"입력 `{f}`의 학습 표본 평균. 입력을 표준화하는 데 쓴다.", unit, when
        )
        out[param("sd", f)] = Meaning(
            f"입력 `{f}`의 학습 표본 표준편차. 입력을 표준화하는 데 쓴다(0에 가까우면 1).", unit, when
        )
    for c in CLASSES:
        name = CLASS_NAMES[c.value]
        out[param("bias", c.value)] = Meaning(f"{name} 클래스 점수의 절편.", "로짓", when)
        for f in INPUTS:
            out[param("w", c.value, f)] = Meaning(
                f"표준화한 `{f}` 값이 1 늘 때 {name} 클래스 점수에 더하는 값. 0이면 L1이 지웠다.",
                "로짓/표준편차",
                when,
            )
    moves = {name: Meaning(m.text, m.unit, when) for name, m in class_move_meanings(TRAINING_DAYS).items()}
    return out | moves


PARAM_MEANINGS: Mapping[str, Meaning] = _param_meanings()


class Shannon:
    name = "shannon"
    features: tuple[str, ...] = INPUTS
    training_days = TRAINING_DAYS
    param_meanings = PARAM_MEANINGS
    card = Card(
        scientist="Claude Shannon",
        character="호가창 해독",
        logic=(
            "최근 1분·5분의 1호가 주문흐름 불균형(OFI), 봉 마감 시점 10단계 잔량 불균형, 최근 5분 체결의 "
            "매수·매도 불균형과 그 방향의 엔트로피, NXT 체결 비중, 프로그램매매·외국계 회원사 흐름으로 "
            "4분 안의 상승·보합·하락(도달 정답)을 다항 로지스틱 회귀로 낸다. 호가가 비어 있으면 예측하지 "
            "않는다."
        ),
        unique_features=("ofi_1", "ofi_5", "trade_entropy"),
        training=(
            "최근 20거래일의 녹화된 호가·체결로 L1 로지스틱 회귀를 다시 학습한다. "
            "표본의 약 20%가 되는 최근 거래일들을 통째로 검증에 두고, 그 전 날들로 학습해 "
            "검증 로그손실이 가장 작은 L1 강도를 골라, 전체로 다시 학습한다."
        ),
        forecasting=(
            f"{TOUCH_LABEL}으로 직접 학습한 다항 로지스틱 회귀(소프트맥스)가 세 "
            "확률을 낸다: 클래스마다 절편 + Σ 가중치 × 표준화한 입력으로 점수를 내고 소프트맥스로 바꾼다. "
            "입력은 학습 표본의 평균과 표준편차로 표준화하고, 모르는 입력은 평균(0)으로 둔다. 기대수익은 "
            "학습에서 클래스별 평균 4분 변화를 세 확률로 가중한 값이다. 봉 마감에 호가가 비어 있으면 "
            "예측하지 않는다(no_book)."
        ),
        failure=(
            "호가·체결 흐름이 가격을 이끌지 않는 장(허수 호가가 많을 때, 큰 주문이 쪼개져 들어올 때, "
            "지수·뉴스가 가격을 끌 때)에서 틀리기 쉽다. 최근 20거래일의 녹화로 배운 관계라 장세가 바뀌면 "
            "가중치가 맞지 않는다. L1이 가중치를 거의 다 지우면 클래스 비율에 가까운 확률만 낸다."
        ),
    )

    def default_model(self) -> Model:
        return Model(f"{self.name}-default", default_params())

    def predict(self, features: FeatureVector, model: Model) -> Forecast:
        if features["depth_imbalance"] is None:
            raise NotPredicted(Abstention.NO_BOOK)
        p = model.params
        x = np.array([[_standardized(features[f], p[param("mean", f)], p[param("sd", f)]) for f in INPUTS]])
        up, flat, down = (float(q) for q in _probabilities(*_weights(p), x)[0])
        expected = sum(q * p[param("move", c.value)] for q, c in zip((up, flat, down), CLASSES, strict=True))
        return Forecast(up, flat, down, expected)

    def fit(self, samples: Sequence[Sample]) -> Model:
        scored = sorted(
            (s for s in samples if s.outcome is not None and s.features["depth_imbalance"] is not None),
            key=lambda s: (s.bar_close, s.symbol),
        )
        if not scored:
            raise NothingToLearn("no training samples with the book")
        penalty, validated = _chosen_penalty(scored)
        params = _trained(scored, penalty) | _validated_on(validated)
        return lowest_log_loss(self.name, scored, [params], lambda f, q: self.predict(f, Model(self.name, q)))


def _standardized(value: float | None, mean: float, sd: float) -> float:
    """An input in standard deviations from its training mean; 0 (the mean) where unknown."""
    known = number(value)
    return 0.0 if math.isnan(known) else (known - mean) / sd


def _chosen_penalty(scored: Sequence[Sample]) -> tuple[float, list[date]]:
    """The penalty whose model, trained on the earlier trading days, has the lowest log loss
    on the latest ones, and those days; the middle penalty and no day with too few to split.

    The latest days are taken whole, as many as bring the samples held out nearest
    VALIDATION_SHARE (the fewer of equals), one day at least being left to train on: a split
    inside a day would let the four-minute outcomes of its bar closes before the cut reach
    into the validation."""
    days = sorted({kst_day(s.bar_close) for s in scored})
    if len(scored) < MIN_SPLIT or len(days) < 2:
        return PENALTIES[len(PENALTIES) // 2], []
    per_day = Counter(kst_day(s.bar_close) for s in scored)
    target = len(scored) * VALIDATION_SHARE

    def off_target(k: int) -> float:
        return abs(sum(per_day[d] for d in days[-k:]) - target)

    held_days = days[-min(range(1, len(days)), key=off_target) :]
    train = [s for s in scored if kst_day(s.bar_close) < held_days[0]]
    held = [s for s in scored if kst_day(s.bar_close) >= held_days[0]]
    best: tuple[float, float] | None = None
    for penalty in PENALTIES:
        params = _trained(train, penalty)
        x, y = _matrix(held, params)
        loss = _cross_entropy(*_weights(params), x, y)
        if best is None or loss < best[0] - 1e-12:
            best = (loss, penalty)
    assert best is not None
    return best[1], held_days


def _validated_on(days: Sequence[date]) -> dict[str, float]:
    """The days the penalty was validated on, as the model keeps them (YYYYMMDD)."""
    if not days:
        return {"validation_from": 0.0, "validation_to": 0.0, "validation_days": 0.0}
    return {
        "validation_from": float(f"{days[0]:%Y%m%d}"),
        "validation_to": float(f"{days[-1]:%Y%m%d}"),
        "validation_days": float(len(days)),
    }


def _trained(scored: Sequence[Sample], penalty: float) -> dict[str, float]:
    """The L1-penalized regression on ``scored``, as parameters."""
    params = default_params()
    raw = np.array([[number(s.features[f]) for f in INPUTS] for s in scored], dtype=float)
    for j, f in enumerate(INPUTS):
        known = raw[~np.isnan(raw[:, j]), j]
        mean = float(known.mean()) if known.size else 0.0
        sd = float(known.std()) if known.size else 1.0
        params[param("mean", f)] = mean
        params[param("sd", f)] = sd if sd > _MIN_SD else 1.0
    x, y = _matrix(scored, params)
    w, b = _fista(x, y, penalty)
    params["penalty"] = penalty
    for i, c in enumerate(CLASSES):
        params[param("bias", c.value)] = float(b[i])
        for j, f in enumerate(INPUTS):
            params[param("w", c.value, f)] = float(w[i, j]) + 0.0  # a zeroed weight is 0, not −0
    return params | class_moves(scored)


def _matrix(scored: Sequence[Sample], params: Mapping[str, float]) -> tuple[np.ndarray, np.ndarray]:
    """The samples' standardized inputs (unknown: 0), and their classes one-hot."""
    x = np.array(
        [
            [_standardized(s.features[f], params[param("mean", f)], params[param("sd", f)]) for f in INPUTS]
            for s in scored
        ],
        dtype=float,
    )
    y = np.array([[float(s.outcome is c) for c in CLASSES] for s in scored], dtype=float)
    return x, y


def _weights(params: Mapping[str, float]) -> tuple[np.ndarray, np.ndarray]:
    w = np.array([[params[param("w", c.value, f)] for f in INPUTS] for c in CLASSES], dtype=float)
    b = np.array([params[param("bias", c.value)] for c in CLASSES], dtype=float)
    return w, b


def _probabilities(w: np.ndarray, b: np.ndarray, x: np.ndarray) -> np.ndarray:
    logits = x @ w.T + b
    logits -= logits.max(axis=1, keepdims=True)
    e = np.exp(logits)
    probabilities: np.ndarray = e / e.sum(axis=1, keepdims=True)
    return probabilities


def _cross_entropy(w: np.ndarray, b: np.ndarray, x: np.ndarray, y: np.ndarray) -> float:
    p = _probabilities(w, b, x)
    return float(-(y * np.log(np.maximum(p, 1e-12))).sum() / len(x))


def _fista(x: np.ndarray, y: np.ndarray, penalty: float) -> tuple[np.ndarray, np.ndarray]:
    """Minimize the mean cross-entropy plus ``penalty`` × the weights' L1 norm (the biases
    are not penalized): accelerated proximal gradient with a backtracking step."""
    n, d = x.shape
    k = y.shape[1]
    w, b = np.zeros((k, d)), np.zeros(k)
    zw, zb = w.copy(), b.copy()
    t, step = 1.0, 1.0
    objective = _cross_entropy(w, b, x, y)
    for _ in range(ROUNDS):
        p = _probabilities(zw, zb, x)
        gw, gb = (p - y).T @ x / n, (p - y).mean(axis=0)
        base = _cross_entropy(zw, zb, x, y)
        if not math.isfinite(base):
            break  # never on standardized inputs; a guard against looping on NaN
        while True:
            nw = _soft(zw - step * gw, step * penalty)
            nb = zb - step * gb
            dw, db = nw - zw, nb - zb
            bound = (
                base
                + float((gw * dw).sum() + (gb * db).sum())
                + float((dw**2).sum() + (db**2).sum()) / (2 * step)
            )
            if _cross_entropy(nw, nb, x, y) <= bound + 1e-12:
                break
            step /= 2
        t_next = (1 + math.sqrt(1 + 4 * t * t)) / 2
        zw = nw + (t - 1) / t_next * (nw - w)
        zb = nb + (t - 1) / t_next * (nb - b)
        w, b, t = nw, nb, t_next
        current = _cross_entropy(w, b, x, y) + penalty * float(np.abs(w).sum())
        if abs(objective - current) < _TOLERANCE * max(1.0, abs(objective)):
            break
        objective = current
    return w, b


def _soft(v: np.ndarray, threshold: float) -> np.ndarray:
    shrunk: np.ndarray = np.sign(v) * np.maximum(np.abs(v) - threshold, 0.0)
    return shrunk
