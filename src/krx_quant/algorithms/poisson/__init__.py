"""Poisson: which side's trades are setting off more of their own.

Personality card:
- **Scientist**: Siméon Denis Poisson.
- **Logic**: buy- and sell-initiated trades as a Hawkes process (a self-exciting point
  process), discretized. Each trade, its size over the day's mean, excites its side and the
  other, the excitation fading with decay times of 10 and 60 seconds. Each side's arrivals
  over the next four minutes are expected as a baseline plus each excitation at the bar
  close times what it sets off. The lean of the two, (buys − sells) / (buys + sells), turns
  into up, flat and down by an ordered logistic: P(up) = σ(k·lean − c), P(down) =
  σ(−k·lean − c). The expected move is the classes' mean moves, weighted.
- **Own features**: the trades' arrival times, sides and sizes only: each side's excitation
  at both decays, and its arrivals in the last minute.
- **Learns**: the baselines and excitations by maximum likelihood of each sample's next
  four minutes of arrivals (the following samples' last minutes), as Poisson counts of a
  linear, nonnegative intensity (EM); then k and c by the lowest log loss over a grid.

The trades' sides are recorded only (backfilled bars have none): on a day with backfilled
bars only it does not learn, and without any trade's side today it forecasts nothing in
particular (every class a third).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from itertools import product

import numpy as np

from krx_quant.algorithms.contract import Card, Model, NothingToLearn, Sample
from krx_quant.algorithms.forecasts import UNKNOWN, class_moves, lowest_log_loss, param
from krx_quant.baselines import Forecast
from krx_quant.features import (
    EXCITATION_SECONDS,
    SIDES,
    FeatureVector,
    arrivals_feature,
    excitation_feature,
)
from krx_quant.pricing import Direction
from krx_quant.reference import BAR

EXCITATIONS = tuple(excitation_feature(side, seconds) for side in SIDES for seconds in EXCITATION_SECONDS)
ARRIVALS = tuple(arrivals_feature(side) for side in SIDES)
STEPS = 4  # minutes ahead
# The ordered logistic's slope and cut tried, after the intensities are learned.
SLOPES = (0.5, 1.0, 2.0, 4.0, 8.0, 16.0)
CUTS = (0.0, 0.25, 0.5, 1.0, 1.5, 2.0, 3.0)
# EM: at most this many rounds, stopping once the log-likelihood barely moves.
ROUNDS = 1000
_TOLERANCE = 1e-10


def default_params() -> dict[str, float]:
    """Before any training: a trade a minute on each side, nothing excited, no lean."""
    params = {"slope": 1.0, "cut": 1.0}
    for side in SIDES:
        params[param("base", side)] = float(STEPS)
        for excitation in EXCITATIONS:
            params[param("excite", side, excitation)] = 0.0
    return params | {param("move", d.value): 0.0 for d in Direction}


class Poisson:
    name = "poisson"
    features: tuple[str, ...] = (*EXCITATIONS, *ARRIVALS)
    training_days = 20
    card = Card(
        scientist="Siméon Denis Poisson",
        character="쏠림 감지",
        logic=(
            "매수·매도 시장가 체결의 도착을 자기흥분 점과정(Hawkes)으로 본다. 체결마다 크기(그날 평균 "
            "대비)만큼 자기 쪽과 반대쪽을 흥분시키고, 흥분은 10초·60초로 사라진다. 봉 마감의 흥분으로 "
            "4분 동안 매수·매도 도착을 기대하고, 그 쏠림을 순서형 로지스틱으로 상승·보합·하락 확률로 바꾼다."
        ),
        unique_features=(*EXCITATIONS, *ARRIVALS),
        training=(
            "최근 20거래일의 녹화된 체결로, 표본마다 다음 4분의 매수·매도 도착을 선형 강도의 Poisson "
            "최대우도(EM)로 맞춰 기준 강도와 흥분 계수를 추정한다. 쏠림의 기울기와 문턱은 로그손실로 고른다."
        ),
    )

    def default_model(self) -> Model:
        return Model(f"{self.name}-default", default_params())

    def predict(self, features: FeatureVector, model: Model) -> Forecast:
        x = [features[f] for f in EXCITATIONS]
        if any(v is None for v in x):
            return UNKNOWN
        p = model.params
        lean = _lean(p, [float(v) for v in x if v is not None])
        return _ordered(p["slope"], p["cut"], lean, p)

    def fit(self, samples: Sequence[Sample]) -> Model:
        pairs = _pairs(samples)
        if not pairs:
            raise NothingToLearn("no training samples with four minutes of the trades' sides after them")
        x = np.array([[1.0, *xs] for xs, _ in pairs])
        params = default_params()
        for i, side in enumerate(SIDES):
            theta = _poisson_em(x, np.array([ys[i] for _, ys in pairs]))
            params[param("base", side)] = float(theta[0])
            for excitation, value in zip(EXCITATIONS, theta[1:], strict=True):
                params[param("excite", side, excitation)] = float(value)
        scored = [s for s in samples if s.outcome is not None and _known(s.features)]
        if not scored:
            raise NothingToLearn("no scored samples with the trades' sides")
        params |= class_moves(scored)
        leans = np.array([_lean(params, [float(s.features[f] or 0.0) for f in EXCITATIONS]) for s in scored])
        outcomes = np.array([_CLASSES.index(s.outcome or Direction.FLAT) for s in scored])
        # Of equals, the first: the gentlest slope, then the lowest cut.
        params["slope"], params["cut"] = min(
            product(SLOPES, CUTS), key=lambda kc: _loss(kc[0], kc[1], leans, outcomes)
        )
        return lowest_log_loss(self.name, scored, [params], lambda f, q: self.predict(f, Model(self.name, q)))


def _known(features: FeatureVector) -> bool:
    return all(features[f] is not None for f in EXCITATIONS)


def _lean(p: Mapping[str, float], x: Sequence[float]) -> float:
    """(Buys − sells) / (buys + sells) expected over the next four minutes."""
    expected = [
        p[param("base", side)]
        + sum(p[param("excite", side, e)] * v for e, v in zip(EXCITATIONS, x, strict=True))
        for side in SIDES
    ]
    buys, sells = expected
    return (buys - sells) / (buys + sells) if buys + sells > 0 else 0.0


def _ordered(slope: float, cut: float, lean: float, p: Mapping[str, float]) -> Forecast:
    up = _sigmoid(slope * lean - cut)
    down = _sigmoid(-slope * lean - cut)
    flat = max(0.0, 1.0 - up - down)
    total = up + flat + down
    up, flat, down = up / total, flat / total, down / total
    expected = up * p[param("move", "up")] + flat * p[param("move", "flat")] + down * p[param("move", "down")]
    return Forecast(up, flat, down, expected)


_CLASSES = (Direction.UP, Direction.FLAT, Direction.DOWN)
_FLOOR = 1e-6  # as the log loss


def _loss(slope: float, cut: float, leans: np.ndarray, outcomes: np.ndarray) -> float:
    """The mean log loss of the ordered logistic over every sample at once."""
    up = 1 / (1 + np.exp(-(slope * leans - cut)))
    down = 1 / (1 + np.exp(-(-slope * leans - cut)))
    flat = np.maximum(0.0, 1.0 - up - down)
    probabilities = np.stack([up, flat, down], axis=1)
    probabilities /= probabilities.sum(axis=1, keepdims=True)
    chosen = probabilities[np.arange(len(outcomes)), outcomes]
    return float(-np.log(np.maximum(chosen, _FLOOR)).mean())


def _sigmoid(z: float) -> float:
    return 1 / (1 + math.exp(-z)) if z >= 0 else math.exp(z) / (1 + math.exp(z))


def _pairs(samples: Sequence[Sample]) -> list[tuple[list[float], list[float]]]:
    """Each sample with its excitations known, and the buy and sell arrivals of its next
    ``STEPS`` minutes: the last minutes of the samples one to four minutes later, all there."""
    by_close = {(s.symbol, s.bar_close): s for s in samples}
    out = []
    for s in samples:
        if not _known(s.features):
            continue
        later = [by_close.get((s.symbol, s.bar_close + step * BAR)) for step in range(1, STEPS + 1)]
        if any(t is None or any(t.features[a] is None for a in ARRIVALS) for t in later):
            continue
        arrivals = [sum(float(t.features[a] or 0.0) for t in later if t is not None) for a in ARRIVALS]
        out.append(([float(s.features[f] or 0.0) for f in EXCITATIONS], arrivals))
    return out


def _poisson_em(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Maximum likelihood of counts ``y`` ~ Poisson(x·θ) with θ ≥ 0, by EM (each round
    rescales θ by how much of the counts its share of the intensity explains)."""
    columns = x.sum(axis=0)
    mean = float(y.mean()) if len(y) else 0.0
    theta = np.where(columns > 0, max(mean, 1e-9) / (x.shape[1] * np.maximum(x.mean(axis=0), 1e-12)), 0.0)
    previous = None
    for _ in range(ROUNDS):
        intensity = np.maximum(x @ theta, 1e-12)
        likelihood = float((y * np.log(intensity) - intensity).sum())
        theta = np.where(columns > 0, theta * (x.T @ (y / intensity)) / np.maximum(columns, 1e-12), 0.0)
        if previous is not None and abs(likelihood - previous) < _TOLERANCE * max(1.0, abs(previous)):
            break
        previous = likelihood
    return theta
