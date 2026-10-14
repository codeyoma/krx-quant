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
  four minutes of arrivals (the following samples' last minutes) under a linear,
  nonnegative intensity (EM). The arrivals are sizes over the mean, not whole counts, so it
  is a quasi-Poisson likelihood. Then k and c by the lowest log loss over a grid.
- **Fails**: when one side's rush is taken in and undone, and when trades are few.

The trades' sides are recorded only (backfilled bars have none): on a day with backfilled
bars only it does not learn, and without any trade's side today it forecasts nothing in
particular (every class a third).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from itertools import product

import numpy as np

from krx_quant.algorithms.contract import Model, NothingToLearn, Sample
from krx_quant.algorithms.forecasts import CLASSES, LOSS_FLOOR, UNKNOWN, class_moves, lowest_log_loss, param
from krx_quant.baselines import Forecast
from krx_quant.cards import TOUCH_LABEL, Card, Meaning, class_move_meanings, learned
from krx_quant.features import ARRIVAL_FEATURES, EXCITATION_FEATURES, SIDES, FeatureVector
from krx_quant.pricing import Direction
from krx_quant.reference import BAR

EXCITATIONS = EXCITATION_FEATURES
ARRIVALS = ARRIVAL_FEATURES  # one minute each: four following samples make the four minutes ahead
STEPS = 4  # minutes ahead
# The ordered logistic's slope and cut tried, after the intensities are learned. Of equals,
# the first: the gentlest slope, then the lowest cut.
SLOPES = (0.5, 1.0, 2.0, 4.0, 8.0, 16.0)
CUTS = (0.0, 0.25, 0.5, 1.0, 1.5, 2.0, 3.0)
# EM: at most this many rounds, stopping once the log-likelihood barely moves.
ROUNDS = 1000
_TOLERANCE = 1e-10


def default_params() -> dict[str, float]:
    """Before any training: four trades' worth over four minutes on each side, nothing
    excited, no lean."""
    params = {"slope": 1.0, "cut": 1.0}
    for side in SIDES:
        params[param("base", side)] = float(STEPS)
        for excitation in EXCITATIONS:
            params[param("excite", side, excitation)] = 0.0
    return params | {param("move", d.value): 0.0 for d in Direction}


TRAINING_DAYS = 20


def _param_meanings() -> dict[str, Meaning]:
    when = f"{learned(TRAINING_DAYS)} 녹화된 날(체결 방향)로만 배운다."
    sides = {"buy": "매수", "sell": "매도"}
    out = {
        "slope": Meaning(
            "순서형 로지스틱의 기울기 k: 클수록 작은 쏠림에도 확률이 한쪽으로 기운다. 0.5, 1, 2, 4, 8, 16 "
            "중에서 로그손실로 고른다.",
            "로짓 ÷ 쏠림",
            when,
        ),
        "cut": Meaning(
            "순서형 로지스틱의 문턱 c: 클수록 보합 확률이 커진다. 0, 0.25, 0.5, 1, 1.5, 2, 3 중에서 "
            "로그손실로 고른다.",
            "로짓",
            when,
        ),
    }
    for side in SIDES:
        out[param("base", side)] = Meaning(
            f"{sides[side]} 쪽 기준 강도: 흥분이 없을 때 다음 4분에 기대하는 {sides[side]} 시장가 체결 "
            "크기의 합. 학습 전에는 4(1분에 평균 크기 하나)다.",
            "평균 체결 크기의 배수 (4분)",
            when,
        )
        for excitation in EXCITATIONS:
            kind = "자기 흥분" if excitation.startswith(side) else "교차 흥분"
            out[param("excite", side, excitation)] = Meaning(
                f"봉 마감의 `{excitation}` 값이 1일 때 다음 4분 {sides[side]} 도착에 더하는 몫({kind}, 0 "
                "이상).",
                "4분 도착 ÷ 흥분",
                when,
            )
    moves = {name: Meaning(m.text, m.unit, when) for name, m in class_move_meanings(TRAINING_DAYS).items()}
    return out | moves


PARAM_MEANINGS: Mapping[str, Meaning] = _param_meanings()


class Poisson:
    name = "poisson"
    features: tuple[str, ...] = (*EXCITATIONS, *ARRIVALS)
    training_days = TRAINING_DAYS
    param_meanings = PARAM_MEANINGS
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
        forecasting=(
            "봉 마감의 흥분 4개로 다음 4분의 매수·매도 도착 기댓값(기준 강도 + Σ 흥분 계수 × 흥분)을 내고, "
            "쏠림 = (매수 − 매도) ÷ (매수 + 매도)를 순서형 로지스틱으로 세 확률로 바꾼다: P(상승) = σ(k × "
            "쏠림 − c), P(하락) = σ(−k × 쏠림 − c), 나머지가 보합. 기울기 k와 문턱 c는 "
            f"{TOUCH_LABEL}에 대한 로그손실로 고른다. 기대수익은 학습에서 클래스별 평균 4분 변화를 세 확률로 "
            "가중한 값이다. 그날 방향이 있는 체결을 받기 전(흥분을 모름)에는 세 확률을 1/3씩 둔다."
        ),
        failure=(
            "한쪽 시장가 체결이 몰린 뒤 반대쪽이 받아 내며 되돌리는 장에서 쏠림을 따라 틀리기 쉽다. 체결이 "
            "드문 시간(점심 무렵)에는 흥분이 작아 방향을 거의 말하지 않는다. 체결 쏠림 없이 호가나 "
            "지수만으로 움직이는 가격은 보지 못한다."
        ),
    )

    def default_model(self) -> Model:
        return Model(f"{self.name}-default", default_params())

    def predict(self, features: FeatureVector, model: Model) -> Forecast:
        x = [features[f] for f in EXCITATIONS]
        if any(v is None for v in x):
            return UNKNOWN
        return _ordered(model.params, _lean(model.params, [float(v) for v in x if v is not None]))

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
        outcomes = np.array([CLASSES.index(s.outcome or Direction.FLAT) for s in scored])
        best: tuple[float, float, float] | None = None
        for slope, cut in product(SLOPES, CUTS):
            chosen = _probabilities(slope, cut, leans)[np.arange(len(outcomes)), outcomes]
            loss = float(-np.log(np.maximum(chosen, LOSS_FLOOR)).mean())
            if best is None or loss < best[0] - 1e-12:
                best = (loss, slope, cut)
        assert best is not None
        params["slope"], params["cut"] = best[1], best[2]
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


def _ordered(p: Mapping[str, float], lean: float) -> Forecast:
    up, flat, down = (float(q) for q in _probabilities(p["slope"], p["cut"], np.array([lean]))[0])
    expected = sum(q * p[param("move", c.value)] for q, c in zip((up, flat, down), CLASSES, strict=True))
    return Forecast(up, flat, down, expected)


def _probabilities(slope: float, cut: float, leans: np.ndarray) -> np.ndarray:
    """The ordered logistic's up, flat and down for each lean: P(up) = σ(k·lean − c),
    P(down) = σ(−k·lean − c), the rest flat (never below 0, as c ≥ 0)."""
    up = 1 / (1 + np.exp(-(slope * leans - cut)))
    down = 1 / (1 + np.exp(slope * leans + cut))
    flat = np.maximum(0.0, 1.0 - up - down)
    probabilities: np.ndarray = np.stack([up, flat, down], axis=1)
    normalized: np.ndarray = probabilities / probabilities.sum(axis=1, keepdims=True)
    return normalized


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
