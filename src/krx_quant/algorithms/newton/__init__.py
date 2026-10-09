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
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from krx_quant.algorithms.contract import Card, Model, Sample
from krx_quant.algorithms.forecasts import (
    HORIZON_MINUTES,
    UNKNOWN,
    Params,
    lowest_log_loss,
    normal_forecast,
    with_outcomes,
)
from krx_quant.baselines import Forecast
from krx_quant.features import FeatureVector

# Heavier than three times the usual volume adds no more mass.
MAX_MASS = 3.0
DAMPINGS = (0.0, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0)
THRESHOLDS = (0.0, 0.5, 1.0, 1.5, 2.0, 3.0)
DEFAULT_PARAMS = {"damping": 0.5, "threshold": 1.0}


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
    training_days = 20
    card = Card(
        scientist="Isaac Newton",
        character="추세 추종",
        logic=(
            "가격의 속도와 가속도에 거래량을 질량으로 곱해 운동량과 힘을 구하고, "
            "마찰(감쇠)을 넣어 4분 뒤 움직임을 외삽한다."
        ),
        unique_features=("velocity", "acceleration", "relative_volume"),
        training="감쇠율과 임계값을 최근 20거래일 1분봉으로 격자 탐색해 log loss가 가장 작은 값을 고른다.",
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
