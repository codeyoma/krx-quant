"""The touch target (#160, #167): within the four minutes after a bar close, did the best bid
reach the bar close's mid-point plus k ticks (up), else the best ask reach it minus k ticks
(down)? Flat when neither; nothing when the quotes could not be followed.

A window follows one symbol from its bar close t: it takes the integrated best quotes
received from t up to t + 4 minutes (by receipt time, as the engine takes its mid-points),
and the integrated trades for diagnosis. One module for the live engine, the Replay Harness,
the past bar closes filled in from their recorded days (``rescore.py``, #168) and, later,
training, so they all label alike.

- Up when a quote with the best bid at or above the upper target was received, at any time
  in the window; else down when a quote with the best ask at or below the lower target was;
  else flat. Both reached is up: the order is not looked at, nor whether one quote reached
  both (a crossed one can).
- A crossed quote (the best ask below the best bid) counts (#187): its best bid and best ask
  are each a price one could trade at, the integrated book's best quotes coming from two
  venues (the KRX bid above the NXT ask, or the other way round), and trades printed at
  them. The reference price and the quote features still drop it (``reference.py``,
  ``market_view._Book``). A one-sided quote (a side missing or not above 0) is dropped; a
  locked one counts.
- A REST snapshot of the book (the fallback when the WebSocket is refused) is not followed
  tick by tick: a window that received one cannot be scored.
- Sixty seconds or more without a WebSocket quote, from t to the end, cannot be scored either:
  a touch could have come and gone unseen (the reference price's own limit).
- k is the cost-based flat band at the mid-point (``pricing.flat_ticks``); the targets are
  the mid-point plus and minus k ticks of its tick size.

Alongside the class it keeps the path (#160): where each target was first reached, how long
the quote stayed there and the size then, the highest bid and lowest ask, the best quotes at
t and at the end, and the trades' high and low. Other widths can be scored from it later
without reading the window again.

Every prediction is scored on it (#170): the statistics, the statuses, the consensus weights
and the screen's ✓/✗, through the Engine API. Training learns it with
``KRX_TARGET_VERSION=3``, the default (#169, #170, #187): a recorded day labelled by these
windows, replaying its quotes, as the engine labels it; a day with one-minute bars only by
the bar rule (``bar_touch``).

The target versions (``TARGET_VERSION``, the ``touches`` rows' and the models'
``target_version``): 1, the four-minute mid-point move (no touch label; ``KRX_TARGET_VERSION=1``
still learns it); 2, this touch label with crossed quotes dropped (#160 to #187: its rows
stay in the DB, no code makes them any more); 3, this touch label with crossed quotes
counted (#187), the one made now.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from krx_quant.feeds.base import KST
from krx_quant.pricing import Direction, flat_ticks, tick_size
from krx_quant.reference import Bar

TARGET_VERSION = 3  # crossed quotes count (#187); 2 dropped them
WINDOW = timedelta(minutes=4)
SILENCE = timedelta(seconds=60)

MISSING_MID = "missing mid"
REST_QUOTES = "rest quotes"
SILENT = "silent quotes"
RESTARTED = "restarted"
# Filled in later (#168): the day's events were found neither in the data directory nor in
# the Object Store, or held no window for the bar close.
NOT_RECORDED = "not recorded"


@dataclass
class Reached:
    """Where a target was first reached: when, at what quote, for how long and with what size."""

    at: datetime
    price: float  # the best bid (upper) or ask (lower) then
    size: float | None  # its size, from the book feed; None from a trade's quote
    left_at: datetime | None = None  # the first quote back off the target; None while it stays

    def dwell(self, end: datetime) -> float:
        """Seconds the quote stayed at the target, up to ``end`` if it never left."""
        return ((self.left_at or end) - self.at).total_seconds()


@dataclass
class TouchResult:
    """A window's label and path."""

    symbol: str
    bar_close: datetime
    direction: Direction | None  # None: could not be scored
    unscorable: str | None
    anchor: float | None = None
    tick: float | None = None
    k: int | None = None
    upper: float | None = None
    lower: float | None = None
    bid_start: float | None = None
    ask_start: float | None = None
    bid_end: float | None = None
    ask_end: float | None = None
    upper_reached: Reached | None = None
    lower_reached: Reached | None = None
    max_bid: float | None = None
    max_bid_at: datetime | None = None
    min_ask: float | None = None
    min_ask_at: datetime | None = None
    trade_high: float | None = None
    trade_low: float | None = None

    @property
    def end(self) -> datetime:
        return self.bar_close + WINDOW


