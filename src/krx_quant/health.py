"""Feed health: gaps in what was recorded, and why.

A gap opens for a WebSocket subscription when the connection drops (closed again
once KIS acknowledges the subscription after the reconnect) and when a feed goes
quiet in continuous trading, per the Market Calendar, for longer than its threshold
(closed by the next message).
Closed gaps are written to the ``gaps`` layer; gaps still open at shutdown are
written with no end.
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime

import pyarrow as pa

from krx_quant.feeds.base import KST, kst_day
from krx_quant.market_calendar import MarketCalendar, Phase
from krx_quant.recorder import ParquetRecorder

log = logging.getLogger(__name__)

Key = tuple[str, str]  # (tr_id, key)

GAP_SCHEMA = pa.schema(
    [
        ("feed", pa.string()),
        ("symbol", pa.string()),
        ("start", pa.timestamp("us", tz="Asia/Seoul")),
        ("end", pa.timestamp("us", tz="Asia/Seoul")),
        ("reason", pa.string()),
    ]
)


class GapKind(enum.Enum):
    DISCONNECTED = "disconnected"
    STALE = "stale"


# Silence that counts as stale, by feed. Busy feeds go quiet only on trouble (a VI
# auction halts trades for 2–2.5 minutes); member flow, US daytime trades, options,
# NXT fills and the REST polls are naturally sparse.
STALE_SECONDS: dict[str, float] = {
    "H0UNCNT0": 180,
    "H0UNASP0": 60,
    "H0IFCNT0": 60,
    "H0UPCNT0": 120,
    "H0UNPGM0": 300,
    "H0CFCNT0": 300,
    "H0STCNT0": 300,
    "H0NXCNT0": 600,
    "H0UNMBC0": 900,
    "HDFSCNT0": 900,
    "H0IOCNT0": 1800,
    "HHPTJ04160200": 1200,
}
DEFAULT_STALE_SECONDS = 300.0

# Feeds that keep their own hours (the US daytime session starts at 10:00 KST outside US
# summer time and skips US holidays): silence counts only once they delivered that day.
OWN_HOURS = frozenset({"HDFSCNT0"})


@dataclass
class _Gap:
    kind: GapKind
    start: datetime
    detail: str = ""

    @property
    def reason(self) -> str:
        return f"{self.kind.value}: {self.detail}" if self.detail else self.kind.value


@dataclass(frozen=True)
class FeedState:
    feed: str
    key: str
    last_received_at: datetime | None
    gap: str | None  # the reason of the open gap, if any


class FeedHealth:
    def __init__(
        self, recorder: ParquetRecorder, now: Callable[[], datetime], calendar: MarketCalendar
    ) -> None:
        self._recorder = recorder
        self._now = now
        self._calendar = calendar
        self._open: dict[Key, _Gap] = {}
        self._last_seen: dict[Key, datetime] = {}
        self._watched: dict[Key, datetime] = {}  # key -> when it started being expected

    def watch(self, key: Key) -> None:
        """``key`` is registered or polled from now on, so it should deliver in market hours."""
        self._watched.setdefault(key, self._now())

    def connection_lost(self, keys: Iterable[Key], detail: str) -> None:
        at = self._now()
        for key in keys:
            self._open.setdefault(key, _Gap(GapKind.DISCONNECTED, at, detail))

    def subscribed(self, key: Key) -> None:
        """KIS acknowledged the subscription: a gap from a dropped connection ends here."""
        self._close_if(key, GapKind.DISCONNECTED, self._now())

    def covered_elsewhere(self, key: Key) -> None:
        """The key moved to REST polling: an open disconnect gap ends now and the key is no
        longer expected to deliver.
        """
        self._close_if(key, GapKind.DISCONNECTED, self._now())
        self._watched.pop(key, None)
        self._last_seen.pop(key, None)

    def received(self, key: Key) -> None:
        at = self._now()
        self._last_seen[key] = at
        self._close_if(key, GapKind.STALE, at)

    def check_stale(self) -> list[Key]:
        """Open a stale gap for every feed silent past its threshold in continuous trading;
        return the new ones.

        Silence counts from the open, or from when the feed was registered or last
        delivered if later. A feed that has not delivered at all today counts only on a day
        KIS confirmed as a trading day, and never for feeds that keep their own hours.
        """
        at = self._now()
        today = kst_day(at)
        session = self._calendar.session(today)
        if session is None or session.phase(at) is not Phase.CONTINUOUS:
            return []
        confirmed = self._calendar.knows(today)
        newly: list[Key] = []
        for key in self._watched.keys() | self._last_seen.keys():
            if key in self._open:
                continue
            seen = self._last_seen.get(key)
            if (seen is None or kst_day(seen) != today) and (not confirmed or key[0] in OWN_HOURS):
                continue
            since = max(t for t in (session.open, seen, self._watched.get(key)) if t is not None)
            if (at - since).total_seconds() > STALE_SECONDS.get(key[0], DEFAULT_STALE_SECONDS):
                self._open[key] = _Gap(GapKind.STALE, since)
                newly.append(key)
        return newly

    def open_gaps(self, kind: GapKind) -> list[Key]:
        return [key for key, gap in self._open.items() if gap.kind is kind]

    def snapshot(self) -> list[FeedState]:
        """Every expected or delivering feed: when it last delivered and any open gap."""
        keys = sorted(self._watched.keys() | self._last_seen.keys())
        return [
            FeedState(
                key[0],
                key[1],
                self._last_seen.get(key),
                gap.reason if (gap := self._open.get(key)) is not None else None,
            )
            for key in keys
        ]

    def close(self) -> None:
        """Write gaps that are still open, with no end."""
        for key in list(self._open):
            self._close(key, None)

    def _close_if(self, key: Key, kind: GapKind, end: datetime) -> None:
        gap = self._open.get(key)
        if gap is not None and gap.kind is kind:
            self._close(key, end)

    def _close(self, key: Key, end: datetime | None) -> None:
        gap = self._open.pop(key)
        row = {
            "feed": key[0],
            "symbol": key[1],
            "start": gap.start.astimezone(KST),
            "end": end.astimezone(KST) if end is not None else None,
            "reason": gap.reason,
        }
        log.info("gap %s %s %s – %s (%s)", key[0], key[1], row["start"], row["end"], gap.reason)
        self._recorder.record("gaps", kst_day(gap.start), key[0], key[1], row, GAP_SCHEMA)
