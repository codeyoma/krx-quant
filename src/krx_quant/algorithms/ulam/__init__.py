"""Ulam: Monte Carlo, the session's own minutes drawn again (#518).

Personality card:
- **Scientist**: Stanisław Ulam.
- **Logic**: no formula for the price's path: the next four minutes are drawn from the session's
  last 30 minutes as they were, each minute its close's change and its high's and low's
  distance from the close before (ticks). A stationary block bootstrap (Politis & Romano 1994)
  keeps runs of consecutive minutes together, a block lasting b minutes on average; the pool's
  mean change is kept by the share w (the rest taken out of every minute) and the minutes are
  scaled by c. Thousands of paths, the same draws at every bar close (a fixed seed), are each
  read as the label reads the bars: up when a minute's high reaches the upper target, else down
  when a low reaches the lower one, else flat (the four-minute move: where the path ends). The
  three probabilities are the paths' shares, smoothed by half a path each; the expected move is
  their mean end.
- **Own features**: the minutes' highs and lows against the close before.
- **Learns**: the block length b, the drift's share w and the scale c, by grid search for the
  lowest log loss over the last 20 trading days, on the first 256 of the same draws.
- **Fails**: when the next minutes are unlike the last half hour (a news break, the open, the
  close's auction): it can only draw what it has seen. With fewer than ten minutes in the pool
  it falls back on a normal distribution of the 20 minutes' volatility.
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
    UNKNOWN,
    Params,
    forecasting,
    log_loss,
    ordered_forecast,
    with_outcomes,
)
from krx_quant.baselines import Forecast
from krx_quant.cards import Card, Meaning, learned
from krx_quant.features import FeatureVector
from krx_quant.pricing import Direction

MINUTES = int(HORIZON_MINUTES)
PATHS = 2000  # drawn at each bar close
FIT_PATHS = 256  # in training: the first of the same draws
MIN_POOL = 10  # minutes; fewer: the normal fallback
BLOCKS = (1.0, 2.0, 4.0)  # b: a block's mean length, in minutes
DRIFTS = (0.0, 0.5, 1.0)  # w: the share of the pool's mean change kept
SCALES = (0.8, 1.0, 1.25)  # c: of every minute
DEFAULT_PARAMS = {"block": 2.0, "drift": 0.0, "scale": 1.0}
TRAINING_DAYS = 20
SMOOTHING = 0.5  # paths added to each class's count
_DRAWS = np.random.default_rng(518)
_PICK = _DRAWS.random((PATHS, MINUTES))  # where a new block starts, as a share of the pool
_STAY = _DRAWS.random((PATHS, MINUTES))  # below 1 − 1/b: the minute carries on its block
PARAM_MEANINGS: Mapping[str, Meaning] = {
    "block": Meaning(
        "블록 평균 길이 b: 이어진 분을 평균 b분씩 함께 뽑아(정상 블록 부트스트랩) 분 사이의 상관을 남긴다. "
        "1이면 분마다 따로 뽑는다. 1, 2, 4 중에서 고른다.",
        "분",
        learned(TRAINING_DAYS),
    ),
    "drift": Meaning(
        "드리프트 비율 w: 뽑을 분들의 평균 종가 변화 중 남기는 몫. 0이면 평균을 모두 빼 방향은 분포의 "
        "모양에서만 나온다. 0, 0.5, 1 중에서 고른다.",
        "비율 (0~1)",
        learned(TRAINING_DAYS),
    ),
    "scale": Meaning(
        "배율 c: 뽑은 분의 종가 변화와 고가·저가 이탈을 c배로 본다. 0.8, 1, 1.25 중에서 고른다.",
        "배",
        learned(TRAINING_DAYS),
    ),
}


class Pool(NamedTuple):
    """The minutes to draw from, oldest first, in ticks, and the targets."""

    changes: np.ndarray
    highs: np.ndarray
    lows: np.ndarray
    distance: float  # to each target: k plus half the spread, or k for the four-minute move
    touch: bool


def pool(features: FeatureVector) -> Pool | None:
    """The last 30 minutes and the targets; None without a flat band or with too few minutes."""
    k = features["flat_ticks"]
    highs, lows = features.series("minute_highs"), features.series("minute_lows")
    n = min(len(highs), len(lows))
    changes = features.series("minute_ticks")[-n:] if n else ()
    if k is None or n < MIN_POOL or len(changes) < n:
        return None
    touch = features.touch
    distance = k + (0.0 if touch is None else touch.half_spread)
    return Pool(np.array(changes), np.array(highs[-n:]), np.array(lows[-n:]), distance, touch is not None)


def draws(size: np.ndarray, block: float, pick: np.ndarray, stay: np.ndarray) -> np.ndarray:
    """The minutes each path draws, as indices into pools of ``size`` minutes (broadcast with the
    draws): a minute starts a new block at a random minute, or carries on the one before (the
    next minute, the pool taken as a circle) with chance 1 − 1/b."""
    fresh = np.floor(pick * size).astype(np.int64)
    sizes = np.broadcast_to(size, fresh.shape)
    out = fresh.copy()
    carry = stay < 1 - 1 / block
    for m in range(1, MINUTES):
        out[..., m] = np.where(carry[..., m], (out[..., m - 1] + 1) % sizes[..., m], fresh[..., m])
    return out


def outcomes(
    changes: np.ndarray, highs: np.ndarray, lows: np.ndarray, distance: np.ndarray, touch: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Each path's up, down (as booleans) and end, the paths along the last axis but one and the
    minutes along the last: the touch target as the label reads bars (a high at the upper target
    first, else a low at the lower), or where the path ends."""
    before = np.cumsum(changes, axis=-1) - changes  # where each minute starts
    end = changes.sum(axis=-1)
    d = distance[..., None]
    reach_up = (before + highs).max(axis=-1) >= d
    reach_down = (before + lows).min(axis=-1) <= -d
    t = touch[..., None]
    up = np.where(t, reach_up, end >= d)
    down = np.where(t, reach_down & ~reach_up, end <= -d)
    return up, down, end


