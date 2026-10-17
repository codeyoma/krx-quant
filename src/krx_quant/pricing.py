"""Price rules for scoring: the KRX tick table and the flat band.

A move counts as up or down only when it is at least k ticks, where k ticks cover the
round-trip cost of trading it: the sell tax plus the commission on both sides.
"""

from __future__ import annotations

import enum
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

# Securities transaction tax on the sell side in 2026 (KOSPI 0.05% + rural special tax 0.15%,
# KOSDAQ 0.20%).
SELL_TAX = 0.0020


@dataclass(frozen=True)
class InverseProduct:
    """A single-stock inverse product a down call can be traded through (#267)."""

    listed: date  # the first day it traded: a down call before it had none
    leverage: int  # −2x: the stock's exposure for half the stock's notional


# The symbols' single-stock inverse products (#267): the −2x ETFs on 005930 (PLUS
# 삼성전자선물단일종목인버스2X) and 000660 (SOL SK하이닉스선물단일종목인버스2X), listed 2026-05-27.
# 009150 has none. An ETF's sale pays no securities transaction tax (the tax falls on shares
# only, 증권거래세법 제2조), so its round trip costs the commissions alone, on its own notional.
INVERSE_PRODUCTS: Mapping[str, InverseProduct] = {
    "005930": InverseProduct(date(2026, 5, 27), 2),
    "000660": InverseProduct(date(2026, 5, 27), 2),
}

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
    # Rounded first so a cost of exactly whole ticks is not pushed up by float error.
    return max(1, math.ceil(round(cost_ticks(price, fee_rate), 9)))


def cost_ticks(price: float, fee_rate: float) -> float:
    """The round-trip cost of trading at ``price`` (the sell tax and the commission on both
    sides), in ticks of its tick size: what ``flat_ticks`` rounds up."""
    return price * (SELL_TAX + 2 * fee_rate) / tick_size(price)


def classify(change: float, k: int, tick: int) -> Direction:
    """Up or down when ``change`` (won) is at least ``k`` ticks, flat otherwise."""
    if change >= k * tick:
        return Direction.UP
    if change <= -k * tick:
        return Direction.DOWN
    return Direction.FLAT
