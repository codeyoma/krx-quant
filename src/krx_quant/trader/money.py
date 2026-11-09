"""A round trip's money (#652): what a trade costs and nets, and how many shares a buy takes. The paper
book, the KIS book (``mock.py``), the store and the nightly replay (``replay.py``) all ask here.

**The net.** A trade bought at ``entry_price`` for ``quantity`` shares, its buy's commission
``buy_fee`` (kept on the trade at its fill), sold for ``proceeds`` (the sale's notional: its price times
its shares, or the sum of its fills): the gross is the proceeds less what the shares cost; the costs
are the buy's commission, the sale's commission (``fee_rate``, ``KRX_FEE_PERCENT``) and the sell tax
(``pricing.SELL_TAX``) on the proceeds; the net is the gross less the costs (``RoundTrip``). An open
position's net at a bid (``open_net``) is the same round trip, its shares not sold yet taken at that bid.

The same expression serves one trade (floats) and a day's quotes at once (numpy arrays of proceeds,
``round_trip`` on ``bids * quantity``): the replay marks a held position on every quote of its window
with it, as the paper book marks it on each quote it takes. Unlike the touch P&L and the barrier P&L
(``stats.touch_profit``, ``barriers.barrier_profit``), which cost the round trip on the reference price
per share in ticks, a trade pays on its fills.

**The size** (#471, spec #468 4 "크기", ``size``): a trade's loss budget over what a share loses at its
stop (``at_risk``), within the cash and the capital (``shares``); the paper book's module doc, "Size".
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, TypeVar

import numpy as np

from krx_quant.pricing import SELL_TAX, tick_size

if TYPE_CHECKING:
    from krx_quant.trader.store import OpenTrade

# Why a buy takes no share (#471, ``size``): its stop not below the ask, or not a price; below one share.
BAD_STOP, NO_SHARES = "bad_stop", "no_shares"

# A figure of one trade, or of each quote of a day's arrays.
N = TypeVar("N", float, np.ndarray)


@dataclass(frozen=True)
class RoundTrip(Generic[N]):
    """A round trip's money, in won: its gross, the buy's and the sale's commissions and the sell tax."""

    gross: N
    buy_fee: float
    sell_fee: N
    tax: N

    @property
    def cost(self) -> N:
        found: N = self.buy_fee + self.sell_fee + self.tax  # numpy's sums are untyped
        return found

    @property
    def net(self) -> N:
        found: N = self.gross - self.cost
        return found


def settled(
    entry_price: float, quantity: int, buy_fee: float, proceeds: N, sell_fee: N, tax: N
) -> RoundTrip[N]:
    """A round trip whose sale brought ``proceeds`` and paid ``sell_fee`` and ``tax`` (as a trade keeps
    them): its gross is the proceeds less what the shares cost."""
    return RoundTrip(proceeds - entry_price * quantity, buy_fee, sell_fee, tax)


def round_trip(
    entry_price: float, quantity: int, buy_fee: float, proceeds: N, fee_rate: float
) -> RoundTrip[N]:
    """A round trip sold for ``proceeds`` (module doc): the sale's commission at ``fee_rate`` and the sell
    tax on the proceeds. ``proceeds`` may be an array: each quote's sale at once."""
    return settled(entry_price, quantity, buy_fee, proceeds, proceeds * fee_rate, proceeds * SELL_TAX)


def commission(price: float, quantity: int, fee_rate: float) -> float:
    """A side's commission on its notional (the buy's, kept on its trade)."""
    return price * quantity * fee_rate


def net_of(entry_price: float, exit_price: float, quantity: int, fee_rate: float) -> float:
    """A round trip bought at ``entry_price`` and sold at ``exit_price``, ``quantity`` shares: its net."""
    return round_trip(
        entry_price, quantity, commission(entry_price, quantity, fee_rate), exit_price * quantity, fee_rate
    ).net


def open_net(trade: OpenTrade, bid: float, fee_rate: float, sold: int = 0, amount: float = 0.0) -> float:
    """What the open ``trade`` would net sold at ``bid`` now (#396): of its shares, ``sold`` sold already
    for ``amount`` (the KIS book's take-profit filled in part, or a sale under way, #405), the rest at
    ``bid``."""
    return round_trip(
        trade.entry_price, trade.quantity, trade.buy_fee, amount + bid * (trade.quantity - sold), fee_rate
    ).net


def at_risk(ask: float, stop: float, fee_rate: float) -> float:
    """What a share bought at ``ask`` loses at its ``stop`` (#471, paper module doc "Size"), in won: the
    ask less the stop, the commissions and the tax at those prices (the buy's at the ask, the sale's at
    the stop), and one adverse tick (the stop's band's: the first bid at or below it sells)."""
    return ask - stop + ask * fee_rate + stop * (fee_rate + SELL_TAX) + tick_size(stop)


def shares(capital: float, ask: float, cash: float, fee_rate: float) -> int:
    """The whole shares a buy at ``ask`` may take, its notional and commission within the capital and
    the ``cash`` (#471)."""
    return math.floor(round(min(capital, max(0.0, cash)) / (ask * (1 + fee_rate)), 9))


def size(
    ask: float,
    stop: float,
    budget: float,
    capital: float,
    cash: float,
    fee_rate: float,
    quantity: int | None = None,
) -> int | str:
    """The shares a buy at ``ask`` takes (#471, paper module doc "Size"): the loss ``budget`` over a
    share's loss at ``stop`` (``at_risk``), or a pick's fixed ``quantity`` (#427), within the capital and
    the ``cash`` (``shares``), rounded down; or why none: ``bad_stop`` (the stop not below the ask, or
    not a price), ``no_shares`` (below one share)."""
    if not (math.isfinite(stop) and 0 < stop < ask):
        return BAD_STOP
    most = shares(capital, ask, cash, fee_rate)
    wanted = quantity if quantity is not None else math.floor(round(budget / at_risk(ask, stop, fee_rate), 9))
    taken = min(wanted, most)
    return taken if taken >= 1 else NO_SHARES
