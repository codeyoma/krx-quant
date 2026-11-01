"""Shannon: what the order book and the tape say about who is in a hurry, beside what the time of
day says of each symbol.

Personality card:
- **Scientist**: Claude Shannon.
- **Logic**: a multinomial logistic regression of the next four minutes (up, flat, down)
  on the order flow imbalance at the best quotes over the last minute and five, the ten
  levels' size imbalance at the bar close, the trades' aggressor imbalance and its
  entropy, NXT's share of the volume, the day's program trading and foreign member
  firms' flows, and the climate prior of the bar close's symbol and 30-minute slot (#429,
  ``priors.py``): the log-odds of up and of down against flat in the class frequencies its
  training window counted there. Each input is standardized; an unknown one is its mean.
  The expected move is the classes' mean moves in training, weighted by their probabilities.
- **Own features**: the order flow imbalance and the entropy of the trades' sides.
- **Learns**: L1-penalized regression over the last 20 trading days' recorded books and
  trades (accelerated proximal gradient). A training sample's prior counts the window's
  days before its own only (recorded and backfilled); the model keeps the prior of the
  whole window in its parameters and predicts with it. The penalty is the one, of a few,
  whose model trained on the earlier trading days has the lowest log loss on the latest
  ones, about 20% of the samples, whole days only: a four-minute outcome never spans the
  two. Then it is trained on all of them. The model keeps the penalty and the days it was
  validated on.
- **Fails**: when the book and the tape do not lead the price (spoofed quotes, split
  orders, a market moved by the index or news), and when a slot's class frequencies turn
  from the last 20 trading days'.

The book and the trades' sides are recorded only (backfilled days have neither): on a
day with backfilled bars only it does not learn, and at a bar close without the book it
does not predict (``no_book``).

The prior is not a feature (``priors.py``, "Kept with the model"): Shannon predicts with the
symbol and the bar close besides its features (``predict_with_context``, ``contract.ContextAware``),
and its ``predict`` without them fails (``MissingContext``). A model fitted before #429 holds no
prior: its prior inputs are read unweighted, so it predicts as it did.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date

import numpy as np

from krx_quant.algorithms.contract import (
    Abstention,
    MissingContext,
    Model,
    NothingToLearn,
    NotPredicted,
    PredictionContext,
    Sample,
)
from krx_quant.algorithms.forecasts import CLASSES, class_moves, number, param
from krx_quant.algorithms.priors import (
    PRIOR_INPUTS,
    PriorMeanings,
    PriorTally,
    prior_inputs,
    prior_of,
    training_priors,
)
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
# The regression's inputs: its features, then the climate prior's two (#429), which are not features.
NAMES = (*INPUTS, *PRIOR_INPUTS)
# L1 penalties tried, strongest first: of equals, the sparser model is kept.
PENALTIES = (0.1, 0.03, 0.01, 0.003, 0.001)
VALIDATION_SHARE = 0.2
# Fewer samples than this, or a single trading day: no validation split; the middle penalty.
MIN_SPLIT = 100
ROUNDS = 1000
_TOLERANCE = 1e-8
_MIN_SD = 1e-9


def default_params(names: Sequence[str] = NAMES) -> dict[str, float]:
    """Before any training: no input of ``names`` matters, every class as likely, no move
    expected. On ``INPUTS`` alone, shannon's before #429's prior."""
    # The days the penalty was validated on (YYYYMMDD; 0 and none: not validated).
    params = {"penalty": 0.0, "validation_from": 0.0, "validation_to": 0.0, "validation_days": 0.0}
    for f in names:
        params[param("mean", f)] = 0.0
        params[param("sd", f)] = 1.0
    for c in CLASSES:
        params[param("bias", c.value)] = 0.0
        params[param("move", c.value)] = 0.0
        for f in names:
            params[param("w", c.value, f)] = 0.0
    return params


# A model fitted before #429 lacks the prior inputs' parameters: read as their defaults (unweighted).
_UNWEIGHTED_PRIOR = {k: v for k, v in default_params().items() if k.endswith(PRIOR_INPUTS)}


TRAINING_DAYS = 20


# The prior's two inputs, as the meanings name them.
_PRIOR_INPUT_TEXT = {
    "climate_up": "봉 마감의 종목·30분 시간대 사전 확률에서 log(p상승 / p보합). 학습 표본마다 그 표본의 날 "
    "전의 날로 센 사전 확률이다",
    "climate_down": "봉 마감의 종목·30분 시간대 사전 확률에서 log(p하락 / p보합). 학습 표본마다 그 표본의 날 "
    "전의 날로 센 사전 확률이다",
}


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
    units = {f: FEATURE_MEANINGS[f].unit for f in INPUTS} | dict.fromkeys(PRIOR_INPUTS, "로그 오즈")
    for f in NAMES:
        what = f"입력 `{f}`" if f in INPUTS else f"사전 확률 입력 `{f}`({_PRIOR_INPUT_TEXT[f]})"
        out[param("mean", f)] = Meaning(
            f"{what}의 학습 표본 평균. 입력을 표준화하는 데 쓴다.", units[f], when
        )
        out[param("sd", f)] = Meaning(
            f"{what}의 학습 표본 표준편차. 입력을 표준화하는 데 쓴다(0에 가까우면 1).", units[f], when
        )
    for c in CLASSES:
        name = CLASS_NAMES[c.value]
        out[param("bias", c.value)] = Meaning(f"{name} 클래스 점수의 절편.", "로짓", when)
        for f in NAMES:
            out[param("w", c.value, f)] = Meaning(
                f"표준화한 `{f}` 값이 1 늘 때 {name} 클래스 점수에 더하는 값. 0이면 L1이 지웠다.",
                "로짓/표준편차",
                when,
            )
    moves = {name: Meaning(m.text, m.unit, when) for name, m in class_move_meanings(TRAINING_DAYS).items()}
    return out | moves


# The prior table's parameters: counted on every day of the window, backfilled ones too.
PARAM_MEANINGS: Mapping[str, Meaning] = PriorMeanings(
    _param_meanings(), f"{learned(TRAINING_DAYS)} 녹화된 날과 백필만 있는 날의 정답을 모두 센다."
)


class Shannon:
    name = "shannon"
    features: tuple[str, ...] = INPUTS
    training_days = TRAINING_DAYS
    param_meanings = PARAM_MEANINGS
    # Its prior's code (``priors.py``) is in its code digest: a change to it asks for a new verdict.
    code_from: tuple[type, ...] = (PriorTally,)
    card = Card(
        scientist="Claude Shannon",
        character="호가창 해독",
        logic=(
            "최근 1분·5분의 1호가 주문흐름 불균형(OFI), 봉 마감 시점 10단계 잔량 불균형, 최근 5분 체결의 "
            "매수·매도 불균형과 그 방향의 엔트로피, NXT 체결 비중, 프로그램매매·외국계 회원사 흐름, 그리고 "
            "그 종목·30분 시간대의 기후 사전 확률(학습 창에서 그 칸의 정답이 상승·보합·하락이었던 비율)로 "
            "4분 안의 상승·보합·하락(도달 정답)을 다항 로지스틱 회귀로 낸다. 호가가 비어 있으면 예측하지 "
            "않는다."
        ),
        unique_features=("ofi_1", "ofi_5", "trade_entropy"),
        training=(
            "최근 20거래일의 녹화된 호가·체결로 L1 로지스틱 회귀를 다시 학습한다. 기후 사전 확률은 학습 "
            "창의 정답(백필만 있는 날 포함)을 종목 × 30분 시간대와 종목 하루로 세어 평활한 비율이다. 학습 "
            "표본마다 그 표본의 날 전의 날만 센다. 모델은 학습 창 전체로 센 사전 확률 표를 파라미터로 갖고, "
            "예측은 그 표로 한다. 표본의 약 20%가 되는 최근 거래일들을 통째로 검증에 두고, 그 전 날들로 "
            "학습해 검증 로그손실이 가장 작은 L1 강도를 골라, 전체로 다시 학습한다."
        ),
        forecasting=(
            f"{TOUCH_LABEL}으로 직접 학습한 다항 로지스틱 회귀(소프트맥스)가 세 "
            "확률을 낸다: 클래스마다 절편 + Σ 가중치 × 표준화한 입력으로 점수를 내고 소프트맥스로 바꾼다. "
            "입력은 피처 여덟과 사전 확률 입력 둘이다. 사전 확률 입력은 봉 마감의 종목·시간대에서 모델의 "
            "사전 확률 표로 낸 log(p상승 / p보합)과 log(p하락 / p보합)이다. 본 적 없는 시간대는 그 종목 "
            "하루의 비율을, 본 적 없는 종목은 세 클래스가 같은 비율을 쓴다. 입력은 학습 표본의 평균과 "
            "표준편차로 표준화하고, 모르는 입력은 평균(0)으로 둔다. 기대수익은 학습에서 클래스별 평균 4분 "
            "변화를 세 확률로 가중한 값이다. 봉 마감에 호가가 비어 있으면 예측하지 않는다(no_book)."
        ),
        failure=(
            "호가·체결 흐름이 가격을 이끌지 않는 장(허수 호가가 많을 때, 큰 주문이 쪼개져 들어올 때, "
            "지수·뉴스가 가격을 끌 때)에서 틀리기 쉽다. 최근 20거래일의 녹화로 배운 관계라 장세가 바뀌면 "
            "가중치가 맞지 않는다. 사전 확률도 최근 20거래일의 시간대별 비율이라, 그 비율이 바뀌면(장세 "
            "전환, 특이한 날) 뒤처진다. L1이 가중치를 거의 다 지우면 클래스 비율에 가까운 확률만 낸다."
        ),
    )

    def default_model(self) -> Model:
        return Model(f"{self.name}-default", default_params())

    def predict(self, features: FeatureVector, model: Model) -> Forecast:
        """Never: shannon predicts with the symbol and the bar close (``predict_with_context``)."""
        raise MissingContext(f"{self.name} predicts with the symbol and the bar close (predict_with_context)")

    def predict_with_context(
        self, features: FeatureVector, model: Model, context: PredictionContext
    ) -> Forecast:
        """The forecast at the context's symbol and bar close, with the prior ``model`` holds
        (module doc); ``no_book`` without the book."""
        if features["depth_imbalance"] is None:
            raise NotPredicted(Abstention.NO_BOOK)
        p = model.params
        if param("w", "up", PRIOR_INPUTS[0]) not in p:  # fitted before #429 (module doc)
            p = {**_UNWEIGHTED_PRIOR, **p}
        prior = prior_inputs(prior_of(p, context.symbol, context.bar_close))
        raw = np.array([[*(number(features[f]) for f in INPUTS), *prior]], dtype=float)
        up, flat, down = probabilities(p, raw, NAMES)
        expected = sum(q * p[param("move", c.value)] for q, c in zip((up, flat, down), CLASSES, strict=True))
        return Forecast(up, flat, down, expected)

    def fit(self, samples: Sequence[Sample]) -> Model:
        """Trained on the samples with a label and the book, each with its causal prior
        (``priors.training_priors``); the model keeps the prior of all of ``samples``' labels."""
        scored = with_the_book(samples)
        raw = np.hstack([inputs_of(scored), np.array(training_priors(samples, scored), dtype=float)])
        y = one_hot(scored)
        params = default_params() | regression(raw, y, [kst_day(s.bar_close) for s in scored], NAMES)
        tally = PriorTally()
        tally.add(samples)
        params |= class_moves(scored) | tally.table()
        return Model.fitted(self.name, params, scored, training_loss(params, raw, y, NAMES))


