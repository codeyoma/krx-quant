"""The server's and the engine's memory by hour, kept in the state folder (#623).

The engine's heartbeat says the memory each minute, to Loki alone (``heartbeat.py``), which the
server's own processes cannot read. So that a scheduled job can post an evening's figures on an
issue (``session_memory_probe.py``), the same readings are kept here as each clock hour's extremes:
the lowest memory available, the most swap in use, the engine's largest resident memory and its
most in swap, with how many readings the hour had. ``state/memory-hours.json`` holds the latest
``KEEP_HOURS``; each reading rewrites it (about 10 kB, written aside and renamed). MB only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from krx_quant.feeds.base import KST

FILE = "memory-hours.json"
KEEP_HOURS = 72


@dataclass(frozen=True)
class Hour:
    """A clock hour's readings: a figure no reading had is None."""

    readings: int
    available_min: float | None  # the server's ``MemAvailable``
    swap_max: float | None  # the server's swap in use
    engine_max: float | None  # the engine's resident memory
    engine_swapped_max: float | None  # the engine's memory in swap


def _key(at: datetime) -> str:
    return at.astimezone(KST).strftime("%Y-%m-%dT%H")


def _lower(a: float | None, b: float | None) -> float | None:
    return b if a is None else a if b is None else min(a, b)


def _higher(a: float | None, b: float | None) -> float | None:
    return b if a is None else a if b is None else max(a, b)


def read(state_dir: Path) -> dict[str, Hour]:
    """The hours kept, by ``YYYY-MM-DDTHH`` (KST), oldest first; none if the file is not there or
    cannot be read."""
    try:
        kept = json.loads((state_dir / FILE).read_text())
        return {key: Hour(**values) for key, values in sorted(kept.items())}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def note(
    state_dir: Path,
    at: datetime,
    available: float | None,
    swap: float | None,
    engine: float | None,
    engine_swapped: float | None,
) -> None:
    """Take one reading into its hour and keep the file. A file that cannot be written is left:
    the heartbeat goes on."""
    hours = read(state_dir)
    before = hours.get(_key(at), Hour(0, None, None, None, None))
    hours[_key(at)] = Hour(
        before.readings + 1,
        _lower(before.available_min, available),
        _higher(before.swap_max, swap),
        _higher(before.engine_max, engine),
        _higher(before.engine_swapped_max, engine_swapped),
    )
    latest = dict(sorted(hours.items())[-KEEP_HOURS:])
    path = state_dir / FILE
    tmp = path.with_name(f".{FILE}.tmp")
    try:
        tmp.write_text(json.dumps({key: vars(hour) for key, hour in latest.items()}))
        tmp.replace(path)
    except OSError:
        pass
