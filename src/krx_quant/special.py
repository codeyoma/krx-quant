"""Special situations: when trading was not normal, for one symbol or the whole market.

- **VI** (volatility interruption): a symbol's trading turns into a two-minute
  single-price auction (released at a random moment up to 30 seconds later). Known from
  the VI status the collector polls: its start, and its release once it is over.
- **Circuit breaker** (KOSPI): the index 8% below the previous close for a minute halts
  all trading for 20 minutes, then a 10-minute single-price auction reopens it. Again at
  15% and at 20% (each also 1 point below the stage before); the third ends the day.
- **Sidecar** (KOSPI): KOSPI200 futures 5% off the previous close for a minute halt
  program trading for 5 minutes, once a day.
- **Market halt** (#625): the whole market's trading halted, as KIS's fields confirm it, apart
  from the breaker the rules infer (module doc "The market halt").

Circuit breakers and sidecars are not read from KIS: they are called by the exchange's
rules from the recorded KOSPI index and KOSPI200 futures, so a replay calls them the
same way. Sidecars are not called in the first 5 minutes of the session; sidecars and the
first two breaker stages not in the last 40 minutes either (the third stage can end the
day at any time). A minute held only counts inside those hours: a gap open from the
futures' 08:45 start is called at 09:06, not before the stock market opens. Breakers count
from the open: on 2026-11-06 the index met the first stage's drop from 09:02:30, the rules
call it at 09:03:30 and trading halted at 09:03:33 (#625's probe).

> 바뀜 (2026-11-09, #632): breakers, like sidecars, were not called in the first 5 minutes →
> they are, from the open. Why: 2026-11-06's breaker halted trading at 09:03:33, and the window
> began at 09:06:00.

**The market halt** (#625). KIS sends no notice of a breaker on the feeds subscribed (the market
operation feed ``H0STMKO0`` is not, #625's probe), but the integrated trades and book say it:

- the last trades before the halt carry ``TRHT_YN`` 'Y' (``halted``): on 2026-11-06 005930's from
  09:03:32.959 and 000660's from 09:03:33.136, three seconds after the rules called the breaker;
- after the halt the book comes back in the reopening's single-price auction, ``HOUR_CLS_CODE``
  'C' (``hour_cls``) with an expected price, 20 minutes on (09:23:33), and the trades with it
  10 minutes later (09:33:38);
- continuous trading is back once the book or the trades of every symbol say '0' (09:34:10).

A halt is confirmed when a trade of one of the engine's symbols flagged halted is received from a
minute before a breaker stage is called to the end of that stage's window by the rules (a halted
trade with no breaker, or outside its window, is one stock's halt): a ``MARKET_HALT`` window from
the first such trade's exchange time, learned when both are known, to the session's close while
the reopening is not known (detail ``정지``). A trade or book of the symbols in the auction ('C'),
or in continuous trading ('0') when the auction's frames were missed, received at least a minute
after, makes it ``단일가``. Once every symbol's latest
trade or book says '0' (continuous trading), the breaker's window ends there (the real
reopening, a later version of it), and once each symbol's trades and book have then kept coming
(each within ``FRESH`` of the one before) for a full ``REOPEN_HOLD``, the halt's window ends there
(``재개``): the engine predicts again, the trader buys again (#625's stage 3). The REST book that
stands in for a book cut by the session limit counts as the book, with no time code. A halt still on
``TRADING_CAP`` (10 minutes) after the trades came back is ended there with a WARNING. A later breaker
stage or halted trade while it is on starts it over (``정지``): its reopening is judged from what came
after alone, the minute's frames after it left out. A halt whose
trades carry no flag is not confirmed: the breaker's window is all there is.

Each is a window of exchange time. A window is known from the moment its event is
received, and only what was received before a moment counts at that moment: live and
replay see the same thing. A window learned later replaces the one learned before it for
the same start (a VI's release becomes known). The rules' state is kept in memory: an
engine restarted during a breaker calls it again from what it receives after the
restart, like the bars it builds. The engine keeps every window it learns in its
database (``special_windows``, #236), and fills past days' from their recordings.

A window's detail says more where it is known: a breaker's stage (``1단계``, ``2단계``,
``3단계``), a VI's type as KIS gives it (``static``, ``dynamic`` or ``both``) and a market halt's
phase (``정지``, ``단일가``, ``재개``).
"""