def with_the_book(samples: Sequence[Sample]) -> list[Sample]:
    """The samples shannon learns from: with a label and the book, in time order. Raises
    NothingToLearn without one."""
    scored = sorted(
        (s for s in samples if s.outcome is not None and s.features["depth_imbalance"] is not None),
        key=lambda s: (s.bar_close, s.symbol),
    )
    if not scored:
        raise NothingToLearn("no training samples with the book")
    return scored


def inputs_of(scored: Sequence[Sample]) -> np.ndarray:
    """The samples' features (``INPUTS``), a row each; NaN where unknown."""
    return np.array([[number(s.features[f]) for f in INPUTS] for s in scored], dtype=float).reshape(
        len(scored), len(INPUTS)
    )


def one_hot(scored: Sequence[Sample]) -> np.ndarray:
    """The samples' classes, one-hot (up, flat, down)."""
    return np.array([[float(s.outcome is c) for c in CLASSES] for s in scored], dtype=float)


def probabilities(
    params: Mapping[str, float], raw: np.ndarray, names: Sequence[str]
) -> tuple[float, float, float]:
    """The model's up, flat and down probabilities of the one row ``raw`` (an input of ``names``
    a column, NaN where unknown)."""
    means, sds, w, b = _parameters(params, names)
    up, flat, down = (float(q) for q in _probabilities(w, b, _standardized(raw, means, sds))[0])
    return up, flat, down


