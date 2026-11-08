"""The collector's quotes stalled (#407, #393 "호가"): what the trader sees of the stream, and when.

The trader fills on the collector's quote stream (``quotes.py``). In continuous trading (from the
open to the closing auction, ``Session.open`` to ``Session.closing_auction``) 005930's quotes come
several a second, so more than ``STALL_AFTER`` (five seconds) without one is a stall: the stream is
down (the collector stopped, its KIS WebSocket dropped, the trader lost its connection) or its
quotes stopped coming. The time is counted from the last quote received, the start of continuous
trading, or the trader's start, whichever is latest: a quote of before the open, or the book as it
stood when the trader connected, does not make a stall at once.

**A stall** (``QuoteWatch.on_time``) is kept (``stalls``) and logged; each book is told
(``paper.PaperTrader.on_stall``): it buys nothing new (a rise read is skipped ``quotes_stalled``,
a buy waiting for its quote given up so, the rises waiting skipped), and its open trade is kept as
open through it (``trade_stalls``). The champion's book in mock mode reads
KIS's best quotes over REST on the trader's key while it holds a position (``mock.MockTrader``), and
keeps checking its stop and time barrier on them; a paper book (paper mode's champion, the mock
mode's paper shadow, every challenger) has no REST access and waits for the stream.

**The quotes back** (``QuoteWatch.on_quote``): the first quote received within ``STALL_AFTER`` of
the trader's clock ends the stall (an older one, the book as it stood when the stream came back, does
not): kept and logged. Each book is told before it takes that quote (``paper.PaperTrader.on_recover``):
the mock book stops its REST reads, and a book whose exit is made on that first quote keeps that
on its trade (``waited``).

**The notice** (#624): a stall is sent to Telegram once, when its silence is ``ALERT_AFTER``
(thirty seconds) long; the clock goes on checking a stall that lasts. The silence is counted as the
stall's own five seconds are, from the last quote (not from when the trader saw the stall), so the
notice goes 25 seconds after the entries stopped. The time it was sent is kept (``stalls.alerted_at``).
The quotes' return is sent once, and only for a stall that was sent: a stall over within thirty
seconds stops the entries, is kept and logged, and tells Telegram nothing. Of the 44 stalls logged
from 2026-11-02 to 11-06, 42 were over within thirty seconds (the median 5 s); a short one needs
nobody, since the entries are stopped whatever is sent and come back by themselves.

**A market halt** (#625): a stall while the engine knows of a market-wide halt that the stall's
silence began in, or at most ``HALT_LEAD`` (five seconds) before (``on_special``: its window from the
halted trade to a minute after the reopening), is the halt, which the engine sends one notice of:
kept and logged as any stall, sent to Telegram neither when it lasts thirty seconds nor when the
quotes return. One whose silence began earlier is a stall of its own and is sent as any.

A stall the trader stopped during is taken up again when it starts on the same day in the same
mode: one already sent is not sent again (the next quote ends it, with its notice); one not sent
yet is sent once thirty seconds have passed since its last quote (or, with none received, since
five seconds before it was seen). One of another day or mode ends at the start (``restart``),
without a notice. Outside continuous trading no stall begins; one begun lasts until a quote comes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta

from krx_quant.feeds.base import KST, kst_day
from krx_quant.market_calendar import Session
from krx_quant.special import Kind, Window
from krx_quant.trader import MOCK
from krx_quant.trader.limits import Notify
from krx_quant.trader.quotes import BestQuote
from krx_quant.trader.store import QUOTE, RESTART, Stall, TraderStore

log = logging.getLogger(__name__)

# More than this long without a quote in continuous trading is a stall (#393 "호가": 5 seconds).
STALL_AFTER = timedelta(seconds=5)
# A stall whose silence is this long is sent to Telegram (#624): counted like ``STALL_AFTER``, from
# the last quote.
ALERT_AFTER = timedelta(seconds=30)
# A stall whose silence began at most this long before a market halt's start is the halt's (#625): the
# stall's own five seconds. On 2026-11-06 the last quote came 0.8 s after the first halted trade; a
# book can come last a moment before it.
HALT_LEAD = STALL_AFTER


def _hms(at: datetime) -> str:
    return at.astimezone(KST).strftime("%H:%M:%S")


class QuoteWatch:
    """Whether the collector's quotes flow, for the trader in ``mode`` started at ``started_at``
    (module doc): stalls kept in ``store``, and sent to ``notify`` once ``alert_after`` long;
    ``sessions`` gives a day's session. ``after`` None watches nothing (a test's made-up quotes,
    sparse as no stream is)."""

    def __init__(
        self,
        store: TraderStore,
        mode: str,
        sessions: Callable[[date], Session | None],
        started_at: datetime,
        notify: Notify,
        after: timedelta | None = STALL_AFTER,
        alert_after: timedelta = ALERT_AFTER,
    ) -> None:
        self._store = store
        self._mode = mode
        self._sessions = sessions
        self._started_at = started_at
        self._notify = notify
        self.after = after
        self.alert_after = alert_after
        self._last: datetime | None = None  # the latest quote received
        self._now: datetime | None = None  # the clock at its last tick
        self.stall: Stall | None = None
        # Of the stall that lasts (#624): from when its silence is counted, and whether it was sent.
        self._silent_since = started_at
        self._alerted = False
        self._halted = False  # the stall that lasts is a market halt's, logged so (#625)
        self._windows: list[Window] = []  # the special windows the engine knows (#625)
        kept = store.open_stall() if after is not None else None
        if kept is None or after is None:
            return
        if kept.mode == mode and kst_day(kept.began_at) == kst_day(started_at):
            self.stall = kept
            self._alerted = kept.alerted_at is not None
            # With no quote before it, the start its five seconds were counted from is not kept.
            self._silent_since = kept.last_quote_at or kept.began_at - after
            log.warning(
                "trader: the quotes had stalled since %s when the trader stopped: no new entry until they"
                " return",
                _hms(kept.began_at),
            )
            return
        store.end_stall(kept.id, started_at, RESTART)
        log.info("trader: the stall of %s ended with the trader's start", kept.began_at.isoformat())

    def on_special(self, windows: Sequence[Window]) -> None:
        """The special windows the engine knows now: a market halt's explains a stall (#625)."""
        self._windows = list(windows)

    def _in_halt(self) -> bool:
        """Whether the stall that lasts is a market halt's (#625): its silence began in a halt the engine
        knows now, or at most ``HALT_LEAD`` before its start (the last quotes come just before the
        halted trade that confirms it)."""
        since = self._silent_since
        return any(w.kind is Kind.MARKET_HALT and w.start - HALT_LEAD <= since < w.end for w in self._windows)

    @property
    def stalled(self) -> bool:
        return self.stall is not None

    def on_time(self, now: datetime) -> Stall | None:
        """The clock at ``now``: the stall that begins now, if one does. One that lasts is sent to
        Telegram once its silence is ``alert_after`` long (#624)."""
        self._now = now
        if self.after is None:
            return None
        if self.stall is not None:
            self._alert(now)
            return None
        session = self._sessions(kst_day(now))
        if session is None or not session.open <= now < session.closing_auction:
            return None
        since = max(t for t in (self._last, session.open, self._started_at) if t is not None)
        if now - since <= self.after:
            return None
        last = self._last
        self.stall = stall = self._store.add_stall(self._mode, now, last)
        self._silent_since, self._alerted, self._halted = since, False, False
        log.warning(
            "trader: the collector's quotes stalled: none for more than %gs (the last at %s): no new entry"
            " until they return",
            self.after.total_seconds(),
            "none since the start" if last is None else _hms(last),
        )
        self._alert(now)  # seen late (the clock held meanwhile), its silence may be that long already
        return stall

    def _alert(self, now: datetime) -> None:
        """The stall that lasts, sent to Telegram once: when its silence is ``alert_after`` long
        (#624). That it was sent is kept first, so that a trader started again does not send it twice."""
        stall = self.stall
        if stall is None or self._alerted or now - self._silent_since < self.alert_after:
            return
        if self._in_halt():
            if not self._halted:
                self._halted = True
                log.warning("trader: the quotes stalled in a market halt: the engine tells it (#625)")
            return
        self._store.alert_stall(stall.id, now)
        self._alerted = True
        last = stall.last_quote_at
        before = "이번 시작 뒤 받은 호가 없음" if last is None else f"마지막 호가 {_hms(last)}"
        if self._mode == MOCK:
            held = "열린 포지션은 trader의 키로 KIS REST 호가를 읽어 손절·시간 규칙대로 지킵니다."
        else:
            held = "열린 포지션은 호가가 돌아오면 그 호가로 목표가·손절가·시간 규칙을 봅니다."
        self._notify(
            f"자동 매매({self._mode}): 수집기의 호가가 {self.alert_after.total_seconds():g}초 이상 오지"
            f" 않습니다({before}). 호가가 돌아올 때까지 새로 사지 않습니다. {held}"
        )

    def on_quote(self, quote: BestQuote) -> Stall | None:
        """A quote of the stream: the stall it ends, if it ends one (a quote received within ``after``
        of the clock's last tick, or of the start before the first). The return of a stall that was
        sent to Telegram is sent; another's is logged alone (#624)."""
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
        back = _hms(quote.received_at)
        log.warning(
            "trader: the quotes are back at %s, %.0fs after the last: on the stream again%s",
            back,
            gap,
            "" if self._alerted else " (the stall was not sent to Telegram, #624)",
        )
        if self._alerted:
            self._notify(
                f"자동 매매({self._mode}): 호가가 다시 옵니다({back}, 끊긴 지 {gap:.0f}초)."
                " 스트림의 호가로 돌아갑니다."
            )
        return stall