@dataclass
class TouchWindow:
    """One symbol's window from its bar close: give it the quotes and trades received, in
    order; ``result`` labels it."""

    symbol: str
    bar_close: datetime
    anchor: float | None  # the mid-point at the bar close (the reference price)
    fee_rate: float
    bid_start: float | None = None  # the best quote received last before the bar close
    ask_start: float | None = None
    tick: float | None = field(default=None, init=False)
    k: int | None = field(default=None, init=False)
    upper: float | None = field(default=None, init=False)
    lower: float | None = field(default=None, init=False)
    _bid: float | None = field(default=None, init=False)
    _ask: float | None = field(default=None, init=False)
    _last_quote: datetime | None = field(default=None, init=False)
    _silent: bool = field(default=False, init=False)
    _rest: bool = field(default=False, init=False)
    _upper: Reached | None = field(default=None, init=False)
    _lower: Reached | None = field(default=None, init=False)
    _max_bid: tuple[float, datetime] | None = field(default=None, init=False)
    _min_ask: tuple[float, datetime] | None = field(default=None, init=False)
    _high: float | None = field(default=None, init=False)
    _low: float | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.anchor is not None:
            self.tick = tick_size(self.anchor)
            self.k = flat_ticks(self.anchor, self.fee_rate)
            self.upper = self.anchor + self.k * self.tick
            self.lower = self.anchor - self.k * self.tick

    @property
    def end(self) -> datetime:
        return self.bar_close + WINDOW

    def _within(self, received_at: datetime) -> bool:
        return self.bar_close <= received_at < self.end

    def quote(
        self,
        received_at: datetime,
        bid: float | None,
        ask: float | None,
        *,
        bid_size: float | None = None,
        ask_size: float | None = None,
        rest: bool = False,
    ) -> None:
        """A best quote received: from the book or trade feed, or (``rest``) a REST snapshot."""
        if not self._within(received_at):
            return
        if rest:
            self._rest = True
            return
        if not bid or not ask or bid <= 0 or ask <= 0:
            return  # one-sided: dropped; a crossed one counts (#187), each side a real price
        self._heard(received_at)
        self._bid, self._ask = bid, ask
        if self._max_bid is None or bid > self._max_bid[0]:
            self._max_bid = (bid, received_at)
        if self._min_ask is None or ask < self._min_ask[0]:
            self._min_ask = (ask, received_at)
        if self.upper is not None:
            self._upper = _follow(self._upper, bid >= self.upper, received_at, bid, bid_size)
        if self.lower is not None:
            self._lower = _follow(self._lower, ask <= self.lower, received_at, ask, ask_size)

    def trade(self, received_at: datetime, price: float | None) -> None:
        """A trade received (kept for diagnosis: a trade print against the quote touch)."""
        if not self._within(received_at) or not price:
            return
        self._high = price if self._high is None else max(self._high, price)
        self._low = price if self._low is None else min(self._low, price)

    def _heard(self, received_at: datetime) -> None:
        last = self._last_quote or self.bar_close
        if received_at - last >= SILENCE:
            self._silent = True
        self._last_quote = received_at

    def result(self) -> TouchResult:
        """The window's label, from what it was given."""
        last = self._last_quote or self.bar_close
        silent = self._silent or self.end - last >= SILENCE
        unscorable = (
            MISSING_MID if self.anchor is None else REST_QUOTES if self._rest else SILENT if silent else None
        )
        direction: Direction | None
        if unscorable is not None:
            direction = None
        elif self._upper is not None:
            direction = Direction.UP
        elif self._lower is not None:
            direction = Direction.DOWN
        else:
            direction = Direction.FLAT
        return TouchResult(
            symbol=self.symbol,
            bar_close=self.bar_close,
            direction=direction,
            unscorable=unscorable,
            anchor=self.anchor,
            tick=self.tick,
            k=self.k,
            upper=self.upper,
            lower=self.lower,
            bid_start=self.bid_start,
            ask_start=self.ask_start,
            bid_end=self._bid,
            ask_end=self._ask,
            upper_reached=self._upper,
            lower_reached=self._lower,
            max_bid=self._max_bid[0] if self._max_bid else None,
            max_bid_at=self._max_bid[1] if self._max_bid else None,
            min_ask=self._min_ask[0] if self._min_ask else None,
            min_ask_at=self._min_ask[1] if self._min_ask else None,
            trade_high=self._high,
            trade_low=self._low,
        )


