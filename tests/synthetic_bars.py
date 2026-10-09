"""Made-up days of one-minute bars, for training tests."""

from __future__ import annotations

import math
import random
from datetime import date, datetime, time, timedelta

from krx_quant.feeds import KST
from krx_quant.reference import Bar

TICK = 100  # the tick from 50,000 to 200,000 won


def day_of_bars(day: date, steps_in_ticks: list[float], start: int = 81_200, seed: int = 0) -> list[Bar]:
    """Bars from 09:00, one per step; each close moves by the step (rounded to whole ticks)."""
    rng = random.Random(seed)
    bars: list[Bar] = []
    level = 0.0
    previous = start
    for i, step in enumerate(steps_in_ticks):
        level += step
        close = start + TICK * round(level)
        at = datetime.combine(day, time(9), KST) + timedelta(minutes=i)
        bars.append(
            Bar(
                start=at,
                open=previous,
                high=max(previous, close),
                low=min(previous, close),
                close=close,
                volume=rng.randint(800, 1200),
                trades=0,
                first_at=at,
                last_at=at,
            )
        )
        previous = close
    return bars


def trending(day: date, ticks_per_minute: float = 1.0, noise: float = 0.3, seed: int = 0) -> list[Bar]:
    rng = random.Random(seed)
    return day_of_bars(day, [ticks_per_minute + rng.gauss(0, noise) for _ in range(390)], seed=seed)


def random_walk(day: date, seed: int = 0) -> list[Bar]:
    rng = random.Random(seed)
    return day_of_bars(day, [rng.choice((-1.0, 0.0, 1.0)) for _ in range(390)], seed=seed)


def mean_reverting(day: date, speed: float = 0.3, noise: float = 1.5, seed: int = 0) -> list[Bar]:
    """A price pulled back to where it opened at ``speed`` per minute (an Ornstein–Uhlenbeck
    process, in ticks), jostled by ``noise``.
    """
    rng = random.Random(seed)
    steps: list[float] = []
    level = 0.0
    for _ in range(390):
        step = -level * (1 - math.exp(-speed)) + rng.gauss(0, noise)
        steps.append(step)
        level += step
    return day_of_bars(day, steps, seed=seed)
