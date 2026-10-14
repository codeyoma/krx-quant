"""Market View: what the engine knows of the market, built from its events.

The predicted symbols' one-minute bars and reference prices (``reference``), the bars of
the series that may lead them (``features.Leaders``: futures, indices, US daytime trades,
ETFs), each by exchange time, as they stood at each bar close; the symbols' microprices at
each minute's end, order flow (the integrated book's best quotes and ten levels, the
trades' aggressor sides, the per-venue trades), trade arrivals and flows (program trading,
foreign member firms, investor estimates), as received before it. The engine predicts from
it, live and in replay; training on a recorded day feeds the same events through it, so it
learns from exactly what the engine saw.

A bar close's bars are kept when the first event received at or after it arrives, before
that event changes them: a prediction made late never sees a trade it could not have.

With a fee rate the view follows each bar close's four minutes (#167): the touch window
(``touch.TouchWindow``), and, for the kinds besides the direction (#179, ``kinds.py``), the
same quotes logged (``kinds.QuoteLog``) for each kind's answer function, by the same rules.
"""

from __future__ import annotations

import bisect
import math
import statistics
from array import array
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from typing import Any, NamedTuple

import numpy as np

from krx_quant.contracts import KOSPI200, USD, days_to_expiry
from krx_quant.features import (
    EXCITATION_SECONDS,
    FLOW_MINUTES,
    MINUTE_MOVES,
    OFI_MINUTES,
    SIDES,
    TRADE_MINUTES,
    Arrivals,
    Flows,
    Leaders,
    OrderFlow,
    Snapshot,
)
from krx_quant.feeds import (
    BOOK,
    BOOK_SNAPSHOT,
    FOREIGN_ESTIMATE,
    INDEX,
    INDEX_REST,
    KOSPI200_FUTURES,
    KRX_TRADE,
    MEMBER,
    MEMBER_REST,
    NXT_TRADE,
    OPTION,
    PROGRAM,
    TRADE,
    US_TRADE,
    USD_FUTURES,
)
from krx_quant.feeds.base import KST, LEVELS, kst_day
from krx_quant.kinds import Answer, Followed, Kind, QuoteLog
from krx_quant.market_calendar import MarketCalendar, Session
from krx_quant.reference import BAR, MAX_QUOTE_AGE, Bar, BarBuilder, ReferencePrice, Source
from krx_quant.touch import RESTARTED, WINDOW, TouchResult, TouchWindow, restarted

HORIZON = timedelta(minutes=4)
# Touch windows are kept this long after they end, for a score running late.
_KEEP_WINDOWS = timedelta(minutes=10)
# How far ahead to look for the next session (long holidays included).
LOOKAHEAD_DAYS = 15
INDEX_FEEDS = (INDEX.tr_id, INDEX_REST.tr_id)
FUTURES_SERIES = {KOSPI200_FUTURES.tr_id: KOSPI200.series, USD_FUTURES.tr_id: USD.series}
# The feeds with the symbols' trades and best quotes.
_QUOTING = (TRADE.tr_id, KRX_TRADE.tr_id, BOOK.tr_id, BOOK_SNAPSHOT.tr_id)
# The venues of the per-venue trade feeds.
VENUES = ("KRX", "NXT")
# The feeds with the symbols' flows (Turing's and Shannon's).
FLOW_FEEDS = (PROGRAM.tr_id, MEMBER.tr_id, MEMBER_REST.tr_id, FOREIGN_ESTIMATE.tr_id)


def prediction_window(session: Session) -> tuple[datetime, datetime]:
    """The first and last bar close predicted in a session."""
    return session.open + BAR, session.closing_auction - HORIZON


def next_bar_close(calendar: MarketCalendar, after: datetime) -> datetime | None:
    """The first bar close in a prediction window strictly after ``after``."""
    local = after.astimezone(KST)
    day = local.date()
    for _ in range(LOOKAHEAD_DAYS):
        session = calendar.session(day)
        if session is not None:
            first, last = prediction_window(session)
            candidate = max(first, local.replace(second=0, microsecond=0) + BAR)
            if candidate <= last:
                return candidate
        day += timedelta(days=1)
        local = datetime.combine(day, time(), KST)
    return None


