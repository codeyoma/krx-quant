"""Special situations: when trading was not normal, for one symbol or the whole market.

- **VI** (volatility interruption): a symbol's trading turns into a two-minute
  single-price auction (released at a random moment up to 30 seconds later). Known from
  the VI status the collector polls: its start, and its release once it is over.

Each is a window of exchange time. A window is known from the moment its event is
received, and only what was received before a moment counts at that moment: live and
replay see the same thing. A window learned later replaces the one learned before it for
the same start (a VI's release becomes known).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

# A VI whose release is not known yet is taken to last this long: two minutes and the
# random end of up to 30 seconds, with a little room.
VI_LONGEST = timedelta(minutes=3)


@dataclass(frozen=True)
class Window:
    kind: str  # vi
    symbol: str | None  # None: the whole market
    start: datetime
    end: datetime  # the release, or the longest it can last while that is not known


class SpecialSituations:
    def __init__(self) -> None:
        # (received at, window), in the order received.
        self._learned: list[tuple[datetime, Window]] = []

    def vi(self, row: dict[str, Any]) -> Window | None:
        """Take a VI status row; the window if it is new or changed, else None."""
        start: datetime = row["event_time"]
        end = row["end_time"] or start + VI_LONGEST
        return self._learn(row["received_at"], Window("vi", row["symbol"], start, end))

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
