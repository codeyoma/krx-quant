"""Price rules for scoring: the KRX tick table and the flat band.

A move counts as up or down only when it is at least k ticks, where k ticks cover the
round-trip cost of trading it: the sell tax plus the commission on both sides.

**Instruments** (#711, ``Instrument``): a share's round trip pays the sell tax and moves on the stock
tick table (``STOCK``); an ETF's pays no securities transaction tax (the tax falls on shares only,
증권거래세법 제2조, ``products.py``) and moves on the ETF tick (``etf_tick``, ``ETF``). Which codes are
ETFs is ``products.instrument``'s to say.
"""

from __future__ import annotations

import enum
import math
from collections.abc import Callable
from dataclasses import dataclass

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


def tick_up(price: float, tick: Callable[[float], int] = tick_size) -> int:
    """The first price on KRX's tick at or above ``price`` (a take-profit's limit, ``trader/mock.py``; the
    scalp's levels, #705), on the stock table or another (``tick``: an ETF's, #711)."""
    up = math.ceil(round(price / tick(price), 9)) * tick(price)
    while up % tick(up):  # past a band's edge, onto the coarser tick of the band above
        up = math.ceil(up / tick(up)) * tick(up)
    return int(up)


# The tick an ETF's price moves by (KRX: 1 won below 2,000 won, 5 won from 2,000 won; the stock table,
# ``tick_size``, does not cover ETFs). The inverse ETF's book bore it out on 2026-11-06: no best quote nor
# spread off it in 36,716 records of continuous trading (#606, ``probe-inverse-book``). Moved here from
# ``products.py`` (#711) beside the stock table.
ETF_TICK_FROM = 2_000


def etf_tick(price: float) -> int:
    return 5 if price >= ETF_TICK_FROM else 1


@dataclass(frozen=True)
class Instrument:
    """What a listed code's round trip pays and the tick it moves by (module doc "Instruments"): a
    stock's, or an ETF's (``etf``)."""

    etf: bool = False

    @property
    def sell_tax(self) -> float:
        """The tax on a sale's notional: ``SELL_TAX`` for a share, none for an ETF."""
        return 0.0 if self.etf else SELL_TAX

    def tick(self, price: float) -> int:
        return etf_tick(price) if self.etf else tick_size(price)

    def tick_up(self, price: float) -> int:
        """The first price on this instrument's tick at or above ``price``."""
        return tick_up(price, self.tick)

    def net(self, entry: float, exit_price: float, fee_rate: float) -> float:
        """A unit bought at ``entry`` and sold at ``exit_price``: its net in won after the commission
        ``fee_rate`` on each side's notional and the sell tax on the sale's (``trader/money.net_of``'s
        round trip, with this instrument's tax)."""
        return exit_price * (1 - fee_rate - self.sell_tax) - entry * (1 + fee_rate)

    def netting(self, price: float, percent: float, fee_rate: float) -> int:
        """The first price on the tick at which a unit bought at ``price`` nets ``percent`` of ``price``
        sold there (``trader/book.ScalpRules.netting``, with this instrument's tax and tick)."""
        return self.tick_up(price * (1 + fee_rate + percent / 100) / (1 - fee_rate - self.sell_tax))


STOCK = Instrument()
ETF = Instrument(etf=True)


def tick_band(price: float) -> tuple[int, int | None]:
    """The edges of the band ``price`` falls in (#307): its lowest price, and the next band's
    lowest (None for the top band). A price at an edge is in the band above it."""
    for i, (low, _) in enumerate(_TICK_BANDS):
        if price >= low:
            return low, _TICK_BANDS[i - 1][0] if i > 0 else None
    raise ValueError(f"no tick band for {price}")


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
