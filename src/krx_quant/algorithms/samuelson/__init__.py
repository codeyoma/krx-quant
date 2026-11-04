"""Samuelson: the stock price as a geometric Brownian motion (#518).

Personality card:
- **Scientist**: Paul Samuelson.
- **Logic**: dS = μS dt + σS dW, the stock price model Samuelson brought to economics (1965):
  the log price is a Brownian motion that drifts ν = μ − σ²/2 a minute and spreads σ per
  √minute. Both are estimated by maximum likelihood from the session's one-minute log returns
  since the open: their mean and their standard deviation. The drift is shrunk toward 0 by
  n ÷ (n + n₀), n the minutes since the open, and weighted by w; σ is scaled by c. The targets
  are set in log prices, up at ln(1 + d·tick ÷ price) and down at ln(1 − d·tick ÷ price) (d:
  k ticks, plus half the spread for the touch target). For the touch target, up is the log
  price's chance of reaching the upper target (the reflection principle), flat of staying
  between the two (``stay_probability``), down the rest; for the four-minute move, of ending
  past them.
- **Own features**: the session's drift and σ, and one tick as a log return.
- **Learns**: the weight w, the prior n₀ and the scale c, by grid search for the lowest log loss
  over the last 20 trading days.
- **Fails**: when the morning's trend turns, and in the first minutes, when the drift rests on
  few minutes and σ on the open's swings: it holds both constant over the session.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from krx_quant.algorithms.contract import Model, Sample
from krx_quant.algorithms.forecasts import (
    HORIZON_MINUTES,
    MIN_SPREAD_TICKS,
    UNKNOWN,
    Params,
    lowest_log_loss,
    reach_probability,
    stay_probability,
    with_outcomes,
)
from krx_quant.baselines import Forecast
from krx_quant.cards import Card, Meaning, learned
from krx_quant.features import FeatureVector

WEIGHTS = (0.0, 0.5, 1.0, 1.5)  # of the estimated drift
PRIORS = (0.0, 30.0, 120.0, 480.0)  # n₀: minutes of no drift the estimate is shrunk with
SCALES = (0.8, 1.0, 1.25)  # of the estimated σ
DEFAULT_PARAMS = {"weight": 0.5, "prior": 120.0, "scale": 1.0}
TRAINING_DAYS = 20
PARAM_MEANINGS: Mapping[str, Meaning] = {
    "weight": Meaning(
        "드리프트 가중치 w: 쓰는 드리프트 = w × 축소한 `session_drift`. 0이면 방향을 보지 않고 변동성만 "
        "본다. 0, 0.5, 1, 1.5 중에서 고른다.",
        "배",
        learned(TRAINING_DAYS),
    ),
    "prior": Meaning(
        "드리프트 축소의 사전 분 n₀: 개장 뒤 n분의 드리프트를 n ÷ (n + n₀)배로 줄인다. 0이면 줄이지 않는다. "
        "0, 30, 120, 480 중에서 고른다.",
        "분",
        learned(TRAINING_DAYS),
    ),
    "scale": Meaning(
        "σ 배율 c: 쓰는 σ = c × `session_sigma`. 0.8, 1, 1.25 중에서 고른다.",
        "배",
        learned(TRAINING_DAYS),
    ),
}


def _upper_tail(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2))


def _share(tick_bp: float) -> float:
    """A tick's share of the price, from its log return in bp (bounded: no overflow)."""
    return math.expm1(min(tick_bp / 1e4, 50.0))


def log_targets(distance: float, tick_bp: float) -> tuple[float, float]:
    """The log returns (bp) that move the price ``distance`` ticks up and down: ln(1 + d·r) and
    −ln(1 − d·r), r a tick's share of the price. Down is a little farther in log prices; a
    distance past the price itself is never reached down."""
    share = _share(tick_bp)
    up = 1e4 * math.log1p(distance * share)
    down = -1e4 * math.log1p(-distance * share) if distance * share < 1 else math.inf
    return up, down


def gbm_forecast(
    drift: float, sigma: float, tick_bp: float, flat_ticks: float, half_spread: float | None
) -> Forecast:
    """Up, flat and down for a log price that drifts ``drift`` and spreads ``sigma`` (bp) over
    the horizon: of reaching the targets k ticks plus ``half_spread`` away (the touch target: up
    reaches the upper one, flat stays between the two, down is the rest, as the label orders
    them), or of ending k ticks away (``half_spread`` None: the four-minute move). The expected
    move is E[S_T ÷ S_0 − 1] = e^(ν + σ²/2) − 1, in ticks.
    """
    spread = max(sigma, MIN_SPREAD_TICKS * tick_bp)
    if half_spread is not None:  # as the label orders them: up at all, flat neither, down the rest
        up, down = log_targets(flat_ticks + half_spread, tick_bp)
        p_up = reach_probability(up, drift, spread)
        p_flat = min(stay_probability(min(down, 1e9), up, drift, spread), 1 - p_up)
        p_down = max(0.0, 1 - p_up - p_flat)
    else:
        up, down = log_targets(flat_ticks, tick_bp)
        p_up = _upper_tail((up - drift) / spread)
        p_down = _upper_tail((down + drift) / spread) if math.isfinite(down) else 0.0
        p_flat = max(0.0, 1 - p_up - p_down)
    total = p_up + p_flat + p_down
    exponent = min((drift + spread * spread / 2) / 1e4, 50.0)
    expected = math.expm1(exponent) / _share(tick_bp)
    return Forecast(p_up / total, p_flat / total, p_down / total, expected)


