"""The daily kind through the Engine API (#264): /v1/kinds says when it is predicted, its
predictions carry their day's band and close, its answers come once the day's KRX candle is in,
its leaderboard and stats are a kind's, and the stream sends them as they are stored; all within
the /v1 schema."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from aiohttp.test_utils import TestClient, TestServer

from krx_quant.api import create_app
from krx_quant.clock import FakeClock
from krx_quant.daily import StoredDays
from krx_quant.engine import PredictionEngine
from krx_quant.feeds.base import KST
from krx_quant.kinds import DIRECTION_KIND
from krx_quant.market_calendar import MarketCalendar
from krx_quant.predictions import PredictionStore
from krx_quant.replay import replay_into
from tests.api_schema import check
from tests.daily_doubles import SYMBOLS, made_history

FEE = 0.015 / 100
MONDAY = date(2026, 10, 19)
CALENDAR = MarketCalendar()


def at(hhmm: str, day: date = MONDAY) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm), KST)


def daily_engine(tmp_path: Path, start: datetime) -> tuple[PredictionEngine, FakeClock]:
    made_history(tmp_path / "data", tmp_path / "state", date(2026, 10, 16), 400, signal=0.5)
    clock = FakeClock(start)
    engine = PredictionEngine(
        PredictionStore(tmp_path / "e.sqlite3"),
        CALENDAR,
        SYMBOLS,
        FEE,
        clock,
        algorithms=(),
        kinds=(DIRECTION_KIND,),
        kind_algorithms=(),
        daily=StoredDays(tmp_path / "data"),
    )
    return engine, clock


def store_monday(tmp_path: Path, change: float) -> None:
    """Monday's KRX auction prices, as the history task keeps them at 15:40 (#264): its candle
    comes after 20:30."""
    from krx_quant.auctions import MINUTE_BAR, read_auctions, write_auctions
    from krx_quant.candles import read_krx_days

    for symbol in SYMBOLS:
        opened = read_krx_days(tmp_path / "data", symbol)[-1]["close"]
        kept = read_auctions(tmp_path / "data", symbol)
        closed = round(opened * (1 + change))
        kept[MONDAY] = {
            "symbol": symbol,
            "date": MONDAY,
            "open": opened,
            "close": closed,
            "source": MINUTE_BAR,
        }
        write_auctions(tmp_path / "data", symbol, kept)


async def test_the_daily_kind_through_the_kind_endpoints(tmp_path: Path) -> None:
    engine, clock = daily_engine(tmp_path, at("07:20"))
    await replay_into(engine, clock, [], until=at("08:51"))

    async with TestClient(TestServer(create_app(engine))) as client:

        async def get(path: str, definition: str) -> Any:
            response = await client.get(path)
            assert response.status == 200, (path, await response.text())
            body = await response.json()
            check(definition, body)
            return body

        kinds = (await get("/v1/kinds", "kinds"))["kinds"]
        assert [k["name"] for k in kinds] == ["direction", "open_to_close"]
        assert kinds[0]["daily"] is None
        daily = kinds[1]
        assert daily["horizon_minutes"] is None
        assert (daily["daily"]["predict_at"], daily["daily"]["from"], daily["daily"]["to"]) == (
            "08:50",
            "open",
            "close",
        )
        assert [b["name"] for b in daily["baselines"]] == ["otc-flat", "otc-climate", "otc-persist"]

        latest = await get("/v1/symbols/005930/kinds/open_to_close/latest", "kind_latest")
        assert latest["bar_close"] == "2026-10-19T08:50:00+09:00"
        for p in latest["predictions"]:
            assert p["target_at"] == "2026-10-19T15:30:00+09:00", "the day's close"
            assert p["band"] is not None and p["band"] > 0
            assert p["answer"] is None, "before the day's closing auction price is in"
            check("open_to_close_output", p["output"])

        store_monday(tmp_path, 0.05)
        await replay_into(engine, clock, [], until=at("15:50"))
        answered = await get(
            "/v1/symbols/005930/kinds/open_to_close/predictions?bar_close=2026-10-19T08:50:00%2B09:00",
            "kind_predictions",
        )
        for p in answered["predictions"]:
            assert p["answer"]["class"] == "up" and p["answer"]["scored_at"] == "2026-10-19T15:45:00+09:00"
            check("open_to_close_path", p["answer"]["path"])

        board = await get("/v1/leaderboard?kind=open_to_close", "leaderboard")
        check("kind_leaderboard", board)
        assert {e["algorithm"] for e in board["baselines"]} == {"otc-flat", "otc-climate", "otc-persist"}
        for e in board["baselines"]:
            check("open_to_close_stats", e["extra"])
        stats = await get("/v1/algorithms/otc-climate/stats", "algorithm_stats")
        check("kind_algorithm_stats", stats)
        assert stats["kind"] == "open_to_close" and list(stats["by_slot"]) == ["08:30"]
        missing = await client.get("/v1/leaderboard?kind=nothing")
        assert "open_to_close" in (await missing.json())["error"]


async def test_the_stream_sends_the_daily_predictions_and_answers_as_they_are_stored(tmp_path: Path) -> None:
    engine, clock = daily_engine(tmp_path, at("08:40"))

    async with TestClient(TestServer(create_app(engine))) as client:
        ws = await client.ws_connect("/v1/stream")
        received: list[dict[str, Any]] = []

        async def listen() -> None:  # beside the replay: a client that keeps up
            while not any(e["type"] == "kind_answer" for e in received):
                event = await ws.receive_json()
                check("event", event)
                received.append(event)

        listening = asyncio.create_task(listen())
        for until in [at("08:51"), *(at(f"{h:02d}:00") for h in range(10, 16))]:  # the queue stays short
            await replay_into(engine, clock, [], until=until)
            await asyncio.sleep(0.05)
        store_monday(tmp_path, -0.05)
        await replay_into(engine, clock, [], until=at("15:46"))
        async with asyncio.timeout(5):
            await listening
        await ws.close()

    [made] = [e for e in received if e["type"] == "kind_prediction"]
    assert (made["prediction_kind"], made["bar_close"]) == ("open_to_close", "2026-10-19T08:50:00+09:00")
    assert {p["symbol"] for p in made["predictions"]} == set(SYMBOLS)
    assert {p["target_at"] for p in made["predictions"]} == {"2026-10-19T15:30:00+09:00"}
    [answer] = [e for e in received if e["type"] == "kind_answer"]
    assert answer["prediction_kind"] == "open_to_close"
    assert {(a["symbol"], a["class"]) for a in answer["answers"]} == {(s, "down") for s in SYMBOLS}


async def test_a_starting_engine_answers_the_days_predicted_before_in_answer_version_2(
    tmp_path: Path,
) -> None:
    """#264: the answers of version 1 (the KRX day candle's close) stay as they were; a starting
    engine answers the days predicted before in version 2, from the kept closing auction price
    (``daily.PENDING_DAYS``), and the API shows those."""
    from krx_quant.kinds import Answer
    from krx_quant.open_to_close import OPEN_TO_CLOSE

    engine, clock = daily_engine(tmp_path, at("07:20"))
    await replay_into(engine, clock, [], until=at("08:51"))
    t = OPEN_TO_CLOSE.predicted_at(MONDAY)
    old = [(s, t, Answer("up", None, {"source": "krx_day_candle"}), False) for s in SYMBOLS]
    engine.store.add_kind_answers(OPEN_TO_CLOSE.name, 1, old, scored_at=at("20:35"))
    engine.store.close()
    store_monday(tmp_path, -0.05)

    tuesday = date(2026, 10, 20)
    engine = PredictionEngine(
        PredictionStore(tmp_path / "e.sqlite3"),
        CALENDAR,
        SYMBOLS,
        FEE,
        FakeClock(at("07:20", tuesday)),
        algorithms=(),
        kinds=(DIRECTION_KIND,),
        kind_algorithms=(),
        daily=StoredDays(tmp_path / "data"),
    )
    clock = engine.clock  # type: ignore[assignment]
    await replay_into(engine, clock, [], until=at("07:21", tuesday))
    answers = engine.store.kind_answers(OPEN_TO_CLOSE.name)
    by_version = {(a["answer_version"], a["symbol"]): a for a in answers}
    for symbol in SYMBOLS:
        assert by_version[(1, symbol)]["class"] == "up", "version 1 stays"
        new = by_version[(2, symbol)]
        assert (new["class"], new["path"]["source"]) == ("down", "krx_minute_bar")
        assert new["scored_at"] == "2026-10-20T07:20:00+09:00"
    async with TestClient(TestServer(create_app(engine))) as client:
        response = await client.get(
            "/v1/symbols/005930/kinds/open_to_close/predictions?bar_close=2026-10-19T08:50:00%2B09:00"
        )
        body = await response.json()
        check("kind_predictions", body)
        assert {p["answer"]["answer_version"] for p in body["predictions"]} == {2}
