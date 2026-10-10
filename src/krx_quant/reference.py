"""Bar Builder & Reference Price.

The reference price is the integrated mid-point: the average of the best bid and ask
across KRX and NXT. NXT opens 30 seconds after KRX, so in the first 30 seconds of
continuous trading only KRX quotes count. A locked book (bid = ask) is normal on the
integrated book (about 29% of updates on 2026-10-08) and gives the shared price.

Everything is looked up by the time the engine received it, so a value "at t" is what
the engine had received before t, the same live and in replay.
"""

from __future__ import annotations

import bisect
import enum
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from krx_quant.feeds.base import KST
from krx_quant.market_calendar import MarketCalendar

# NXT's main market opens this long after KRX's continuous trading.
KRX_ONLY = timedelta(seconds=30)
# A quote older than this no longer counts: the book of a liquid stock changes many times
# a second, so a minute of silence means the feed stopped (the stale threshold of the
# integrated book feed is the same).
MAX_QUOTE_AGE = timedelta(seconds=60)
# Quotes are kept this long, enough for a four-minute look back and a late lookup.
_KEEP = timedelta(minutes=10)
BAR = timedelta(minutes=1)


class Source(enum.Enum):
    INTEGRATED = "integrated"  # KRX + NXT
    KRX = "krx"


@dataclass
class _Quotes:
    times: list[datetime]
    mids: list[float]


class ReferencePrice:
    def __init__(self, calendar: MarketCalendar) -> None:
        self._calendar = calendar
        self._quotes: dict[tuple[str, Source], _Quotes] = {}

    def quote(self, symbol: str, source: Source, received_at: datetime, bid: int, ask: int) -> None:
        """Record best quotes; an empty side or a crossed book is not a usable quote."""
        if bid <= 0 or ask <= 0 or ask < bid:
            return
        quotes = self._quotes.setdefault((symbol, source), _Quotes([], []))
        if quotes.times and received_at < quotes.times[-1]:
            return  # out of order: the engine only ever moves forward
        quotes.times.append(received_at)
        quotes.mids.append((bid + ask) / 2)
        if received_at - quotes.times[0] > 2 * _KEEP:
            cut = bisect.bisect_left(quotes.times, received_at - _KEEP)
            del quotes.times[:cut], quotes.mids[:cut]

    def mid(self, symbol: str, at: datetime) -> float | None:
        """The mid-point just before ``at`` in continuous trading, or None if it is unknown."""
        local = at.astimezone(KST)
        session = self._calendar.session(local.date())
        if session is None or not session.open <= local <= session.closing_auction:
            return None
        nxt_open = session.open + KRX_ONLY
        source, since = (Source.KRX, session.open) if local < nxt_open else (Source.INTEGRATED, nxt_open)
        quotes = self._quotes.get((symbol, source))
        if quotes is None:
            return None
        # Received strictly before ``at``: at a bar close the engine runs before it takes
        # what arrives in that same instant, and a later look back must agree.
        i = bisect.bisect_left(quotes.times, at) - 1
        if i < 0 or quotes.times[i] < max(since, at - MAX_QUOTE_AGE):
            return None
        return quotes.mids[i]


@dataclass
class Bar:
    start: datetime  # the minute the bar covers, by exchange time
    # Prices: won for stocks; futures, indices and US trades have decimals.
    open: float
    high: float
    low: float
    close: float
    volume: int
    trades: int
    # Exchange times of the first and last trade: a trade can arrive after a later one.
    first_at: datetime = field(repr=False)
    last_at: datetime = field(repr=False)


class BarBuilder:
    """One-minute bars of a series' trades, by exchange time, for the latest day."""

    def __init__(self) -> None:
        self._day: dict[str, date] = {}
        self._bars: dict[str, dict[datetime, Bar]] = {}

    def trade(self, symbol: str, traded_at: datetime, price: float, qty: int) -> None:
        local = traded_at.astimezone(KST)
        day = self._day.get(symbol)
        if day is None or local.date() > day:
            self._day[symbol] = local.date()
            self._bars[symbol] = {}
        elif local.date() < day:
            return  # a straggler from a finished day
        start = local.replace(second=0, microsecond=0)
        bar = self._bars[symbol].get(start)
        if bar is None:
            self._bars[symbol][start] = Bar(start, price, price, price, price, qty, 1, local, local)
            return
        bar.high = max(bar.high, price)
        bar.low = min(bar.low, price)
        if local < bar.first_at:
            bar.open, bar.first_at = price, local
        if local >= bar.last_at:
            bar.close, bar.last_at = price, local
        bar.volume += qty
        bar.trades += 1

    def closed(self, symbol: str, now: datetime) -> list[Bar]:
        """Bars of ``now``'s day whose minute has ended, oldest first."""
        local = now.astimezone(KST)
        if self._day.get(symbol) != local.date():
            return []
        return sorted(
            (b for b in self._bars[symbol].values() if b.start + BAR <= local), key=lambda b: b.start
        )
