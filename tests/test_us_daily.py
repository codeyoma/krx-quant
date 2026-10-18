"""The US daily closes the daily kinds learn from (#264): ten years first, a month after, merged by
day; and the move of a morning, by the macro read's rule."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from krx_quant.config import Settings
from krx_quant.feeds.base import KST
from krx_quant.macro import Observation
from krx_quant.us_daily import US_DAILY, UsDailyKeeper, level_before, move_before, read_us_daily

NEW_YORK = UTC  # the fake answers in UTC: its dates are the stamps' days


def chart(days: list[date], closes: list[float]) -> dict[str, Any]:
    stamps = [int(datetime(d.year, d.month, d.day, 14, 30, tzinfo=NEW_YORK).timestamp()) for d in days]
    result = {
        "meta": {"exchangeTimezoneName": "UTC", "currentTradingPeriod": {"regular": {"start": 0, "end": 1}}},
        "timestamp": stamps,
        "indicators": {"quote": [{"close": closes}]},
    }
    return {"chart": {"result": [result], "error": None}}


async def test_the_closes_are_taken_whole_then_a_month_at_a_time(tmp_path: Path) -> None:
    asked: list[tuple[str, str]] = []

    async def yahoo(request: web.Request) -> web.Response:
        asked.append((request.match_info["symbol"], request.query["range"]))
        if request.query["range"] == "10y":
            return web.json_response(chart([date(2026, 10, 13), date(2026, 10, 14)], [100.0, 101.0]))
        return web.json_response(chart([date(2026, 10, 14), date(2026, 10, 15)], [101.5, 102.0]))

    app = web.Application()
    app.router.add_get("/yahoo/v8/finance/chart/{symbol}", yahoo)
    async with TestServer(app) as server:
        settings = Settings(
            kis_app_key="",
            kis_app_secret="",
            data_dir=tmp_path,
            state_dir=tmp_path / "state",
            yahoo_url=str(server.make_url("/yahoo")),
        )
        keeper = UsDailyKeeper(settings)
        now = datetime(2026, 10, 16, 20, 30, tzinfo=KST)
        assert await keeper.run(now) == len(US_DAILY)
        assert await keeper.run(now) == len(US_DAILY), "a month again: the 14th revised, the 15th new"
    assert [r for _, r in asked] == ["10y"] * len(US_DAILY) + ["1mo"] * len(US_DAILY)
    assert {s for s, _ in asked} == {"^SOX", "NVDA", "MU", "^VIX"}
    assert read_us_daily(tmp_path, "sox") == [
        Observation(date(2026, 10, 13), 100.0),
        Observation(date(2026, 10, 14), 101.5),
        Observation(date(2026, 10, 15), 102.0),
    ]


def test_a_mornings_move_is_the_macro_reads() -> None:
    closes = [Observation(date(2026, 10, 8), 100.0), Observation(date(2026, 10, 9), 98.0)]
    # Monday the 12th, after Hangul Day (Friday the 9th, closed in Seoul): Thursday's morning knew
    # the 7th's close (none here), Monday's the 9th's. From Friday the 8th's morning (the 7th) …
    assert move_before(closes, date(2026, 10, 12), date(2026, 10, 9)) == pytest.approx(-0.02)
    assert move_before(closes, date(2026, 10, 9), date(2026, 10, 8)) is None, "no close before the 8th"
    assert move_before(closes, date(2026, 10, 12), None) is None
    assert level_before(closes, date(2026, 10, 12)) == 98.0
    assert level_before(closes, date(2026, 10, 20)) is None, "more than seven days old"
