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
same way. A market this far off is special whether or not the exchange's own rules (such
as no breaker in the last 40 minutes) let it halt.

Each is a window of exchange time. A window is known from the moment its event is
received, and only what was received before a moment counts at that moment: live and
replay see the same thing. A window learned later replaces the one learned before it for
the same start (a VI's release becomes known).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from krx_quant.feeds.base import KST

# A VI whose release is not known yet is taken to last this long: two minutes and the
# random end of up to 30 seconds, with a little room.
VI_LONGEST = timedelta(minutes=3)

KOSPI = "0001"
HOLD = timedelta(minutes=1)  # how long a move must last before a breaker or sidecar
# The drops from the previous close (%) that call the breaker's stages, and how far below
# the stage before each later one must be (points).
BREAKER_STAGES = (8.0, 15.0, 20.0)
BREAKER_STEP = 1.0
BREAKER_HALT = timedelta(minutes=30)  # 20 minutes' halt and the 10-minute auction after it
SIDECAR_MOVE = 5.0
SIDECAR_HALT = timedelta(minutes=5)


@dataclass
class _Held:
    """A condition on a market feed that must hold for a minute."""

    since: datetime | None = None

    def check(self, met: bool, at: datetime) -> datetime | None:
        """The moment it has held a minute, if ``at`` is that moment or later; else None."""
        if not met:
            self.since = None
            return None
        if self.since is None:
            self.since = at
        return self.since + HOLD if at - self.since >= HOLD else None


@dataclass
class _Day:
    """The market rules' state for one day."""

    day: date
    breaker_stage: int = 0  # stages called so far
    breaker_level: float = 0.0  # the drop (%) the last stage was called at
    breaker: _Held = field(default_factory=_Held)
    sidecar_called: bool = False
    sidecar: _Held = field(default_factory=_Held)


@dataclass(frozen=True)
class Window:
    kind: str  # vi, circuit_breaker, sidecar
    symbol: str | None  # None: the whole market
    start: datetime
    end: datetime  # the release, or the longest it can last while that is not known


class SpecialSituations:
    def __init__(self) -> None:
        # (received at, window), in the order received.
        self._learned: list[tuple[datetime, Window]] = []
        self._market: _Day | None = None

    def vi(self, row: dict[str, Any]) -> Window | None:
        """Take a VI status row; the window if it is new or changed, else None."""
        start: datetime = row["event_time"]
        end = row["end_time"] or start + VI_LONGEST
        return self._learn(row["received_at"], Window("vi", row["symbol"], start, end))

    def index(self, row: dict[str, Any]) -> Window | None:
        """Take a market index tick; a circuit breaker's window if one is called."""
        change = row.get("change_pct")
        if row["symbol"] != KOSPI or change is None:
            return None
        at: datetime = row["event_time"]
        day = self._day(at)
        if day.breaker_stage == len(BREAKER_STAGES):
            return None
        drop = -change
        needed = BREAKER_STAGES[day.breaker_stage]
        met = drop >= needed and (day.breaker_stage == 0 or drop >= day.breaker_level + BREAKER_STEP)
        called = day.breaker.check(met, at)
        if called is None:
            return None
        day.breaker_stage += 1
        day.breaker_level = drop
        day.breaker = _Held()
        last = day.breaker_stage == len(BREAKER_STAGES)  # the day is over
        end = datetime.combine(day.day, time.max, KST) if last else called + BREAKER_HALT
        return self._learn(row["received_at"], Window("circuit_breaker", None, called, end))

    def futures(self, row: dict[str, Any]) -> Window | None:
        """Take a KOSPI200 futures trade; a sidecar's window if one is called."""
        change = row.get("change_pct")
        if change is None:
            return None  # recorded before the change was kept
        at: datetime = row["event_time"]
        day = self._day(at)
        if day.sidecar_called:
            return None
        called = day.sidecar.check(abs(change) >= SIDECAR_MOVE, at)
        if called is None:
            return None
        day.sidecar_called = True
        return self._learn(row["received_at"], Window("sidecar", None, called, called + SIDECAR_HALT))

    def in_vi(self, symbol: str, at: datetime, known_before: datetime) -> bool:
        """Whether ``symbol`` was in a VI at ``at``, from what was received before ``known_before``."""
        return any(
            w.kind == "vi" and w.symbol == symbol and w.start <= at < w.end for w in self.known(known_before)
        )

    def touches(self, symbol: str, start: datetime, end: datetime, known_before: datetime) -> bool:
        """Whether any window of ``symbol`` or the whole market falls in ``[start, end]``."""
        return any(
            w.symbol in (symbol, None) and w.start < end and w.end > start for w in self.known(known_before)
        )

    def known(self, before: datetime) -> list[Window]:
        """The windows as known from what was received before ``before``: the latest of each."""
        latest: dict[tuple[str, str | None, datetime], Window] = {}
        for received_at, w in self._learned:
            if received_at >= before:
                break
            latest[(w.kind, w.symbol, w.start)] = w
        return list(latest.values())

    def _day(self, at: datetime) -> _Day:
        day = at.astimezone(KST).date()
        if self._market is None or self._market.day != day:
            self._market = _Day(day)
        return self._market

    def _learn(self, received_at: datetime, window: Window) -> Window | None:
        same = [
            w
            for _, w in self._learned
            if (w.kind, w.symbol, w.start) == (window.kind, window.symbol, window.start)
        ]
        if same and same[-1] == window:
            return None
        self._learned.append((received_at, window))
        return window
