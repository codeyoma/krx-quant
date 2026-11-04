"""A night's training inputs for the tests (#511): eight trading days of archived bars and events,
the night they are trained on, when the run writes its list, and a file it wrote read back. Every
value is made up.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from krx_quant.archive import archive
from krx_quant.backfill import BAR_SCHEMA, MINUTE_BARS_TR
from krx_quant.engine import Event
from krx_quant.feeds import KST
from krx_quant.object_store import LocalObjectStore
from tests.replay_doubles import record
from tests.synthetic_bars import trending

FEE = 0.015 / 100
# Eight trading days: the last three validate, the four before them train (the stand-ins
# train on 4), and the first is too old to be used.
DAYS = [d for d in (date(2026, 9, 14) + timedelta(days=i) for i in range(12)) if d.weekday() < 5][:8]
NIGHT = DAYS[-1]
FINISHED = datetime.combine(NIGHT, time(21, 40), KST)  # when the run writes its list


def archive_bars(root: Path, day: date, symbol: str = "005930", ticks_per_minute: float = 1.0) -> None:
    """A rising (or falling) day of bars, where the archive puts it."""
    rows = [
        {
            "symbol": symbol,
            "bar_time": b.start,
            "venue": "UN",
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
            "cum_value": 0,
        }
        for b in trending(day, ticks_per_minute, seed=day.day)
    ]
    path = root / "bars" / f"date={day}" / f"feed={MINUTE_BARS_TR}" / f"symbol={symbol}" / "bars.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=BAR_SCHEMA), path)


def archive_events(root: Path, day: date, events: list[Event], work: Path) -> None:
    """A recorded day of ``events``, stored where the archive puts it."""
    data = work / f"recorded-{day}"
    record(data, sorted(events, key=lambda e: e[1]["received_at"]), day=day)
    asyncio.run(archive(data, LocalObjectStore(root)))


def read(root: Path, key: str) -> Any:
    return json.loads((root / key).read_text())
