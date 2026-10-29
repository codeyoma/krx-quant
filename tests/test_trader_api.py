"""``GET /v1/trader/trades`` (#394) and ``GET /v1/trader/policies`` (#399): the trader's trades and
policy versions as its store keeps them, within the /v1 contract (``schemas/engine-api-v1.json``)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aiohttp.test_utils import TestClient, TestServer

from krx_quant.api import create_app
from krx_quant.predictions import DB_NAME as ENGINE_DB
from krx_quant.touch import TARGET_VERSION
from krx_quant.trader import CAPITAL
from krx_quant.trader.policy import FIRST
from krx_quant.trader.refit import Grid, refit_from
from krx_quant.trader.store import DB_NAME as TRADER_DB
from tests.api_schema import check
from tests.replay_events import at as replay_at
from tests.replay_events import run
from tests.test_trader_refit import DAYS, write_five
from tests.trader_doubles import CALENDAR, FEE, Barrier, Day, Event, Read, at, quote

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


# ``GET /v1/trader/policies`` (#399).


async def policies_answer(scratch: Path, state: Path | None, query: str = "") -> tuple[int, Any]:
    engine = await run(scratch / "replay.db", [], until=replay_at("09:00:00"))
    async with TestClient(TestServer(create_app(engine, state_dir=state))) as client:
        response = await client.get(f"/v1/trader/policies{query}")
        return response.status, await response.json()


async def test_the_policies_newest_first_the_champion_marked(tmp_path: Path) -> None:
    """Two nights' re-fits of the same five predictions: the first the champion, the second a
    challenger with the same values."""
    write_five(tmp_path)
    for night in ("22:31:00", "22:45:00"):
        refit_from(
            DAYS,
            tmp_path / ENGINE_DB,
            tmp_path / TRADER_DB,
            CALENDAR.session,
            capital=CAPITAL,
            fee_rate=FEE,
            target_version=TARGET_VERSION,
            grid=Grid(min_trades=1),
            now=at(night),
        )

    status, body = await policies_answer(tmp_path, tmp_path)
    _, page = await policies_answer(tmp_path, tmp_path, "?limit=1")

    assert status == 200
    check("trader_policies", body)
    assert body["champion"] == 1
    assert [(p["version"], p["status"]) for p in body["policies"]] == [(2, "challenger"), (1, "champion")]
    assert [p["version"] for p in page["policies"]] == [2]
    challenger, champion = body["policies"]
    assert challenger["cells"] == champion["cells"]
    [cell] = champion["cells"]
    assert (cell["kind"], cell["predictor"], cell["threshold"], cell["kelly_fraction"]) == (
        "barrier",
        "breiman",
        0.7,
        0.5,
    )
    assert champion["fit"]["result"]["net"] == 7_000
    assert body["champions"] == [{"at": "2026-10-14T22:31:00+09:00", "version": 1, "reason": FIRST}]


async def test_no_policies_before_the_first_fit(tmp_path: Path) -> None:
    empty: dict[str, Any] = {"champion": None, "policies": [], "champions": []}
    assert await policies_answer(tmp_path, tmp_path / "empty") == (200, empty)
    assert await policies_answer(tmp_path, None) == (200, empty)
    Day(tmp_path).run(DAY)  # a trader's store without a policy yet
    assert await policies_answer(tmp_path, tmp_path) == (200, empty)
    status, body = await policies_answer(tmp_path, None, "?limit=0")
    assert status == 400
    check("error", body)


async def test_a_trade_carries_its_policy(tmp_path: Path) -> None:
    Day(tmp_path).run(DAY)
    _, body = await trades(tmp_path, tmp_path)
    assert {t["policy"] for t in body["trades"]} == {None}, "before the first champion"


async def test_a_store_the_nightly_fit_made_before_the_trader_s_first_start_answers_every_route(
    tmp_path: Path,
) -> None:
    """The re-fit ran before the trader ever started (a collector without it): the store it made
    holds the trader's tables too, and every trader route answers with nothing yet."""
    write_five(tmp_path)
    assert not (tmp_path / TRADER_DB).exists()
    refit_from(
        DAYS,
        tmp_path / ENGINE_DB,
        tmp_path / TRADER_DB,
        CALENDAR.session,
        capital=CAPITAL,
        fee_rate=FEE,
        target_version=TARGET_VERSION,
        grid=Grid(min_trades=1),
        now=at("22:31:00"),
    )

    engine = await run(tmp_path / "replay.db", [], until=replay_at("09:00:00"))
    async with TestClient(TestServer(create_app(engine, state_dir=tmp_path))) as client:
        answers = {}
        for route in ("/v1/trader/trades", "/v1/trader/policies", "/v1/symbols/005930/trader-marks"):
            response = await client.get(route)
            answers[route] = (response.status, await response.json())

    assert answers["/v1/trader/trades"] == (200, {"trades": []})
    assert answers["/v1/symbols/005930/trader-marks"] == (200, {"symbol": "005930", "marks": []})
    status, policies = answers["/v1/trader/policies"]
    assert (status, policies["champion"], len(policies["policies"])) == (200, 1, 1)
    check("trader_trades", answers["/v1/trader/trades"][1])
    check("trader_policies", policies)
