"""Bishop: probabilistic deep learning, a mixture density network (#518).

Personality card:
- **Scientist**: Christopher Bishop.
- **Logic**: a neural network that answers with a distribution, not a number (Bishop 1994's
  mixture density network). Its input is the last half hour's bar shapes, minute by minute:
  the close's change and the high's and low's distance from the close before, in the last 20
  minutes' volatility, and the volume against the hour's usual (log 1 + x), the latest last,
  missing minutes 0, and how many minutes there are. Two tanh layers of 32 give a mixture of
  three normal distributions of the four-minute move (in four minutes' volatility): their
  weights, means and spreads. Each component is turned into the three probabilities as a
  Brownian motion with its mean and spread, in the label's order (``ordered_forecast``), and
  they are mixed by the
  weights; the expected move is the mixture's mean.
- **Own features**: the minutes' volumes against the hour's usual.
- **Learns**: by maximum likelihood of the four-minute moves over the last 20 trading days
  (Adam, mini-batches of 256, 20 passes, a little weight decay): the same weights for the same
  samples (a fixed seed). numpy only, no deep-learning framework.
- **Fails**: on days unlike its 20 (a network does not reach past what it saw), and where the
  bars say little: a quiet half hour gives it nothing to tell the classes apart.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence

import numpy as np

from krx_quant.algorithms.contract import Model, NothingToLearn, Sample
from krx_quant.algorithms.forecasts import (
    HORIZON_MINUTES,
    UNKNOWN,
    forecasting,
    log_loss,
    ordered_forecast,
    with_outcomes,
)
from krx_quant.baselines import Forecast
from krx_quant.cards import Card, Meaning, learned
from krx_quant.features import FeatureVector
from krx_quant.pricing import Direction

WINDOW = 30  # minutes of bar shapes
SHAPES = ("minute_ticks", "minute_highs", "minute_lows")  # in volatilities
INPUTS = (len(SHAPES) + 1) * WINDOW + 1  # and the volumes, and how many minutes
HIDDEN = 32
COMPONENTS = 3
EPOCHS = 20
BATCH = 256
LEARNING_RATE = 3e-3
DECAY = 1e-4  # L2 on the weights
SEED = 0
MIN_ROWS = 200  # samples with a move and a volatility, at least
CLIP = 8.0  # the move, at most this many four-minute volatilities
MIN_VOLATILITY = 0.5  # ticks: a quieter minute is scaled as this
_LOG_SPREAD = (-3.0, 2.0)  # each component's log spread, kept within
TRAINING_DAYS = 20
_NETS: dict[str, dict[str, np.ndarray]] = {}  # read once per model version
_KEPT = 4
PARAM_MEANINGS: Mapping[str, Meaning] = {
    "components": Meaning("4분 움직임을 섞는 정규분포의 수.", "개", "정해져 있다(3)."),
    "hidden": Meaning("두 은닉층 각각의 tanh 단위 수.", "개", "정해져 있다(32)."),
    "epochs": Meaning(
        "학습 표본 전체를 몇 번 지나며 가중치를 맞췄는지. 학습 전에는 0이고 그때는 0 중심 정규분포 하나다.",
        "번",
        learned(TRAINING_DAYS),
    ),
}


def inputs(features: FeatureVector) -> np.ndarray | None:
    """The network's input; None without the 20 minutes' volatility."""
    volatility = features["volatility"]
    if volatility is None:
        return None
    scale = max(volatility, MIN_VOLATILITY)
    x = np.zeros(INPUTS)
    count = 0
    for i, name in enumerate(SHAPES):
        values = features.series(name)[-WINDOW:]
        count = max(count, len(values))
        if values:
            x[(i + 1) * WINDOW - len(values) : (i + 1) * WINDOW] = np.clip(np.array(values) / scale, -10, 10)
    volumes = features.series("minute_volumes")[-WINDOW:]
    if volumes:
        start = len(SHAPES) * WINDOW
        x[start + WINDOW - len(volumes) : start + WINDOW] = np.clip(np.log1p(np.array(volumes)), 0, 5)
    x[-1] = count / WINDOW
    return x


def _initial(rng: np.random.Generator) -> dict[str, np.ndarray]:
    """Glorot-uniform weights; the components' means spread apart so they learn apart."""
    net: dict[str, np.ndarray] = {}
    for name, (fan_in, fan_out) in (
        ("1", (INPUTS, HIDDEN)),
        ("2", (HIDDEN, HIDDEN)),
        ("3", (HIDDEN, 3 * COMPONENTS)),
    ):
        bound = math.sqrt(6 / (fan_in + fan_out))
        net[f"w{name}"] = rng.uniform(-bound, bound, (fan_in, fan_out))
        net[f"b{name}"] = np.zeros(fan_out)
    net["b3"][COMPONENTS : 2 * COMPONENTS] = np.linspace(-0.5, 0.5, COMPONENTS)
    return net


