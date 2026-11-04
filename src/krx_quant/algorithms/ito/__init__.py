"""Itô: a stochastic differential equation whose volatility has a shape (#518).

Personality card:
- **Scientist**: Kiyosi Itô (伊藤清).
- **Logic**: the price's move X from the bar close's mid-point follows the Itô equation
  dX = σ(t)·g(X) dW, with no drift: a martingale. σ(t)² is Heston's expected variance path,
  the last five minutes' variance v₀ (Parkinson's, from the bars' ranges) returning to the last
  20 minutes' θ at speed κ: σ(t)² = c²·(θ + (v₀ − θ)·e^(−κt)). g(X) is the volatility's skew,
  (σ₊ ÷ σ̄)^w above the start and (σ₋ ÷ σ̄)^w below, σ₊ and σ₋ the last 30 minutes' up and down
  semideviations (σ̄ their root mean square). As σ(t) only multiplies g(X), time can run on the
  variance clock Λ = ∫₀⁴ σ(t)² dt, and the backward Kolmogorov equation ∂u/∂Λ = ½·g(x)²·∂²u/∂x²
  is solved on a grid of ticks, as the chain of the grid's nodes (its generator's eigenvectors,
  once per prediction). For the touch target: up is the chance of reaching the upper target at
  all (the lower one left open, a far reflecting side), flat of reaching neither (both
  absorbing), down the rest: exactly as the label orders them, where the normal forecast takes
  the two as independent. For the four-minute move: where the move ends.
- **Own features**: the up and down semideviations, Parkinson's five-minute volatility.
- **Learns**: the speed κ, the skew's weight w and the scale c, by grid search for the lowest log
  loss over the last 20 trading days. The grid's decomposition depends on w alone: κ and c only
  change the clock.
- **Fails**: it has no drift, so it says nothing of a trend; the direction comes only from the
  skew, and in a quiet market the five minutes' ranges and the semideviations are a few ticks.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import NamedTuple

import numpy as np

from krx_quant.algorithms.contract import Model, Sample
from krx_quant.algorithms.forecasts import (
    HORIZON_MINUTES,
    LOSS_FLOOR,
    MIN_SPREAD_TICKS,
    UNKNOWN,
    Params,
    with_outcomes,
)
from krx_quant.baselines import Forecast
from krx_quant.cards import Card, Meaning, learned
from krx_quant.features import FeatureVector
from krx_quant.pricing import Direction

SPEEDS = (0.0, 0.1, 0.3, 1.0, 3.0)  # κ: how fast the fast variance returns to the slow, per minute
SKEWS = (0.0, 0.5, 1.0)  # w: how much of the semideviations' skew
SCALES = (0.8, 1.0, 1.25)  # c: of both variances
DEFAULT_PARAMS = {"speed": 0.3, "skew": 0.5, "scale": 1.0}
TRAINING_DAYS = 20
# The grid: at least this many steps from the start to a target and to a standard deviation of
# the move; at most this many from the start to a target, and nodes in all.
STEPS = 8
MAX_STEPS = 40
MAX_NODES = 120
# The far side of a one-sided chain, in standard deviations of the move (at w = 1, c = 1.25).
REACH = 5.0
# A side's semideviation over σ̄, at least: a side that has not moved in 30 minutes still can.
MIN_SIDE = 0.1
PARAM_MEANINGS: Mapping[str, Meaning] = {
    "speed": Meaning(
        "분산의 되돌림 속도 κ: 최근 5분 분산(`parkinson_5`²)이 최근 20분 분산(`volatility`²)으로 e^(−κt)처럼 "
        "돌아간다(Heston의 기대 분산 경로). 0이면 5분 분산이 4분 내내 그대로다. 0, 0.1, 0.3, 1, 3 중에서 "
        "고른다.",
        "1/분",
        learned(TRAINING_DAYS),
    ),
    "skew": Meaning(
        "비대칭의 가중치 w: 출발점 위의 변동성은 (σ₊ ÷ σ̄)^w배, 아래는 (σ₋ ÷ σ̄)^w배다(σ₊, σ₋: `semivol_up`, "
        "`semivol_down`, σ̄: 둘의 제곱평균제곱근). 0이면 위아래가 같다. 0, 0.5, 1 중에서 고른다.",
        "지수",
        learned(TRAINING_DAYS),
    ),
    "scale": Meaning(
        "변동성 배율 c: 두 분산 모두 c²배로 본다. 0.8, 1, 1.25 중에서 고른다.",
        "배",
        learned(TRAINING_DAYS),
    ),
}


class Terms(NamedTuple):
    """What a forecast is made of, from the features: in ticks, and variances in ticks²."""

    distance: float  # to each target (k plus half the spread), or k for the four-minute move
    touch: bool  # the touch target, or where the move ends
    fast: float  # v₀: the last five minutes' variance a minute
    slow: float  # θ: the last 20 minutes'
    up: float  # σ₊ ÷ σ̄
    down: float  # σ₋ ÷ σ̄
    scale: float  # the grid's: a standard deviation of the move at its widest


def terms(features: FeatureVector) -> Terms | None:
    """The forecast's terms; None without a flat band or the 20 minutes' volatility."""
    k, slow = features["flat_ticks"], features["volatility"]
    if k is None or slow is None:
        return None
    fast = features["parkinson_5"]
    fast = slow if fast is None else fast
    rise, fall = features["semivol_up"], features["semivol_down"]
    up = down = 1.0  # unknown semideviations: symmetric
    if rise is not None and fall is not None and (mean := math.sqrt((rise * rise + fall * fall) / 2)) > 0:
        up, down = max(rise / mean, MIN_SIDE), max(fall / mean, MIN_SIDE)
    touch = features.touch
    distance = max(k + (0.0 if touch is None else touch.half_spread), MIN_SPREAD_TICKS)
    widest = 2 * max(fast, slow) * max(up, down) * max(SCALES)
    return Terms(
        distance, touch is not None, fast * fast, slow * slow, up, down, max(widest, MIN_SPREAD_TICKS)
    )


def clock(fast: float, slow: float, speed: float, scale: float) -> float:
    """The variance clock Λ = ∫₀⁴ c²·(θ + (v₀ − θ)·e^(−κt)) dt, in ticks²; never under a half
    tick's square."""
    if speed < 1e-9:
        base = HORIZON_MINUTES * fast
    else:
        base = HORIZON_MINUTES * slow + (fast - slow) * -math.expm1(-HORIZON_MINUTES * speed) / speed
    return max(scale * scale * base, MIN_SPREAD_TICKS**2)