class _Flow:
    """A cumulative value of the day, in the order received; a new day starts empty."""

    def __init__(self) -> None:
        self._day: date | None = None
        self._at: list[datetime] = []
        self._values: list[float] = []

    def add(self, at: datetime, value: float | None) -> None:
        if value is None:
            return
        day = kst_day(at)
        if day != self._day:
            self._day, self._at, self._values = day, [], []
        i = bisect.bisect_right(self._at, at)  # in receipt order, even if one came late
        self._at.insert(i, at)
        self._values.insert(i, float(value))

    def before(self, at: datetime) -> float | None:
        """The last value received strictly before ``at``, on ``at``'s day."""
        if kst_day(at) != self._day:
            return None
        i = bisect.bisect_left(self._at, at)
        return self._values[i - 1] if i else None

    def base(self, at: datetime, opened: datetime | None) -> float | None:
        """What a change up to now is measured from: the value before ``at``; 0 if nothing
        was received by then and the day's first value came early (it counted from 0); else
        unknown (the engine came back mid-day, holding a whole morning in one value)."""
        before = self.before(at)
        if before is not None or kst_day(at) != self._day or not self._at:
            return before
        early = opened is not None and self._at[0] <= opened + timedelta(minutes=FLOW_MINUTES)
        return 0.0 if early else None


class _Estimates:
    """A symbol's investor estimates of the day: each poll gives every time slot so far.
    The slots are KIS's codes (``bsop_hour_gb``), later ones larger ⚠ (not checked live)."""

    def __init__(self) -> None:
        self._day: date | None = None
        self._rows: list[tuple[datetime, str, float | None, float | None]] = []

    def add(self, row: dict[str, Any]) -> None:
        day = kst_day(row["received_at"])
        if day != self._day:
            self._day, self._rows = day, []
        foreign, institution = row["foreign_net_qty"], row["institution_net_qty"]
        self._rows.append((row["received_at"], row["slot"], foreign, institution))

    def before(self, at: datetime) -> tuple[float | None, float | None]:
        """The foreign and institutional estimates of the latest slot that has each,
        received before ``at`` on its day."""
        known = sorted((r for r in self._rows if r[0] < at), key=lambda r: (r[1], r[0]))
        if kst_day(at) != self._day or not known:
            return None, None
        foreign = next((r[2] for r in reversed(known) if r[2] is not None), None)
        institution = next((r[3] for r in reversed(known) if r[3] is not None), None)
        return foreign, institution


# An option trade older than this says nothing about where the money is at a bar close.
OPTION_STALE = timedelta(minutes=10)
# An option's implied volatility is the median of its trades in this long before the close.
OPTION_SMOOTHING = timedelta(minutes=1)


class _OptionTrade(NamedTuple):
    received_at: datetime
    iv: float  # implied volatility (%)
    delta: float


class _Options:
    """The KOSPI200 options' trades by receipt time, each one's implied volatility and
    delta, for the latest ``OPTION_STALE`` or so (Markov's at-the-money implied volatility).
    An option that stopped trading (the money moved, a new day) is dropped."""

    _KEEP = 2 * OPTION_STALE

    def __init__(self) -> None:
        self._trades: dict[str, list[_OptionTrade]] = {}  # by option code
        self._last: tuple[datetime, float | None] | None = None  # the last answer, until a trade

    def add(self, row: dict[str, Any]) -> None:
        """An option trade; one without an implied volatility or a delta says nothing."""
        iv, delta = row.get("implied_vol"), row.get("delta")
        if not iv or iv <= 0 or not delta:
            return
        self._last = None
        trade = _OptionTrade(row["received_at"], float(iv), float(delta))
        trades = self._trades.setdefault(row["symbol"], [])
        trades.insert(bisect.bisect_right(trades, trade.received_at, key=_received), trade)
        oldest = trade.received_at - self._KEEP
        for code, kept in list(self._trades.items()):
            del kept[: bisect.bisect_left(kept, oldest, key=_received)]
            if not kept:
                del self._trades[code]

    def atm_iv(self, at: datetime) -> float | None:
        """The mean implied volatility of the call whose delta is nearest 0.5 and the put
        whose delta is nearest −0.5, each option chosen by its latest trade received in the
        ``OPTION_STALE`` before ``at``; one side's alone when the other has none. An option's
        implied volatility is the median of its trades in the ``OPTION_SMOOTHING`` before
        ``at`` (one trade's is noisy), else its latest trade's. Asked again for the same
        ``at`` (every symbol at a bar close), the answer is kept."""
        if self._last is not None and self._last[0] == at:
            return self._last[1]
        self._last = (at, self._atm_iv(at))
        return self._last[1]

    def _atm_iv(self, at: datetime) -> float | None:
        latest = []
        for trades in self._trades.values():
            i = bisect.bisect_left(trades, at, key=_received)
            if i and trades[i - 1].received_at >= at - OPTION_STALE:
                recent = trades[bisect.bisect_left(trades, at - OPTION_SMOOTHING, key=_received) : i]
                iv = statistics.median(t.iv for t in recent) if recent else trades[i - 1].iv
                latest.append(trades[i - 1]._replace(iv=iv))
        calls = [t for t in latest if t.delta > 0]
        puts = [t for t in latest if t.delta < 0]
        sides = [_nearest_iv(side, target) for side, target in ((calls, 0.5), (puts, -0.5)) if side]
        return sum(sides) / len(sides) if sides else None