def _follow(
    reached: Reached | None, at_target: bool, received_at: datetime, price: float, size: float | None
) -> Reached | None:
    """A target's first reach, and when the quote first left it again."""
    if reached is None:
        return Reached(received_at, price, size) if at_target else None
    if not at_target and reached.left_at is None:
        reached.left_at = received_at
    return reached


def restarted(symbol: str, bar_close: datetime) -> TouchResult:
    """The label of a window the engine was not following through: it restarted meanwhile."""
    return TouchResult(symbol, bar_close, None, RESTARTED)


def touch_path(r: TouchResult) -> dict[str, float | str | None]:
    """A window's path as stored and served (#167): the targets, where each was first reached
    and for how long (seconds), the highest bid and lowest ask, the best quotes at the bar close
    and at the end, the trades' high and low. Times in ISO 8601."""

    def at(t: datetime | None) -> str | None:
        return t.astimezone(KST).isoformat() if t else None

    def reached(prefix: str, x: Reached | None) -> dict[str, float | str | None]:
        return {
            f"{prefix}_at": at(x.at) if x else None,
            f"{prefix}_price": x.price if x else None,
            f"{prefix}_dwell": round(x.dwell(r.end), 3) if x else None,
            f"{prefix}_size": x.size if x else None,
        }

    return {
        "anchor": r.anchor,
        "tick": r.tick,
        "k": r.k,
        "upper": r.upper,
        "lower": r.lower,
        "bid_start": r.bid_start,
        "ask_start": r.ask_start,
        "bid_end": r.bid_end,
        "ask_end": r.ask_end,
        **reached("upper", r.upper_reached),
        **reached("lower", r.lower_reached),
        "max_bid": r.max_bid,
        "max_bid_at": at(r.max_bid_at),
        "min_ask": r.min_ask,
        "min_ask_at": at(r.min_ask_at),
        "trade_high": r.trade_high,
        "trade_low": r.trade_low,
    }


# The one-minute bar rule (#160, #169), for a day with backfilled bars only: no quotes, so
# the label is read from the trades' highs and lows. A best bid reaching a price shows in
# the bars as a trade a tick above it, as a rule: the targets carry one tick more. On the
# recorded days of 2026-10-12 and 13 it agreed with the quote label on 93.2% of windows
# (target version 2's, crossed quotes dropped). It is the rule of every touch target
# version: counting crossed quotes (#187) changed the quote label, not the bars.
BAR_BUFFER = 1  # ticks


def close_side(bar: Bar, next_open: float | None) -> int:
    """Which side of the book a bar's close traded on: +1 the ask (the bar closed at its
    high), −1 the bid (at its low); a bar that did neither (one price, or a close between)
    by the next trade: an open below the close +1, above it −1, the same 0. 0 too without
    a next trade."""
    if bar.high > bar.low:
        if bar.close == bar.high:
            return 1
        if bar.close == bar.low:
            return -1
    if next_open is None:
        return 0
    return 1 if next_open < bar.close else -1 if next_open > bar.close else 0


def bar_touch(closing: Bar, after: Sequence[Bar], fee_rate: float) -> Direction | None:
    """The touch label of the four minutes after a bar close, from one-minute bars: the bar
    that ended at the bar close (``closing``) and the bars of the four minutes after it
    (``after``, those with trades). None without any trade in those minutes.

    The close is the anchor, and k is the flat band at it. Up when the highest high reaches
    the close plus k ticks, one tick of buffer, and one more if the close traded on the bid
    (``close_side`` −1: the mid-point was above it); else down when the lowest low reaches
    the close minus as many, one more if it traded on the ask; else flat. The next bar's
    open only places the close: it is never a feature.
    """
    if not after:
        return None
    price = closing.close
    tick = tick_size(price)
    k = flat_ticks(price, fee_rate)
    side = close_side(closing, after[0].open)
    upper = price + (k + BAR_BUFFER + (side == -1)) * tick
    lower = price - (k + BAR_BUFFER + (side == 1)) * tick
    if max(b.high for b in after) >= upper:
        return Direction.UP
    if min(b.low for b in after) <= lower:
        return Direction.DOWN
    return Direction.FLAT
