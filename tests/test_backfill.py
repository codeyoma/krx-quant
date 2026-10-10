"""One-minute bar backfill (Backfiller), through the fake KIS seam."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest

from krx_quant.backfill import Backfiller, FuturesBars, StockBars, run_backfill
from krx_quant.contracts import KOSPI200, USD
from krx_quant.fake_kis import FakeKis
from krx_quant.feeds.base import KST
from krx_quant.health import GAP_SCHEMA
from krx_quant.market_calendar import MarketCalendar
from krx_quant.recorder import ParquetRecorder
from tests.kis_rest import (
    FUTURES_BARS,
    HOLIDAYS,
    MINUTE_BARS,
    day_bars,
    futures_bars_api,
    futures_day_bars,
    holiday_api,
    minute_bars_api,
    requests_for,
    rest_client,
)
from tests.test_collector import read, settings_for

AFTER_HOURS = datetime(2026, 10, 8, 21, 0, tzinfo=KST)  # every session of the day is over


def read_bars(
    data_dir: Path, day: date, symbol: str = "005930", feed: str = MINUTE_BARS
) -> list[dict[str, Any]]:
    path = data_dir / "bars" / f"date={day.isoformat()}" / f"feed={feed}" / f"symbol={symbol}"
    rows: list[dict[str, Any]] = pq.read_table(path).to_pylist()
    return rows


def days_with_bars(data_dir: Path, symbol: str | None = None, feed: str = MINUTE_BARS) -> list[date]:
    """The days with bars of ``symbol`` (any series without one)."""
    pattern = "date=*" if symbol is None else f"date=*/feed={feed}/symbol={symbol}"
    return sorted(
        {
            date.fromisoformat(p.relative_to(data_dir / "bars").parts[0].removeprefix("date="))
            for p in (data_dir / "bars").glob(pattern)
        }
    )


@pytest.fixture(autouse=True)
def no_futures_bars_yet(kis: FakeKis) -> None:
    """KIS answers the futures queries with no bars unless a test says otherwise."""
    kis.rest_responses[FUTURES_BARS] = futures_bars_api(lambda market, contract, day: [])


async def test_the_first_run_fills_a_year_of_minute_bars_for_every_trading_day(
    kis: FakeKis, tmp_path: Path
) -> None:
    holidays = {date(2025, 12, 25), date(2026, 1, 1), date(2026, 10, 9)}
    kis.rest_responses[HOLIDAYS] = holiday_api(holidays)
    kis.rest_responses[MINUTE_BARS] = minute_bars_api(lambda market, symbol, day: day_bars(day, 3))
    settings = replace(settings_for(kis, tmp_path), rest_max_per_second=1000, backfill_max_per_second=1000)

    await run_backfill(settings, clock=lambda: AFTER_HOURS)

    year = [date(2025, 10, 8) + timedelta(days=i) for i in range(366)]
    trading = [d for d in year if d.weekday() < 5 and d not in holidays]
    assert days_with_bars(tmp_path) == trading
    assert len(requests_for(kis, MINUTE_BARS)) == len(trading), "one query per day of three bars"
    assert [(b["bar_time"], b["close"], b["volume"]) for b in read_bars(tmp_path, date(2026, 10, 8))] == [
        (datetime(2026, 10, 8, 8, 0, tzinfo=KST), 80000, 1000),
        (datetime(2026, 10, 8, 8, 1, tzinfo=KST), 80100, 1001),
        (datetime(2026, 10, 8, 8, 2, tzinfo=KST), 80200, 1002),
    ]


async def test_a_day_is_queried_back_from_its_end_120_bars_at_a_time_within_the_rest_limit(
    kis: FakeKis, tmp_path: Path
) -> None:
    kis.rest_responses[HOLIDAYS] = holiday_api()
    # 08:00–19:39: six queries a day, the last one short.
    kis.rest_responses[MINUTE_BARS] = minute_bars_api(lambda market, symbol, day: day_bars(day, 700))
    settings = replace(
        settings_for(kis, tmp_path, symbols=("005930", "000660")), backfill_days=6, backfill_max_per_second=20
    )

    await run_backfill(settings, clock=lambda: AFTER_HOURS)

    days = [date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8)]
    assert days_with_bars(tmp_path) == days
    for day in days:
        for symbol in ("005930", "000660"):
            times = [b["bar_time"] for b in read_bars(tmp_path, day, symbol)]
            assert len(times) == 700 and times == sorted(set(times))
            assert times[0].time() == datetime(2026, 1, 1, 8, 0).time()
    one_day = [
        q["FID_INPUT_HOUR_1"]
        for q in requests_for(kis, MINUTE_BARS)
        if (q["FID_INPUT_ISCD"], q["FID_INPUT_DATE_1"]) == ("005930", "20261008")
    ]
    assert one_day == ["235959", "173900", "153900", "133900", "113900", "093900"]
    assert len(requests_for(kis, MINUTE_BARS)) == 5 * 2 * 6
    times = sorted(t for t, _, path, _ in kis.rest_requests if "/quotations/" in path)
    assert max(sum(1 for u in times if t <= u < t + 1.0) for t in times) <= 20


async def test_filling_the_same_days_again_gives_the_same_bars(kis: FakeKis, tmp_path: Path) -> None:
    kis.rest_responses[HOLIDAYS] = holiday_api()
    kis.rest_responses[MINUTE_BARS] = minute_bars_api(lambda market, symbol, day: day_bars(day, 250))
    settings = replace(settings_for(kis, tmp_path), backfill_days=6)
    await run_backfill(settings, clock=lambda: AFTER_HOURS)
    first = {day: read_bars(tmp_path, day) for day in days_with_bars(tmp_path)}
    queries = len(requests_for(kis, MINUTE_BARS))

    await run_backfill(settings, clock=lambda: AFTER_HOURS)
    assert len(requests_for(kis, MINUTE_BARS)) == queries, "filled days are not queried again"

    async with rest_client(kis, tmp_path, AFTER_HOURS) as client:
        backfiller = Backfiller(
            client, MarketCalendar(), tmp_path, [StockBars("005930")], 6, max_per_second=20
        )
        for day in first:
            await backfiller.fill(StockBars("005930"), day)  # and filled again on purpose

    assert {day: read_bars(tmp_path, day) for day in days_with_bars(tmp_path)} == first
    assert all(len(rows) == 250 for rows in first.values())
    assert len(list((tmp_path / "bars").rglob("*.parquet"))) == len(first), "one file per day, replaced"


async def test_a_day_is_filled_only_once_its_last_session_is_over(kis: FakeKis, tmp_path: Path) -> None:
    kis.rest_responses[HOLIDAYS] = holiday_api()
    kis.rest_responses[MINUTE_BARS] = minute_bars_api(lambda market, symbol, day: day_bars(day, 3))
    settings = replace(settings_for(kis, tmp_path), backfill_days=6)

    await run_backfill(settings, clock=lambda: datetime(2026, 10, 8, 16, 0, tzinfo=KST))  # NXT still trading
    assert days_with_bars(tmp_path)[-1] == date(2026, 10, 7)

    await run_backfill(settings, clock=lambda: AFTER_HOURS)
    assert days_with_bars(tmp_path)[-1] == date(2026, 10, 8)


async def test_order_book_and_tick_gaps_stay_unfillable_while_the_bars_are_filled(
    kis: FakeKis, tmp_path: Path
) -> None:
    day = date(2026, 10, 8)
    outage = (datetime(2026, 10, 8, 10, 0, tzinfo=KST), datetime(2026, 10, 8, 10, 5, tzinfo=KST))
    recorder = ParquetRecorder(tmp_path, flush_rows=100, flush_seconds=60)
    for feed in ("H0UNCNT0", "H0UNASP0"):
        row = {"feed": feed, "symbol": "005930", "start": outage[0], "end": outage[1]}
        recorder.record("gaps", day, feed, "005930", {**row, "reason": "disconnected"}, GAP_SCHEMA)
    recorder.close()
    kis.rest_responses[HOLIDAYS] = holiday_api()
    kis.rest_responses[MINUTE_BARS] = minute_bars_api(lambda market, symbol, day: day_bars(day, 700))

    await run_backfill(replace(settings_for(kis, tmp_path), backfill_days=1), clock=lambda: AFTER_HOURS)

    for feed in ("H0UNCNT0", "H0UNASP0"):
        [gap] = read(tmp_path, "gaps", feed)
        assert (gap["start"], gap["end"], gap["reason"]) == (*outage, "disconnected")
    bar_times = {b["bar_time"] for b in read_bars(tmp_path, day)}
    assert {outage[0] + timedelta(minutes=m) for m in range(5)} <= bar_times


async def test_a_query_refused_for_the_per_second_limit_is_retried(kis: FakeKis, tmp_path: Path) -> None:
    answer = minute_bars_api(lambda market, symbol, day: day_bars(day, 3))
    refused: list[str] = []

    def busy_now_and_then(params: dict[str, str]) -> dict[str, Any]:
        day = params["FID_INPUT_DATE_1"]
        if day in ("20261007", "20261005") and day not in refused:
            refused.append(day)
            return {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과하였습니다."}
        return answer(params)

    kis.rest_responses[HOLIDAYS] = holiday_api()
    kis.rest_responses[MINUTE_BARS] = busy_now_and_then
    settings = replace(settings_for(kis, tmp_path), backfill_days=6)

    await run_backfill(settings, clock=lambda: AFTER_HOURS)

    assert refused == ["20261007", "20261005"]
    assert days_with_bars(tmp_path) == [date(2026, 10, d) for d in (2, 5, 6, 7, 8)]


async def test_days_without_integrated_bars_are_filled_with_krx_bars(kis: FakeKis, tmp_path: Path) -> None:
    nxt_closed = date(2026, 10, 7)  # say NXT did not trade 005930 that day

    def bars(market: str, symbol: str, day: date) -> list[dict[str, str]]:
        on_nxt = symbol == "005930" and day != nxt_closed  # the ETF trades on KRX only
        return day_bars(day, 3) if market == "J" or on_nxt else []

    kis.rest_responses[HOLIDAYS] = holiday_api()
    kis.rest_responses[MINUTE_BARS] = minute_bars_api(bars)
    settings = replace(settings_for(kis, tmp_path), etf_symbols=("133690",), backfill_days=2)

    await run_backfill(settings, clock=lambda: AFTER_HOURS)

    def venues(symbol: str) -> list[set[str]]:
        return [{b["venue"] for b in read_bars(tmp_path, day, symbol)} for day in days_with_bars(tmp_path)]

    assert days_with_bars(tmp_path) == [date(2026, 10, 6), nxt_closed, date(2026, 10, 8)]
    assert venues("005930") == [{"UN"}, {"KRX"}, {"UN"}], "one day's fallback does not carry over"
    assert venues("133690") == [{"KRX"}] * 3
    assert len(read_bars(tmp_path, nxt_closed, "133690")) == 3


async def test_a_day_kis_answers_empty_is_tried_again_on_the_next_run(kis: FakeKis, tmp_path: Path) -> None:
    published = {"yet": False}

    def bars(market: str, symbol: str, day: date) -> list[dict[str, str]]:
        return day_bars(day, 3) if day != date(2026, 10, 8) or published["yet"] else []

    kis.rest_responses[HOLIDAYS] = holiday_api()
    kis.rest_responses[MINUTE_BARS] = minute_bars_api(bars)
    settings = replace(settings_for(kis, tmp_path), backfill_days=1)

    await run_backfill(settings, clock=lambda: AFTER_HOURS)
    assert days_with_bars(tmp_path) == [date(2026, 10, 7)], "nothing is stored for an empty answer"

    published["yet"] = True
    await run_backfill(settings, clock=lambda: AFTER_HOURS)
    assert days_with_bars(tmp_path) == [date(2026, 10, 7), date(2026, 10, 8)]


async def test_futures_bars_are_filled_for_a_year_from_each_days_nearest_month(
    kis: FakeKis, tmp_path: Path
) -> None:
    lunar_new_year = {date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18)}
    holidays = {date(2025, 12, 25), date(2026, 1, 1), date(2026, 10, 9), *lunar_new_year}
    kis.rest_responses[HOLIDAYS] = holiday_api(holidays)
    kis.rest_responses[MINUTE_BARS] = minute_bars_api(lambda market, symbol, day: day_bars(day, 3))
    kis.rest_responses[FUTURES_BARS] = futures_bars_api(
        lambda market, contract, day: futures_day_bars(day, 3)
    )
    settings = replace(settings_for(kis, tmp_path), rest_max_per_second=1000, backfill_max_per_second=1000)

    await run_backfill(settings, clock=lambda: AFTER_HOURS)

    year = [date(2025, 10, 8) + timedelta(days=i) for i in range(366)]
    trading = [d for d in year if d.weekday() < 5 and d not in holidays]
    for series in ("KOSPI200F", "USDF"):
        assert days_with_bars(tmp_path, series, FUTURES_BARS) == trading, series
    queries = requests_for(kis, FUTURES_BARS)
    assert len(queries) == 2 * len(trading), "one query per day of three bars"
    assert {(q["FID_COND_MRKT_DIV_CODE"], q["FID_INPUT_ISCD"][:3]) for q in queries} == {
        ("F", "101"),
        ("F", "A01"),
        ("CF", "175"),
        ("CF", "A75"),
    }

    def contracts(series: str, day: date) -> set[str]:
        return {b["contract"] for b in read_bars(tmp_path, day, series, FUTURES_BARS)}

    assert contracts("KOSPI200F", date(2025, 12, 10)) == {"101W12"}
    assert contracts("KOSPI200F", date(2025, 12, 11)) == {"A01603"}  # 101W12's last trading day
    assert contracts("USDF", date(2026, 2, 12)) == {"A75602"}
    assert contracts("USDF", date(2026, 2, 13)) == {"A75603"}  # the third Monday was a holiday
    # KIS gives the traded value of KOSPI200 futures in thousands of won, of USD futures in won.
    assert read_bars(tmp_path, date(2026, 10, 8), "KOSPI200F", FUTURES_BARS)[0]["cum_value"] == 23_000_000_000
    assert read_bars(tmp_path, date(2026, 10, 8), "USDF", FUTURES_BARS)[0]["cum_value"] == 23_000_000
    assert [
        (b["symbol"], b["bar_time"], b["close"], b["high"], b["volume"])
        for b in read_bars(tmp_path, date(2026, 10, 8), "USDF", FUTURES_BARS)
    ] == [
        ("USDF", datetime(2026, 10, 8, 8, 45, tzinfo=KST), 585.0, 585.05, 100),
        ("USDF", datetime(2026, 10, 8, 8, 46, tzinfo=KST), 585.05, 585.1, 101),
        ("USDF", datetime(2026, 10, 8, 8, 47, tzinfo=KST), 585.1, 585.15, 102),
    ]


async def test_a_futures_day_is_queried_back_102_bars_at_a_time_and_filled_again_the_same(
    kis: FakeKis, tmp_path: Path
) -> None:
    kis.rest_responses[HOLIDAYS] = holiday_api()
    kis.rest_responses[MINUTE_BARS] = minute_bars_api(lambda market, symbol, day: [])
    # 08:45–15:35: five queries a day, the last one short.
    kis.rest_responses[FUTURES_BARS] = futures_bars_api(
        lambda market, contract, day: futures_day_bars(day, 411)
    )
    settings = replace(
        settings_for(kis, tmp_path), rest_max_per_second=1000, backfill_max_per_second=1000, backfill_days=2
    )

    await run_backfill(settings, clock=lambda: AFTER_HOURS)

    days = [date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8)]
    first = {(s, d): read_bars(tmp_path, d, s, FUTURES_BARS) for s in ("KOSPI200F", "USDF") for d in days}
    for bars in first.values():
        times = [b["bar_time"] for b in bars]
        assert len(times) == 411 and times == sorted(set(times))
        assert {b["contract"] for b in bars} <= {"A01612", "A75610"}
    one_day = [
        q["FID_INPUT_HOUR_1"]
        for q in requests_for(kis, FUTURES_BARS)
        if (q["FID_INPUT_ISCD"], q["FID_INPUT_DATE_1"]) == ("A01612", "20261008")
    ]
    assert one_day == ["235959", "135300", "121100", "102900", "084700"]
    queries = len(requests_for(kis, FUTURES_BARS))
    assert queries == 2 * 3 * 5

    await run_backfill(settings, clock=lambda: AFTER_HOURS)
    assert len(requests_for(kis, FUTURES_BARS)) == queries, "filled days are not queried again"

    async with rest_client(kis, tmp_path, AFTER_HOURS) as client:
        calendar = MarketCalendar()
        series = [FuturesBars(KOSPI200), FuturesBars(USD)]
        backfiller = Backfiller(client, calendar, tmp_path, series, 2, max_per_second=1000)
        for s in series:
            for day in days:
                await backfiller.fill(s, day)  # and filled again on purpose

    assert {
        (s, d): read_bars(tmp_path, d, s, FUTURES_BARS) for s in ("KOSPI200F", "USDF") for d in days
    } == first
    assert len(list((tmp_path / "bars").rglob("*.parquet"))) == len(first), "one file per day, replaced"
