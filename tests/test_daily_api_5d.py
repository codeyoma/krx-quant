"""The five-session kind through the Engine API (#310): listed by /v1/kinds as awaiting the
dividend probe and holding its algorithms' review days; once it runs, its predictions end at their
window's last session's close, its answers come on that day's night, and its leaderboard's extra is
the five sessions' P&L with Driscoll–Kraay intervals; all within the /v1 schema."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from aiohttp.test_utils import TestClient, TestServer

from krx_quant.api import create_app
from krx_quant.clock import FakeClock
from krx_quant.daily import DailyKind, StoredDays
from krx_quant.engine import PredictionEngine
from krx_quant.feeds.base import KST
from krx_quant.kind_registry import DAILY_KINDS
from krx_quant.kinds import DIRECTION_KIND
from krx_quant.market_calendar import MarketCalendar
from krx_quant.open_to_close import OPEN_TO_CLOSE
from krx_quant.open_to_close_5d import AWAITING, OPEN_TO_CLOSE_5D, REVIEW_HOLD
from krx_quant.predictions import PredictionStore
from krx_quant.replay import replay_into
from tests.api_schema import check
from tests.daily_doubles import SYMBOLS, made_history

FEE = 0.015 / 100
MONDAY, FRIDAY = date(2026, 10, 19), date(2026, 10, 23)
CALENDAR = MarketCalendar()
RUNNING = replace(OPEN_TO_CLOSE_5D, awaiting=None)  # once the dividend probe lets it run (#338)


def at(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm), KST)


def engine(tmp_path: Path, kinds: Sequence[DailyKind] = DAILY_KINDS) -> tuple[PredictionEngine, FakeClock]:
    """An engine whose stored KRX days run to Friday the 23rd: Monday's window is over that night."""
    made_history(tmp_path / "data", tmp_path / "state", FRIDAY, 400, signal=0.5)
    clock = FakeClock(at(MONDAY, "07:20"))
    e = PredictionEngine(
        PredictionStore(tmp_path / "e.sqlite3"),
        CALENDAR,
        SYMBOLS,
        FEE,
        clock,
        algorithms=(),
        kinds=(DIRECTION_KIND,),
        kind_algorithms=(),
        daily=StoredDays(tmp_path / "data"),
        daily_kinds=kinds,
    )
    return e, clock


async def get(client: TestClient[Any, Any], path: str, definition: str) -> Any:
    response = await client.get(path)
    assert response.status == 200, (path, await response.text())
    body = await response.json()
    check(definition, body)
    return body


async def test_the_kind_is_listed_awaiting_the_dividend_probe_and_holding_its_review_days(
    tmp_path: Path,
) -> None:
    e, clock = engine(tmp_path)
    await replay_into(e, clock, [], until=at(MONDAY, "08:51"))
    async with TestClient(TestServer(create_app(e))) as client:
        kinds = {k["name"]: k for k in (await get(client, "/v1/kinds", "kinds"))["kinds"]}
        daily = kinds["open_to_close_5d"]["daily"]
        assert (daily["sessions"], daily["answer_from"], daily["awaiting"], daily["review_hold"]) == (
            5,
            "20:35",
            AWAITING,
            REVIEW_HOLD,
        )
        assert kinds["open_to_close"]["daily"]["awaiting"] is None
        latest = await client.get("/v1/symbols/005930/kinds/open_to_close_5d/latest")
        body = await latest.json()
        assert latest.status == 200 and body["predictions"] == [], "listed, not predicted"


async def test_once_it_runs_its_window_ends_on_its_fifth_session_and_is_answered_that_night(
    tmp_path: Path,
) -> None:
    e, clock = engine(tmp_path, (OPEN_TO_CLOSE, RUNNING))
    await replay_into(e, clock, [], until=at(MONDAY, "08:51"))
    async with TestClient(TestServer(create_app(e))) as client:
        latest = await get(client, "/v1/symbols/005930/kinds/open_to_close_5d/latest", "kind_latest")
        assert latest["bar_close"] == "2026-10-19T08:50:00+09:00"
        for p in latest["predictions"]:
            assert p["target_at"] == "2026-10-23T15:30:00+09:00", "its fifth session's close"
            assert p["band"] is not None and p["answer"] is None
            check("open_to_close_output", p["output"])
        await replay_into(e, clock, [], until=at(FRIDAY, "20:36"))
        monday = await get(
            client,
            "/v1/symbols/005930/kinds/open_to_close_5d/predictions?bar_close=2026-10-19T08:50:00%2B09:00",
            "kind_predictions",
        )
        assert monday["predictions"]
        for p in monday["predictions"]:
            answer = p["answer"]
            assert answer["scored_at"] == "2026-10-23T20:35:00+09:00"
            check("open_to_close_5d_path", answer["path"])
            assert answer["path"]["end_day"] == "2026-10-23" and len(answer["path"]["closes"]) == 5
        board = await get(client, "/v1/leaderboard?kind=open_to_close_5d", "leaderboard")
        check("kind_leaderboard", board)
        for entry in board["baselines"]:
            check("open_to_close_5d_stats", entry["extra"])
            assert (entry["overlap_sessions"], entry["independent_days"]) == (5, 0), "one day: ⌊1 ÷ 5⌋"
