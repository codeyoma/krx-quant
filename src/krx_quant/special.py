"""Special situations: when trading was not normal, for one symbol or the whole market.

- **VI** (volatility interruption): a symbol's trading turns into a two-minute
  single-price auction (released at a random moment up to 30 seconds later). Known from
  the VI status the collector polls: its start, and its release once it is over.
- **Circuit breaker** (KOSPI): the index 8% below the previous close for a minute halts
  all trading for 20 minutes, then a 10-minute single-price auction reopens it. Again at
  15% and at 20% (each also 1 point below the stage before); the third ends the day.
- **Sidecar** (KOSPI): KOSPI200 futures 5% off the previous close for a minute halt
  program trading for 5 minutes, once a day.

Circuit breakers and sidecars are not read from KIS: they are called by the exchange's
rules from the recorded KOSPI index and KOSPI200 futures, so a replay calls them the
same way. Neither is called in the first 5 minutes of the session; sidecars and the
first two breaker stages not in the last 40 minutes either (the third stage can end the
day at any time). A minute held only counts inside those hours: a gap open from the
futures' 08:45 start is called at 09:06, not before the stock market opens.

Each is a window of exchange time. A window is known from the moment its event is
received, and only what was received before a moment counts at that moment: live and
replay see the same thing. A window learned later replaces the one learned before it for
the same start (a VI's release becomes known). The rules' state is kept in memory: an
engine restarted during a breaker calls it again from what it receives after the
restart, like the bars it builds.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from krx_quant.feeds.base import KST
from krx_quant.market_calendar import MarketCalendar, Session

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


class Kind(enum.Enum):
    VI = "vi"
    CIRCUIT_BREAKER = "circuit_breaker"
    SIDECAR = "sidecar"


@dataclass(frozen=True)
class Window:
    kind: Kind
    symbol: str | None  # None: the whole market
    start: datetime
    end: datetime  # the release, or the least it can last while that is not known

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
            _in_hours(at, session, until_close=last)
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


def _in_hours(at: datetime, session: Session, *, until_close: bool) -> bool:
    """Whether the rules may call a halt at ``at``: from 5 minutes after the open, and
    before the last 40 minutes unless ``until_close``.
    """
    end = session.close if until_close else session.close - QUIET_BEFORE_CLOSE
    return session.open + QUIET_AFTER_OPEN <= at < end


class SpecialSituations:
    def __init__(self, calendar: MarketCalendar) -> None:
        self._calendar = calendar
        # (received at, window), in the order received.
        self._learned: list[tuple[datetime, Window]] = []
        self._session: Session | None = None
        self._breaker = _Breaker()
        self._sidecar = _Sidecar()

    def vi(self, row: dict[str, Any]) -> Window | None:
        """Take a VI status row; the window if it is new or changed, else None."""
        start: datetime = row["event_time"]
        released: datetime | None = row["end_time"]
        if row["active"] or released is None:
            # Still on: whatever release KIS shows, the random end may come later.
            end = max(released or start, start + VI_LONGEST)
        else:
            end = released
        return self._learn(row["received_at"], Window(Kind.VI, row["symbol"], start, end))

    def index(self, row: dict[str, Any]) -> Window | None:
        """Take a market index tick; a circuit breaker's window if one is called."""
        change = row["change_pct"]
        session = self._today(row["event_time"])
        if row["symbol"] != KOSPI or change is None or session is None:
            return None
        called = self._breaker.update(change, row["event_time"], session)
        if called is None:
            return None
        return self._learn(row["received_at"], Window(Kind.CIRCUIT_BREAKER, None, *called))

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

    def known(self, before: datetime) -> list[Window]:
        """The windows as known from what was received before ``before``: the latest of each."""
        latest: dict[tuple[Kind, str | None, datetime], Window] = {}
        for received_at, w in self._learned:
            if received_at >= before:
                break
            latest[(w.kind, w.symbol, w.start)] = w
        return list(latest.values())

    def _today(self, at: datetime) -> Session | None:
        """The session of ``at``'s day; a new day starts the rules' state afresh."""
        day = at.astimezone(KST).date()
        if self._session is None or self._session.day != day:
            self._session = self._calendar.session(day)
            self._breaker = _Breaker()
            self._sidecar = _Sidecar()
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