class Chain(NamedTuple):
    """A probability as Σ_l c_l·e^(λ_l·Λ): of the chain of the grid's nodes run for the clock Λ."""

    rates: np.ndarray  # λ_l: the generator's eigenvalues (0 or less)
    coefficients: np.ndarray  # c_l


class _Decomposed(NamedTuple):
    eigenvalues: np.ndarray
    vectors: np.ndarray
    root: np.ndarray  # R^½: the square roots of the nodes' rates


def _chain(positions: np.ndarray, t: Terms, skew: float, h: float, reflect: tuple[bool, bool]) -> _Decomposed:
    """The eigen-decomposition of the generator ½·g(x)²·∂²/∂x² on the nodes ``positions`` (ticks,
    step ``h``): a node's rate to each neighbour is g² ÷ 2h²; a missing neighbour is a target
    (absorbing) or, where ``reflect`` says so (low side, high side), a wall."""
    up, down = t.up**skew, t.down**skew
    g2 = np.where(positions > 0, up * up, np.where(positions < 0, down * down, (up * up + down * down) / 2))
    rates = g2 / (2 * h * h)
    n = len(rates)
    a = np.diag(np.full(n, -2.0)) + np.diag(np.ones(n - 1), 1) + np.diag(np.ones(n - 1), -1)
    if reflect[0]:
        a[0, 0] = -1.0
    if reflect[1]:
        a[-1, -1] = -1.0
    root = np.sqrt(rates)
    eigenvalues, vectors = np.linalg.eigh(root[:, None] * a * root[None, :])
    return _Decomposed(eigenvalues, vectors, root)


