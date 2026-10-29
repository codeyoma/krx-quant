"""The trader through the Engine API, within the /v1 contract (``schemas/engine-api-v1.json``):
its trades (``GET /v1/trader/trades``, #394), its state and days (``GET /v1/trader``,
``/v1/trader/days``, #396), and the screen's pause (``POST /v1/trader/pause``, #396)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from aiohttp.test_utils import TestClient, TestServer

from krx_quant.api import create_app
from krx_quant.trader.paper import Rules
from tests.api_schema import check
from tests.replay_events import at as replay_at
from tests.replay_events import run
from tests.trader_doubles import FEE, AskPause, AskResume, Barrier, Day, Event, Read, Tick, at, quote

DAY: list[Event] = [
    Read("10:00:02.3", [Barrier("10:00:00")]),
    quote("10:00:02.5", 70_000, 70_100),
    quote("10:02:00", 70_300, 70_400),  # take-profit
    Read("10:05:02.3", [Barrier("10:05:00")]),
    quote("10:05:02.5", 70_000, 70_100),
    quote("10:06:00", 69_800, 69_900),  # stop
    Read("10:10:02.3", [Barrier("10:10:00")]),
    quote("10:10:02.5", 70_000, 70_100),  # bought, still open
]


async def trades(scratch: Path, state: Path | None, query: str = "") -> tuple[int, Any]:
    """The answer of an engine (replayed into ``scratch``) whose state folder is ``state``."""
    engine = await run(scratch / "replay.db", [], until=replay_at("09:00:00"))
    async with TestClient(TestServer(create_app(engine, state_dir=state))) as client:
        response = await client.get(f"/v1/trader/trades{query}")
        return response.status, await response.json()


async def test_the_trades_newest_first_each_with_its_orders_and_fills(tmp_path: Path) -> None:
    Day(tmp_path).run(DAY)

    status, body = await trades(tmp_path, tmp_path)

    assert status == 200
    check("trader_trades", body)
    assert [t["bar_close"][11:16] for t in body["trades"]] == ["10:10", "10:05", "10:00"]
    open_, stopped, taken = body["trades"]
    assert open_["exit"] is None and open_["net"] is None and open_["sell_fee"] is None
    assert [(o["purpose"], o["status"]) for o in open_["orders"]] == [
        ("entry", "filled"),
        ("take_profit", "open"),
    ]
    assert stopped["exit"]["reason"] == "stop_loss" and taken["exit"]["reason"] == "take_profit"
    assert taken["orders"][0]["fills"] == [
        {
            "at": "2026-10-14T10:00:02.500000+09:00",
            "price": 70_100,
            "quantity": 14,
            "quote_at": "2026-10-14T10:00:02.500000+09:00",
        }
    ]
    assert {t["mode"] for t in body["trades"]} == {"paper"}


async def test_before_and_limit_page_back(tmp_path: Path) -> None:
    Day(tmp_path).run(DAY)

    _, page = await trades(tmp_path, tmp_path, "?before=2026-10-14T10:10:02.5%2B09:00&limit=1")
    _, earlier = await trades(tmp_path, tmp_path, "?before=2026-10-14T10:05:02.5%2B09:00")

    assert [t["bar_close"][11:16] for t in page["trades"]] == ["10:05"], "entered before 10:10:02.5"
    assert [t["bar_close"][11:16] for t in earlier["trades"]] == ["10:00"]


async def test_no_trades_before_the_trader_s_first(tmp_path: Path) -> None:
    assert await trades(tmp_path, tmp_path / "empty") == (200, {"trades": []})
    assert await trades(tmp_path, None) == (200, {"trades": []}), (
        "an engine without a state folder (replay, demo)"
    )


async def test_a_bad_query_is_refused(tmp_path: Path) -> None:
    for query in ("?limit=0", "?limit=1001", "?before=10:00"):
        status, body = await trades(tmp_path, None, query)
        assert status == 400
        check("error", body)


# GET /v1/trader, GET /v1/trader/days and POST /v1/trader/pause (#396).

PAUSED_DAY: list[Event] = [
    *DAY[:6],  # a take-profit and a stop
    AskPause("10:07:00"),
    Read("10:10:02.3", [Barrier("10:10:00")]),  # not bought: entries paused
    Read("2026-10-15 10:00:02.3", [Barrier("2026-10-15 10:00:00")]),
    AskResume("2026-10-15 10:00:02.4"),
    quote("2026-10-15 10:00:02.5", 70_000, 70_100),
    Read("2026-10-15 10:05:02.3", [Barrier("2026-10-15 10:05:00")]),
    quote("2026-10-15 10:05:02.5", 70_000, 70_100),  # bought, still open
    quote("2026-10-15 10:06:00", 69_900, 70_000),
]


@asynccontextmanager
async def engine_api(scratch: Path, state: Path | None) -> AsyncIterator[TestClient[Any, Any]]:
    engine = await run(scratch / "replay.db", [], until=replay_at("09:00:00"))
    async with TestClient(TestServer(create_app(engine, state_dir=state))) as client:
        yield client


async def test_the_trader_s_state_with_its_position_limits_and_controls(tmp_path: Path) -> None:
    Day(tmp_path).run(PAUSED_DAY)

    async with engine_api(tmp_path, tmp_path) as client:
        response = await client.get("/v1/trader")
        body = await response.json()

    assert response.status == 200
    check("trader", body)
    assert (body["mode"], body["status"], body["alive"]) == ("paper", "running", False), (
        "the replay's clock is days before the state: not running"
    )
    assert body["updated_at"] == "2026-10-15T10:06:00+09:00"
    assert body["position"]["bid"] == 69_900 and body["position"]["quantity"] == 14
    assert body["today"]["day"] == "2026-10-15" and body["today"]["trades"] == 0
    assert body["limits"]["daily"]["won"] == 100_000 and body["limits"]["cumulative"]["won"] == 200_000
    assert [(c["action"], c["source"], c["sell_now"]) for c in body["controls"]] == [
        ("resume", "cli", None),
        ("pause", "api", False),
    ], "newest first"
    assert all(c["applied_at"] is not None for c in body["controls"])


async def test_the_trader_s_days_oldest_first(tmp_path: Path) -> None:
    Day(tmp_path).run(PAUSED_DAY)

    async with engine_api(tmp_path, tmp_path) as client:
        body = await (await client.get("/v1/trader/days?from=2026-10-01&to=2026-10-31")).json()
        default = await (await client.get("/v1/trader/days")).json()

    check("trader_days", body)
    fourteenth, fifteenth = body["days"]
    assert (fourteenth["day"], fourteenth["trades"], fourteenth["status"]) == (
        "2026-10-14",
        2,
        "entries_paused",
    )
    assert fourteenth["net"] < 0 and fourteenth["daily"]["worst"] <= fourteenth["net"]
    assert (fifteenth["day"], fifteenth["trades"], fifteenth["status"]) == ("2026-10-15", 0, "running")
    assert (default["from"], default["to"]) == ("2025-10-13", "2026-10-12"), "the year to the engine's today"


async def test_no_trader_state_before_the_trader_s_first(tmp_path: Path) -> None:
    for state in (tmp_path / "empty", None):
        async with engine_api(tmp_path, state) as client:
            body = await (await client.get("/v1/trader")).json()
            days = await (await client.get("/v1/trader/days")).json()
        check("trader", body)
        check("trader_days", days)
        assert (body["status"], body["alive"], body["controls"]) == (None, False, [])
        assert days["days"] == []


async def test_a_pause_from_the_screen_is_written_for_the_trader_to_read(tmp_path: Path) -> None:
    """The engine writes the pause into the trader's store (made if the trader has not made it yet)
    and answers 202 with it; the trader reads it there (``applied_at`` null until then)."""
    async with engine_api(tmp_path, tmp_path) as client:
        response = await client.post("/v1/trader/pause", json={"sell_now": True})
        written = await response.json()
        state = await (await client.get("/v1/trader")).json()

    assert response.status == 202
    check("trader_pause", written)
    control = written["control"]
    assert (control["action"], control["source"], control["sell_now"], control["applied_at"]) == (
        "pause",
        "api",
        True,
        None,
    )
    assert control["at"] == "2026-10-12T09:00:00+09:00", "the engine's clock"
    assert state["controls"] == [control]
    Day(tmp_path, started_at=at("10:00:00")).run([Tick("10:00:00.5")])
    async with engine_api(tmp_path, tmp_path) as client:
        state = await (await client.get("/v1/trader")).json()
    assert state["status"] == "paused" and state["controls"][0]["applied_at"] == "2026-10-14T10:00:00+09:00"


async def test_a_pause_asks_for_json_with_sell_now_alone(tmp_path: Path) -> None:
    async with engine_api(tmp_path, tmp_path) as client:
        for body, status in (
            ({}, 400),
            ({"sell_now": "yes"}, 400),
            ({"sell_now": 1}, 400),
            ({"sell_now": True, "resume": True}, 400),
            ([True], 400),
        ):
            response = await client.post("/v1/trader/pause", json=body)
            assert response.status == status, body
            check("error", await response.json())
        broken = await client.post(
            "/v1/trader/pause", data="{sell_now", headers={"Content-Type": "application/json"}
        )
        assert broken.status == 400
        form = await client.post("/v1/trader/pause", data='{"sell_now": true}')  # a form's text/plain
        assert form.status == 415, "a cross-site form cannot pause the trader without asking first"
        check("error", await form.json())
        for path in ("/v1/trader/resume", "/v1/trader/rearm", "/v1/trader/mode"):
            assert (await client.post(path, json={})).status in (404, 405), "the API resumes nothing"
    assert not (tmp_path / "trader.sqlite3").exists(), "a refused pause writes nothing"
    async with engine_api(tmp_path, None) as client:
        response = await client.post("/v1/trader/pause", json={"sell_now": False})
        assert response.status == 503
        check("error", await response.json())


async def test_the_chart_marks_a_sale_by_a_pause_or_a_limit(tmp_path: Path) -> None:
    """#398's marks show #396's sales: a pause selling now, and the daily limit reached."""
    paused = tmp_path / "paused"
    Day(paused).run([*DAY[:2], AskPause("10:00:30", sell_now=True), quote("10:00:31", 70_000, 70_100)])
    limited = tmp_path / "limited"
    Day(limited, rules=Rules(fee_rate=FEE, daily_loss_percent=0.5)).run(
        [*DAY[:2], quote("10:01:30", 69_900, 70_000)]
    )

    for state, reason in ((paused, "pause"), (limited, "daily_limit")):
        async with engine_api(tmp_path, state) as client:
            body = await (await client.get("/v1/symbols/005930/trader-marks")).json()
        check("trader_marks", body)
        sell, buy = body["marks"]
        assert (sell["side"], sell["exit_reason"], buy["side"]) == ("sell", reason, "buy")