from __future__ import annotations

import dataclasses
import enum
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from krx_quant.feeds import BOOK, BOOK_SNAPSHOT, INDEX, INDEX_REST, KOSPI200_FUTURES, TRADE, VI_STATUS
from krx_quant.feeds.base import KST
from krx_quant.market_calendar import MarketCalendar, Session

log = logging.getLogger(__name__)

# A VI still on (or whose release is not known) is taken to last at least this long: two
# minutes and the random end of up to 30 seconds, with a little room.
VI_LONGEST = timedelta(minutes=3)

KOSPI = "0001"  # the index code the breaker reads
HOLD = timedelta(minutes=1)  # how long a move must last before a breaker or sidecar
# Not called this soon after the open, or (but the last breaker stage) this close to the close.
QUIET_AFTER_OPEN = timedelta(minutes=5)
QUIET_BEFORE_CLOSE = timedelta(minutes=40)
# The drops from the previous close (%) that call the breaker's stages, and how far below
# the stage before each later one must be (points).
BREAKER_STAGES = (8.0, 15.0, 20.0)
BREAKER_STEP = 1.0
BREAKER_HALT = timedelta(minutes=30)  # 20 minutes' halt and the 10-minute auction after it
SIDECAR_MOVE = 5.0
SIDECAR_HALT = timedelta(minutes=5)
# A VI's types as the VI status gives them (``feeds.stock._VI_KINDS``); another is not shown.
VI_TYPES = ("static", "dynamic", "both")
# The market halt (#625, module doc): the trades' and book's time codes (``HOUR_CLS_CODE``) of a
# single-price auction and of continuous trading, as #625's probe read them on 2026-11-06.
AUCTION_CODE, CONTINUOUS_CODE = "C", "0"
# A trade or book in the auction counts as the reopening only this long after the halt: the last
# frames before it (received just after the halted trade) may be one stock's auction (a VI).
QUIET_AFTER_HALT = timedelta(minutes=1)
# Continuous trading counts as back once each symbol's trades and book have kept coming for this
# long, each within ``FRESH`` of every moment.
REOPEN_HOLD = timedelta(minutes=1)
FRESH = timedelta(seconds=10)
# A halt still on this long after the trades came back is ended there, with a WARNING: the time codes
# that would end it are not coming (a symbol's books or trades without them).
TRADING_CAP = timedelta(minutes=10)
# The symbols' feeds a market halt follows: the integrated trades and book, and the REST book that
# stands in for the book cut by the session limit (fresh quotes; it carries no time code).
_HALT_FEEDS = (TRADE.tr_id, BOOK.tr_id, BOOK_SNAPSHOT.tr_id)
HALTED, AUCTION, REOPENED = "정지", "단일가", "재개"  # a market halt's phases (``Window.detail``)
# The version of these rules a past day's windows are decided by (``special_days.rules``): a day
# decided by an earlier one is decided again from its recording. 2: #625 (the breaker from the open,
# the market halt).
RULES = 2


class Kind(enum.Enum):
    VI = "vi"
    CIRCUIT_BREAKER = "circuit_breaker"
    SIDECAR = "sidecar"
    MARKET_HALT = "market_halt"  # confirmed by KIS's fields (#625); the breaker is inferred