def training_loss(params: Mapping[str, float], raw: np.ndarray, y: np.ndarray, names: Sequence[str]) -> float:
    """The model's mean log loss on the rows it was trained on (``raw``, ``y`` one-hot)."""
    means, sds, w, b = _parameters(params, names)
    return _cross_entropy(w, b, _standardized(raw, means, sds), y)


def regression(
    raw: np.ndarray, y: np.ndarray, days: Sequence[date], names: Sequence[str]
) -> dict[str, float]:
    """The L1 logistic regression on ``raw`` (a row a sample in time order, ``days`` their
    trading days; a column an input of ``names``, NaN where unknown), ``y`` the classes one-hot:
    its parameters (``penalty``, ``validation_*``, ``mean.<input>``, ``sd.<input>``,
    ``w.<class>.<input>``, ``bias.<class>``)."""
    penalty, validated = _chosen_penalty(raw, y, days)
    means, sds, w, b = _fitted(raw, y, penalty)
    params = {"penalty": penalty, **_validated_on(validated)}
    for j, f in enumerate(names):
        params[param("mean", f)] = float(means[j])
        params[param("sd", f)] = float(sds[j])
    for i, c in enumerate(CLASSES):
        params[param("bias", c.value)] = float(b[i])
        for j, f in enumerate(names):
            params[param("w", c.value, f)] = float(w[i, j]) + 0.0  # a zeroed weight is 0, not −0
    return params


