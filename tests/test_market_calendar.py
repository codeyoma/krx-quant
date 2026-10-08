"""Trading days and session phases (Market Calendar); KIS holiday data through the fake KIS seam."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from krx_quant.fake_kis import FakeKis
from krx_quant.feeds.base import KST
from krx_quant.market_calendar import CalendarStore, MarketCalendar, Phase
from tests.kis_rest import HOLIDAYS, holiday_api, requests_for, rest_client


def at(day: date, hhmmss: str) -> datetime:
    h, m, s = (int(p) for p in hhmmss.split(":"))
    return datetime.combine(day, time(h, m, s), KST)


def phases(calendar: MarketCalendar, day: date, *times: str) -> list[Phase]:
    return [calendar.phase(at(day, t)) for t in times]


def test_a_holiday_is_closed_all_day() -> None:
    hangul_day = date(2026, 10, 9)  # a Friday
    calendar = MarketCalendar({hangul_day: False})

    assert calendar.session(hangul_day) is None
    assert phases(calendar, hangul_day, "08:00:00", "10:00:00", "15:25:00") == [Phase.CLOSED] * 3


def test_a_regular_day_trades_continuously_from_nine_to_three_twenty() -> None:
    day = date(2026, 10, 8)
    calendar = MarketCalendar({day: True})

    times = ("08:29:59", "08:30:00", "08:59:59", "09:00:00", "15:19:59", "15:20:00", "15:29:59", "15:30:00")
    assert phases(calendar, day, *times) == [
        Phase.PRE_OPEN,
        Phase.OPENING_AUCTION,
        Phase.OPENING_AUCTION,
        Phase.CONTINUOUS,
        Phase.CONTINUOUS,
        Phase.CLOSING_AUCTION,
        Phase.CLOSING_AUCTION,
        Phase.AFTER_CLOSE,
    ]


def test_the_first_trading_day_of_the_year_opens_at_ten_and_closes_as_usual() -> None:
    # 2027-01-01 is a Friday holiday and the 2nd and 3rd a weekend: Monday the 4th opens the year.
    calendar = MarketCalendar(
        {
            date(2027, 1, 1): False,
            date(2027, 1, 2): False,
            date(2027, 1, 3): False,
            date(2027, 1, 4): True,
            date(2027, 1, 5): True,
        }
    )

    assert phases(calendar, date(2027, 1, 4), "09:29:59", "09:30:00", "10:00:00", "15:20:00", "15:30:00") == [
        Phase.PRE_OPEN,
        Phase.OPENING_AUCTION,
        Phase.CONTINUOUS,
        Phase.CLOSING_AUCTION,
        Phase.AFTER_CLOSE,
    ]
    assert calendar.phase(at(date(2027, 1, 5), "09:00:00")) is Phase.CONTINUOUS, "only the first day is late"


def test_the_exam_day_opens_and_closes_an_hour_late() -> None:
    exam_day = date(2026, 11, 19)  # the college entrance exam for 2027 admissions
    calendar = MarketCalendar({exam_day: True})

    times = ("09:29:59", "09:30:00", "10:00:00", "15:30:00", "16:19:59", "16:20:00", "16:30:00")
    assert phases(calendar, exam_day, *times) == [
        Phase.PRE_OPEN,
        Phase.OPENING_AUCTION,
        Phase.CONTINUOUS,
        Phase.CONTINUOUS,
        Phase.CONTINUOUS,
        Phase.CLOSING_AUCTION,
        Phase.AFTER_CLOSE,
    ]


def test_without_kis_data_weekdays_are_taken_for_regular_trading_days() -> None:
    calendar = MarketCalendar()

    assert not calendar.knows(date(2026, 10, 8))
    assert calendar.phase(at(date(2026, 10, 8), "09:00:00")) is Phase.CONTINUOUS  # Thursday
    assert calendar.phase(at(date(2026, 10, 10), "10:00:00")) is Phase.CLOSED  # Saturday
    assert calendar.session(date(2027, 1, 1)) is None, "New Year's Day is never a trading day"
    assert calendar.session(date(2026, 12, 31)) is None, "nor the year-end closing day"
    assert calendar.phase(at(date(2027, 1, 4), "09:30:00")) is Phase.OPENING_AUCTION, "the year's first day"


NOW = datetime(2026, 10, 8, 16, 0, tzinfo=KST)


async def test_the_calendar_reads_a_year_of_kis_holidays_once_then_one_query_a_day(
    kis: FakeKis, tmp_path: Path
) -> None:
    kis.rest_responses[HOLIDAYS] = holiday_api(closed={date(2025, 10, 9), date(2026, 10, 9)})
    cache = tmp_path / "state" / "market_calendar.json"
    today = date(2026, 10, 8)

    async with rest_client(kis, tmp_path, NOW) as client:
        await CalendarStore(cache, history_days=365).refresh(client, today)
        first_start = [q["BASS_DT"] for q in requests_for(kis, HOLIDAYS)]
        restarted = CalendarStore(cache, history_days=365)
        await restarted.refresh(client, today)  # cached: no query
        await restarted.refresh(client, today + timedelta(days=1))
    queries = [q["BASS_DT"] for q in requests_for(kis, HOLIDAYS)]

    assert first_start[0] == "20251008" and len(first_start) == 16, "a year in pages of 24 days"
    assert queries[len(first_start) :] == ["20261009"], "then only from the new day on"
    calendar = restarted.calendar
    assert calendar.session(date(2026, 10, 9)) is None
    assert calendar.trading_days(date(2025, 10, 1), date(2025, 10, 13)) == [
        date(2025, 10, 8),
        date(2025, 10, 10),
        date(2025, 10, 13),
    ], "from a year back, weekends and holidays left out"
    assert calendar.knows(date(2026, 10, 31)), "the page that reaches today runs past it"


async def test_a_day_kis_leaves_out_is_not_queried_again_every_day(kis: FakeKis, tmp_path: Path) -> None:
    answer = holiday_api()

    def without_one_day(params: dict[str, str]) -> dict[str, Any]:
        body = answer(params)
        body["output"] = [row for row in body["output"] if row["bass_dt"] != "20260601"]
        return body

    kis.rest_responses[HOLIDAYS] = without_one_day
    store = CalendarStore(tmp_path / "state" / "market_calendar.json", history_days=365)
    async with rest_client(kis, tmp_path, NOW) as client:
        await store.refresh(client, date(2026, 10, 8))
        first_start = len(requests_for(kis, HOLIDAYS))
        await store.refresh(client, date(2026, 10, 9))

    assert len(requests_for(kis, HOLIDAYS)) == first_start + 1
    assert store.calendar.session(date(2026, 6, 1)) is None, "a day KIS does not list is not a trading day"
