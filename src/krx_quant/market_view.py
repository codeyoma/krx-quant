"""Market View: what the engine knows of the market, built from its events.

The predicted symbols' one-minute bars and reference prices (``reference``), and the bars
of the series that may lead them (``features.Leaders``: futures, indices, US daytime
trades, ETFs), each by exchange time, as they stood at each bar close. The engine predicts
from it, live and in replay; training on a recorded day feeds the same events through it,
so it learns from exactly what the engine saw.

A bar close's bars are kept when the first event received at or after it arrives, before
that event changes them: a prediction made late never sees a trade it could not have.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from typing import Any

from krx_quant.contracts import KOSPI200, USD
from krx_quant.features import Leaders, Snapshot
from krx_quant.feeds import (
    BOOK,
    BOOK_SNAPSHOT,
    INDEX,
    INDEX_REST,
    KOSPI200_FUTURES,
    KRX_TRADE,
    TRADE,
    US_TRADE,
    USD_FUTURES,
)
from krx_quant.feeds.base import KST
from krx_quant.market_calendar import MarketCalendar, Session
from krx_quant.reference import BAR, Bar, BarBuilder, ReferencePrice, Source

HORIZON = timedelta(minutes=4)
# How far ahead to look for the next session (long holidays included).
LOOKAHEAD_DAYS = 15
INDEX_FEEDS = (INDEX.tr_id, INDEX_REST.tr_id)
FUTURES_SERIES = {KOSPI200_FUTURES.tr_id: KOSPI200.series, USD_FUTURES.tr_id: USD.series}
# The feeds with the symbols' trades and best quotes.
_QUOTING = (TRADE.tr_id, KRX_TRADE.tr_id, BOOK.tr_id, BOOK_SNAPSHOT.tr_id)


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


@dataclass(frozen=True)
class Frozen:
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
        self._frozen: dict[datetime, Frozen] = {}
        self._frozen_through = start

    def on_event(self, feed: str, row: dict[str, Any]) -> None:
        """Take a trade or a best quote into the bars and reference prices; other events
        are not the view's.
        """
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
            # Integrated trades make the bars; their best quotes are integrated quotes too.
            self._bars.trade(symbol, row["event_time"], row["price"], row["qty"])
            self._reference.quote(symbol, Source.INTEGRATED, received_at, row["bid1"], row["ask1"])
        elif feed == KRX_TRADE.tr_id:
            # KRX-only trades carry KRX's best quotes, for the 30 seconds before NXT opens.
            self._reference.quote(symbol, Source.KRX, received_at, row["bid1"], row["ask1"])
        else:
            self._reference.quote(symbol, Source.INTEGRATED, received_at, row["bid_px_1"], row["ask_px_1"])

    def at_close(self, close: datetime) -> Frozen:
        """The bars as they stood at ``close``: kept when an event at or after it arrived,
        else (nothing received since) as they are. Bars kept for earlier closes go.
        """
        frozen = self._frozen.pop(close, None)
        for stale in [c for c in self._frozen if c < close]:
            del self._frozen[stale]  # a close no longer predicted (the calendar changed)
        self._frozen_through = max(self._frozen_through, close)
        if frozen is not None:
            return frozen
        return Frozen(
            close,
            {s: self._bars.closed(s, close) for s in self.symbols},
            {k: self._leader_bars.closed(k, close) for k in self.leaders.series},
        )

    def snapshot(self, frozen: Frozen, symbol: str, fee_rate: float) -> Snapshot:
        """``symbol`` at a bar close: its bars, reference price and leaders there."""
        session = self.calendar.session(frozen.close.astimezone(KST).date())
        return Snapshot.at(
            symbol,
            frozen.close,
            frozen.symbols.get(symbol, []),
            self.mid(symbol, frozen.close),
            fee_rate,
            session_open=session.open if session is not None else None,
            leaders=self.leaders.groups(symbol, frozen.symbols, frozen.leaders),
        )

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
            self._frozen[close] = Frozen(
                close,
                {s: [replace(b) for b in self._bars.closed(s, close)] for s in self.symbols},
                {k: [replace(b) for b in self._leader_bars.closed(k, close)] for k in self.leaders.series},
            )
            self._frozen_through = close