def _nearest_iv(trades: Sequence[_OptionTrade], delta: float) -> float:
    """The implied volatility of the option whose delta is nearest ``delta``; the mean of
    those equally near, so no IV wins a tie."""
    nearest = min(abs(t.delta - delta) for t in trades)
    ivs = [t.iv for t in trades if math.isclose(abs(t.delta - delta), nearest)]
    return sum(ivs) / len(ivs)


def _received(trade: _OptionTrade) -> datetime:
    return trade.received_at


class _Running:
    """Running totals of what was received, for the latest ``keep`` or so: a window's sum is
    the total at its end less the total before its start."""

    def __init__(self, width: int, keep: timedelta) -> None:
        self._keep = keep
        self._at: list[datetime] = []
        self._totals: list[tuple[float, ...]] = []
        self._zero = (0.0,) * width
        self._dropped = self._zero  # the total before the first one kept

    def add(self, at: datetime, values: tuple[float, ...]) -> None:
        if self._at and at < self._at[-1]:
            at = self._at[-1]  # the engine only ever moves forward
        last = self._totals[-1] if self._totals else self._dropped
        self._at.append(at)
        self._totals.append(tuple(a + b for a, b in zip(last, values, strict=True)))
        if at - self._at[0] > 2 * self._keep:
            cut = bisect.bisect_left(self._at, at - self._keep)
            if cut:
                self._dropped = self._totals[cut - 1]
                del self._at[:cut], self._totals[:cut]

    def between(self, start: datetime, end: datetime) -> tuple[float, ...]:
        """The sums of what was received in [start, end), ``keep`` back at most."""
        lo, hi = bisect.bisect_left(self._at, start), bisect.bisect_left(self._at, end)
        if hi <= lo:
            return self._zero
        before = self._totals[lo - 1] if lo > 0 else self._dropped
        return tuple(a - b for a, b in zip(self._totals[hi - 1], before, strict=True))


class _Quote(NamedTuple):
    """The best quotes and their sizes."""

    bid: int
    bid_size: int
    ask: int
    ask_size: int


def _order_flow_imbalance(before: _Quote, now: _Quote) -> float:
    """Cont, Kukanov and Stoikov's order flow imbalance of one update: the size a bid at the
    same or a higher price adds, less what left at the same or a lower one, and the mirror
    on the ask."""
    bids = (now.bid_size if now.bid >= before.bid else 0) - (before.bid_size if now.bid <= before.bid else 0)
    asks = (now.ask_size if now.ask <= before.ask else 0) - (before.ask_size if now.ask >= before.ask else 0)
    return float(bids - asks)


