"""Gauss: mean reversion, as an Ornstein–Uhlenbeck process.

Personality card:
- **Scientist**: Carl Friedrich Gauss.
- **Logic**: the price is drawn back to the session's VWAP (from the open) like a
  particle on a spring, jostled by noise: its gap X from the VWAP follows an
  Ornstein–Uhlenbeck process, dX = −κX dt + σ dW. Four minutes on, the gap is expected to
  have shrunk to X·e^(−4κ), so the price to have moved −X·(1 − e^(−4κ)) ticks. The speed κ
  blends today's (fitted to the price's one-minute moves against its gaps so far) with the
  one learned over past days. A gap within θ standard deviations of the VWAP (its z-score)
  is noise: no pull is expected. The forecast is a normal distribution around the move, as
  wide as the process spreads in four minutes at the last 20 minutes' volatility.
- **Own features**: the z-score against the VWAP, the reversion speed.
- **Learns**: the speed κ, by least squares of the four-minute moves on the gaps beyond
  the threshold; the weight of today's speed and the threshold θ, by grid search for the
  lowest log loss over the last 20 trading days.
- **Fails**: when a trend runs on, away from the VWAP; and with no reversion in its
  training days it learns κ = 0 and says nothing of the direction (#162).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from krx_quant.algorithms.contract import Model, Sample
from krx_quant.algorithms.forecasts import (
    HORIZON_MINUTES,
    NORMAL_CLASSES,
    UNKNOWN,
    Params,
    lowest_log_loss,
    normal_forecast,
    with_outcomes,
)
from krx_quant.baselines import Forecast
from krx_quant.cards import Card, Meaning, learned
from krx_quant.features import FeatureVector, ou_speed

WEIGHTS = (0.0, 0.25, 0.5, 0.75, 1.0)  # of today's reversion speed against the learned one
THRESHOLDS = (0.0, 0.5, 1.0, 1.5, 2.0, 2.5)  # |z| below this is noise
DEFAULT_PARAMS = {"speed": 0.05, "weight": 0.5, "threshold": 1.0}
TRAINING_DAYS = 20
PARAM_MEANINGS: Mapping[str, Meaning] = {
    "speed": Meaning(
        "학습한 평균회귀 속도 κ: 4분 뒤 VWAP 대비 괴리가 e^(−4κ)배로 줄어든다고 본다. 0이면 되돌림이 없다. "
        "임계값 밖 괴리의 4분 움직임에 최소제곱으로 맞춘다.",
        "1/분",
        learned(TRAINING_DAYS),
    ),
    "weight": Meaning(
        "당일 속도(`reversion_speed`)의 가중치 w: 쓰는 κ = w × 당일 속도 + (1 − w) × 학습한 속도. 당일 "
        "속도가 없으면 학습한 속도만 쓴다. 0, 0.25, 0.5, 0.75, 1 중에서 고른다.",
        "비율 (0~1)",
        learned(TRAINING_DAYS),
    ),
    "threshold": Meaning(
        "z 임계값 θ: VWAP 대비 z-score의 절댓값이 θ보다 작으면 잡음으로 보고 되돌림을 0으로 둔다. 0, 0.5, 1, "
        "1.5, 2, 2.5 중에서 고른다. κ와 w가 0이면 어느 θ에서나 예측이 같아 뜻이 없다.",
        "표준편차",
        learned(TRAINING_DAYS),
    ),
}


def pullback(gap: float, speed: float, horizon: float = HORIZON_MINUTES) -> float:
    """The expected move of a price ``gap`` from its mean, pulled back at ``speed`` per
    minute for ``horizon`` minutes.
    """
    return -gap * (1 - math.exp(-speed * horizon)) + 0.0  # + 0.0: no pull is 0, not −0


def move_spread(volatility: float, speed: float, horizon: float = HORIZON_MINUTES) -> float:
    """The standard deviation of that move, when one minute's is ``volatility``.

    Pulled back by φ = e^(−κ) a minute, the noise of the minutes adds up to
    √((1 − φ^(2h)) / (1 − φ²)) minutes' worth: √h for a random walk (κ = 0), one minute's
    at the strongest pull.
    """
    if speed < 1e-9:
        return volatility * math.sqrt(horizon)
    return volatility * math.sqrt((1 - math.exp(-2 * speed * horizon)) / (1 - math.exp(-2 * speed)))


class Gauss:
    name = "gauss"
    features: tuple[str, ...] = ("vwap_gap", "vwap_z", "reversion_speed", "volatility", "flat_ticks")
    training_days = TRAINING_DAYS
    param_meanings = PARAM_MEANINGS
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
        forecasting=(
            "4분 뒤 중간가 변화를 되돌림 기대값 −X × (1 − e^(−4κ))(X: VWAP 대비 괴리, 틱)를 중심으로, 폭이 "
            "OU 과정이 4분 동안 퍼지는 만큼(1분 변동성 × √((1 − e^(−8κ)) ÷ (1 − e^(−2κ))), κ = 0이면 × √4)인 "
            "정규분포로 본다. κ는 당일 속도와 학습한 속도를 가중치로 섞은 값이고, |z|가 임계값보다 작으면 "
            f"중심을 0으로 둔다. {NORMAL_CLASSES} 기대수익은 그 중심이다. 변동성이나 보합 범위를 모르면 세 "
            "확률을 1/3씩 둔다. κ = 0, w = 0을 배운 모델은 중심이 늘 0이라 상승·하락 확률이 같다."
        ),
        failure=(
            "추세가 이어지는 장(뉴스, 지수 급변, 개장 직후 한 방향 흐름)에서 VWAP로의 되돌림을 기대해 틀리기 "
            "쉽다. 개장 초에는 VWAP를 만든 봉이 적어 괴리와 z가 흔들린다. 되돌림이 없던 기간으로 학습하면 κ "
            "= 0, w = 0을 배워 방향 정보 없이 0 중심 분포만 낸다."
        ),
    )

    def default_model(self) -> Model:
        return Model("gauss-default", dict(DEFAULT_PARAMS))

    def predict(self, features: FeatureVector, model: Model) -> Forecast:
        return self._forecast(features, model.params)

    def fit(self, samples: Sequence[Sample]) -> Model:
        scored = with_outcomes(samples)
        grid = []
        for threshold in THRESHOLDS:
            speed = self._fit_speed(scored, threshold)
            grid += [{"speed": speed, "weight": w, "threshold": threshold} for w in WEIGHTS]
        return lowest_log_loss(self.name, scored, grid, self._forecast)

    @staticmethod
    def _fit_speed(scored: Sequence[Sample], threshold: float) -> float:
        """κ by least squares: a four-minute move of −X·(1 − e^(−4κ)) on each gap X beyond the
        threshold.
        """
        pairs = [
            (gap, s.change_ticks)
            for s in scored
            if (gap := _gap_beyond(s.features, threshold)) is not None and s.change_ticks is not None
        ]
        return ou_speed(pairs, HORIZON_MINUTES)

    def _forecast(self, features: FeatureVector, params: Params) -> Forecast:
        learned, weight, threshold = params["speed"], params["weight"], params["threshold"]
        k, volatility = features["flat_ticks"], features["volatility"]
        if k is None or volatility is None:
            return UNKNOWN
        today = features["reversion_speed"]
        speed = learned if today is None else weight * today + (1 - weight) * learned
        gap = _gap_beyond(features, threshold)
        move = 0.0 if gap is None else pullback(gap, speed)
        return normal_forecast(move, move_spread(volatility, speed), k)


def _gap_beyond(features: FeatureVector, threshold: float) -> float | None:
    """The gap from the VWAP in ticks, if it is ``threshold`` standard deviations or more."""
    gap, z = features["vwap_gap"], features["vwap_z"]
    if gap is None or z is None or abs(z) < threshold:
        return None
    return gap