def shares(up: np.ndarray, down: np.ndarray) -> np.ndarray:
    """Up, flat and down as the paths' shares, smoothed, along a last axis."""
    paths = up.shape[-1]
    n_up, n_down = up.sum(axis=-1), down.sum(axis=-1)
    n_flat = paths - n_up - n_down
    counts = np.stack([n_up, n_flat, n_down], axis=-1) + SMOOTHING
    return counts / (paths + 3 * SMOOTHING)


class Ulam:
    name = "ulam"
    features: tuple[str, ...] = ("minute_highs", "minute_lows", "minute_ticks", "volatility", "flat_ticks")
    training_days = TRAINING_DAYS
    param_meanings = PARAM_MEANINGS
    card = Card(
        scientist="Stanisław Ulam",
        character="몬테카를로",
        logic=(
            "그날 최근 30분의 1분봉(종가 변화, 고가·저가 이탈)을 블록 부트스트랩으로 다시 뽑아 4분 경로를 "
            "2,000개 만들고, 경로마다 도달 정답을 봉으로 읽듯 세어 세 확률을 낸다."
        ),
        unique_features=("minute_highs", "minute_lows"),
        training=(
            "블록 길이, 드리프트 비율, 배율을 최근 20거래일 1분봉으로 격자 탐색해 log loss가 가장 작은 값을 "
            "고른다. 학습은 같은 난수의 앞 256개 경로로 한다."
        ),
        forecasting=(
            "최근 30분의 분마다 (종가 변화, 고가 − 앞 분 종가, 저가 − 앞 분 종가)(틱)를 정상 블록 "
            "부트스트랩으로 4분 뽑는다(평균 b분씩 이어서, 풀은 원으로 본다). 풀의 평균 종가 변화 중 1 − "
            "w만큼을 분마다 빼고 c배 한다. 고정 시드의 같은 난수로 경로 2,000개를 만들고, 상승은 어느 분의 "
            "고가가 위 목표(봉 마감 중간가 + k틱 + 스프레드의 절반)에 닿은 경로, 하락은 그렇지 않고 저가가 "
            "아래 목표에 닿은 경로, 나머지가 보합이다(도달 정답의 순서 그대로). 세 몫에 경로 반 개씩을 더해 "
            "확률로 본다. 기대수익은 경로 끝의 평균이다. 풀이 10분보다 짧으면 0 중심, 폭이 1분 변동성 × √4인 "
            "브라운 운동으로 보고 같은 순서로 세 확률을 낸다(상승은 위 목표에 닿을 확률, 보합은 두 목표 "
            "사이에 머물 확률, 하락은 나머지). 보합 범위를 모르면(그 브라운 운동에서는 변동성도) 세 확률을 "
            "1/3씩 둔다."
        ),
        failure=(
            "다음 4분이 최근 30분과 다를 때(뉴스, 개장 직후, 종가 동시호가 직전) 본 적 없는 움직임을 뽑지 "
            "못해 틀린다. 조용한 30분 뒤에는 보합을, 출렁인 30분 뒤에는 도달을 너무 높게 본다. 경로 수가 "
            "유한해 아주 작은 확률은 경로 반 개 몫보다 작아지지 않는다."
        ),
    )

    def default_model(self) -> Model:
        return Model("ulam-default", dict(DEFAULT_PARAMS))

    def predict(self, features: FeatureVector, model: Model) -> Forecast:
        p = pool(features)
        if p is None:
            return self._fallback(features)
        params = model.params
        size = np.array(len(p.changes))
        index = draws(size, params["block"], _PICK, _STAY)
        shift = (1 - params["drift"]) * p.changes.mean()
        scale = params["scale"]
        up, down, end = outcomes(
            scale * (p.changes[index] - shift),
            scale * (p.highs[index] - shift),
            scale * (p.lows[index] - shift),
            np.array(p.distance),
            np.array(p.touch),
        )
        p_up, p_flat, p_down = shares(up, down)
        return Forecast(float(p_up), float(p_flat), float(p_down), float(end.mean()))

    @staticmethod
    def _fallback(features: FeatureVector) -> Forecast:
        k, volatility = features["flat_ticks"], features["volatility"]
        if k is None or volatility is None:
            return UNKNOWN
        return ordered_forecast(0.0, volatility * math.sqrt(HORIZON_MINUTES), k)

    def fit(self, samples: Sequence[Sample]) -> Model:
        scored = with_outcomes(samples)
        grid: list[Params] = [
            {"block": b, "drift": w, "scale": c} for b in BLOCKS for w in DRIFTS for c in SCALES
        ]
        losses = np.zeros(len(grid))
        pooled: list[tuple[Pool, int]] = []
        for s in scored:
            p = pool(s.features)
            if p is None:
                with forecasting(s.features):
                    losses += log_loss(self._fallback(s.features), s.outcome or Direction.FLAT)
            else:
                pooled.append((p, _COLUMN[s.outcome or Direction.FLAT]))
        for at in range(0, len(pooled), _CHUNK):
            losses += self._chunk_losses(pooled[at : at + _CHUNK], grid)
        losses /= len(scored)
        best = 0
        for i, loss in enumerate(losses):  # the first of equals, as ``lowest_log_loss``
            if loss < losses[best] - 1e-12:
                best = i
        return Model.fitted(self.name, grid[best], scored, float(losses[best]))

    @staticmethod
    def _chunk_losses(chunk: Sequence[tuple[Pool, int]], grid: Sequence[Params]) -> np.ndarray:
        """The summed log loss of each grid point over the chunk's samples, on FIT_PATHS paths each."""
        n = len(chunk)
        width = max(len(p.changes) for p, _ in chunk)
        changes, highs, lows = (np.zeros((n, width)) for _ in range(3))
        for i, (p, _) in enumerate(chunk):
            size = len(p.changes)
            changes[i, :size], highs[i, :size], lows[i, :size] = p.changes, p.highs, p.lows
        size = np.array([len(p.changes) for p, _ in chunk])
        distance = np.array([p.distance for p, _ in chunk])
        touch = np.array([p.touch for p, _ in chunk])
        column = np.array([c for _, c in chunk])
        means = changes.sum(axis=1) / size
        rows = np.arange(n)[:, None, None]
        losses = np.zeros(len(grid))
        for block in BLOCKS:
            index = draws(size[:, None, None], block, _PICK[None, :FIT_PATHS], _STAY[None, :FIT_PATHS])
            c, h, lo = changes[rows, index], highs[rows, index], lows[rows, index]
            for i, params in enumerate(grid):
                if params["block"] != block:
                    continue
                shift = ((1 - params["drift"]) * means)[:, None, None]
                scale = params["scale"]
                up, down, _ = outcomes(
                    scale * (c - shift), scale * (h - shift), scale * (lo - shift), distance, touch
                )
                p = shares(up, down)[np.arange(n), column]
                losses[i] -= np.log(np.maximum(p, LOSS_FLOOR)).sum()
        return losses


_COLUMN = {Direction.UP: 0, Direction.FLAT: 1, Direction.DOWN: 2}
_CHUNK = 512  # samples drawn at once in training: 512 × 256 paths × 4 minutes