class _Book:
    """A symbol's book updates by receipt time, for the latest hour or so: each one's
    microprice, best quotes and ten levels' sizes, and running totals of the order flow
    imbalance at the best quotes, of their mean size and of the updates."""

    _KEEP = timedelta(minutes=MINUTE_MOVES + 10)

    def __init__(self) -> None:
        self._at: list[datetime] = []
        self._micro: list[float] = []
        self._depth: list[tuple[float, float]] = []  # bid, ask: the ten levels' sizes
        self._top: list[_Quote] = []  # the best quotes and their sizes
        self._flow = _Running(3, self._KEEP)  # order flow imbalance, best quotes' mean size, updates
        self._last: _Quote | None = None

    def add(self, at: datetime, row: dict[str, Any]) -> None:
        """A book update; an empty side or a crossed book is not a usable one."""
        quote = _Quote(row["bid_px_1"], row.get("bid_qty_1") or 0, row["ask_px_1"], row.get("ask_qty_1") or 0)
        if quote.bid <= 0 or quote.ask <= 0 or quote.ask < quote.bid or (self._at and at < self._at[-1]):
            return
        # The microprice leans toward the side with less waiting: the other is likelier to go.
        # Without both sizes (a file recorded without them) it is the mid-point.
        if quote.bid_size > 0 and quote.ask_size > 0:
            micro = (quote.bid * quote.ask_size + quote.ask * quote.bid_size) / (
                quote.bid_size + quote.ask_size
            )
        else:
            micro = (quote.bid + quote.ask) / 2
        # The first update after the book went missing (the day's first too) is no move from
        # the one before the gap.
        flow = 0.0
        if self._last is not None and self._at and at - self._at[-1] <= MAX_QUOTE_AGE:
            flow = _order_flow_imbalance(self._last, quote)
        self._last = quote
        self._at.append(at)
        self._micro.append(micro)
        self._depth.append(
            (
                float(sum(row.get(f"bid_qty_{i}") or 0 for i in LEVELS)),
                float(sum(row.get(f"ask_qty_{i}") or 0 for i in LEVELS)),
            )
        )
        self._top.append(quote)
        self._flow.add(at, (flow, (quote.bid_size + quote.ask_size) / 2, 1.0))
        if at - self._at[0] > 2 * self._KEEP:
            cut = bisect.bisect_left(self._at, at - self._KEEP)
            del self._at[:cut], self._micro[:cut], self._depth[:cut], self._top[:cut]

    def _latest(self, at: datetime) -> int | None:
        """The update received just before ``at``, unless the book went missing (none in the
        minute before)."""
        i = bisect.bisect_left(self._at, at) - 1
        return None if i < 0 or self._at[i] < at - MAX_QUOTE_AGE else i

    def at_minute_ends(self, ends: Sequence[datetime]) -> list[float]:
        """The microprice received just before each of ``ends`` (oldest first), back to the
        last end without one received in the minute before it."""
        out: list[float] = []
        for end in reversed(ends):
            i = self._latest(end)
            if i is None:
                break
            out.append(self._micro[i])
        return out[::-1]

    def depth(self, at: datetime) -> tuple[float, float] | None:
        """The ten levels' sizes (bid, ask) just before ``at``; None if the book is missing."""
        i = self._latest(at)
        return None if i is None else self._depth[i]

    def top(self, at: datetime, since: datetime) -> _Quote | None:
        """The best quotes received just before ``at``, from ``since`` on; None if the book is
        missing or they came before ``since``."""
        i = self._latest(at)
        return None if i is None or self._at[i] < since else self._top[i]

    def flow(self, start: datetime, end: datetime) -> tuple[float, float | None]:
        """The order flow imbalance of the updates received in [start, end), and the best
        quotes' mean size over them (None without any)."""
        imbalance, size, updates = self._flow.between(start, end)
        return imbalance, size / updates if updates else None


class _Arrivals:
    """A symbol's buy- and sell-initiated trades of the day by receipt time, compact (17 bytes
    a trade): a liquid symbol has many thousands a day."""

    # Older trades are left out of the decayed sizes: they weigh under e^(−10) of their size
    # at the longest decay.
    _REACH = 10 * max(EXCITATION_SECONDS)
    _RECENT = 60  # seconds: the last minute's arrivals

    def __init__(self) -> None:
        self._day: date | None = None
        self._reset()

    def _reset(self) -> None:
        self._at = array("d")  # seconds since the epoch
        self._buy = array("b")  # 1 bought (buy-initiated), 0 sold
        self._total = array("d")  # running total of the sizes

    def add(self, at: datetime, side: str, size: float) -> None:
        day = kst_day(at)
        if day != self._day:
            self._day = day
            self._reset()
        t = at.timestamp()
        if self._at and t < self._at[-1]:
            t = self._at[-1]  # the engine only ever moves forward
        self._at.append(t)
        self._buy.append(1 if side == "buy" else 0)
        self._total.append((self._total[-1] if self._total else 0.0) + size)

    def before(self, opened: datetime, close: datetime) -> Arrivals:
        """The trades received from ``opened`` to just before ``close``, as ``Arrivals``."""
        if self._day != kst_day(close):
            return Arrivals()
        end = close.timestamp()
        lo, hi = bisect.bisect_left(self._at, opened.timestamp()), bisect.bisect_left(self._at, end)
        if hi <= lo:
            return Arrivals()
        mean = (self._total[hi - 1] - (self._total[lo - 1] if lo else 0.0)) / (hi - lo)
        if mean <= 0:
            return Arrivals()
        first = max(lo, bisect.bisect_left(self._at, end - self._REACH))
        age = end - np.frombuffer(self._at[first:hi])
        totals = np.frombuffer(self._total[max(first - 1, 0) : hi])
        size = np.diff(totals, prepend=0.0) if first == 0 else np.diff(totals)
        buy = np.frombuffer(self._buy[first:hi], dtype=np.int8).astype(bool)
        recent = age <= self._RECENT
        decayed: dict[tuple[str, int], float] = {}
        last: dict[str, float] = {}
        for side, mask in zip(SIDES, (buy, ~buy), strict=True):
            for seconds in EXCITATION_SECONDS:
                decayed[(side, seconds)] = float((size[mask] * np.exp(-age[mask] / seconds)).sum())
            last[side] = float(size[mask & recent].sum())
        return Arrivals(mean, decayed, last)


