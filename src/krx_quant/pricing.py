"""Price rules for scoring: the KRX tick table and the flat band.

A move counts as up or down only when it is at least k ticks, where k ticks cover the
round-trip cost of trading it: the sell tax plus the commission on both sides.
"""

from __future__ import annotations

import enum
import math

# Securities transaction tax on the sell side in 2026 (KOSPI 0.05% + rural special tax 0.15%,
# KOSDAQ 0.20%).
SELL_TAX = 0.0020

# KRX tick sizes by price band, unified for KOSPI and KOSDAQ since 2023-01-25:
# (lowest price of the band, tick).
_TICK_BANDS = (
    (500_000, 1_000),
    (200_000, 500),
    (50_000, 100),
    (20_000, 50),
    (5_000, 10),
    (2_000, 5),
    (0, 1),
)


class Direction(enum.Enum):
    UP = "up"
    FLAT = "flat"
    DOWN = "down"


def most_likely(p_up: float, p_flat: float, p_down: float) -> Direction:
    """The class a forecast predicts: the most likely; a tie goes to flat, then up."""
    best = max(p_up, p_flat, p_down)
    if p_flat == best:
        return Direction.FLAT
    return Direction.UP if p_up == best else Direction.DOWN


def tick_size(price: float) -> int:
    """The tick of the band ``price`` falls in; a mid-point between ticks included."""
    return next(tick for low, tick in _TICK_BANDS if price >= low)


def flat_ticks(price: float, fee_rate: float) -> int:
    """The fewest ticks at ``price`` that cover the round-trip cost.

    ``fee_rate`` is the commission per side as a fraction (0.015% is 0.00015).
    """
    cost = price * (SELL_TAX + 2 * fee_rate)
    # Rounded first so a cost of exactly whole ticks is not pushed up by float error.
    return max(1, math.ceil(round(cost / tick_size(price), 9)))


def classify(change: float, k: int, tick: int) -> Direction:
    """Up or down when ``change`` (won) is at least ``k`` ticks, flat otherwise."""
    if change >= k * tick:
        return Direction.UP
    if change <= -k * tick:
        return Direction.DOWN
    return Direction.FLAT
