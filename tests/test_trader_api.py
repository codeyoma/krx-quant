"""``GET /v1/trader/trades`` (#394): the trader's trades as its store keeps them, within the /v1
contract (``schemas/engine-api-v1.json``)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aiohttp.test_utils import TestClient, TestServer

from krx_quant.api import create_app
from tests.api_schema import check
from tests.replay_events import at as replay_at
from tests.replay_events import run
from tests.trader_doubles import Barrier, Day, Event, Read, quote

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
