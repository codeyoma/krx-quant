"""The US daily closes the daily kinds learn from (#264): ten years of the Philadelphia
semiconductor index, NVIDIA, Micron and the VIX, from the Yahoo chart API, the macro read's
source (``macro.py``).

The macro read (07:30) records each morning's move of these, which the engine reads at 08:50
(``macro.reading_before``). Training needs the same moves for every day of ten years, so the
collector's history task (at start and at 20:30, when the macro reads are on: ``KRX_MACRO_AT``)
keeps each one's daily closes in ``data/us_daily/symbol=<indicator>/closes.parquet``: the whole
ten years at first (``range=10y``), the last month after (``range=1mo``), merged by day (a day
fetched again replaces the stored one). It is market data kept on the server only: not
uploaded, fetched again if lost.

A day's move (``move_before``) is the macro read's: from the last close dated before the KRX
trading day before to the last one dated before the day (exchange dates, so a US session that
closed in the early morning, Seoul, counts for that morning), each no more than seven days
older than its day; a US holiday adds nothing, a KRX holiday adds every session since.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path

import aiohttp
import pyarrow as pa
import pyarrow.parquet as pq

from krx_quant.config import Settings
from krx_quant.macro import _YAHOO_HEADERS, INDICATORS, Observation, SourceError, latest_before, parse_yahoo

log = logging.getLogger(__name__)

# The indicators kept, by the macro read's names.
US_DAILY = ("sox", "nvda", "mu", "vix")
FIRST_RANGE, NEXT_RANGE = "10y", "1mo"
_TIMEOUT = aiohttp.ClientTimeout(total=30)
SCHEMA = pa.schema([("day", pa.date32()), ("close", pa.float64())])


def us_daily_path(data_dir: Path, name: str) -> Path:
    return data_dir / "us_daily" / f"symbol={name}" / "closes.parquet"


def read_us_daily(data_dir: Path, name: str) -> list[Observation]:
    """The stored closes of ``name``, oldest first; none without a file."""
    path = us_daily_path(data_dir, name)
    if not path.exists():
        return []
    return [Observation(r["day"], float(r["close"])) for r in pq.read_table(path).to_pylist()]


def write_us_daily(data_dir: Path, name: str, closes: Sequence[Observation]) -> None:
    path = us_daily_path(data_dir, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    rows = [{"day": o.day, "close": o.value} for o in sorted(closes)]
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), tmp)
    tmp.replace(path)


def move_before(closes: Sequence[Observation], day: date, previous: date | None) -> float | None:
    """The move (a fraction) known on the morning of ``day`` since that of ``previous`` (the
    KRX trading day before), as the macro read's ``change_pct``; None without both closes."""
    if previous is None:
        return None
    current, base = latest_before(closes, day), latest_before(closes, previous)
    if current is None or base is None or base.value == 0:
        return None
    return current.value / base.value - 1


def level_before(closes: Sequence[Observation], day: date) -> float | None:
    """The last close known on the morning of ``day`` (the VIX's level); None if none is recent."""
    found = latest_before(closes, day)
    return None if found is None else found.value


class UsDailyKeeper:
    """Keeps the US daily closes up to date, a run at a time (module doc)."""

    def __init__(self, settings: Settings) -> None:
        self._data_dir = settings.data_dir
        self._url = settings.yahoo_url
        self._series = {i.name: i.series for i in INDICATORS if i.name in US_DAILY}

    async def run(self, now: datetime) -> int:
        """Fetch and merge every indicator's closes; how many files changed. One that fails is
        logged and left for the next run."""
        changed = 0
        async with aiohttp.ClientSession() as session:
            for name, series in self._series.items():
                try:
                    changed += await self._update(session, name, series, now)
                except (
                    aiohttp.ClientError,
                    SourceError,
                    TimeoutError,
                    ValueError,
                    KeyError,
                    TypeError,
                    OSError,
                ) as e:
                    log.warning("US daily closes of %s not updated: %s", name, e)
        return changed

    async def _update(self, session: aiohttp.ClientSession, name: str, series: str, now: datetime) -> bool:
        stored = read_us_daily(self._data_dir, name)
        span = NEXT_RANGE if stored else FIRST_RANGE
        url = f"{self._url}/v8/finance/chart/{series}"
        params = {"range": span, "interval": "1d"}
        async with session.get(url, params=params, headers=_YAHOO_HEADERS, timeout=_TIMEOUT) as response:
            if response.status >= 400:
                raise ValueError(f"HTTP {response.status}")
            body = await response.json(content_type=None)
        fetched = parse_yahoo(body, now)
        if not fetched:
            return False
        merged = {o.day: o for o in stored} | {o.day: o for o in fetched}
        closes = sorted(merged.values())
        if closes == stored:
            return False
        write_us_daily(self._data_dir, name, closes)
        return True
