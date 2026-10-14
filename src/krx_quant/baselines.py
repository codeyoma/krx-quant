"""Baseline predictions: the simple rules every algorithm has to beat (lift).

- always flat;
- the last four minutes' direction goes on;
- a random class, drawn in the proportions of the day's classes so far.

Each predicts one class with certainty. The random one draws from a generator seeded by
the symbol and bar close, so the same input always gives the same prediction. Each has a
card like an algorithm's (``cards.Card``), named after no scientist.
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from krx_quant.cards import Card
from krx_quant.pricing import Direction, classify


@dataclass(frozen=True)
class Forecast:
    p_up: float
    p_flat: float
    p_down: float
    expected_ticks: float

    @classmethod
    def certain(cls, direction: Direction, expected_ticks: float = 0.0) -> Forecast:
        return cls(
            p_up=1.0 if direction is Direction.UP else 0.0,
            p_flat=1.0 if direction is Direction.FLAT else 0.0,
            p_down=1.0 if direction is Direction.DOWN else 0.0,
            expected_ticks=expected_ticks,
        )

    def probability(self, direction: Direction) -> float:
        if direction is Direction.UP:
            return self.p_up
        return self.p_flat if direction is Direction.FLAT else self.p_down


@dataclass(frozen=True)
class Inputs:
    """What a baseline knows at a bar close."""

    symbol: str
    bar_close: datetime
    mid: float | None  # the reference price at the bar close
    mid_before: float | None  # four minutes earlier
    flat_ticks: int | None
    tick: int | None
    # Classes (by bar close) of the symbol's predictions already scored today.
    day_classes: Mapping[Direction, int]


class Baseline(Protocol):
    name: str
    version: str
    card: Card

    def predict(self, inputs: Inputs) -> Forecast: ...


_NOT_LEARNED = "배우지 않는다. 모든 알고리즘이 넘어야 하는 기준이라 규칙이 고정되어 있다."


class AlwaysFlat:
    name = "baseline-flat"
    version = "v1"
    card = Card(
        scientist=None,
        character="기준 예측: 늘 보합",
        logic=(
            "어느 봉 마감에서나 4분 뒤 중간가 변화가 보합 범위(k틱) 안에 머문다고 본다. 아무 정보도 쓰지 "
            "않는다."
        ),
        unique_features=(),
        training=_NOT_LEARNED,
        forecasting="보합에 확률 1, 상승과 하락에 0을 준다. 기대수익은 0틱이다.",
        failure=(
            "4분 변화가 보합 범위를 넘을 때마다 틀린다: 변동이 큰 장, 개장 직후, 호가단위에 비해 많이 "
            "움직이는 종목. 조용한 장에서는 맞힌 비율이 높아 넘기 어려운 기준이 된다."
        ),
    )

    def predict(self, inputs: Inputs) -> Forecast:
        return Forecast.certain(Direction.FLAT)


class Persistence:
    """The move of the last four minutes, classified like an outcome, continues."""

    name = "baseline-persist"
    version = "v1"
    card = Card(
        scientist=None,
        character="기준 예측: 직전 4분 방향",
        logic=(
            "직전 4분(t − 4분 → t)의 중간가 변화를 채점과 같은 규칙(보합 범위 k틱)으로 상승·보합·하락으로 "
            "나누고, 그 방향이 다음 4분에도 이어진다고 본다. 중간가를 모르면 보합이다."
        ),
        unique_features=(),
        training=_NOT_LEARNED,
        forecasting="그 방향에 확률 1, 나머지 둘에 0을 준다. 기대수익은 직전 4분 변화의 틱 수다.",
        failure=(
            "방향이 자주 바뀌는 장과 되돌림(직전 4분의 움직임이 되밀릴 때)에서 틀린다. 추세가 이어지거나 "
            "조용한 장에서는 맞기 쉽다."
        ),
    )

    def predict(self, inputs: Inputs) -> Forecast:
        if (
            inputs.mid is None
            or inputs.mid_before is None
            or inputs.flat_ticks is None
            or inputs.tick is None
        ):
            return Forecast.certain(Direction.FLAT)
        change = inputs.mid - inputs.mid_before
        return Forecast.certain(classify(change, inputs.flat_ticks, inputs.tick), change / inputs.tick)


class ClassRatioRandom:
    """A class drawn in the day's proportions so far, each class counted once more."""

    name = "baseline-random"
    version = "v1"
    card = Card(
        scientist=None,
        character="기준 예측: 비율대로 무작위",
        logic=(
            "그날 그 종목에서 이미 채점된 봉 마감의 클래스 비율(클래스마다 1을 더한다)대로 상승·보합·하락 중 "
            "하나를 뽑는다. 종목과 봉 마감으로 시드를 정해서 같은 입력이면 늘 같은 값이 나온다."
        ),
        unique_features=(),
        training="배우지 않는다. 그날 채점된 클래스 수만 센다(그날 첫 채점 전에는 셋이 똑같다).",
        forecasting="뽑은 클래스에 확률 1, 나머지 둘에 0을 준다. 기대수익은 0틱이다.",
        failure=(
            "정보 없이 뽑으므로 늘 운이다: 그날 비율이 이어지면 맞힌 비율이 대략 클래스 비율의 제곱합이다. "
            "장 초반에는 셀 봉이 적어 거의 고르게 뽑고, 장중에 클래스 비율이 바뀌면 지난 비율로 뽑는다."
        ),
    )

    def predict(self, inputs: Inputs) -> Forecast:
        rng = random.Random(f"{self.name}:{inputs.symbol}:{inputs.bar_close.isoformat()}")
        classes = list(Direction)
        weights = [inputs.day_classes.get(d, 0) + 1 for d in classes]
        return Forecast.certain(rng.choices(classes, weights)[0])


BASELINES: tuple[Baseline, ...] = (AlwaysFlat(), Persistence(), ClassRatioRandom())
BASELINE_NAMES = frozenset(b.name for b in BASELINES)
