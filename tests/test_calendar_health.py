"""Stale-feed detection on the Market Calendar, observed through the fake KIS seam."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest

from krx_quant.collector import run_collector
from krx_quant.config import Settings
from krx_quant.fake_kis import FakeKis
from krx_quant.feeds.base import KST
from krx_quant.health import FeedHealth, Key
from tests.collector_doubles import TRADE_KEY, Clock, settings_for
from tests.kis_rest import HOLIDAYS, holiday_api
from tests.waiting import until


def gaps(data_dir: Path, day: date, feed: str) -> list[dict[str, Any]]:
    path = data_dir / "gaps" / f"date={day.isoformat()}" / f"feed={feed}" / "symbol=005930"
    rows: list[dict[str, Any]] = pq.read_table(path).to_pylist() if path.exists() else []
    return rows


@dataclass
class Checks:
    """The collector's stale checks as they run: the gaps each one opened."""

    opened: list[list[Key]] = field(default_factory=list)

    async def next(self) -> None:
        """Until a check that began after now has run: it saw the clock as the test just set it."""
        ran = len(self.opened)
        await until(lambda: len(self.opened) > ran)

    def stale(self, key: Key) -> bool:
        return any(key in opened for opened in self.opened)


def health_checks(monkeypatch: pytest.MonkeyPatch) -> Checks:
    """Each stale check the collector runs (``FeedHealth.check_stale``, every
    ``health_check_seconds``), so a test that looks for nothing going stale waits for the next one."""
    checks = Checks()
    check_stale = FeedHealth.check_stale

    def recorded(health: FeedHealth) -> list[Key]:
        opened = check_stale(health)
        checks.opened.append(opened)
        return opened

    monkeypatch.setattr(FeedHealth, "check_stale", recorded)
    return checks


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
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checks = health_checks(monkeypatch)
    start = datetime(2026, 10, 8, 9, 30, 16, tzinfo=KST)
    clock = Clock(start)
    stop, task = await run_until_the_calendar_is_read(watching(kis, tmp_path), clock)

    clock.at = start + timedelta(minutes=2)  # under the 180s threshold for trades
    await checks.next()
    assert not checks.stale(TRADE_KEY)
    clock.at = start + timedelta(minutes=4)
    await until(lambda: checks.stale(TRADE_KEY))
    stop.set()
    await asyncio.wait_for(task, 5)

    [gap] = gaps(tmp_path, start.date(), "H0UNCNT0")
    assert (gap["start"], gap["end"], gap["reason"]) == (start, None, "stale"), "silent since registered"


async def test_on_a_holiday_silent_subscriptions_are_not_stale(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checks = health_checks(monkeypatch)
    holiday = datetime(2026, 10, 9, 10, 0, tzinfo=KST)  # Hangul Day, a Friday
    clock = Clock(holiday)
    stop, task = await run_until_the_calendar_is_read(watching(kis, tmp_path, {holiday.date()}), clock)

    clock.at = holiday + timedelta(hours=1)
    await checks.next()
    stop.set()
    await asyncio.wait_for(task, 5)

    assert not (tmp_path / "gaps").exists()


async def test_on_the_exam_day_silence_counts_from_the_ten_oclock_open(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checks = health_checks(monkeypatch)
    before_open = datetime(2026, 11, 19, 9, 58, tzinfo=KST)
    clock = Clock(before_open)
    stop, task = await run_until_the_calendar_is_read(watching(kis, tmp_path), clock)

    clock.at = before_open.replace(hour=10, minute=2)  # four minutes since registered, two since the open
    await checks.next()
    assert not checks.stale(TRADE_KEY)
    clock.at = before_open.replace(hour=10, minute=4)
    await until(lambda: checks.stale(TRADE_KEY))
    stop.set()
    await asyncio.wait_for(task, 5)

    [gap] = gaps(tmp_path, before_open.date(), "H0UNCNT0")
    assert gap["start"] == before_open.replace(hour=10, minute=0)