@dataclass(frozen=True)
class Window:
    kind: Kind
    symbol: str | None  # None: the whole market
    start: datetime
    end: datetime  # the release, or the least it can last while that is not known
    # A breaker's stage ("1단계"), a VI's type ("static"), a market halt's phase ("정지"); else None.
    detail: str | None = None

    def touches(self, symbol: str, start: datetime, end: datetime) -> bool:
        """Whether this window and the open span from ``start`` to ``end`` of ``symbol`` meet."""
        return self.symbol in (symbol, None) and self.start < end and self.end > start


class _Held:
    """A condition on a market feed that must hold for a minute."""

    def __init__(self) -> None:
        self.since: datetime | None = None

    def check(self, met: bool, at: datetime) -> datetime | None:
        """The moment it has held a minute, if ``at`` is that moment or later; else None."""
        if not met:
            self.since = None
            return None
        if self.since is None:
            self.since = at
        return self.since + HOLD if at - self.since >= HOLD else None


class _Breaker:
    """The KOSPI circuit breaker's stages through one day."""

    def __init__(self) -> None:
        self.stage = 0  # stages called so far
        self.level = 0.0  # the drop (%) the last stage was called at
        self._held = _Held()

    def update(self, change: float, at: datetime, session: Session) -> tuple[datetime, datetime] | None:
        """The window of a stage called by this tick, if one is."""
        if self.stage == len(BREAKER_STAGES):
            return None
        last = self.stage == len(BREAKER_STAGES) - 1
        drop = -change
        met = (
            _in_hours(at, session, until_close=last, after_open=False)
            and drop >= BREAKER_STAGES[self.stage]
            and (self.stage == 0 or drop >= self.level + BREAKER_STEP)
        )
        called = self._held.check(met, at)
        if called is None:
            return None
        self.stage += 1
        self.level = drop
        self._held = _Held()
        end = datetime.combine(session.day, time.max, KST) if last else called + BREAKER_HALT
        return called, end


class _Sidecar:
    """The KOSPI sidecar through one day: once at most."""

    def __init__(self) -> None:
        self.called = False
        self._held = _Held()

    def update(self, change: float, at: datetime, session: Session) -> tuple[datetime, datetime] | None:
        if self.called:
            return None
        met = _in_hours(at, session, until_close=False) and abs(change) >= SIDECAR_MOVE
        called = self._held.check(met, at)
        if called is None:
            return None
        self.called = True
        return called, called + SIDECAR_HALT


def _in_hours(at: datetime, session: Session, *, until_close: bool, after_open: bool = True) -> bool:
    """Whether the rules may call a halt at ``at``: from the open (5 minutes after it if
    ``after_open``: a sidecar), and before the last 40 minutes unless ``until_close``.
    """
    end = session.close if until_close else session.close - QUIET_BEFORE_CLOSE
    start = session.open + QUIET_AFTER_OPEN if after_open else session.open
    return start <= at < end


class _Halt:
    """A market-wide halt KIS's fields confirmed, from its halted trade to its reopening (module doc).

    What releases it is read afresh from what came after its latest reset (``since``: its confirmation,
    a later breaker stage, a later halted trade), never carried across one: every symbol's latest time
    code since then continuous trading, and every symbol's trades and book kept coming for a minute."""

    def __init__(self, window: Window, breaker: Window, confirmed_at: datetime) -> None:
        self.window = window  # the halt's window, as last learned
        self.breaker = breaker  # the latest breaker stage it is the halt of
        self.reset(confirmed_at)

    def reset(self, at: datetime) -> None:
        """The halt (again) from ``at``: what came before tells nothing of its reopening."""
        self.since = at
        self.codes: dict[str, str] = {}  # each symbol's latest time code since then
        self.trade_at: dict[str, datetime] = {}  # each symbol's last trade, book since then
        self.book_at: dict[str, datetime] = {}
        self.fresh_since: datetime | None = None  # since when the release's conditions held
        self.trading_since: datetime | None = None  # the first trade not halted since then
        self.ended_breaker = False  # the breaker's window ended at the continuous trading seen

    def continuous(self, symbols: Sequence[str]) -> bool:
        """Whether every symbol's latest time code since the reset says continuous trading."""
        return all(self.codes.get(s) == CONTINUOUS_CODE for s in symbols)

    def fresh(self, at: datetime, symbols: Sequence[str]) -> bool:
        """Whether every one of ``symbols`` had a trade and a book received within ``FRESH`` of ``at``."""
        return all(
            (t := self.trade_at.get(s)) is not None
            and at - t <= FRESH
            and (b := self.book_at.get(s)) is not None
            and at - b <= FRESH
            for s in symbols
        )


