"""Baseline predictions: the simple rules every algorithm has to beat (lift).

- always flat;
- the last four minutes' direction goes on;
- a random class, drawn in the proportions of the day's classes so far.

Each predicts one class with certainty. The random one draws from a generator seeded by
the symbol and bar close, so the same input always gives the same prediction.
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

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
        return {Direction.UP: self.p_up, Direction.FLAT: self.p_flat, Direction.DOWN: self.p_down}[direction]


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

    def predict(self, inputs: Inputs) -> Forecast: ...


class AlwaysFlat:
    name = "baseline-flat"
    version = "v1"

    def predict(self, inputs: Inputs) -> Forecast:
        return Forecast.certain(Direction.FLAT)


class Persistence:
    """The move of the last four minutes, classified like an outcome, continues."""

    name = "baseline-persist"
    version = "v1"

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

    def predict(self, inputs: Inputs) -> Forecast:
        rng = random.Random(f"{self.name}:{inputs.symbol}:{inputs.bar_close.isoformat()}")
        classes = list(Direction)
        weights = [inputs.day_classes.get(d, 0) + 1 for d in classes]
        return Forecast.certain(rng.choices(classes, weights)[0])


BASELINES: tuple[Baseline, ...] = (AlwaysFlat(), Persistence(), ClassRatioRandom())
