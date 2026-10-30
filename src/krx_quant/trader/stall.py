"""The collector's quotes stalled (#407, #393 "호가"): what the trader sees of the stream, and when.

The trader fills on the collector's quote stream (``quotes.py``). In continuous trading (from the
open to the closing auction, ``Session.open`` to ``Session.closing_auction``) 005930's quotes come
several a second, so more than ``STALL_AFTER`` (five seconds) without one is a stall: the stream is
down (the collector stopped, its KIS WebSocket dropped, the trader lost its connection) or its
quotes stopped coming. The time is counted from the last quote received, the start of continuous
trading, or the trader's start, whichever is latest: a quote of before the open, or the book as it
stood when the trader connected, does not make a stall at once.

**A stall** (``QuoteWatch.on_time``) is kept (``stalls``), logged and sent to Telegram once; each
book is told (``paper.PaperTrader.on_stall``): it buys nothing new (a rise read is skipped
``quotes_stalled``, a buy waiting for its quote given up so, the rises waiting skipped), and its
open trade is kept as open through it (``trade_stalls``). The champion's book in mock mode reads
KIS's best quotes over REST on the trader's key while it holds a position (``mock.MockTrader``), and
keeps checking its stop and time barrier on them; a paper book (paper mode's champion, the mock
mode's paper shadow, every challenger) has no REST access and waits for the stream.

**The quotes back** (``QuoteWatch.on_quote``): the first quote received within ``STALL_AFTER`` of
the trader's clock ends the stall (an older one, the book as it stood when the stream came back, does
not): kept, logged and sent once. Each book is told before it takes that quote
(``paper.PaperTrader.on_recover``): the mock book stops its REST reads, and a book whose exit is
made on that first quote keeps that on its trade (``waited``).

A stall the trader stopped during is taken up again when it starts on the same day in the same
mode (no second notice: the next quote ends it, with its notice); one of another day or mode ends
at the start (``restart``), without a notice. Outside continuous trading no stall begins; one
begun lasts until a quote comes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta

from krx_quant.feeds.base import KST, kst_day
from krx_quant.market_calendar import Session
from krx_quant.trader import MOCK
from krx_quant.trader.limits import Notify
from krx_quant.trader.quotes import BestQuote
from krx_quant.trader.store import QUOTE, RESTART, Stall, TraderStore

log = logging.getLogger(__name__)

# More than this long without a quote in continuous trading is a stall (#393 "호가": 5 seconds).
STALL_AFTER = timedelta(seconds=5)


def _hms(at: datetime) -> str:
    return at.astimezone(KST).strftime("%H:%M:%S")


class QuoteWatch:
    """Whether the collector's quotes flow, for the trader in ``mode`` started at ``started_at``
    (module doc): stalls kept in ``store`` and sent to ``notify``; ``sessions`` gives a day's
    session. ``after`` None watches nothing (a test's made-up quotes, sparse as no stream is)."""

    def __init__(
        self,
        store: TraderStore,
        mode: str,
        sessions: Callable[[date], Session | None],
        started_at: datetime,
        notify: Notify,
        after: timedelta | None = STALL_AFTER,
    ) -> None:
        self._store = store
        self._mode = mode
        self._sessions = sessions
        self._started_at = started_at
        self._notify = notify
        self.after = after
        self._last: datetime | None = None  # the latest quote received
        self._now: datetime | None = None  # the clock at its last tick
        self.stall: Stall | None = None
        kept = store.open_stall() if after is not None else None
        if kept is None:
            return
        if kept.mode == mode and kst_day(kept.began_at) == kst_day(started_at):
            self.stall = kept
            log.warning(
                "trader: the quotes had stalled since %s when the trader stopped: no new entry until they"
                " return",
                _hms(kept.began_at),
            )
            return
        store.end_stall(kept.id, started_at, RESTART)
        log.info("trader: the stall of %s ended with the trader's start", kept.began_at.isoformat())

    @property
    def stalled(self) -> bool:
        return self.stall is not None

    def on_time(self, now: datetime) -> Stall | None:
        """The clock at ``now``: the stall that begins now, if one does."""
        self._now = now
        if self.after is None or self.stall is not None:
            return None
        session = self._sessions(kst_day(now))
        if session is None or not session.open <= now < session.closing_auction:
            return None
        since = max(t for t in (self._last, session.open, self._started_at) if t is not None)
        if now - since <= self.after:
            return None
        last = self._last
        self.stall = stall = self._store.add_stall(self._mode, now, last)
        seconds = self.after.total_seconds()
        before = "이번 시작 뒤 받은 호가 없음" if last is None else f"마지막 호가 {_hms(last)}"
        log.warning(
            "trader: the collector's quotes stalled: none for more than %gs (the last at %s): no new entry"
            " until they return",
            seconds,
            "none since the start" if last is None else _hms(last),
        )
        if self._mode == MOCK:
            held = "열린 포지션은 trader의 키로 KIS REST 호가를 읽어 손절·시간 규칙대로 지킵니다."
        else:
            held = "열린 포지션은 호가가 돌아오면 그 호가로 목표가·손절가·시간 규칙을 봅니다."
        self._notify(
            f"자동 매매({self._mode}): 수집기의 호가가 {seconds:g}초 넘게 오지 않습니다({before})."
            f" 호가가 돌아올 때까지 새로 사지 않습니다. {held}"
        )
        return stall

    def on_quote(self, quote: BestQuote) -> Stall | None:
        """A quote of the stream: the stall it ends, if it ends one (a quote received within ``after``
        of the clock's last tick, or of the start before the first)."""
        if self._last is None or quote.received_at > self._last:
            self._last = quote.received_at
        stall = self.stall
        if stall is None or self.after is None:
            return None
        if (self._now or self._started_at) - quote.received_at > self.after:
            return None  # the book as it stood: not the stream back
        self._store.end_stall(stall.id, quote.received_at, QUOTE)
        self.stall = None
        since = stall.last_quote_at or stall.began_at
        gap = (quote.received_at - since).total_seconds()
        log.warning(
            "trader: the quotes are back at %s, %.0fs after the last: on the stream again",
            _hms(quote.received_at),
            gap,
        )
        self._notify(
            f"자동 매매({self._mode}): 호가가 다시 옵니다({_hms(quote.received_at)}, 끊긴 지 {gap:.0f}초)."
            " 스트림의 호가로 돌아갑니다."
        )
        return stall
