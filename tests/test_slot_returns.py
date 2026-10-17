"""The same 10-minute slot's returns on the days before (#265): a day's returns by slot, the
features Turing and Breiman read, and the previous days read from the backfilled bars."""

from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path

import pytest

from krx_quant.backfill import PreviousDay
from krx_quant.features import (
    FEATURE_MEANINGS,
    FEATURES,
    SLOT_RETURN_FEATURES,
    Snapshot,
    day_slot_returns,
    slot_return_feature,
    ten_minute_slot,
)
from krx_quant.feeds import KST
from krx_quant.market_calendar import MarketCalendar
from krx_quant.reference import Bar
from krx_quant.training import needs_previous_day, previous_days_needed
from tests.test_mandelbrot import store_bars

FEE = 0.00015
MONDAY, TUESDAY, WEDNESDAY = date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14)
CALENDAR = MarketCalendar(dict.fromkeys((MONDAY, TUESDAY, WEDNESDAY), True))


def bar(day: date, hhmm: str, o: int, c: int) -> Bar:
    at = datetime.combine(day, time.fromisoformat(hhmm), KST)
    high, low = max(o, c), min(o, c)
    return Bar(start=at, open=o, high=high, low=low, close=c, volume=1, trades=1, first_at=at, last_at=at)


def day_of(day: date) -> list[Bar]:
    return [
        bar(day, "08:30", 99_000, 99_500),  # NXT pre-market: left out
        bar(day, "09:00", 100_000, 100_100),
        bar(day, "09:05", 100_100, 100_200),  # 09:00 slot: 100,000 → 100,200
        bar(day, "09:20", 100_200, 100_000),  # 09:10 had no trade: 0; 09:20: 100,200 → 100,000
        bar(day, "15:19", 100_000, 101_000),  # 15:10 slot: 100,000 → 101,000
        bar(day, "15:20", 101_000, 102_000),  # the closing auction: left out
    ]


def test_a_day_s_returns_by_ten_minute_slot() -> None:
    session = CALENDAR.session(MONDAY)
    assert session is not None
    slots = day_slot_returns(day_of(MONDAY), session)
    assert slots["09:00"] == pytest.approx(20.0)
    assert slots["09:10"] == 0.0
    assert slots["09:20"] == pytest.approx((100_000 / 100_200 - 1) * 1e4)
    assert slots["12:00"] == 0.0
    assert slots["15:10"] == pytest.approx(100.0)
    assert len(slots) == 38  # 09:00 to 15:10, every slot after the first trade
    assert day_slot_returns([bar(MONDAY, "15:20", 1, 2)], session) == {}


def snapshot(at: str, previous: list[dict[str, float]]) -> Snapshot:
    t = datetime.combine(WEDNESDAY, time.fromisoformat(at), KST)
    return Snapshot.at(SYMBOL_, t, [], 100_000, FEE, session_open=None, previous_slot_returns=previous)


SYMBOL_ = "005930"


def test_the_features_read_the_bar_close_s_slot_on_the_days_before() -> None:
    days = [{"09:30": float(i)} for i in range(1, 21)]  # 1 bp the day before, 20 twenty days back
    values = {name: FEATURES[name](snapshot("09:31", days)) for name in SLOT_RETURN_FEATURES}
    assert ten_minute_slot(datetime(2026, 10, 14, 9, 39)) == "09:30"
    assert values == {
        slot_return_feature(1): 1.0,
        slot_return_feature(5): pytest.approx(3.0),
        slot_return_feature(20): pytest.approx(10.5),
    }
    # The 09:40 slot was never traded; nor are 20 days known with 5.
    assert FEATURES[slot_return_feature(1)](snapshot("09:41", days)) is None
    assert FEATURES[slot_return_feature(20)](snapshot("09:31", days[:5])) is None
    assert all(name in FEATURE_MEANINGS for name in SLOT_RETURN_FEATURES)
    assert needs_previous_day([slot_return_feature(5)])
    # The nightly training fetches the bars of the 20 trading days before each day.
    assert previous_days_needed([slot_return_feature(1)]) == 20
    assert previous_days_needed(["range_prev_day"]) == 1
    assert previous_days_needed(["velocity"]) == 0


def test_the_previous_days_come_from_the_backfilled_bars(tmp_path: Path) -> None:
    store_bars(tmp_path, MONDAY, day_of(MONDAY), SYMBOL_)
    # Tuesday has no file: an empty day. Monday is the first day the calendar knows: the end.
    found = PreviousDay(tmp_path, CALENDAR).slot_returns(SYMBOL_, WEDNESDAY)
    assert len(found) == 2
    assert found[0] == {}
    assert found[1]["09:00"] == pytest.approx(20.0)


def test_a_day_without_its_bars_is_asked_again(tmp_path: Path) -> None:
    """The backfill may fill a missing day while the engine runs: it is not cached empty."""
    previous = PreviousDay(tmp_path, CALENDAR)
    assert previous.slot_returns(SYMBOL_, TUESDAY) == [{}]
    store_bars(tmp_path, MONDAY, day_of(MONDAY), SYMBOL_)
    assert previous.slot_returns(SYMBOL_, TUESDAY)[0]["09:00"] == pytest.approx(20.0)
