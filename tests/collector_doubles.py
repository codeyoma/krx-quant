"""The collector on the fake KIS (``fake_kis.FakeKis``) for the tests (#511): its settings, a run
until every scripted frame is handled, the rows it recorded on the fixed day, a settable clock, and
the derivatives masters it reads. Every value is made up.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow.parquet as pq

from krx_quant.collector import run_collector
from krx_quant.config import Settings
from krx_quant.fake_kis import FakeKis
from tests.kis_frames import master_zips

KST = ZoneInfo("Asia/Seoul")
FIXED_NOW = datetime(2026, 10, 8, 9, 30, 16, tzinfo=KST)


def settings_for(kis: FakeKis, data_dir: Path, symbols: tuple[str, ...] = ("005930",)) -> Settings:
    return Settings(
        kis_app_key=kis.app_key,
        kis_app_secret=kis.app_secret,
        kis_rest_url=kis.rest_url,
        kis_ws_url=kis.ws_url,
        symbols=symbols,
        etf_symbols=(),
        index_codes=(),
        us_symbols=(),
        venue_trades=False,
        master_url=kis.master_url,
        state_dir=data_dir / "state",
        foreign_estimate_poll_seconds=3600,
        data_dir=data_dir,
    )


async def collect(kis: FakeKis, settings: Settings, now: datetime = FIXED_NOW) -> None:
    """Run the collector until every scripted frame has been handled, then stop it."""
    stop = asyncio.Event()
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=lambda: now))
    await kis.wait_for_subscriptions(set(kis.script))
    await kis.round_trip()
    stop.set()
    await asyncio.wait_for(task, 5)


def read(data_dir: Path, layer: str, feed: str, symbol: str = "005930") -> list[dict[str, Any]]:
    path = data_dir / layer / "date=2026-10-08" / f"feed={feed}" / f"symbol={symbol}"
    rows: list[dict[str, Any]] = pq.read_table(path).to_pylist()
    return rows


TRADE_KEY = ("H0UNCNT0", "005930")


class Clock:
    """A settable clock for the collector (time is a system boundary)."""

    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


# Nearest expiry 202610 with ATM 1,072.5; two strikes each side are recorded, the rest are not.
STRIKES = ["1,065.0", "1,067.5", "1,070.0", "1,072.5", "1,075.0", "1,077.5", "1,080.0"]
ATM = "1,072.5"
CALLS = [f"B01610C{i}" for i in range(len(STRIKES))]
PUTS = [f"C01610P{i}" for i in range(len(STRIKES))]


def options_of(month: str, calls: list[str], puts: list[str]) -> list[tuple[str, str, str, bool]]:
    """A month's options in the master, one call and one put at each strike."""
    rows = [(c, "C", f"{month} {k}", k == ATM) for c, k in zip(calls, STRIKES, strict=True)]
    return rows + [(p, "P", f"{month} {k}", k == ATM) for p, k in zip(puts, STRIKES, strict=True)]


def serve_masters(kis: FakeKis, options: list[tuple[str, str, str, bool]] | None = None) -> None:
    if options is None:
        options = [
            *options_of("202610", CALLS, PUTS),
            ("B01611C3", "C", f"202611 {ATM}", True),
        ]  # later: ignored
    kis.masters = master_zips(
        kospi200_futures=[("A01703", "2"), ("A01612", "1")],
        usd_futures=[("A75611", "2"), ("A75610", "1")],
        options=options,
    )
