"""The collector stops itself before each trading day's session, for Docker to start it
afresh with that day's contracts (#115).

Input: when the collector started, the market calendar and a fake clock. Output: when it
asks to stop.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta

import pytest

from krx_quant.collector import restart_each_day
from krx_quant.config import Settings, SettingsError
from krx_quant.feeds.base import KST
from krx_quant.market_calendar import MarketCalendar

MONDAY, TUESDAY, WEDNESDAY = date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14)
# Tuesday is closed: the restart waits for Wednesday.
CALENDAR = MarketCalendar({MONDAY: True, TUESDAY: False, WEDNESDAY: True})


async def stopped_at(started: datetime) -> datetime:
    clock = [started]
    stop = asyncio.Event()

    async def sleep(seconds: float) -> None:
        clock[0] += timedelta(seconds=seconds)

    await restart_each_day(time(7, 50), CALENDAR, lambda: clock[0], stop, sleep)
    assert stop.is_set()
    return clock[0]


async def test_a_collector_running_since_an_earlier_day_stops_before_the_next_session() -> None:
    # Started Monday afternoon: it stops at 07:50 on the next trading day, Wednesday.
    assert await stopped_at(datetime(2026, 10, 12, 14, 0, tzinfo=KST)) == datetime(
        2026, 10, 14, 7, 50, tzinfo=KST
    )


async def test_a_collector_started_that_morning_keeps_running_through_its_day() -> None:
    # Started at Monday's 06:00 deploy: it already read Monday's contracts.
    assert await stopped_at(datetime(2026, 10, 12, 6, 0, tzinfo=KST)) == datetime(
        2026, 10, 14, 7, 50, tzinfo=KST
    )


def test_the_restart_is_before_the_nxt_pre_market_unless_off() -> None:
    keys = {"KIS_APP_KEY": "key", "KIS_APP_SECRET": "secret"}
    assert Settings.from_env(keys, dotenv_path=None).restart_at == time(7, 50)
    assert Settings.from_env({**keys, "KRX_RESTART_AT": "off"}, dotenv_path=None).restart_at is None
    with pytest.raises(SettingsError, match="KRX_RESTART_AT"):
        Settings.from_env({**keys, "KRX_RESTART_AT": "09:30"}, dotenv_path=None)
