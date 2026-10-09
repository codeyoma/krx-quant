"""Gauss: mean reversion, as an Ornstein–Uhlenbeck process.

Personality card:
- **Scientist**: Carl Friedrich Gauss.
- **Logic**: the price is drawn back to the session's VWAP (from the open) like a
  particle on a spring, jostled by noise: its gap X from the VWAP follows an
  Ornstein–Uhlenbeck process, dX = −κX dt + σ dW. Four minutes on, the gap is expected to
  have shrunk to X·e^(−4κ), so the price to have moved −X·(1 − e^(−4κ)) ticks. The speed κ
  blends today's (fitted on the session's gaps so far) with the one learned over past
  days. A gap within θ standard deviations of the VWAP (its z-score) is noise: no pull is
  expected. The forecast is a normal distribution around the move, as wide as the process
  spreads in four minutes at the last 20 minutes' volatility.
- **Own features**: the z-score against the VWAP, the reversion speed.
- **Learns**: the speed κ, by least squares of the four-minute moves on the gaps beyond
  the threshold; the weight of today's speed and the threshold θ, by grid search for the
  lowest log loss over the last 20 trading days.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from krx_quant.algorithms.contract import Card, Model, Sample
from krx_quant.algorithms.forecasts import UNKNOWN, log_loss, normal_forecast
from krx_quant.baselines import Forecast
from krx_quant.features import MAX_REVERSION_SPEED, FeatureVector
from krx_quant.pricing import Direction

HORIZON_MINUTES = 4.0
WEIGHTS = (0.0, 0.25, 0.5, 0.75, 1.0)  # of today's reversion speed against the learned one
THRESHOLDS = (0.0, 0.5, 1.0, 1.5, 2.0, 2.5)  # |z| below this is noise
DEFAULT_PARAMS = {"speed": 0.05, "weight": 0.5, "threshold": 1.0}


def reverted(gap: float, speed: float, horizon: float = HORIZON_MINUTES) -> float:
    """The expected move of a price ``gap`` from its mean, pulled back at ``speed`` per minute
    for ``horizon`` minutes.
    """
    return -gap * (1 - math.exp(-speed * horizon))


def spread(volatility: float, speed: float, horizon: float = HORIZON_MINUTES) -> float:
    """The standard deviation of that move, when one minute's is ``volatility``.

    Pulled back at φ = e^(−κ) a minute, the noise of the minutes adds up to
    √((1 − φ^(2h)) / (1 − φ²)) minutes' worth: √h for a random walk (κ = 0), one minute's at
    the strongest pull.
    """
    if speed < 1e-9:
        return volatility * math.sqrt(horizon)
    return volatility * math.sqrt((1 - math.exp(-2 * speed * horizon)) / (1 - math.exp(-2 * speed)))


class Gauss:
    name = "gauss"
    features: tuple[str, ...] = ("vwap_gap", "vwap_z", "reversion_speed", "volatility", "flat_ticks")
    training_days = 20
    card = Card(
        scientist="Carl Friedrich Gauss",
        character="역추세",
        logic=(
            "당일 VWAP(정규장 시작부터) 대비 괴리를 Ornstein–Uhlenbeck 과정으로 보고, "
            "평균회귀 속도로 4분 뒤 되돌림을 낸다. z-score가 임계값 안이면 잡음으로 본다."
        ),
        unique_features=("vwap_z", "reversion_speed"),
        training=(
            "OU 평균회귀 속도는 임계값 밖 괴리의 4분 움직임에 최소제곱으로 맞추고, "
            "당일 속도의 가중치와 z 임계값은 최근 20거래일 1분봉으로 격자 탐색해 "
            "log loss가 가장 작은 값을 고른다."
        ),
    )

    def default_model(self) -> Model:
        return Model("gauss-default", dict(DEFAULT_PARAMS))

    def predict(self, features: FeatureVector, model: Model) -> Forecast:
        return self._forecast(features, model.params)

    def fit(self, samples: Sequence[Sample]) -> Model:
        scored = [s for s in samples if s.outcome is not None]
        if not scored:
            raise ValueError("no training samples with an outcome")
        best: tuple[float, dict[str, float]] | None = None
        for threshold in THRESHOLDS:
            speed = self._fit_speed(scored, threshold)
            for weight in WEIGHTS:
                params = {"speed": speed, "weight": weight, "threshold": threshold}
                loss = sum(self._loss(s, params) for s in scored) / len(scored)
                if best is None or loss < best[0] - 1e-12:
                    best = (loss, params)
        assert best is not None
        loss, params = best
        return Model.fitted(self.name, params, scored, loss)

    @staticmethod
    def _fit_speed(scored: Sequence[Sample], threshold: float) -> float:
        """κ by least squares: a four-minute move of −X·(1 − e^(−4κ)) on each gap X beyond the
        threshold. 0 when gaps that far grew on the whole, or there are none.
        """
        pairs = [
            (gap, s.change_ticks)
            for s in scored
            if (gap := _beyond(s.features, threshold)) is not None and s.change_ticks is not None
        ]
        squares = sum(gap * gap for gap, _ in pairs)
        if squares <= 0:
            return 0.0
        shrink = -sum(gap * move for gap, move in pairs) / squares  # 1 − e^(−4κ)
        if shrink <= 0:
            return 0.0
        shrink = min(shrink, 1 - math.exp(-MAX_REVERSION_SPEED * HORIZON_MINUTES))
        return -math.log1p(-shrink) / HORIZON_MINUTES

    def _loss(self, sample: Sample, params: Mapping[str, float]) -> float:
        return log_loss(self._forecast(sample.features, params), sample.outcome or Direction.FLAT)

    def _forecast(self, features: FeatureVector, params: Mapping[str, float]) -> Forecast:
        k, volatility = features["flat_ticks"], features["volatility"]
        if k is None or volatility is None:
            return UNKNOWN
        today = features["reversion_speed"]
        speed = params["speed"]
        if today is not None:
            speed = params["weight"] * today + (1 - params["weight"]) * speed
        speed = min(max(speed, 0.0), MAX_REVERSION_SPEED)
        gap = _beyond(features, params["threshold"])
        move = 0.0 if gap is None else reverted(gap, speed)
        return normal_forecast(move, spread(volatility, speed), k)


def _beyond(features: FeatureVector, threshold: float) -> float | None:
    """The gap from the VWAP in ticks, if it is ``threshold`` standard deviations or more."""
    gap, z = features["vwap_gap"], features["vwap_z"]
    if gap is None or z is None or abs(z) < threshold:
        return None
    return gap