def mixture(net: Mapping[str, np.ndarray], x: np.ndarray) -> tuple[np.ndarray, ...]:
    """The rows' mixtures: weights, means, log spreads (in four minutes' volatility), and the
    hidden layers and raw log spreads training needs."""
    h1 = np.tanh(x @ net["w1"] + net["b1"])
    h2 = np.tanh(h1 @ net["w2"] + net["b2"])
    out = h2 @ net["w3"] + net["b3"]
    logits, means, raw = (out[:, i * COMPONENTS : (i + 1) * COMPONENTS] for i in range(3))
    logits = logits - logits.max(axis=1, keepdims=True)
    weights = np.exp(logits)
    weights /= weights.sum(axis=1, keepdims=True)
    return weights, means, np.clip(raw, *_LOG_SPREAD), h1, h2, raw


def _gradients(
    net: Mapping[str, np.ndarray], x: np.ndarray, y: np.ndarray
) -> tuple[float, dict[str, np.ndarray]]:
    """The batch's mean negative log likelihood of ``y`` and its gradient (with the decay)."""
    weights, means, logs, h1, h2, raw = mixture(net, x)
    z = (y[:, None] - means) / np.exp(logs)
    log_joint = np.log(np.maximum(weights, 1e-300)) - 0.5 * z * z - logs - 0.5 * math.log(2 * math.pi)
    top = log_joint.max(axis=1, keepdims=True)
    log_density = top[:, 0] + np.log(np.exp(log_joint - top).sum(axis=1))
    resp = np.exp(log_joint - log_density[:, None])  # each component's share of the row
    n = len(y)
    d_logits = (weights - resp) / n
    d_means = -resp * z / np.exp(logs) / n
    inside = (raw > _LOG_SPREAD[0]) & (raw < _LOG_SPREAD[1])
    d_logs = resp * (1 - z * z) / n * inside
    d_out = np.concatenate([d_logits, d_means, d_logs], axis=1)
    grads = {"w3": h2.T @ d_out, "b3": d_out.sum(axis=0)}
    d_h2 = d_out @ net["w3"].T * (1 - h2 * h2)
    grads |= {"w2": h1.T @ d_h2, "b2": d_h2.sum(axis=0)}
    d_h1 = d_h2 @ net["w2"].T * (1 - h1 * h1)
    grads |= {"w1": x.T @ d_h1, "b1": d_h1.sum(axis=0)}
    for name in ("w1", "w2", "w3"):
        grads[name] += DECAY * net[name]
    return float(-log_density.mean()), grads


def train(x: np.ndarray, y: np.ndarray) -> dict[str, np.ndarray]:
    """Adam over mini-batches, from a fixed seed: the same net for the same rows."""
    rng = np.random.default_rng(SEED)
    net = _initial(rng)
    first = {k: np.zeros_like(v) for k, v in net.items()}
    second = {k: np.zeros_like(v) for k, v in net.items()}
    step = 0
    for _ in range(EPOCHS):
        order = rng.permutation(len(y))
        for at in range(0, len(y), BATCH):
            batch = order[at : at + BATCH]
            _, grads = _gradients(net, x[batch], y[batch])
            step += 1
            for k, g in grads.items():
                first[k] = 0.9 * first[k] + 0.1 * g
                second[k] = 0.999 * second[k] + 0.001 * g * g
                corrected = first[k] / (1 - 0.9**step)
                net[k] -= LEARNING_RATE * corrected / (np.sqrt(second[k] / (1 - 0.999**step)) + 1e-8)
    return net


def mixed_forecast(net: Mapping[str, np.ndarray] | None, x: np.ndarray, unit: float, k: float) -> Forecast:
    """The three probabilities of the mixture, each component an ``ordered_forecast`` of its mean
    and spread (``unit``: a four-minute volatility, in ticks); one N(0, 1) without a net."""
    if net is None:
        return ordered_forecast(0.0, unit, k)
    weights, means, logs, *_ = mixture(net, x[None, :])
    p_up = p_flat = p_down = expected = 0.0
    for w, m, s in zip(weights[0], means[0], np.exp(logs[0]), strict=True):
        f = ordered_forecast(float(m) * unit, float(s) * unit, k)
        p_up, p_flat, p_down = p_up + w * f.p_up, p_flat + w * f.p_flat, p_down + w * f.p_down
        expected += w * f.expected_ticks
    total = p_up + p_flat + p_down
    return Forecast(float(p_up / total), float(p_flat / total), float(p_down / total), float(expected))


