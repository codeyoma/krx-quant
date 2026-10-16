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
from tests.api_schema import KIND_ROUTES, ROUTES, SCHEMA, STREAM, check
from tests.kind_doubles import TEST_KIND, FixedKindAlgorithm
from tests.kis_frames import book_frame
from tests.replay_events import LIVE_PREDICTORS, api, at, run
from tests.test_collector import settings_for
from tests.test_live_predictions import free_port, poll
from tests.test_replay import busy_morning

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
        for period in ("day", "week", "month", "year"):
            await ask(f"/v1/symbols/{symbol}/candles?period={period}", "candles")
        await ask(
            f"/v1/symbols/{symbol}/candles?period=day&before=2026-10-08T00:00:00%2B09:00&limit=3", "candles"
        )
        await ask(
            f"/v1/symbols/{symbol}/predictions?before=2026-10-12T10:00:00%2B09:00&limit=5", "predictions"
        )
        await ask(f"/v1/symbols/{symbol}/special", "special")  # #236
        await ask(f"/v1/symbols/{symbol}/special?from=2026-10-01&to=2026-10-12", "special")
        await ask(f"/v1/symbols/{symbol}/supply", "supply")  # #226
        await ask(f"/v1/symbols/{symbol}/supply?from=2026-10-01&to=2026-10-12", "supply")
    for query in ("", "?period=5d&basis=a&special=include", f"?symbol={symbols[0]}&period=20d"):
        await ask(f"/v1/leaderboard{query}", "leaderboard")
    for name in algorithms:
        await ask(f"/v1/algorithms/{name}", "algorithm_detail")
    for name in [*algorithms, "baseline-flat", "consensus-final"]:
        await ask(f"/v1/algorithms/{name}/stats", "algorithm_stats")
    await ask(f"/v1/algorithms/{algorithms[0]}/stats?basis=b", "algorithm_stats")  # a screen from before #170
    await ask("/v1/consensus", "consensus")
    await ask("/v1/macro", "macro")
    await ask("/v1/calendar", "calendar")
    await ask("/v1/calendar?limit=5", "calendar")
    listed = (await ask("/v1/kinds", "kinds"))["kinds"]
    kinds = [k["name"] for k in listed]
    assert kinds[0] == "direction"
    direction = await ask("/v1/leaderboard?kind=direction", "leaderboard")
    assert direction == await ask("/v1/leaderboard", "leaderboard"), "the direction's, as before (#179)"
    for kind in listed[1:]:  # the kinds besides the direction (#179)
        name = kind["name"]
        for symbol in symbols:
            await ask(f"/v1/symbols/{symbol}/kinds/{name}/latest", "kind_latest")
            await ask(f"/v1/symbols/{symbol}/kinds/{name}/predictions", "kind_predictions")
            await ask(
                f"/v1/symbols/{symbol}/kinds/{name}/predictions?limit=5&before=2026-10-12T10:00:00%2B09:00",
                "kind_predictions",
            )
            await ask(
                f"/v1/symbols/{symbol}/kinds/{name}/predictions?bar_close=2026-10-12T09:31:00%2B09:00",
                "kind_predictions",
            )
        for query in ("", "&period=5d&special=include", f"&symbol={symbols[0]}&period=20d"):
            check("kind_leaderboard", await ask(f"/v1/leaderboard?kind={name}{query}", "leaderboard"))
        for baseline in kind["baselines"]:
            check(
                "kind_algorithm_stats",
                await ask(f"/v1/algorithms/{baseline['name']}/stats", "algorithm_stats"),
            )
    for path, status in (
        ("/v1/symbols/005930/kinds/direction/latest", 404),
        ("/v1/symbols/005930/kinds/nothing/predictions", 404),
        ("/v1/leaderboard?kind=nothing", 400),
    ):
        await ask(path, "error", status)
    for path, status in (
        ("/v1/symbols/999999/latest", 404),
        ("/v1/symbols/005930/predictions?limit=0", 400),
        ("/v1/symbols/005930/predictions?before=10:00", 400),
        ("/v1/symbols/005930/bars?limit=0", 400),
        ("/v1/symbols/005930/bars?before=10:00", 400),
        ("/v1/symbols/005930/candles", 400),
        ("/v1/symbols/005930/candles?period=minute", 400),
        ("/v1/symbols/005930/candles?period=quarter", 400),
        ("/v1/symbols/005930/candles?period=day&limit=0", 400),
        ("/v1/symbols/999999/candles?period=day", 404),
        ("/v1/symbols/999999/special", 404),
        ("/v1/symbols/005930/special?from=2026-10-1", 400),
        ("/v1/symbols/005930/special?from=2026-10-12&to=2026-10-11", 400),
        ("/v1/symbols/005930/special?from=2026-08-13&to=2026-10-12", 400),
        ("/v1/symbols/999999/supply", 404),
        ("/v1/symbols/005930/supply?to=2026-10-1", 400),
        ("/v1/symbols/005930/supply?from=2026-10-12&to=2026-10-11", 400),
        ("/v1/symbols/005930/supply?from=2015-01-01&to=2026-10-12", 400),
        ("/v1/leaderboard?period=week", 400),
        ("/v1/algorithms/nobody/stats", 404),
        ("/v1/algorithms/nobody", 404),
        ("/v1/calendar?limit=0", 400),
    ):
        await ask(path, "error", status)
    asked = {*ROUTES.values(), "error"} - (set() if kinds[1:] else KIND_ROUTES)
    assert set(checked) == asked, "every route asked"
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
    fragments = {
        "/$defs/entry_fields",
        "/$defs/query",
        "/$defs/algorithm_fields",
        "/$defs/prediction_fields",
        # The kinds besides the direction's (#179).
        "/$defs/kind_entry_fields",
        "/$defs/kind_prediction_fields",
        "/$defs/kind_answer_fields",
    }

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