class Samuelson:
    name = "samuelson"
    features: tuple[str, ...] = (
        "session_drift",
        "session_sigma",
        "tick_bp",
        "minutes_since_open",
        "flat_ticks",
    )
    training_days = TRAINING_DAYS
    param_meanings = PARAM_MEANINGS
    card = Card(
        scientist="Paul Samuelson",
        character="기하 브라운 운동",
        logic=(
            "주가를 기하 브라운 운동 dS = μS dt + σS dW로 보고, 개장부터의 1분 로그수익률로 드리프트와 σ를 "
            "최대우도로 추정해, 로그 가격이 4분 안에 목표에 닿을 확률을 반사 원리로 낸다."
        ),
        unique_features=("session_drift", "session_sigma", "tick_bp"),
        training=(
            "드리프트 가중치, 축소의 사전 분, σ 배율을 최근 20거래일 1분봉으로 격자 탐색해 log loss가 가장 "
            "작은 값을 고른다."
        ),
        forecasting=(
            "로그 가격이 4분 동안 4 × w × ν̂ × n ÷ (n + n₀)만큼 흐르고 c × σ̂ × √4만큼 퍼지는 브라운 "
            "운동이라고 본다(ν̂, σ̂: `session_drift`, `session_sigma`, n: 개장 뒤 분). 목표는 로그 가격으로 "
            "옮긴다: 위는 ln(1 + d × 호가단위 ÷ 기준가), 아래는 −ln(1 − d × 호가단위 ÷ 기준가)(d: k틱 + 봉 "
            "마감 스프레드의 절반). 상승은 위 목표에 4분 안에 한 번이라도 닿을 확률(반사 원리), 보합은 두 "
            "목표 사이에 4분 내내 머물 확률(상의 방법과 Girsanov 가중), 하락은 나머지다. 도달 정답이 위를 "
            "먼저 보는 순서 그대로라 위·아래를 독립으로 보는 근사가 없다(k: 보합 범위. 폭은 0.5틱보다 "
            "좁아지지 않는다). 기대수익은 E[S₄ ÷ S₀ − 1] = e^(드리프트 + 폭²/2) − 1을 틱으로 잰 값이다. "
            "드리프트·σ·틱 크기·보합 범위 중 하나라도 모르면 세 확률을 1/3씩 둔다."
        ),
        failure=(
            "아침의 추세가 꺾이는 날에 개장부터의 드리프트를 이어 가 틀리기 쉽다. σ를 그날 내내 일정하다고 "
            "보므로 개장 직후의 큰 출렁임이 오후의 σ를 부풀리고, 조용하던 장이 갑자기 움직이면 보합을 너무 "
            "높게 본다. 개장 직후에는 드리프트를 정한 분이 적어 n₀가 대부분을 지운다."
        ),
    )

    def default_model(self) -> Model:
        return Model("samuelson-default", dict(DEFAULT_PARAMS))

    def predict(self, features: FeatureVector, model: Model) -> Forecast:
        return self._forecast(features, model.params)

    def fit(self, samples: Sequence[Sample]) -> Model:
        grid = ({"weight": w, "prior": n, "scale": c} for w in WEIGHTS for n in PRIORS for c in SCALES)
        return lowest_log_loss(self.name, with_outcomes(samples), grid, self._forecast)

    def _forecast(self, features: FeatureVector, params: Params) -> Forecast:
        k, tick = features["flat_ticks"], features["tick_bp"]
        nu, sigma = features["session_drift"], features["session_sigma"]
        if k is None or tick is None or nu is None or sigma is None or tick <= 0:
            return UNKNOWN
        minutes = max(features["minutes_since_open"] or 0.0, 0.0)
        prior = params["prior"]
        shrink = 1.0 if prior <= 0 else minutes / (minutes + prior)
        drift = HORIZON_MINUTES * params["weight"] * nu * shrink
        spread = params["scale"] * sigma * math.sqrt(HORIZON_MINUTES)
        touch = features.touch
        return gbm_forecast(drift, spread, tick, k, None if touch is None else touch.half_spread)
