"""Every Engine API answer keeps the /v1 contract: ``schemas/engine-api-v1.json``
(``tests/api_schema.py``).

Input: engines in each mode (an empty replayed morning, the demo engine, the live
collector on the fake KIS seam), asked every endpoint with its query variants and its
errors. Output: each body checked against the schema of its endpoint.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer
from jsonschema import Draft202012Validator

from krx_quant.api import create_app
from krx_quant.clock import FakeClock
from krx_quant.collector import run_collector
from krx_quant.demo import demo_app
from krx_quant.fake_kis import FakeKis
from tests.api_schema import ROUTES, SCHEMA, STREAM, check
from tests.kis_frames import book_frame
from tests.replay_events import LIVE_PREDICTORS, api, at, run
from tests.test_collector import settings_for
from tests.test_live_predictions import free_port, poll

Get = Callable[[str], Awaitable[tuple[int, Any]]]  # a path to its status and JSON body


def through(client: TestClient[Any, Any]) -> Get:
    async def get(path: str) -> tuple[int, Any]:
        response = await client.get(path)
        return response.status, await response.json()

    return get


async def walk(get: Get) -> dict[str, int]:
    """Ask every endpoint, its query variants and its errors; check each answer. Returns
    how many answers each definition checked."""
    checked: dict[str, int] = {}

    async def ask(path: str, definition: str, status: int = 200) -> Any:
        answered, body = await get(path)
        assert answered == status, f"{path}: {answered} {body}"
        check(definition, body)
        checked[definition] = checked.get(definition, 0) + 1
        return body

    await ask("/v1/status", "status")
    symbols = [s["symbol"] for s in (await ask("/v1/symbols", "symbols"))["symbols"]]
    algorithms = [a["name"] for a in (await ask("/v1/algorithms", "algorithms"))["algorithms"]]
    for symbol in symbols:
        await ask(f"/v1/symbols/{symbol}/latest", "latest")
        await ask(f"/v1/symbols/{symbol}/predictions", "predictions")
        await ask(f"/v1/symbols/{symbol}/predictions?limit=5&algorithm=consensus-final", "predictions")
        await ask(f"/v1/symbols/{symbol}/bars", "bars")
        await ask(f"/v1/symbols/{symbol}/bars?limit=500", "bars")  # into earlier days, if stored
        await ask(
            f"/v1/symbols/{symbol}/predictions?before=2026-10-12T10:00:00%2B09:00&limit=5", "predictions"
        )
    for query in ("", "?period=5d&basis=a&special=include", f"?symbol={symbols[0]}&period=20d"):
        await ask(f"/v1/leaderboard{query}", "leaderboard")
    for name in algorithms:
        await ask(f"/v1/algorithms/{name}", "algorithm_detail")
    for name in [*algorithms, "baseline-flat", "consensus-final"]:
        await ask(f"/v1/algorithms/{name}/stats", "algorithm_stats")
    await ask("/v1/consensus", "consensus")
    await ask("/v1/macro", "macro")
    for path, status in (
        ("/v1/symbols/999999/latest", 404),
        ("/v1/symbols/005930/predictions?limit=0", 400),
        ("/v1/symbols/005930/predictions?before=10:00", 400),
        ("/v1/symbols/005930/bars?limit=0", 400),
        ("/v1/symbols/005930/bars?before=10:00", 400),
        ("/v1/leaderboard?period=week", 400),
        ("/v1/algorithms/nobody/stats", 404),
        ("/v1/algorithms/nobody", 404),
    ):
        await ask(path, "error", status)
    assert set(checked) == {*ROUTES.values(), "error"}, "every route asked"
    return checked


async def test_every_route_has_its_definition(tmp_path: Path) -> None:
    engine = await run(tmp_path / "engine.db", [], until=at("09:00:00"))

    routes = {
        r.resource.canonical for r in create_app(engine).router.routes() if r.method == "GET" and r.resource
    }

    assert routes == set(ROUTES) | set(STREAM)
    assert {*ROUTES.values(), *STREAM.values()} <= set(SCHEMA["$defs"])


async def test_the_status_names_the_commit_running_and_when_the_engine_started(tmp_path: Path) -> None:
    engine = await run(tmp_path / "engine.db", [], until=at("09:00:00"))
    async with TestClient(TestServer(create_app(engine))) as client:
        unknown = await (await client.get("/v1/status")).json()
        engine.commit = "0123456789abcdef0123456789abcdef01234567"
        deployed = await (await client.get("/v1/status")).json()

    check("status", deployed)
    assert unknown["commit"] == "unknown", "an engine not built by the auto-deploy"
    assert deployed["commit"] == "0123456789abcdef0123456789abcdef01234567"
    assert deployed["started_at"] == "2026-10-12T00:00:00+09:00", "the replay's clock when it was made"


async def test_live_and_demo_need_their_kis_status(tmp_path: Path) -> None:
    engine = await run(tmp_path / "engine.db", [], until=at("09:00:00"))

    for mode in ("live", "demo"):
        with pytest.raises(ValueError, match="kis"):
            create_app(engine, mode=mode)


def test_the_schema_is_valid_and_closed() -> None:
    Draft202012Validator.check_schema(SCHEMA)

    # Fragments others extend with allOf, closing them with unevaluatedProperties.
    fragments = {"/$defs/entry_fields", "/$defs/query", "/$defs/algorithm_fields", "/$defs/prediction_fields"}

    def open_objects(node: Any, path: str) -> list[str]:
        found = []
        if isinstance(node, dict):
            closed = "additionalProperties" in node or "unevaluatedProperties" in node
            if "properties" in node and not closed and path not in fragments:
                found.append(path)
            for key, value in node.items():
                if key != "if":  # a condition reads fields, it does not name them
                    found += open_objects(value, f"{path}/{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                found += open_objects(value, f"{path}/{i}")
        return found

    assert open_objects(SCHEMA, "") == [], "a field the schema does not name must fail the contract"


async def test_an_empty_morning_keeps_the_contract(tmp_path: Path) -> None:
    engine = await run(tmp_path / "engine.db", [], until=at("09:00:00"))

    async with api(engine) as client:
        checked = await walk(through(client))

    assert checked["status"] == 1


async def test_the_demo_engine_keeps_the_contract(tmp_path: Path) -> None:
    app = await demo_app(tmp_path / "demo.sqlite3")

    async with TestClient(TestServer(app)) as client:
        checked = await walk(through(client))

    assert checked["latest"] == 3


async def test_the_live_engine_keeps_the_contract(kis: FakeKis, tmp_path: Path) -> None:
    port = free_port()
    settings = replace(settings_for(kis, tmp_path), api_port=port)
    clock = FakeClock(at("09:30:10"))
    kis.script[("H0UNASP0", "005930")] = [
        book_frame({"MKSC_SHRN_ISCD": "005930", "BSOP_HOUR": "093010", "BIDP1": "81200", "ASKP1": "81300"})
    ]
    stop = asyncio.Event()
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=clock))
    try:
        await kis.wait_for_subscriptions(set(kis.script))
        await kis.round_trip()
        base = f"http://127.0.0.1:{port}"
        await poll(f"{base}/v1/status", lambda b: True)
        async with aiohttp.ClientSession(base) as http, http.ws_connect("/v1/stream") as ws:
            clock.set(at("09:31:00.2"))
            events: list[dict[str, Any]] = []
            async with asyncio.timeout(5):
                while not events or events[-1].get("kind") != "final":
                    events.append(await ws.receive_json())
                    check("event", events[-1])
            await poll(
                f"{base}/v1/symbols/005930/latest", lambda b: len(b["predictions"]) == len(LIVE_PREDICTORS)
            )

            async def get(path: str) -> tuple[int, Any]:
                async with http.get(path) as response:
                    return response.status, await response.json()

            checked = await walk(get)
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)

    assert checked["status"] == 1
    assert events[0]["type"] == "health", "the KIS status first, on connecting"
    assert {e["type"] for e in events} >= {"prediction", "consensus"}
