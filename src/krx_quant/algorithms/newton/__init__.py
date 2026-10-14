"""Newton: trend following, as mechanics.

Personality card:
- **Scientist**: Isaac Newton.
- **Logic**: the price has a velocity and an acceleration (a parabola through the last
  six one-minute closes, in ticks: within a price band a tick is a fixed share of the
  price, so this is the slope and curvature of returns). Volume is the mass: a move on
  heavy volume carries more momentum and force (both scaled by volume relative to the
  last hour). Friction slows it down: with damping γ, dv/dt = F − γv, and the move over
  the next four minutes is extrapolated from there. A move smaller than θ·k (the flat
  band) is treated as none. The forecast is a normal distribution around that move, as
  wide as the last 20 minutes' volatility.
- **Own features**: velocity, acceleration, relative volume.
- **Learns**: the damping γ and the threshold θ, by grid search for the lowest log loss
  over the last 20 trading days.
- **Fails**: in a range that keeps turning, and when a move on heavy volume turns at once.
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
from krx_quant.features import FeatureVector

# Heavier than three times the usual volume adds no more mass.
MAX_MASS = 3.0
DAMPINGS = (0.0, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0)
THRESHOLDS = (0.0, 0.5, 1.0, 1.5, 2.0, 3.0)
DEFAULT_PARAMS = {"damping": 0.5, "threshold": 1.0}
TRAINING_DAYS = 20
PARAM_MEANINGS: Mapping[str, Meaning] = {
    "damping": Meaning(
        "감쇠율 γ: dv/dt = F − γv의 마찰. 클수록 추세가 빨리 꺾인다고 본다. 0, 0.1, 0.25, 0.5, 1, 2, 4 "
        "중에서 고른다.",
        "1/분",
        learned(TRAINING_DAYS),
    ),
    "threshold": Meaning(
        "임계값 θ: 외삽한 4분 변위가 θ × 보합 범위 k보다 작으면 움직임이 없다고 본다. 0, 0.5, 1, 1.5, 2, 3 "
        "중에서 고른다.",
        "보합 범위 k의 배수",
        learned(TRAINING_DAYS),
    ),
}


def displacement(velocity: float, force: float, damping: float, horizon: float = HORIZON_MINUTES) -> float:
    """How far a body moving at ``velocity``, pushed by ``force`` against friction ``damping``,
    goes in ``horizon``: the solution of dv/dt = force − damping·v.
    """
    if damping < 1e-9:
        return velocity * horizon + force * horizon**2 / 2
    terminal = force / damping
    return terminal * horizon + (velocity - terminal) * (1 - math.exp(-damping * horizon)) / damping


class Newton:
    name = "newton"
    features: tuple[str, ...] = ("velocity", "acceleration", "relative_volume", "volatility", "flat_ticks")
    training_days = TRAINING_DAYS
    param_meanings = PARAM_MEANINGS
    card = Card(
        scientist="Isaac Newton",
        character="추세 추종",
        logic=(
            "가격의 속도와 가속도에 거래량을 질량으로 곱해 운동량과 힘을 구하고, "
            "마찰(감쇠)을 넣어 4분 뒤 움직임을 외삽한다."
        ),
        unique_features=("velocity", "acceleration", "relative_volume"),
        training="감쇠율과 임계값을 최근 20거래일 1분봉으로 격자 탐색해 log loss가 가장 작은 값을 고른다.",
        forecasting=(
            "4분 뒤 중간가 변화를 외삽한 변위(틱)를 중심으로, 폭이 1분 변동성 × √4인 정규분포로 본다. "
            f"{NORMAL_CLASSES} 기대수익은 그 변위다. 속도·가속도·상대 거래량 중 하나라도 모르면 변위를 "
            "0으로, 변동성이나 보합 범위를 모르면 세 확률을 1/3씩 둔다."
        ),
        failure=(
            "방향이 자주 뒤집히는 박스권과 급등락 뒤 되돌림 장에서 직전 추세를 외삽해 틀리기 쉽다. 거래량이 "
            "한꺼번에 몰린 봉 뒤에는 힘을 크게 잡아 꺾이는 순간을 놓친다. 체결이 드문 종목에서는 6개 봉 "
            "종가로 맞춘 속도·가속도가 잡음이다."
        ),
    )

    def default_model(self) -> Model:
        return Model("newton-default", dict(DEFAULT_PARAMS))

    def predict(self, features: FeatureVector, model: Model) -> Forecast:
        return self._forecast(features, model.params)

    def fit(self, samples: Sequence[Sample]) -> Model:
        grid = ({"damping": d, "threshold": t} for d in DAMPINGS for t in THRESHOLDS)
        return lowest_log_loss(self.name, with_outcomes(samples), grid, self._forecast)

    def _forecast(self, features: FeatureVector, params: Params) -> Forecast:
        damping, threshold = params["damping"], params["threshold"]
        k = features["flat_ticks"]
        spread = features["volatility"]
        if k is None or spread is None:
            return UNKNOWN
        v, a, mass = features["velocity"], features["acceleration"], features["relative_volume"]
        move = 0.0
        if v is not None and a is not None and mass is not None:
            m = min(max(mass, 0.0), MAX_MASS)
            move = displacement(m * v, m * a, damping)
            if abs(move) < threshold * k:
                move = 0.0
        return normal_forecast(move, spread * math.sqrt(HORIZON_MINUTES), k)
