"""Market View: what the engine knows of the market, built from its events.

The predicted symbols' one-minute bars and reference prices (``reference``), the bars of
the series that may lead them (``features.Leaders``: futures, indices, US daytime trades,
ETFs), each by exchange time, as they stood at each bar close, the symbols' microprices at
each minute's end (from the integrated book), and their flows (program trading, foreign
member firms, investor estimates), as received before it. The engine predicts
from it, live and in replay; training on a recorded day feeds the same events through it,
so it learns from exactly what the engine saw.

A bar close's bars are kept when the first event received at or after it arrives, before
that event changes them: a prediction made late never sees a trade it could not have.
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from typing import Any

from krx_quant.contracts import KOSPI200, USD, days_to_expiry
from krx_quant.features import (
    FLOW_MINUTES,
    MINUTE_MOVES,
    OFI_MINUTES,
    TRADE_MINUTES,
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
    PROGRAM,
    TRADE,
    US_TRADE,
    USD_FUTURES,
)
from krx_quant.feeds.base import KST, LEVELS, kst_day
from krx_quant.market_calendar import MarketCalendar, Session
from krx_quant.reference import BAR, MAX_QUOTE_AGE, Bar, BarBuilder, ReferencePrice, Source

HORIZON = timedelta(minutes=4)
# How far ahead to look for the next session (long holidays included).
LOOKAHEAD_DAYS = 15
INDEX_FEEDS = (INDEX.tr_id, INDEX_REST.tr_id)
FUTURES_SERIES = {KOSPI200_FUTURES.tr_id: KOSPI200.series, USD_FUTURES.tr_id: USD.series}
# The feeds with the symbols' trades and best quotes.
_QUOTING = (TRADE.tr_id, KRX_TRADE.tr_id, BOOK.tr_id, BOOK_SNAPSHOT.tr_id)
# The feeds with the symbols' flows (Turing's).
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


class _Book:
    """A symbol's book updates by receipt time, for the latest hour or so: the microprice,
    the ten levels' sizes, and running sums of the order flow imbalance at the best quotes
    and of their mean size."""

    _KEEP = timedelta(minutes=MINUTE_MOVES + 10)

    def __init__(self) -> None:
        self._at: list[datetime] = []
        self._micro: list[float] = []
        self._depth: list[tuple[float, float]] = []  # bid, ask: the ten levels' sizes
        self._ofi: list[float] = []  # running sums
        self._size: list[float] = []
        self._last: tuple[int, int, int, int] | None = None  # bid, its size, ask, its size

    def add(self, at: datetime, row: dict[str, Any]) -> None:
        """A book update; an empty side or a crossed book is not a usable one."""
        bid, ask = row["bid_px_1"], row["ask_px_1"]
        if bid <= 0 or ask <= 0 or ask < bid or (self._at and at < self._at[-1]):
            return
        bid_qty, ask_qty = row.get("bid_qty_1") or 0, row.get("ask_qty_1") or 0
        # The microprice leans toward the side with less waiting: the other is likelier to go.
        # Without both sizes (a file recorded without them) it is the mid-point.
        if bid_qty > 0 and ask_qty > 0:
            micro = (bid * ask_qty + ask * bid_qty) / (bid_qty + ask_qty)
        else:
            micro = (bid + ask) / 2
        flow = 0.0
        if self._last is not None:  # Cont, Kukanov and Stoikov's order flow imbalance
            b0, bq0, a0, aq0 = self._last
            flow = (bid_qty if bid >= b0 else 0) - (bq0 if bid <= b0 else 0)
            flow -= (ask_qty if ask <= a0 else 0) - (aq0 if ask >= a0 else 0)
        self._last = (bid, bid_qty, ask, ask_qty)
        depth = tuple(float(sum(row.get(f"{side}_qty_{i}") or 0 for i in LEVELS)) for side in ("bid", "ask"))
        self._at.append(at)
        self._micro.append(micro)
        self._depth.append((depth[0], depth[1]))
        self._ofi.append((self._ofi[-1] if self._ofi else 0.0) + flow)
        self._size.append((self._size[-1] if self._size else 0.0) + (bid_qty + ask_qty) / 2)
        if at - self._at[0] > 2 * self._KEEP:
            cut = bisect.bisect_left(self._at, at - self._KEEP)
            for kept in (self._at, self._micro, self._depth, self._ofi, self._size):
                del kept[:cut]

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

    def flow(self, start: datetime, end: datetime) -> tuple[float, float | None]:
        """The order flow imbalance of the updates received in [start, end), and the best
        quotes' mean size over them (None without any)."""
        lo, hi = bisect.bisect_left(self._at, start), bisect.bisect_left(self._at, end)
        if hi <= lo:
            return 0.0, None
        ofi = self._ofi[hi - 1] - (self._ofi[lo - 1] if lo > 0 else 0.0)
        size = self._size[hi - 1] - (self._size[lo - 1] if lo > 0 else 0.0)
        return ofi, size / (hi - lo)


