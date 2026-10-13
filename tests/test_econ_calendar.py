"""The economic calendar (#150): reading TradingView's feed, keeping the latest answer,
recording what changed, the alert, the settings, and ``/v1/calendar``.

Input: a fake server answering as TradingView's economic calendar and Telegram do. Output:
the kept answer, the ``calendar`` layer, the Telegram messages, and the Engine API's answer.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import pyarrow.parquet as pq
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from krx_quant.api import create_app
from krx_quant.archive import partition_files
from krx_quant.config import Settings, SettingsError
from krx_quant.econ_calendar import (
    ALERT_AFTER,
    LAYER,
    CalendarCollector,
    CalendarError,
    calendar_answer,
    keep_calendar,
    latest_path,
    parse_tradingview,
    query,
    read_latest,
)
from krx_quant.feeds import KST
from krx_quant.notify import Alerts
from krx_quant.recorder import ParquetRecorder
from tests.api_schema import check
from tests.replay_events import at, run

NOW = datetime(2026, 10, 13, 9, 15, tzinfo=KST)  # a Tuesday morning


def item(ident: str, when: str, title: str, **values: Any) -> dict[str, Any]:
    """A feed item as TradingView sends it, trimmed to what is read."""
    return {
        "id": ident,
        "title": title,
        "country": values.pop("country", "US"),
        "indicator": values.pop("indicator", title),
        "category": "prce",
        "period": values.pop("period", "Sep"),
        "date": when,
        "actual": values.pop("actual", None),
        "forecast": values.pop("forecast", None),
        "previous": values.pop("previous", None),
        "unit": values.pop("unit", "%"),
        "currency": "USD",
        "importance": values.pop("importance", 1),
        "source": "Bureau of Labour Statistics",
        "source_url": "https://www.bls.gov",
        **values,
    }


ITEMS = [
    item("2", "2026-10-14T12:30:00.000Z", "Inflation Rate YoY", forecast=3.7, previous=3.4),
    item(
        "1",
        "2026-10-12T23:00:00.000Z",
        "Unemployment Rate",
        country="kr",
        importance=0,
        previous=2.7,
        unit=None,
    ),
    item(
        "3",
        "2026-10-11T23:50:00.000Z",
        "Machinery Orders",
        country="JP",
        importance=-1,
        actual=1.2,
        previous=0.4,
    ),
    item("", "2026-10-14T12:30:00.000Z", "No id"),
    item("5", "2026-10-14 12:30", "No offset"),
    item("6", "", "No time"),
    item("7", "2026-10-15T12:30:00.000Z", "Text values", actual="1.2%", forecast=float("nan")),
]


def test_the_feed_reads_as_events_in_time_order() -> None:
    events = parse_tradingview({"status": "ok", "result": ITEMS})
    assert [e.id for e in events] == ["3", "1", "2", "7"]  # no id, no offset, no time: left out
    machinery, unemployment, inflation, text = events
    assert machinery.at == datetime(2026, 10, 11, 23, 50, tzinfo=UTC)
    assert (machinery.actual, machinery.previous, machinery.importance) == (1.2, 0.4, "low")
    assert (unemployment.country, unemployment.importance, unemployment.unit) == ("KR", "medium", None)
    assert (inflation.forecast, inflation.previous, inflation.importance) == (3.7, 3.4, "high")
    assert (text.actual, text.forecast) == (None, None)  # not numbers
    assert inflation.to_json()["at"] == "2026-10-14T21:30:00+09:00"


def test_an_answer_that_is_not_a_calendar_is_an_error() -> None:
    bodies: list[Any] = [{"status": "error"}, [], "nope", {"result": "x"}]
    for body in bodies:
        with pytest.raises(CalendarError):
            parse_tradingview(body)


def test_the_query_runs_from_the_start_of_yesterday_for_eight_days() -> None:
    assert query(["US", "KR"], NOW) == {
        "from": "2026-10-11T15:00:00.000Z",  # 2026-10-12 00:00 KST
        "to": "2026-10-19T15:00:00.000Z",
        "countries": "US,KR",
    }


@dataclass
class Feed:
    """What the fake server answers, and what it was asked and told."""

    items: list[dict[str, Any]] = field(default_factory=lambda: list(ITEMS))
    down: bool = False
    asked: list[dict[str, str]] = field(default_factory=list)
    origins: list[str] = field(default_factory=list)
    sent: list[str] = field(default_factory=list)


def fake_server(feed: Feed) -> web.Application:
    async def events(request: web.Request) -> web.Response:
        feed.asked.append(dict(request.query))
        feed.origins.append(request.headers.get("Origin", ""))
        if feed.down:
            return web.Response(status=503, text="down")
        return web.json_response({"status": "ok", "result": feed.items})

    async def telegram(request: web.Request) -> web.Response:
        feed.sent.append((await request.json())["text"])
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_get("/events", events)
    app.router.add_post("/telegram/bottoken/sendMessage", telegram)
    return app


@dataclass
class Setup:
    feed: Feed
    data_dir: Path
    make: Callable[[], CalendarCollector]
    recorder: ParquetRecorder


@pytest.fixture
async def setup(tmp_path: Path) -> AsyncIterator[Setup]:
    feed = Feed()
    server = TestServer(fake_server(feed))
    await server.start_server()
    base = str(server.make_url("")).rstrip("/")
    data_dir = tmp_path / "data"
    recorder = ParquetRecorder(data_dir, flush_rows=1000, flush_seconds=3600)
    async with aiohttp.ClientSession() as session:
        alerts = Alerts(session, "token", "chat", f"{base}/telegram")

        def make() -> CalendarCollector:
            return CalendarCollector(
                session=session,
                data_dir=data_dir,
                countries=("US", "KR", "JP"),
                alerts=alerts,
                recorder=recorder,
                now=lambda: NOW,
                url=f"{base}/events",
            )

        yield Setup(feed, data_dir, make, recorder)
        await alerts.close()
    await server.close()


def recorded(data_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for partition in sorted((data_dir / LAYER).glob("date=*/feed=*/symbol=*")):
        for path in partition_files(partition):
            rows.extend(pq.read_table(path).to_pylist())
    return sorted(rows, key=lambda r: (r["recorded_at"], r["id"]))


async def test_a_read_keeps_the_answer_and_records_every_event_once(setup: Setup) -> None:
    collector = setup.make()
    assert await collector.run()
    assert setup.feed.asked == [query(["US", "KR", "JP"], NOW)]
    assert setup.feed.origins == ["https://www.tradingview.com"]
    kept = read_latest(setup.data_dir)
    assert kept is not None
    assert kept["fetched_at"] == "2026-10-13T09:15:00+09:00"
    assert kept["countries"] == ["US", "KR", "JP"]
    assert [e["id"] for e in kept["events"]] == ["3", "1", "2", "7"]
    rows = recorded(setup.data_dir)
    assert sorted(r["id"] for r in rows) == ["1", "2", "3", "7"]
    by_country = {p.name for p in (setup.data_dir / LAYER / "date=2026-10-13" / "feed=tradingview").iterdir()}
    assert by_country == {"symbol=US", "symbol=KR", "symbol=JP"}

    # The same answer again: nothing new to record.
    assert await collector.run()
    assert len(recorded(setup.data_dir)) == 4

    # The actual arrives for one event: only it is recorded again.
    setup.feed.items = [dict(i, actual=3.9) if i["id"] == "2" else i for i in ITEMS]
    assert await collector.run()
    rows = recorded(setup.data_dir)
    assert len(rows) == 5
    assert [r["actual"] for r in rows if r["id"] == "2"] == [None, 3.9]  # in the order recorded

    # A restarted collector starts from the kept answer: nothing recorded again.
    assert await setup.make().run()
    assert len(recorded(setup.data_dir)) == 5


async def test_failures_keep_the_last_answer_and_alert_after_an_hour(setup: Setup) -> None:
    collector = setup.make()
    assert await collector.run()
    kept = read_latest(setup.data_dir)
    setup.feed.down = True
    for _ in range(ALERT_AFTER + 2):
        assert not await collector.run()
    await asyncio.sleep(0.2)  # the alert is sent in the background
    assert read_latest(setup.data_dir) == kept
    assert len(setup.feed.sent) == 1
    assert setup.feed.sent[0].startswith("[장애] 경제 캘린더(TradingView)를 4번 연속 읽지 못했습니다")
    setup.feed.down = False
    assert await collector.run()
    await asyncio.sleep(0.2)
    assert setup.feed.sent[1:] == ["[복구] 경제 캘린더를 다시 읽습니다."]
    assert collector.failures == 0


async def test_the_task_reads_now_and_then_every_interval(setup: Setup) -> None:
    collector = setup.make()
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) == 3:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await keep_calendar(collector, timedelta(minutes=15), sleep)
    assert slept == [900.0, 900.0, 900.0]
    assert len(setup.feed.asked) == 3


def test_the_answer_shows_today_on_in_time_order_up_to_the_limit(tmp_path: Path) -> None:
    events = parse_tradingview({"status": "ok", "result": ITEMS})
    kept = {
        "fetched_at": "2026-10-13T09:00:00+09:00",
        "source": "tradingview",
        "countries": ["US"],
        "events": [e.to_json() for e in events],
    }
    answer = calendar_answer(kept, NOW, 100)
    # Machinery Orders came out on the 12th at 08:50 KST; the unemployment rate today at 08:00.
    assert [e["id"] for e in answer["events"]] == ["1", "2", "7"]
    assert [e["id"] for e in calendar_answer(kept, NOW, 1)["events"]] == ["1"]
    assert calendar_answer(None, NOW, 10) == {
        "fetched_at": None,
        "source": "tradingview",
        "countries": [],
        "events": [],
    }


async def test_the_engine_api_serves_the_kept_answer(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    events = parse_tradingview({"status": "ok", "result": ITEMS})
    latest_path(data_dir).parent.mkdir(parents=True)
    latest_path(data_dir).write_text(
        json.dumps(
            {
                "fetched_at": "2026-10-13T09:00:00+09:00",
                "source": "tradingview",
                "countries": ["US"],
                "events": [e.to_json() for e in events],
            }
        )
    )
    engine = await run(tmp_path / "engine.db", [], until=at("09:00:00"))  # 2026-10-12, the engine's today
    async with TestClient(TestServer(create_app(engine, data_dir=data_dir))) as client:
        response = await client.get("/v1/calendar?limit=2")
        assert response.status == 200
        body = await response.json()
        check("calendar", body)
        # Machinery Orders came out at 08:50 KST that day, so it is still shown.
        assert [e["id"] for e in body["events"]] == ["3", "1"]
        bad = await client.get("/v1/calendar?limit=x")
        assert bad.status == 400


def test_the_settings_read_the_interval_and_the_countries() -> None:
    base = {"KIS_APP_KEY": "k", "KIS_APP_SECRET": "s"}
    default = Settings.from_env(base, dotenv_path=None)
    assert default.calendar_every == timedelta(minutes=15)
    assert default.calendar_countries == ("US", "KR", "CN", "JP", "EU", "DE", "GB")
    chosen = Settings.from_env(
        {**base, "KRX_CALENDAR_EVERY": "30", "KRX_CALENDAR_COUNTRIES": "us, kr"}, dotenv_path=None
    )
    assert (chosen.calendar_every, chosen.calendar_countries) == (timedelta(minutes=30), ("US", "KR"))
    assert Settings.from_env({**base, "KRX_CALENDAR_EVERY": "off"}, dotenv_path=None).calendar_every is None
    for wrong in (
        {"KRX_CALENDAR_EVERY": "2"},
        {"KRX_CALENDAR_EVERY": "soon"},
        {"KRX_CALENDAR_COUNTRIES": "USA"},
    ):
        with pytest.raises(SettingsError):
            Settings.from_env({**base, **wrong}, dotenv_path=None)
