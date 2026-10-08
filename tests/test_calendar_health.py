"""Stale-feed detection on the Market Calendar, observed through the fake KIS seam."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from krx_quant.collector import run_collector
from krx_quant.config import Settings
from krx_quant.fake_kis import FakeKis
from krx_quant.feeds.base import KST
from tests.kis_rest import HOLIDAYS, holiday_api
from tests.test_collector import settings_for
from tests.test_resilience import TRADE_KEY, Clock
from tests.test_subscription_limit import until


def gaps(data_dir: Path, day: date, feed: str) -> list[dict[str, Any]]:
    path = data_dir / "gaps" / f"date={day.isoformat()}" / f"feed={feed}" / "symbol=005930"
    rows: list[dict[str, Any]] = pq.read_table(path).to_pylist() if path.exists() else []
    return rows


def watching(kis: FakeKis, tmp_path: Path, closed: Iterable[date] = ()) -> Settings:
    kis.rest_responses[HOLIDAYS] = holiday_api(closed)
    kis.script[TRADE_KEY] = []  # registered, never delivers
    return replace(settings_for(kis, tmp_path), health_check_seconds=0.05, backfill_days=1)


async def run_until_the_calendar_is_read(settings: Settings, clock: Clock) -> tuple[asyncio.Event, Any]:
    stop = asyncio.Event()
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=clock))
    await until(lambda: (settings.state_dir / "market_calendar.json").exists())
    return stop, task


async def test_on_a_trading_day_a_subscription_that_never_delivers_goes_stale(
    kis: FakeKis, tmp_path: Path
) -> None:
    start = datetime(2026, 10, 8, 9, 30, 16, tzinfo=KST)
    clock = Clock(start)
    stop, task = await run_until_the_calendar_is_read(watching(kis, tmp_path), clock)

    clock.at = start + timedelta(minutes=2)  # under the 180s threshold for trades
    await asyncio.sleep(0.2)
    assert not gaps(tmp_path, start.date(), "H0UNCNT0")
    clock.at = start + timedelta(minutes=4)
    await asyncio.sleep(0.2)
    stop.set()
    await asyncio.wait_for(task, 5)

    [gap] = gaps(tmp_path, start.date(), "H0UNCNT0")
    assert (gap["start"], gap["end"], gap["reason"]) == (start, None, "stale"), "silent since registered"


async def test_on_a_holiday_silent_subscriptions_are_not_stale(kis: FakeKis, tmp_path: Path) -> None:
    holiday = datetime(2026, 10, 9, 10, 0, tzinfo=KST)  # Hangul Day, a Friday
    clock = Clock(holiday)
    stop, task = await run_until_the_calendar_is_read(watching(kis, tmp_path, {holiday.date()}), clock)

    clock.at = holiday + timedelta(hours=1)
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(task, 5)

    assert not (tmp_path / "gaps").exists()


async def test_on_the_exam_day_silence_counts_from_the_ten_oclock_open(kis: FakeKis, tmp_path: Path) -> None:
    before_open = datetime(2026, 11, 19, 9, 58, tzinfo=KST)
    clock = Clock(before_open)
    stop, task = await run_until_the_calendar_is_read(watching(kis, tmp_path), clock)

    clock.at = before_open.replace(hour=10, minute=2)  # four minutes since registered, two since the open
    await asyncio.sleep(0.2)
    assert not gaps(tmp_path, before_open.date(), "H0UNCNT0")
    clock.at = before_open.replace(hour=10, minute=4)
    await asyncio.sleep(0.2)
    stop.set()
    await asyncio.wait_for(task, 5)

    [gap] = gaps(tmp_path, before_open.date(), "H0UNCNT0")
    assert gap["start"] == before_open.replace(hour=10, minute=0)
