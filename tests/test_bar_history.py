"""Scrolling the chart back (#108): one-minute bars and predictions before a time, across days.

Input: a replayed morning (today's bars in memory, predictions in the store) and the
backfill's one-minute bar files of earlier days in a data directory. Output: the Engine
API's ``bars?before=&limit=`` and ``predictions?before=`` answers.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from aiohttp.test_utils import TestClient, TestServer

from krx_quant.api import MAX_BARS, create_app
from krx_quant.backfill import BAR_SCHEMA, MINUTE_BARS_TR, bars_key
from krx_quant.market_calendar import MarketCalendar
from tests.api_schema import check
from tests.replay_events import SYMBOL, at, get_json, run, steady_book, trade

WED, THU, MON = date(2026, 10, 7), date(2026, 10, 8), date(2026, 10, 12)  # Friday the 9th is a holiday
CALENDAR = MarketCalendar({WED: True, THU: True, date(2026, 10, 9): False, MON: True})


def store_day(data_dir: Path, day: date, minutes: list[str], close: int = 81_000) -> None:
    """A day's bars as the backfill stores them: one per minute, closing at ``close``."""
    rows = [
        {
            "symbol": SYMBOL,
            "bar_time": at(m, day),
            "venue": "UN",
            "open": close,
            "high": close + 100,
            "low": close - 100,
            "close": close,
            "volume": 7,
            "cum_value": 0,
        }
        for m in minutes
    ]
    path = data_dir / bars_key(day, MINUTE_BARS_TR, SYMBOL)
    path.parent.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=BAR_SCHEMA), path)


def starts(body: dict[str, Any]) -> list[str]:
    check("bars", body)
    return [b["start"] for b in body["bars"]]


async def test_bars_before_a_time_go_back_across_days_oldest_first(tmp_path: Path) -> None:
    data = tmp_path / "data"
    store_day(data, WED, ["15:18:00", "15:19:00"])
    store_day(data, THU, ["09:00:00", "09:01:00", "15:19:00"])
    events = [trade(at(f"09:3{m}:05"), 81_200 + 100 * m) for m in range(4)]
    engine = await run(tmp_path / "engine.db", events, until=at("09:33:30"), calendar=CALENDAR)

    async with TestClient(TestServer(create_app(engine, data_dir=data))) as client:
        today = await get_json(client, f"/v1/symbols/{SYMBOL}/bars")
        across = await get_json(
            client, f"/v1/symbols/{SYMBOL}/bars?before=2026-10-12T09:32:00%2B09:00&limit=4"
        )
        earlier = await get_json(
            client, f"/v1/symbols/{SYMBOL}/bars?before=2026-10-08T09:01:00%2B09:00&limit=5"
        )
        nothing = await get_json(
            client, f"/v1/symbols/{SYMBOL}/bars?before=2026-10-07T09:00:00%2B09:00&limit=5"
        )
        bad = [
            await client.get(f"/v1/symbols/{SYMBOL}/bars?before={q}")
            for q in ("2026-10-12T09:32:00", "yesterday", "9999-12-31T23:59:59-12:00")
        ] + [
            await client.get(f"/v1/symbols/{SYMBOL}/bars?before=2026-10-12T09:32:00%2B09:00&limit={n}")
            for n in (0, MAX_BARS + 1, "x")
        ]

    # Without before and limit: today's finished bars, as before.
    assert starts(today) == [f"2026-10-12T09:3{m}:00+09:00" for m in range(3)]
    # Today's from memory, then the days before from the backfill's files, oldest first.
    assert starts(across) == [
        "2026-10-08T09:01:00+09:00",
        "2026-10-08T15:19:00+09:00",
        "2026-10-12T09:30:00+09:00",
        "2026-10-12T09:31:00+09:00",
    ]
    assert starts(earlier) == [
        "2026-10-07T15:18:00+09:00",
        "2026-10-07T15:19:00+09:00",
        "2026-10-08T09:00:00+09:00",
    ]
    stored = earlier["bars"][0]
    assert (stored["open"], stored["high"], stored["low"], stored["close"], stored["volume"]) == (
        81_000,
        81_100,
        80_900,
        81_000,
        7,
    )
    assert stored["trades"] is None, "the backfill does not count trades"
    assert starts(nothing) == []
    assert [r.status for r in bad] == [400] * 6, "no offset, no time, past the calendar in KST, bad limits"


async def test_a_backfilled_day_replaces_what_memory_kept_of_it(tmp_path: Path) -> None:
    # The engine restarted at 09:31: memory has 09:31 on; the 20:30 backfill has the whole day.
    data = tmp_path / "data"
    events = [trade(at(f"09:3{m}:05"), 81_200) for m in range(1, 4)]
    engine = await run(tmp_path / "engine.db", events, until=at("09:33:30"), calendar=CALENDAR)

    async with TestClient(TestServer(create_app(engine, data_dir=data))) as client:
        path = f"/v1/symbols/{SYMBOL}/bars?before=2026-10-12T09:33:00%2B09:00&limit=10"
        gap = await get_json(client, path)
        store_day(data, MON, ["09:29:00", "09:30:00", "09:31:00", "09:32:00"])
        filled = await get_json(client, path)

    assert starts(gap) == ["2026-10-12T09:31:00+09:00", "2026-10-12T09:32:00+09:00"]
    assert starts(filled) == [f"2026-10-12T09:{m}:00+09:00" for m in (29, 30, 31, 32)]


async def test_predictions_before_a_time_are_of_earlier_bar_closes_only(tmp_path: Path) -> None:
    # The engine goes on predicting from the last quote to Thursday's last bar close, 15:16.
    thursday = steady_book(at("09:00:30", THU), at("09:20:00", THU), bid=81_200, ask=81_300)
    monday = steady_book(at("09:00:30"), at("09:10:00"), bid=81_200, ask=81_300)
    engine = await run(
        tmp_path / "engine.db",
        [*thursday, *monday],
        until=at("09:10:00"),
        calendar=CALENDAR,
        start=datetime.combine(THU, at("00:00:00").timetz()),
    )

    async with TestClient(TestServer(create_app(engine))) as client:
        path = f"/v1/symbols/{SYMBOL}/predictions?algorithm=consensus-final"
        before = await get_json(client, f"{path}&before=2026-10-12T09:03:00%2B09:00&limit=4")
        thursday_end = await get_json(client, f"{path}&before=2026-10-08T09:21:00%2B09:00&limit=2")
        every = await get_json(
            client, f"/v1/symbols/{SYMBOL}/predictions?before=2026-10-12T09:02:00%2B09:00&limit=2"
        )
        bad = await client.get(f"{path}&before=09:03")

    check("predictions", before)
    assert [p["bar_close"] for p in before["predictions"]] == [
        "2026-10-12T09:02:00+09:00",
        "2026-10-12T09:01:00+09:00",
        "2026-10-08T15:16:00+09:00",
        "2026-10-08T15:15:00+09:00",
    ], "newest first, across days"
    assert [p["bar_close"] for p in thursday_end["predictions"]] == [
        "2026-10-08T09:20:00+09:00",
        "2026-10-08T09:19:00+09:00",
    ]
    # Every predictor's: the last two bar closes before.
    assert {p["bar_close"] for p in every["predictions"]} == {
        "2026-10-12T09:01:00+09:00",
        "2026-10-08T15:16:00+09:00",
    }
    assert bad.status == 400