class _Running:
    """Running sums of what was received, for the latest minutes."""

    _KEEP = timedelta(minutes=TRADE_MINUTES + 5)

    def __init__(self, width: int) -> None:
        self._at: list[datetime] = []
        self._sums: list[tuple[float, ...]] = []
        self._zero = (0.0,) * width

    def add(self, at: datetime, values: tuple[float, ...]) -> None:
        if self._at and at < self._at[-1]:
            at = self._at[-1]  # the engine only ever moves forward
        last = self._sums[-1] if self._sums else self._zero
        self._at.append(at)
        self._sums.append(tuple(a + b for a, b in zip(last, values, strict=True)))
        if at - self._at[0] > 2 * self._KEEP:
            cut = bisect.bisect_left(self._at, at - self._KEEP)
            del self._at[:cut], self._sums[:cut]

    def between(self, start: datetime, end: datetime) -> tuple[float, ...]:
        """The sums of what was received in [start, end)."""
        lo, hi = bisect.bisect_left(self._at, start), bisect.bisect_left(self._at, end)
        if hi <= lo:
            return self._zero
        before = self._sums[lo - 1] if lo > 0 else self._zero
        return tuple(a - b for a, b in zip(self._sums[hi - 1], before, strict=True))


@dataclass(frozen=True)
class BarsAtClose:
    """The bars as they stood at a bar close."""

    close: datetime
    symbols: dict[str, list[Bar]]
    leaders: dict[str, list[Bar]]


class MarketView:
    def __init__(
        self, calendar: MarketCalendar, symbols: Sequence[str], leaders: Leaders, start: datetime
    ) -> None:
        """A view with nothing received yet, whose first bar close is the first after ``start``."""
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
        self._sides = {s: _Running(4) for s in self.symbols}  # buys, sells, their volumes
        self._venues = {s: _Running(2) for s in self.symbols}  # KRX, NXT volume
        self._expiry: dict[date, int] = {}

    def on_event(self, feed: str, row: dict[str, Any]) -> None:
        """Take a trade or a best quote into the bars and reference prices; other events
        are not the view's.
        """
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
                self._venues[row["symbol"]].add(row["received_at"], (0.0, float(row.get("qty") or 0)))
            return
        if feed not in _QUOTING or row["symbol"] not in self.symbols:
            return
        symbol, received_at = row["symbol"], row["received_at"]
        self._freeze_before(received_at)
        if feed == TRADE.tr_id:
            self._bars.trade(symbol, row["event_time"], row["price"], row["qty"])
            self._reference.quote(symbol, Source.INTEGRATED, received_at, row["bid1"], row["ask1"])
            side, qty = row.get("side"), float(row["qty"] or 0)
            if side in ("buy", "sell"):
                buy = side == "buy"
                self._sides[symbol].add(received_at, (float(buy), float(not buy), qty * buy, qty * (not buy)))
        elif feed == KRX_TRADE.tr_id:
            self._reference.quote(symbol, Source.KRX, received_at, row["bid1"], row["ask1"])
            self._venues[symbol].add(received_at, (float(row.get("qty") or 0), 0.0))
        else:
            bid, ask = row["bid_px_1"], row["ask_px_1"]
            self._reference.quote(symbol, Source.INTEGRATED, received_at, bid, ask)
            self._books[symbol].add(received_at, row)

    def at_close(self, close: datetime) -> BarsAtClose:
        """The bars as they stood at ``close``: kept when an event at or after it arrived,
        else (nothing received since) as they are. Bars kept for earlier closes go, and none
        are kept for ``close`` from now on.
        """
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
            orders=self.orders(symbol, frozen.close),
        )

    def orders(self, symbol: str, at: datetime) -> OrderFlow:
        """``symbol``'s book and trades received in the minutes before ``at``, from the open."""
        session = self.calendar.session(kst_day(at))
        if symbol not in self._books or session is None:
            return OrderFlow()

        def since(minutes: int) -> datetime:
            return max(session.open, at - minutes * BAR)

        book = self._books[symbol]
        depth = book.depth(at)
        flows = {m: book.flow(since(m), at) for m in OFI_MINUTES}
        buys, sells, bought, sold = self._sides[symbol].between(since(TRADE_MINUTES), at)
        krx, nxt = self._venues[symbol].between(since(TRADE_MINUTES), at)
        return OrderFlow(
            ofi={m: ofi for m, (ofi, _) in flows.items()},
            size={m: size for m, (_, size) in flows.items() if size is not None},
            bid_depth=None if depth is None else depth[0],
            ask_depth=None if depth is None else depth[1],
            buys=int(buys),
            sells=int(sells),
            buy_volume=bought,
            sell_volume=sold,
            krx_volume=krx,
            nxt_volume=nxt,
        )

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
            self._frozen[close] = BarsAtClose(
                close,
                {s: [replace(b) for b in self._bars.closed(s, close)] for s in self.symbols},
                {k: [replace(b) for b in self._leader_bars.closed(k, close)] for k in self.leaders.series},
            )
            self._frozen_through = close