async def test_a_morning_with_another_kind_keeps_the_contract(tmp_path: Path) -> None:
    """#179. Input: a replayed busy morning with the test kind (``drift``) and an algorithm of it.
    Output: every endpoint, the kind's included (its predictions, its leaderboard, its
    predictors' stats), within the contract."""
    engine = await run(
        tmp_path / "engine.db",
        busy_morning(),
        until=at("10:00:00"),
        target_version=2,
        kinds=(TEST_KIND,),
        kind_algorithms=[FixedKindAlgorithm("drifter", features=("velocity",), unique=("velocity",))],
    )

    async with api(engine) as client:
        checked = await walk(through(client))
        latest = await (await client.get("/v1/symbols/005930/kinds/drift/latest")).json()
        board = await (await client.get("/v1/leaderboard?kind=drift")).json()

    assert checked["kind_latest"] == 1
    assert {p["algorithm"] for p in latest["predictions"]} == {"drift-held", "drift-persist", "drifter"}
    assert [e["algorithm"] for e in board["unranked"]] == ["drifter"]  # too few to rank
    assert board["baselines"][0]["n"] > 0, "scored"


async def test_a_morning_with_the_range_and_the_barrier_keeps_the_contract(tmp_path: Path) -> None:
    """#180, #183. Input: a replayed busy morning with the kinds the engine predicts (the range
    and the barrier). Output: every endpoint within the contract; each kind's outputs, answer
    paths and own statistics within theirs (``range_output``, ``range_path``, ``range_stats``;
    ``barrier_output``, ``barrier_path``, ``barrier_stats``); a scored prediction's k
    (``flat_ticks``, #200) is its answer's, its mid-point and tick (``anchor``, ``tick``, #210)
    its answer's, and a barrier prediction's width (``width_ticks``) its answer's w."""
    engine = await run(tmp_path / "engine.db", busy_morning(), until=at("10:00:00"), target_version=2)

    async with api(engine) as client:
        checked = await walk(through(client))
        made = {
            kind: await (await client.get(f"/v1/symbols/005930/kinds/{kind}/predictions?limit=60")).json()
            for kind in ("range", "barrier")
        }
        boards = {
            kind: await (await client.get(f"/v1/leaderboard?kind={kind}")).json()
            for kind in ("range", "barrier")
        }

    assert checked["kind_latest"] == 2
    for kind in ("range", "barrier"):
        ok = [p for p in made[kind]["predictions"] if p["status"] == "ok"]
        assert ok and all(p["answer"] is not None for p in ok if p["bar_close"] < "2026-10-12T09:56")
        for p in ok:
            check(f"{kind}_output", p["output"])
            if p["answer"] is not None:
                check(f"{kind}_path", p["answer"]["path"])
                assert p["flat_ticks"] == p["answer"]["path"]["k"], "the bar close's k, the answer's (#200)"
                reference = (p["answer"]["path"]["anchor"], p["answer"]["path"]["tick"])
                assert (p["anchor"], p["tick"]) == reference, "the bar close's mid-point, the answer's (#210)"
                if kind == "barrier":
                    assert p["width_ticks"] == p["answer"]["path"]["w"], "the bar close's width, the answer's"
                else:
                    assert p["width_ticks"] is None, "the range has no barriers"
        for e in boards[kind]["baselines"]:
            check(f"{kind}_stats", e["extra"])


async def test_a_screen_from_before_reads_the_touch_label_where_it_read_the_bases(tmp_path: Path) -> None:
    """For one release (#170). Input: a replayed morning on the touch target, read as the
    screen before #170 reads it. Output: every scored outcome's ``class_a`` and ``class_b`` are
    its bar close's touch label, ``unscorable`` why there is none; the leaderboard and the
    stats echo the basis asked for; every answer within the contract."""
    engine = await run(tmp_path / "engine.db", busy_morning(), until=at("10:00:00"), target_version=2)

    async with api(engine) as client:
        predictions = (await (await client.get("/v1/symbols/005930/predictions?limit=1000")).json())[
            "predictions"
        ]
        board = await (await client.get("/v1/leaderboard?basis=b")).json()
        stats = await (await client.get("/v1/algorithms/newton/stats?basis=a")).json()

    check("predictions", {"symbol": "005930", "predictions": predictions})
    check("leaderboard", board)
    check("algorithm_stats", stats)
    outcomes = [p["outcome"] for p in predictions if p["outcome"] is not None]
    assert outcomes, "the morning's predictions are scored"
    for o in outcomes:
        label = o["touch"]["class"] if o["touch"] else None
        assert o["class_a"] == o["class_b"] == label
        assert o["unscorable"] == (None if label else (o["touch"] or {}).get("unscorable", "no touch label"))
    assert {o["class_a"] for o in outcomes} - {None}, "labelled"
    assert (board["basis"], stats["basis"]) == ("b", "a")


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