class _Sides(NamedTuple):
    """Trades by aggressor: how many, and their volume."""

    buys: float
    sells: float
    bought: float
    sold: float


@dataclass(frozen=True)
class BarsAtClose:
    """The bars as they stood at a bar close."""

    close: datetime
    symbols: dict[str, list[Bar]]
    leaders: dict[str, list[Bar]]


class MarketView:
    def __init__(
        self,
        calendar: MarketCalendar,
        symbols: Sequence[str],
        leaders: Leaders,
        start: datetime,
        fee_rate: float | None = None,
        kinds: Sequence[Kind] = (),
    ) -> None:
        """A view with nothing received yet, whose first bar close is the first after ``start``.
        With ``fee_rate`` it also follows each bar close's touch window (#167), and, given
        ``kinds`` (besides the direction, #179), the quotes their answers are made from."""
        self.calendar = calendar
        self.symbols = tuple(symbols)
        self.leaders = leaders
        self._reference = ReferencePrice(calendar)
        self._bars = BarBuilder()
        self._leader_bars = BarBuilder()
        # Bars as they stood at bar closes not taken yet, kept once an event received at or
        # after the close arrives.
        self._frozen: dict[datetime, BarsAtClose] = {}
        self._frozen_through = start
        self._program = {s: _Flow() for s in self.symbols}
        self._foreign_members = {s: _Flow() for s in self.symbols}
        self._estimates = {s: _Estimates() for s in self.symbols}
        self._books = {s: _Book() for s in self.symbols}
        trades = timedelta(minutes=TRADE_MINUTES + 5)
        self._sides = {s: _Running(len(_Sides._fields), trades) for s in self.symbols}
        self._venues = {s: _Running(len(VENUES), trades) for s in self.symbols}  # volume by venue
        self._traded: dict[tuple[str, str], date] = {}  # (symbol, venue): the last day it traded
        self._arrivals = {s: _Arrivals() for s in self.symbols}
        self._expiry: dict[date, int] = {}
        self._options = _Options()
        self._fee_rate = fee_rate
        # The touch windows followed (#167), by symbol and bar close, and each symbol's last
        # integrated best quote (bid, ask), the one before a window opens.
        self._windows: dict[str, dict[datetime, TouchWindow]] = {s: {} for s in self.symbols}
        self._best: dict[str, tuple[float, float]] = {}
        self._ended_through: datetime | None = None  # the last bar close whose windows were handed out
        # The kinds' quotes (#179): each symbol's, kept as long as its touch windows.
        self._logs: dict[str, QuoteLog] = (
            {s: QuoteLog() for s in self.symbols} if kinds and fee_rate is not None else {}
        )

    def on_event(self, feed: str, row: dict[str, Any]) -> None:
        """Take an event into the bars, reference prices, microprices, order flow, flows or
        options; other events are not the view's.
        """
        if feed == OPTION.tr_id:
            self._options.add(row)
            return
        if feed in FLOW_FEEDS:
            self._flow(feed, row)
            return
        series = self._leading(feed, row)
        if series is not None:
            self._freeze_before(row["received_at"])
            price = row["value"] if feed in INDEX_FEEDS else row["price"]
            if price:  # None from a recorded file without the column
                self._leader_bars.trade(series, row["event_time"], price, row.get("qty") or 0)
        if feed == NXT_TRADE.tr_id:
            if row["symbol"] in self.symbols:
                self._venue_trade(row["symbol"], "NXT", row)
            return
        if feed not in _QUOTING or row["symbol"] not in self.symbols:
            return
        symbol, received_at = row["symbol"], row["received_at"]
        self._freeze_before(received_at)
        self._follow(feed, symbol, received_at, row)
        if feed == TRADE.tr_id:
            self._bars.trade(symbol, row["event_time"], row["price"], row["qty"])
            self._reference.quote(symbol, Source.INTEGRATED, received_at, row["bid1"], row["ask1"])
            qty = float(row["qty"] or 0)
            if row.get("side") == "buy":
                self._sides[symbol].add(received_at, _Sides(1.0, 0.0, qty, 0.0))
                self._arrivals[symbol].add(received_at, "buy", qty)
            elif row.get("side") == "sell":
                self._sides[symbol].add(received_at, _Sides(0.0, 1.0, 0.0, qty))
                self._arrivals[symbol].add(received_at, "sell", qty)
        elif feed == KRX_TRADE.tr_id:
            self._reference.quote(symbol, Source.KRX, received_at, row["bid1"], row["ask1"])
            self._venue_trade(symbol, "KRX", row)
        else:
            bid, ask = row["bid_px_1"], row["ask_px_1"]
            self._reference.quote(symbol, Source.INTEGRATED, received_at, bid, ask)
            self._books[symbol].add(received_at, row)

    def at_close(self, close: datetime) -> BarsAtClose:
        """The bars as they stood at ``close``: kept when an event at or after it arrived,
        else (nothing received since) as they are. Bars kept for earlier closes go, and none
        are kept for ``close`` from now on.
        """
        self._open_windows(close)  # nothing received since the close: opened now
        frozen = self._frozen.pop(close, None)
        for stale in [c for c in self._frozen if c < close]:
            del self._frozen[stale]  # a close no longer predicted (the calendar changed)
        self._frozen_through = max(self._frozen_through, close)
        if frozen is not None:
            return frozen
        return BarsAtClose(
            close,
            {s: self._bars.closed(s, close) for s in self.symbols},
            {k: self._leader_bars.closed(k, close) for k in self.leaders.series},
        )

    def snapshot(
        self, frozen: BarsAtClose, symbol: str, fee_rate: float, regime: str | None = None
    ) -> Snapshot:
        """``symbol`` at a bar close: its bars, reference price, leaders and flows there, in
        the day's macro ``regime``."""
        day = frozen.close.astimezone(KST).date()
        session = self.calendar.session(day)
        if day not in self._expiry:
            self._expiry[day] = days_to_expiry(day, self.calendar)
        return Snapshot.at(
            symbol,
            frozen.close,
            frozen.symbols.get(symbol, []),
            self.mid(symbol, frozen.close),
            fee_rate,
            session_open=session.open if session is not None else None,
            leaders=self.leaders.groups(symbol, frozen.symbols, frozen.leaders),
            regime=regime,
            flows=self.flows(symbol, frozen.close),
            expiry_days=self._expiry[day],
            microprices=self.microprices(symbol, frozen.close),
            order_flow=self.order_flow(symbol, frozen.close),
            arrivals=self.arrivals(symbol, frozen.close),
            atm_iv=self._options.atm_iv(frozen.close),
        )

    def arrivals(self, symbol: str, at: datetime) -> Arrivals:
        """``symbol``'s buy- and sell-initiated trades received before ``at``, from the open."""
        session = self.calendar.session(kst_day(at))
        if symbol not in self._arrivals or session is None:
            return Arrivals()
        return self._arrivals[symbol].before(session.open, at)

    def order_flow(self, symbol: str, at: datetime) -> OrderFlow:
        """``symbol``'s book and trades received in the minutes before ``at``, from the open.
        A venue that has not traded today is unknown (its feed is not there)."""
        day = kst_day(at)
        session = self.calendar.session(day)
        if symbol not in self._books or session is None:
            return OrderFlow()

        def since(minutes: int) -> datetime:
            return max(session.open, at - minutes * BAR)

        book = self._books[symbol]
        depth = book.depth(at)
        top = book.top(at, session.open)
        # A minute before: from the open too, as the windows (none in the first minute).
        top_before = book.top(at - BAR, session.open) if at - BAR > session.open else None
        imbalances = {m: book.flow(since(m), at) for m in OFI_MINUTES}
        sides = _Sides(*self._sides[symbol].between(since(TRADE_MINUTES), at))
        volumes: dict[str, float | None] = {
            venue: volume if self._traded.get((symbol, venue)) == day else None
            for venue, volume in zip(
                VENUES, self._venues[symbol].between(since(TRADE_MINUTES), at), strict=True
            )
        }
        return OrderFlow(
            ofi={m: imbalance for m, (imbalance, _) in imbalances.items()},
            quote_size={m: size for m, (_, size) in imbalances.items() if size is not None},
            bid_depth=None if depth is None else depth[0],
            ask_depth=None if depth is None else depth[1],
            best_bid=None if top is None else float(top.bid),
            best_ask=None if top is None else float(top.ask),
            best_bid_size=None if top is None else float(top.bid_size),
            best_ask_size=None if top is None else float(top.ask_size),
            best_size_before=None if top_before is None else float(top_before.bid_size + top_before.ask_size),
            buys=int(sides.buys),
            sells=int(sides.sells),
            buy_volume=sides.bought,
            sell_volume=sides.sold,
            krx_volume=volumes["KRX"],
            nxt_volume=volumes["NXT"],
        )

    def _venue_trade(self, symbol: str, venue: str, row: dict[str, Any]) -> None:
        received_at = row["received_at"]
        self._traded[(symbol, venue)] = kst_day(received_at)
        qty = float(row.get("qty") or 0)
        self._venues[symbol].add(received_at, tuple(qty if v == venue else 0.0 for v in VENUES))

    def microprices(self, symbol: str, at: datetime) -> list[float]:
        """``symbol``'s microprice at each minute's end from the open up to ``at``, the last
        hour of them, since the book last went missing (``Snapshot.microprices``)."""
        session = self.calendar.session(kst_day(at))
        if symbol not in self._books or session is None:
            return []
        first = max(session.open + BAR, at - MINUTE_MOVES * BAR)
        ends = [first + i * BAR for i in range((at - first) // BAR + 1)]
        return self._books[symbol].at_minute_ends(ends)

    def flows(self, symbol: str, at: datetime) -> Flows:
        """``symbol``'s flows as received before ``at``, and ``FLOW_MINUTES`` before that."""
        if symbol not in self._program:
            return Flows()
        earlier = at - timedelta(minutes=FLOW_MINUTES)
        session = self.calendar.session(kst_day(at))
        opened = session.open if session is not None else None
        program, members = self._program[symbol], self._foreign_members[symbol]
        foreign, institution = self._estimates[symbol].before(at)
        return Flows(
            program_now=program.before(at),
            program_before=program.base(earlier, opened),
            foreign_member_now=members.before(at),
            foreign_member_before=members.base(earlier, opened),
            foreign_estimate=foreign,
            institution_estimate=institution,
        )

    def _flow(self, feed: str, row: dict[str, Any]) -> None:
        symbol = row.get("symbol")
        if symbol not in self._program:
            return
        if feed == PROGRAM.tr_id:
            self._program[symbol].add(row["received_at"], row.get("net_value"))
        elif feed == FOREIGN_ESTIMATE.tr_id:
            self._estimates[symbol].add(row)
        else:
            self._foreign_members[symbol].add(row["received_at"], row.get("foreign_net_qty"))

    def mid(self, symbol: str, at: datetime) -> float | None:
        """The reference price just before ``at``, or None if it is unknown."""
        return self._reference.mid(symbol, at)

    def touch(self, symbol: str, bar_close: datetime) -> TouchResult:
        """The touch label of ``symbol``'s window from ``bar_close`` (#167), as followed so far;
        a window not followed (the engine started after its bar close) says so."""
        window = self._windows.get(symbol, {}).get(bar_close)
        return window.result() if window is not None else restarted(symbol, bar_close)

    def followed(self, symbol: str, bar_close: datetime) -> Followed | None:
        """``symbol``'s four minutes from ``bar_close`` as followed so far, for a kind's answer
        (#179): the touch window's start (mid-point, tick, k, best quote) and the quotes and
        trades logged since; None for a window not followed (the engine started after its
        bar close) or without kinds."""
        window = self._windows.get(symbol, {}).get(bar_close)
        log = self._logs.get(symbol)
        if window is None or log is None:
            return None
        return log.window(
            symbol, bar_close, window.anchor, window.tick, window.k, window.bid_start, window.ask_start
        )

    def kind_answer(self, kind: Kind, symbol: str, bar_close: datetime) -> Answer:
        """``kind``'s answer of ``symbol``'s window from ``bar_close`` (#179), as followed so far;
        a window not followed says so (``restarted``), as its touch label does."""
        window = self.followed(symbol, bar_close)
        return Answer(None, RESTARTED) if window is None else kind.answer_to(window)

    def quote_at(self, symbol: str, bar_close: datetime) -> tuple[float, float] | None:
        """The best quote (bid, ask) ``symbol``'s touch window from ``bar_close`` starts from
        (#169): the last integrated one received before it. None without one, or without
        touch windows."""
        window = self._windows.get(symbol, {}).get(bar_close)
        if window is None or window.bid_start is None or window.ask_start is None:
            return None
        return window.bid_start, window.ask_start

    def ended_windows(self, due: datetime) -> list[tuple[str, datetime]]:
        """The touch windows over by ``due`` (#167), as (symbol, bar close), each handed out
        once: every symbol's, predicted or not (a VI, a missing book)."""
        closes = sorted(
            {
                c
                for windows in self._windows.values()
                for c in windows
                if c + WINDOW <= due and (self._ended_through is None or c > self._ended_through)
            }
        )
        if not closes:
            return []
        self._ended_through = closes[-1]
        return [(s, c) for c in closes for s in self.symbols if c in self._windows[s]]

    def _open_windows(self, close: datetime) -> None:
        """Start following each symbol's touch window from the bar close ``close``, once."""
        if self._fee_rate is None:
            return
        for symbol in self.symbols:
            windows = self._windows[symbol]
            if close in windows:
                continue
            best = self._best.get(symbol)
            windows[close] = TouchWindow(
                symbol,
                close,
                self._reference.mid(symbol, close),
                self._fee_rate,
                bid_start=best[0] if best else None,
                ask_start=best[1] if best else None,
            )
            # Windows long over (kept a while for a late score): dropped.
            for old in [c for c in windows if c + WINDOW + _KEEP_WINDOWS < close]:
                del windows[old]
            if symbol in self._logs:  # and the quotes only they read
                self._logs[symbol].forget(min(windows))

    def _follow(self, feed: str, symbol: str, received_at: datetime, row: dict[str, Any]) -> None:
        """Give the symbol's open touch windows a quote or trade (#167): the integrated feeds'
        best quotes, a REST book snapshot (marked), the integrated trades' prices."""
        if feed == KRX_TRADE.tr_id or self._fee_rate is None:
            return  # the KRX-only quotes before NXT opens: not the integrated book
        rest = feed == BOOK_SNAPSHOT.tr_id
        if feed == TRADE.tr_id:
            bid, ask, bid_size, ask_size = row["bid1"], row["ask1"], None, None
        else:
            bid, ask = row["bid_px_1"], row["ask_px_1"]
            bid_size, ask_size = row.get("bid_qty_1"), row.get("ask_qty_1")
        if not rest and bid and ask and 0 < bid <= ask:
            self._best[symbol] = (float(bid), float(ask))
        for window in self._windows[symbol].values():
            window.quote(received_at, bid, ask, bid_size=bid_size, ask_size=ask_size, rest=rest)
            if feed == TRADE.tr_id:
                window.trade(received_at, row["price"])
        log = self._logs.get(symbol)
        # The kinds' windows read the same (#179): kept while a window open now reads it (every
        # window an event falls in is open by the time it is followed, ``_freeze_before``).
        if log is not None and any(c <= received_at < c + WINDOW for c in self._windows[symbol]):
            log.quote(received_at, bid, ask, bid_size, ask_size, rest)
            if feed == TRADE.tr_id:
                log.trade(received_at, row["price"])

    def latest_lead(self, series: str, now: datetime) -> tuple[float, datetime] | None:
        """A leading series' latest trade of ``now``'s day (``features.Leaders``): price and time."""
        return self._leader_bars.latest(series, now)

    def last_bar(self, symbol: str, now: datetime) -> Bar | None:
        """``symbol``'s latest bar of ``now``'s day, the one being made if its minute goes on."""
        return self._bars.last(symbol, now)

    def bars(self, symbol: str, now: datetime) -> list[Bar]:
        """``symbol``'s bars of ``now``'s day whose minute has ended."""
        return self._bars.closed(symbol, now)

    def _leading(self, feed: str, row: dict[str, Any]) -> str | None:
        """The leading series an event trades in, if it is one."""
        if feed in FUTURES_SERIES:
            return FUTURES_SERIES[feed]
        symbol = row["symbol"]
        if feed in INDEX_FEEDS:
            return symbol if symbol in self.leaders.indices else None
        if feed == US_TRADE.tr_id:
            return symbol if symbol in self.leaders.us else None
        if feed == KRX_TRADE.tr_id:
            return symbol if symbol in self.leaders.etfs else None
        return None

    def _freeze_before(self, received_at: datetime) -> None:
        """Keep the bars of every bar close this event comes after (at the same instant
        counts as after), before the event changes them.
        """
        while (close := next_bar_close(self.calendar, self._frozen_through)) is not None:
            if received_at < close:
                return
            self._open_windows(close)
            self._frozen[close] = BarsAtClose(
                close,
                {s: [replace(b) for b in self._bars.closed(s, close)] for s in self.symbols},
                {k: [replace(b) for b in self._leader_bars.closed(k, close)] for k in self.leaders.series},
            )
            self._frozen_through = close