def _coefficients(decomposed: _Decomposed, start: int, weights: np.ndarray) -> Chain:
    """Σ_j P(at node j after Λ | at ``start``)·weights_j as a Chain: the generator Q is similar to
    the symmetric R^½·A·R^½ (R its rates), so e^(QΛ) = R^½·V·e^(λΛ)·Vᵀ·R^(−½)."""
    eigenvalues, vectors, root = decomposed
    return Chain(eigenvalues, root[start] * vectors[start] * (vectors.T @ (weights / root)))


def _steps(distance: float, scale: float, most: int) -> int:
    """Steps from the start to the target: at least STEPS to it and to a standard deviation."""
    return min(max(STEPS, math.ceil(STEPS * distance / scale)), most)


def chains(t: Terms, skew: float) -> tuple[Chain, Chain]:
    """The two probabilities a forecast is made of. The touch target: not reaching the upper
    target (the lower side a far wall), and reaching neither (both absorbing). The four-minute
    move: ending k ticks up or more, and k ticks down or more (walls far on both sides; a node on
    k counts half)."""
    d = t.distance
    if t.touch:
        m = _steps(d, t.scale, MAX_STEPS)
        h = d / m
        far = min(math.ceil(REACH * t.scale / h), MAX_NODES - m)
        below = h * np.arange(-far, m)  # the upper target, m·h, absorbs
        stays = _coefficients(_chain(below, t, skew, h, (True, False)), far, np.ones(len(below)))
        mb = _steps(d, t.scale, MAX_NODES // 2)
        hb = d / mb
        inside = hb * np.arange(-mb + 1, mb)  # both targets absorb
        neither = _coefficients(_chain(inside, t, skew, hb, (False, False)), mb - 1, np.ones(len(inside)))
        return stays, neither
    m = _steps(d, t.scale, MAX_STEPS)
    h = d / m
    far = min(m + math.ceil(REACH * t.scale / h), (MAX_NODES - 1) // 2)
    index = np.arange(-far, far + 1)
    decomposed = _chain(h * index, t, skew, h, (True, True))
    above = np.where(index > m, 1.0, np.where(index == m, 0.5, 0.0))
    below = np.where(index < -m, 1.0, np.where(index == -m, 0.5, 0.0))
    return _coefficients(decomposed, far, above), _coefficients(decomposed, far, below)


def probabilities(t: Terms, pair: tuple[Chain, Chain], clocks: np.ndarray) -> np.ndarray:
    """Up, flat and down at each clock Λ, as rows."""
    first, second = (np.exp(np.outer(clocks, c.rates)) @ c.coefficients for c in pair)
    if t.touch:
        p_up = np.clip(1 - first, 0.0, 1.0)
        p_flat = np.clip(second, 0.0, 1 - p_up)
        p_down = 1 - p_up - p_flat
    else:
        p_up = np.clip(first, 0.0, 1.0)
        p_down = np.clip(second, 0.0, 1 - p_up)
        p_flat = 1 - p_up - p_down
    return np.stack([p_up, p_flat, p_down], axis=1)


_COLUMN = {Direction.UP: 0, Direction.FLAT: 1, Direction.DOWN: 2}


class Ito:
    name = "ito"
    features: tuple[str, ...] = ("semivol_up", "semivol_down", "parkinson_5", "volatility", "flat_ticks")
    training_days = TRAINING_DAYS
    param_meanings = PARAM_MEANINGS
    card = Card(
        scientist="Kiyosi Itô (伊藤清)",
        character="확률미분방정식",
        logic=(
            "4분 움직임을 드리프트 없는 Itô 방정식 dX = σ(t)·g(X) dW로 본다. σ(t)는 빠른 분산이 느린 "
            "분산으로 되돌아가는 Heston의 기대 경로, g(X)는 위·아래 반편차로 정한 비대칭이다. 후진 "
            "Kolmogorov 방정식을 틱 격자에서 풀어 도달 정답의 세 확률을 낸다."
        ),
        unique_features=("semivol_up", "semivol_down", "parkinson_5"),
        training=(
            "분산의 되돌림 속도, 비대칭의 가중치, 변동성 배율을 최근 20거래일 1분봉으로 격자 탐색해 log "
            "loss가 가장 작은 값을 고른다. 격자의 고유분해는 비대칭 가중치에만 달려 있어 표본마다 세 번 "
            "한다."
        ),
        forecasting=(
            "분산 시계 Λ = c² × ∫₀⁴(θ + (v₀ − θ)e^(−κt))dt(v₀: `parkinson_5`², θ: `volatility`², 틱²) 동안 "
            "출발점 위에서는 (σ₊ ÷ σ̄)^w, 아래에서는 (σ₋ ÷ σ̄)^w의 변동성으로 퍼지는 마팅게일을 틱 격자의 "
            "마르코프 사슬로 푼다. 상승은 위 목표(봉 마감 중간가 + k틱 + 스프레드의 절반)에 4분 안에 한 "
            "번이라도 닿을 확률(아래는 열어 둔다), 보합은 위·아래 어느 목표에도 닿지 않을 확률, 하락은 "
            "나머지다. 도달 정답이 위를 먼저 보는 순서 그대로라 위·아래를 독립으로 보는 근사가 없다(k: 보합 "
            "범위. Λ는 0.5틱의 제곱보다 작아지지 않는다). 드리프트가 없어 기대수익은 0이다. 20분 변동성이나 "
            "보합 범위를 모르면 세 확률을 1/3씩 둔다. 5분 변동성을 모르면 20분 것을, 반편차를 모르면 대칭을 "
            "쓴다."
        ),
        failure=(
            "드리프트가 없어 추세장에서 방향을 말하지 못한다. 방향은 반편차의 비대칭에서만 나오므로, "
            "한쪽으로 크게 움직인 뒤 되돌림이 오는 장에서 그 쪽을 계속 높게 본다. 체결이 드문 분에는 5분 "
            "범위가 0에 가까워 분산이 낮게 잡힌다."
        ),
    )

    def default_model(self) -> Model:
        return Model("ito-default", dict(DEFAULT_PARAMS))

    def predict(self, features: FeatureVector, model: Model) -> Forecast:
        t = terms(features)
        if t is None:
            return UNKNOWN
        params = model.params
        lam = clock(t.fast, t.slow, params["speed"], params["scale"])
        p_up, p_flat, p_down = probabilities(t, chains(t, params["skew"]), np.array([lam]))[0]
        return Forecast(float(p_up), float(p_flat), float(p_down), 0.0)

    def fit(self, samples: Sequence[Sample]) -> Model:
        scored = with_outcomes(samples)
        grid: list[Params] = [
            {"speed": s, "skew": w, "scale": c} for s in SPEEDS for w in SKEWS for c in SCALES
        ]
        losses = np.zeros(len(grid))
        rows = {w: [i for i, p in enumerate(grid) if p["skew"] == w] for w in SKEWS}
        for s in scored:
            t = terms(s.features)
            if t is None:
                losses += math.log(3)
                continue
            column = _COLUMN[s.outcome or Direction.FLAT]
            for skew, at in rows.items():
                clocks = np.array([clock(t.fast, t.slow, grid[i]["speed"], grid[i]["scale"]) for i in at])
                p = probabilities(t, chains(t, skew), clocks)[:, column]
                losses[at] -= np.log(np.maximum(p, LOSS_FLOOR))
        losses /= len(scored)
        best = 0
        for i, loss in enumerate(losses):  # the first of equals, as ``lowest_log_loss``
            if loss < losses[best] - 1e-12:
                best = i
        return Model.fitted(self.name, grid[best], scored, float(losses[best]))