def _standardization(raw: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Each column's mean and standard deviation over its known values: 0 and 1 for a column
    with none, 1 for one that does not vary."""
    means, sds = [], []
    for j in range(raw.shape[1]):
        known = raw[~np.isnan(raw[:, j]), j]
        mean = float(known.mean()) if known.size else 0.0
        sd = float(known.std()) if known.size else 1.0
        means.append(mean)
        sds.append(sd if sd > _MIN_SD else 1.0)
    return np.array(means), np.array(sds)


def _standardized(raw: np.ndarray, means: np.ndarray, sds: np.ndarray) -> np.ndarray:
    """The inputs in standard deviations from their training means; 0 (the mean) where unknown."""
    out: np.ndarray = np.where(np.isnan(raw), 0.0, (raw - means) / sds)
    return out


def _fitted(raw: np.ndarray, y: np.ndarray, penalty: float) -> tuple[np.ndarray, ...]:
    means, sds = _standardization(raw)
    w, b = _fista(_standardized(raw, means, sds), y, penalty)
    return means, sds, w, b


def _parameters(params: Mapping[str, float], names: Sequence[str]) -> tuple[np.ndarray, ...]:
    """The means, standard deviations, weights and biases of the inputs ``names``, from a model's
    parameters."""
    means = np.array([params[param("mean", f)] for f in names], dtype=float)
    sds = np.array([params[param("sd", f)] for f in names], dtype=float)
    w = np.array([[params[param("w", c.value, f)] for f in names] for c in CLASSES], dtype=float)
    b = np.array([params[param("bias", c.value)] for c in CLASSES], dtype=float)
    return means, sds, w, b


def _chosen_penalty(raw: np.ndarray, y: np.ndarray, days: Sequence[date]) -> tuple[float, list[date]]:
    """The penalty whose model, trained on the earlier trading days, has the lowest log loss
    on the latest ones, and those days; the middle penalty and no day with too few to split.

    The latest days are taken whole, as many as bring the samples held out nearest
    VALIDATION_SHARE (the fewer of equals), one day at least being left to train on: a split
    inside a day would let the four-minute outcomes of its bar closes before the cut reach
    into the validation. ``raw`` and ``y`` are in time order, ``days`` their trading days."""
    distinct = sorted(set(days))
    if len(raw) < MIN_SPLIT or len(distinct) < 2:
        return PENALTIES[len(PENALTIES) // 2], []
    per_day = Counter(days)
    target = len(raw) * VALIDATION_SHARE

    def off_target(k: int) -> float:
        return abs(sum(per_day[d] for d in distinct[-k:]) - target)

    held_days = distinct[-min(range(1, len(distinct)), key=off_target) :]
    held = np.array([d >= held_days[0] for d in days])
    best: tuple[float, float] | None = None
    for penalty in PENALTIES:
        means, sds, w, b = _fitted(raw[~held], y[~held], penalty)
        loss = _cross_entropy(w, b, _standardized(raw[held], means, sds), y[held])
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