class SpecialSituations:
    def __init__(self, calendar: MarketCalendar) -> None:
        self._calendar = calendar
        # (received at, window), in the order received.
        self._learned: list[tuple[datetime, Window]] = []
        self._session: Session | None = None
        self._breaker = _Breaker()
        self._sidecar = _Sidecar()
        self._reset_halt()

    def _reset_halt(self) -> None:
        """No halt known, pending or flagged: a new day."""
        self._halt: _Halt | None = None
        # The last breaker stage called and not yet confirmed, with when it was called (received).
        self._pending: tuple[Window, datetime] | None = None
        # The halted trades received in the last minute (traded, received), oldest first.
        self._flags: list[tuple[datetime, datetime]] = []

    def event(self, feed: str, row: dict[str, Any], symbols: Sequence[str]) -> tuple[Window, ...]:
        """Take one of the engine's events; the windows it calls or changes: a VI of one of
        ``symbols``, a circuit breaker (KOSPI), a sidecar (KOSPI200 futures), a market halt and
        the breaker's end (the integrated trades and book of ``symbols``, #625).
        """
        if feed in _HALT_FEEDS:
            if row["symbol"] not in symbols or (self._halt is None and not row.get("halted")):
                return ()  # nearly every event: nothing halted
            return self._market(feed, row, symbols)
        if feed in (INDEX.tr_id, INDEX_REST.tr_id):
            return self.index(row)
        if feed == KOSPI200_FUTURES.tr_id:
            return self._one(self.futures(row))
        if feed == VI_STATUS.tr_id and row["symbol"] in symbols:
            return self._one(self.vi(row))
        return ()

    @staticmethod
    def _one(window: Window | None) -> tuple[Window, ...]:
        return () if window is None else (window,)

    def vi(self, row: dict[str, Any]) -> Window | None:
        """Take a VI status row; the window if it is new or changed, else None."""
        start: datetime = row["event_time"]
        released: datetime | None = row["end_time"]
        if row["active"] or released is None:
            # Still on: whatever release KIS shows, the random end may come later.
            end = max(released or start, start + VI_LONGEST)
        else:
            end = released
        # Rows recorded or read without the type (diagnose) have none.
        detail = row.get("kind") if row.get("kind") in VI_TYPES else None
        return self._learn(row["received_at"], Window(Kind.VI, row["symbol"], start, end, detail))

    def index(self, row: dict[str, Any]) -> tuple[Window, ...]:
        """Take a market index tick: a circuit breaker's window if one is called, and a market
        halt's if a halted trade came in the minute before (module doc "The market halt")."""
        change = row["change_pct"]
        session = self._today(row["event_time"])
        if row["symbol"] != KOSPI or change is None or session is None:
            return ()
        called = self._breaker.update(change, row["event_time"], session)
        if called is None:
            return ()
        stage = f"{self._breaker.stage}단계"  # the stage just called
        window = Window(Kind.CIRCUIT_BREAKER, None, *called, stage)
        received = row["received_at"]
        out = self._one(self._learn(received, window))
        halt = self._halt
        if halt is None:
            self._pending = (window, received)
            out += self._one(self._confirm(received, session))
        else:  # a later stage in the halt or its reopening: the halt again, of that stage
            halt.breaker = window
            out += self._one(self._again(received))
        return out

    def futures(self, row: dict[str, Any]) -> Window | None:
        """Take a KOSPI200 futures trade; a sidecar's window if one is called."""
        change = row["change_pct"]  # None in files recorded before it was kept
        session = self._today(row["event_time"])
        if change is None or session is None:
            return None
        called = self._sidecar.update(change, row["event_time"], session)
        if called is None:
            return None
        return self._learn(row["received_at"], Window(Kind.SIDECAR, None, *called))

    def _market(self, feed: str, row: dict[str, Any], symbols: Sequence[str]) -> tuple[Window, ...]:
        """Take an integrated trade or book of one of ``symbols`` while a halt is on or flagged:
        the halt's and the breaker's windows it changes (module doc "The market halt")."""
        received: datetime = row["received_at"]
        session = self._today(received)  # a new day: a halt of an earlier day is over
        if session is None:
            return ()
        halt = self._halt
        if feed == TRADE.tr_id and row.get("halted"):
            self._flags = [f for f in self._flags if f[1] >= received - HOLD]
            self._flags.append((row["event_time"], received))
            if halt is None:
                return self._one(self._confirm(received, session))
            return self._one(self._again(received))  # halted again: the reopening starts over
        if halt is None or received < halt.since + QUIET_AFTER_HALT:
            return ()  # no halt, or the frames trailing its (re)start
        symbol: str = row["symbol"]
        code = row.get("hour_cls")
        out: list[Window] = []
        if halt.window.detail == HALTED and code in (AUCTION_CODE, CONTINUOUS_CODE):
            # The market speaks again: the reopening auction, or (its frames missed) continuous trading.
            out += self._one(self._halt_version(received, halt.window.end, AUCTION))
        # Each symbol's latest time code, trade and book since the reset. A trade or book more than
        # ``FRESH`` after the one before it is a gap: the fresh minute starts again.
        seen = halt.trade_at if feed == TRADE.tr_id else halt.book_at
        before = seen.get(symbol)
        gap = before is not None and received - before > FRESH
        seen[symbol] = received
        if code in (AUCTION_CODE, CONTINUOUS_CODE):
            halt.codes[symbol] = code
        if feed == TRADE.tr_id and halt.trading_since is None:
            halt.trading_since = received
        if halt.trading_since is not None and received - halt.trading_since >= TRADING_CAP:
            log.warning(
                "market halt of %s: trading came back at %s and no fresh minute of continuous trading by"
                " %s; ended there",
                halt.window.start.isoformat(),
                halt.trading_since.isoformat(),
                received.isoformat(),
            )
            return (*out, *self._release(received))
        continuous = halt.continuous(symbols)
        if continuous and not halt.ended_breaker:
            halt.ended_breaker = True
            if halt.breaker.detail != f"{len(BREAKER_STAGES)}단계":
                # The breaker ends at the real reopening, not the rules' 30 minutes.
                breaker = dataclasses.replace(halt.breaker, end=received)
                out += self._one(self._learn(received, breaker))
        if not (continuous and halt.fresh(received, symbols)):
            halt.fresh_since = None
        elif gap or halt.fresh_since is None:
            halt.fresh_since = received
        elif received - halt.fresh_since >= REOPEN_HOLD:
            out += self._release(received)
        return tuple(out)

    def _again(self, received: datetime) -> Window | None:
        """The halt on again from ``received`` (a later stage, a halted trade): its reopening starts
        over, back to ``정지`` if it was further on."""
        halt = self._halt
        assert halt is not None
        halt.reset(received)
        if halt.window.detail == HALTED:
            return None
        return self._halt_version(received, halt.window.end, HALTED)

    def _release(self, received: datetime) -> tuple[Window, ...]:
        """The halt over at ``received``: its window ends there (``재개``)."""
        released = self._one(self._halt_version(received, received, REOPENED))
        self._halt = None  # a later stage starts afresh
        return released

    def _confirm(self, received: datetime, session: Session) -> Window | None:
        """The market halt confirmed at ``received``, if the breaker stage pending and a halted trade
        meet (module doc): a trade received from a minute before the stage was called to the end of
        the stage's window by the rules. Its window starts at the first such trade's time and lasts
        to the session's close while the reopening is not known."""
        if self._pending is None:
            return None
        breaker, called_at = self._pending
        flags = [traded for traded, r in self._flags if called_at - HOLD <= r < breaker.end]
        if not flags or received >= breaker.end:
            return None
        self._pending = None
        # To the close while the reopening is not known: no halt outlives the session.
        window = Window(Kind.MARKET_HALT, None, min(flags), session.close, HALTED)
        self._halt = _Halt(window, breaker, received)
        return self._learn(received, window)

    def _halt_version(self, received: datetime, end: datetime, phase: str) -> Window | None:
        """A later version of the market halt's window: its ``phase``, ending at ``end``."""
        halt = self._halt
        assert halt is not None
        halt.window = Window(Kind.MARKET_HALT, None, halt.window.start, end, phase)
        return self._learn(received, halt.window)

    def known(self, before: datetime) -> list[Window]:
        """The windows as known from what was received before ``before``: the latest of each."""
        return [w for _, w in self.latest(before)]

    def latest(self, before: datetime | None = None) -> list[tuple[datetime, Window]]:
        """The latest version of each window received before ``before`` (of all, if None), with
        when it was received, in the order first learned: what the engine keeps (#236)."""
        latest: dict[tuple[Kind, str | None, datetime], tuple[datetime, Window]] = {}
        for received_at, w in self._learned:
            if before is not None and received_at >= before:
                break
            latest[(w.kind, w.symbol, w.start)] = (received_at, w)
        return list(latest.values())

    def versions(self) -> list[tuple[datetime, Window]]:
        """Every version of every window learned (of today and the windows reaching it), with when it
        was received, in that order: the fill marks a day decided again with them (#625)."""
        return list(self._learned)

    @staticmethod
    def latest_of(versions: Sequence[tuple[datetime, Window]]) -> list[tuple[datetime, Window]]:
        """The latest of each window of ``versions`` (in receipt order), in the order first learned."""
        latest: dict[tuple[Kind, str | None, datetime], tuple[datetime, Window]] = {}
        for received_at, w in versions:
            latest[(w.kind, w.symbol, w.start)] = (received_at, w)
        return list(latest.values())

    def learned_since(self, at: datetime) -> list[Window]:
        """The windows learned at or after ``at``, each version (a VI's end changes when it is
        released): the engine marks what they touch special when it learns them.
        """
        return [w for received_at, w in self._learned if received_at >= at]

    def _today(self, at: datetime) -> Session | None:
        """The session of ``at``'s day; a new day starts the rules' state afresh."""
        day = at.astimezone(KST).date()
        if self._session is None or self._session.day != day:
            self._session = self._calendar.session(day)
            self._breaker = _Breaker()
            self._sidecar = _Sidecar()
            self._reset_halt()
            # Windows of earlier days touch nothing predicted from now on.
            self._learned = [(r, w) for r, w in self._learned if w.end.astimezone(KST).date() >= day]
        return self._session

    def _learn(self, received_at: datetime, window: Window) -> Window | None:
        key = (window.kind, window.symbol, window.start)
        same = [w for _, w in self._learned if (w.kind, w.symbol, w.start) == key]
        if same and same[-1] == window:
            return None
        self._learned.append((received_at, window))
        return window


def in_vi(windows: Sequence[Window], symbol: str, at: datetime) -> bool:
    """Whether ``symbol`` was in a VI at ``at``, by ``windows``."""
    return any(w.kind is Kind.VI and w.symbol == symbol and w.start <= at < w.end for w in windows)


def touching(windows: Sequence[Window], symbol: str, start: datetime, end: datetime) -> bool:
    """Whether any of ``windows`` meets the open span from ``start`` to ``end`` of ``symbol``."""
    return any(w.touches(symbol, start, end) for w in windows)
