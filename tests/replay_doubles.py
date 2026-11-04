"""Replayed days for the tests (#511): a busy morning of books and trades, every prediction it
stored, the recorder's files of a day's events, a VI and ticks held off the previous close, the
06:00 deploy after the day, and an algorithm's predictions read through the Engine API. Every value
is made up.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa

from krx_quant.engine import Event
from krx_quant.feeds import INDEX_REST, VI_STATUS, WS_FEEDS
from krx_quant.recorder import ParquetRecorder
from tests.replay_events import (
    DAY,
    SYMBOL,
    api,
    at,
    book,
    every,
    get_json,
    trade,
    vi,
)


def busy_morning(day: date = DAY, end: str = "10:00:00") -> list[Event]:
    """A book and trades that wander, so every baseline and class shows up."""
    events: list[Event] = []
    when, mid = at("09:00:00", day), 81_250
    step = 0
    while when <= at(end, day):
        step += 1
        mid += 100 * ((step * 7919) % 5 - 2)  # a deterministic walk of -2..+2 ticks
        events.append(book(when, mid - 50, mid + 50))
        events.append(trade(when + timedelta(seconds=1), mid + 50, step % 9 + 1))
        when += timedelta(seconds=13)
    return events


def stored(db: Path) -> list[tuple[Any, ...]]:
    """Every prediction with its outcome, without row IDs: algorithms finish in any order."""
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT p.symbol, p.algorithm, p.model_version, p.bar_close, p.displayed_at, p.p_up, p.p_flat,"
            " p.p_down, p.expected_ticks, p.status, o.target_at, o.mid_close, o.mid_display, o.mid_target,"
            " o.flat_ticks, o.class_a, o.class_b, o.special, o.unscorable, o.scored_at"
            " FROM predictions p LEFT JOIN outcomes o ON o.prediction_id = p.id"
            " ORDER BY p.bar_close, p.symbol, p.algorithm"
        ).fetchall()


END_OF_DAY = at("23:59:59.999999")
SCHEMAS = {
    **{f: feed.schema for f, feed in WS_FEEDS.items()},
    **{f.tr_id: f.schema for f in (VI_STATUS, INDEX_REST)},
}


def record(
    data_dir: Path, events: list[Event], schemas: dict[str, pa.Schema] = SCHEMAS, *, day: date = DAY
) -> None:
    recorder = ParquetRecorder(data_dir, flush_rows=50, flush_seconds=3600)
    for feed, row in events:
        recorder.record("events", day, feed, row["symbol"], row, schemas[feed])
    recorder.close()


# A VI from 09:30:20 to 09:32:40, seen on by 09:30:24 and released by 09:32:44.
VI = [vi(at("09:30:24"), "09:30:20"), vi(at("09:32:44"), "09:30:20", "09:32:40")]


def held(start: str, end: str, make: Any, change_pct: float) -> list[Any]:
    """Ticks every 5 seconds from ``start`` to ``end``, all ``change_pct`` off the previous close."""
    return every(at(start), at(end), 5, lambda when: make(when, change_pct))


NEXT_MORNING = at("06:00:00", DAY + timedelta(days=1))  # the 06:00 deploy after the day


async def predictions_of(engine: Any, algorithm: str, limit: int = 1000) -> list[dict[str, Any]]:
    async with api(engine) as client:
        body = await get_json(client, f"/v1/symbols/{SYMBOL}/predictions?algorithm={algorithm}&limit={limit}")
    predictions: list[dict[str, Any]] = body["predictions"]
    return predictions