def _net(model: Model) -> dict[str, np.ndarray] | None:
    if model.blob is None:
        return None
    net = _NETS.get(model.version)
    if net is None:
        net = {k: np.array(v, dtype=float) for k, v in json.loads(model.blob).items()}
        if len(_NETS) >= _KEPT:
            _NETS.pop(next(iter(_NETS)))
        _NETS[model.version] = net
    return net


class Bishop:
    name = "bishop"
    features: tuple[str, ...] = ("minute_volumes", *SHAPES, "volatility", "flat_ticks")
    training_days = TRAINING_DAYS
    param_meanings = PARAM_MEANINGS
    card = Card(
        scientist="Christopher Bishop",
        character="확률 딥러닝",
        logic=(
            "최근 30분 1분봉의 모양(종가 변화, 고가·저가 이탈, 상대 거래량)을 신경망에 넣어, 4분 움직임을 "
            "숫자 하나가 아닌 정규분포 세 개의 혼합으로 낸다(혼합 밀도 신경망). 성분마다 도달 확률로 바꿔 "
            "가중치로 섞는다."
        ),
        unique_features=("minute_volumes",),
        training=(
            "최근 20거래일 1분봉의 4분 움직임에 대한 혼합 분포의 우도를 최대화한다(Adam, 256개씩, 20번, 약한 "
            "가중치 감쇠). numpy로만 계산하고, 같은 표본이면 같은 가중치가 나온다(고정 시드)."
        ),
        forecasting=(
            "입력은 분마다 종가 변화·고가 이탈·저가 이탈을 20분 변동성으로 나눈 값과 ln(1 + 상대 거래량), "
            "최신이 마지막(빈 분은 0), 그리고 분의 수다. tanh 32 × 2층이 정규분포 세 개의 가중치, 평균, "
            "폭(4분 변동성 단위, 4분 변동성 = 1분 변동성 × √4)을 낸다. 성분마다 그 평균만큼 흐르고 그 폭만큼 "
            "퍼지는 브라운 운동으로 보고 도달 정답의 세 확률로 바꾼 뒤(상승은 위 목표에 닿을 확률, 보합은 두 "
            "목표 사이에 머물 확률, 하락은 나머지: 정답의 순서 그대로) 가중치로 섞는다. 기대수익은 혼합의 "
            "평균이다. 학습 전에는 0 중심, 폭 1인 정규분포 하나다. 변동성이나 보합 범위를 모르면 세 확률을 "
            "1/3씩 둔다."
        ),
        failure=(
            "학습한 20거래일과 다른 장세에서 틀리기 쉽다. 신경망은 본 값의 범위 밖을 외삽하지 못한다. 조용한 "
            "30분에는 입력이 0에 가까워 세 클래스를 가르지 못한다. 학습 정답은 4분 끝의 움직임이라 도달 "
            "정답과 다를 수 있다."
        ),
    )

    def default_model(self) -> Model:
        """No net before training: one normal distribution about 0."""
        return Model(
            f"{self.name}-default", {"components": float(COMPONENTS), "hidden": float(HIDDEN), "epochs": 0.0}
        )

    def predict(self, features: FeatureVector, model: Model) -> Forecast:
        return self._predict_with(_net(model), features)

    def fit(self, samples: Sequence[Sample]) -> Model:
        scored = with_outcomes(samples)
        rows, moves = [], []
        for s in scored:
            x = inputs(s.features)
            volatility = s.features["volatility"]
            if x is None or volatility is None or s.change_ticks is None:
                continue
            unit = max(volatility, MIN_VOLATILITY) * math.sqrt(HORIZON_MINUTES)
            rows.append(x)
            moves.append(float(np.clip(s.change_ticks / unit, -CLIP, CLIP)))
        if len(rows) < MIN_ROWS:
            raise NothingToLearn(f"{len(rows)} samples with a move and a volatility, fewer than {MIN_ROWS}")
        net = train(np.array(rows), np.array(moves))
        blob = json.dumps({k: v.tolist() for k, v in net.items()}, sort_keys=True)
        params = {"components": float(COMPONENTS), "hidden": float(HIDDEN), "epochs": float(EPOCHS)}
        loss = 0.0
        for s in scored:  # the training samples' log loss, of the target each was labelled with
            with forecasting(s.features):
                loss += log_loss(self._predict_with(net, s.features), s.outcome or Direction.FLAT)
        return Model.fitted(self.name, params, scored, loss / len(scored), blob=blob)

    @staticmethod
    def _predict_with(net: Mapping[str, np.ndarray] | None, features: FeatureVector) -> Forecast:
        k, volatility = features["flat_ticks"], features["volatility"]
        x = inputs(features)
        if k is None or volatility is None or x is None:
            return UNKNOWN
        return mixed_forecast(net, x, max(volatility, MIN_VOLATILITY) * math.sqrt(HORIZON_MINUTES), k)
