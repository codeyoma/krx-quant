"""Market View: what the engine knows of the market, built from its events.

The predicted symbols' one-minute bars and reference prices (``reference``), the bars of
the series that may lead them (``features.Leaders``: futures, indices, US daytime trades,
ETFs), each by exchange time, as they stood at each bar close, and the symbols' flows
(program trading, foreign member firms, investor estimates) as received before it. The engine predicts
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
from krx_quant.features import FLOW_MINUTES, Flows, Leaders, Snapshot
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
    PROGRAM,
    TRADE,
    US_TRADE,
    USD_FUTURES,
)
from krx_quant.feeds.base import KST, kst_day
from krx_quant.market_calendar import MarketCalendar, Session
from krx_quant.reference import BAR, Bar, BarBuilder, ReferencePrice, Source

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
        if feed not in _QUOTING or row["symbol"] not in self.symbols:
            return
        symbol, received_at = row["symbol"], row["received_at"]
        self._freeze_before(received_at)
        if feed == TRADE.tr_id:
            self._bars.trade(symbol, row["event_time"], row["price"], row["qty"])
            self._reference.quote(symbol, Source.INTEGRATED, received_at, row["bid1"], row["ask1"])
        elif feed == KRX_TRADE.tr_id:
            self._reference.quote(symbol, Source.KRX, received_at, row["bid1"], row["ask1"])
        else:
            self._reference.quote(symbol, Source.INTEGRATED, received_at, row["bid_px_1"], row["ask_px_1"])

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
        )

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
