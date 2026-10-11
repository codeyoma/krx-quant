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
  trained on the earlier 80% of the samples has the lowest log loss on the last 20%;
  then it is trained on all of them.

The book and the trades' sides are recorded only (backfilled days have neither): on a
day with backfilled bars only it does not learn, and at a bar close without the book it
does not predict (``no_book``).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

import numpy as np

from krx_quant.algorithms.contract import Abstention, Card, Model, NothingToLearn, NotPredicted, Sample
from krx_quant.algorithms.forecasts import CLASSES, class_moves, lowest_log_loss, number, param
from krx_quant.baselines import Forecast
from krx_quant.features import FeatureVector

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
# Fewer samples than this: no validation split; the middle penalty.
MIN_SPLIT = 100
ROUNDS = 1000
_TOLERANCE = 1e-8
_MIN_SD = 1e-9


def default_params() -> dict[str, float]:
    """Before any training: no input matters, every class as likely, no move expected."""
    params = {"penalty": 0.0}
    for f in INPUTS:
        params[param("mean", f)] = 0.0
        params[param("sd", f)] = 1.0
    for c in CLASSES:
        params[param("bias", c.value)] = 0.0
        params[param("move", c.value)] = 0.0
        for f in INPUTS:
            params[param("w", c.value, f)] = 0.0
    return params


class Shannon:
    name = "shannon"
    features: tuple[str, ...] = INPUTS
    training_days = 20
    card = Card(
        scientist="Claude Shannon",
        character="호가창 해독",
        logic=(
            "최근 1분·5분의 1호가 주문흐름 불균형(OFI), 봉 마감 시점 10단계 잔량 불균형, 최근 5분 체결의 "
            "매수·매도 불균형과 그 방향의 엔트로피, NXT 체결 비중, 프로그램매매·외국계 회원사 흐름으로 "
            "4분 뒤 상승·보합·하락을 다항 로지스틱 회귀로 낸다. 호가가 비어 있으면 예측하지 않는다."
        ),
        unique_features=("ofi_1", "ofi_5", "trade_entropy"),
        training=(
            "최근 20거래일의 녹화된 호가·체결로 L1 로지스틱 회귀를 다시 학습한다. 표본의 앞 80%로 학습하고 "
            "뒤 20%의 로그손실이 가장 작은 L1 강도를 골라, 전체로 다시 학습한다."
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
        penalty = _chosen_penalty(scored)
        params = _trained(scored, penalty)
        return lowest_log_loss(self.name, scored, [params], lambda f, q: self.predict(f, Model(self.name, q)))


def _standardized(value: float | None, mean: float, sd: float) -> float:
    """An input in standard deviations from its training mean; 0 (the mean) where unknown."""
    known = number(value)
    return 0.0 if math.isnan(known) else (known - mean) / sd


def _chosen_penalty(scored: Sequence[Sample]) -> float:
    """The penalty whose model, trained on the earlier samples, has the lowest log loss on the
    later ones; the middle one with too few to split."""
    if len(scored) < MIN_SPLIT:
        return PENALTIES[len(PENALTIES) // 2]
    cut = int(len(scored) * (1 - VALIDATION_SHARE))
    train, held = scored[:cut], scored[cut:]
    best: tuple[float, float] | None = None
    for penalty in PENALTIES:
        params = _trained(train, penalty)
        x, y = _matrix(held, params)
        loss = _cross_entropy(*_weights(params), x, y)
        if best is None or loss < best[0] - 1e-12:
            best = (loss, penalty)
    assert best is not None
    return best[1]


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
