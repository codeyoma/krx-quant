"""The Engine API's stream: what the engine does, as it does it, over a WebSocket.

Input: an engine on a fake clock fed hand-made events, a status command from another
process, and a KIS status that changes. Output: the events a client receives, each
checked against the /v1 schema.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import ClientWebSocketResponse
from aiohttp.test_utils import TestClient, TestServer

from krx_quant.api import create_app
from krx_quant.baselines import Forecast
from krx_quant.clock import FakeClock
from krx_quant.engine import PredictionEngine
from krx_quant.lifecycle import Status
from krx_quant.predictions import PredictionStore
from krx_quant.replay import replay_into
from krx_quant.touch import TARGET_VERSION
from tests.algorithm_doubles import FixedAlgorithm
from tests.api_schema import check
from tests.replay_events import CALENDAR, FEE, SYMBOL, at, trade
from tests.test_replay import busy_morning

UP = Forecast(0.7, 0.2, 0.1, 2.0)


def members(store: PredictionStore, *names: str) -> None:
    for name in names:
        store.add_status(name, at("08:00:00"), None, Status.ACTIVE, "test")
    store.mark_target(TARGET_VERSION, at("08:00:00"))  # active on the touch target already (#170)


def engine_at(db: Path, start: datetime) -> tuple[PredictionEngine, FakeClock]:
    store = PredictionStore(db)
    members(store, "first", "second")
    clock = FakeClock(start)
    algorithms = [
        FixedAlgorithm("first", features=("velocity",), unique=("velocity",), forecast=UP),
        FixedAlgorithm("second", features=("acceleration",), unique=("acceleration",), forecast=UP),
    ]
    return PredictionEngine(store, CALENDAR, (SYMBOL,), FEE, clock, algorithms=algorithms), clock


async def receive_until(
    ws: ClientWebSocketResponse, done: Callable[[dict[str, Any]], bool], timeout: float = 5.0
) -> list[dict[str, Any]]:
    """Events until one is ``done``, each checked against the schema."""
    events: list[dict[str, Any]] = []
    async with asyncio.timeout(timeout):
        while True:
            event = await ws.receive_json()
            check("event", event)
            events.append(event)
            if done(event):
                return events


async def test_a_client_receives_predictions_the_consensus_and_outcomes_as_they_are_made(
    tmp_path: Path,
) -> None:
    engine, clock = engine_at(tmp_path / "engine.db", at("09:00:00"))

    async with TestClient(TestServer(create_app(engine))) as client:
        ws = await client.ws_connect("/v1/stream")
        await replay_into(engine, clock, busy_morning(end="09:05:30"), until=at("09:05:30"))
        events = await receive_until(ws, lambda e: e["type"] == "outcome")
        await ws.close()

    first = [e for e in events if e.get("bar_close") == "2026-10-12T09:01:00+09:00"]
    kinds = [(e["type"], e.get("kind")) for e in first]
    assert kinds[-2:] == [("consensus", "fast"), ("consensus", "final")], "the fast one first"
    made = {p["algorithm"] for e in first if e["type"] == "prediction" for p in e["predictions"]}
    assert made == {"baseline-flat", "baseline-persist", "baseline-random", "first", "second"}
    [final] = [e for e in first if e.get("kind") == "final"]
    assert final["predictions"][0]["p_up"] == UP.p_up and final["predictions"][0]["symbol"] == SYMBOL
    assert final["predictions"][0]["votes"] == {"up": 2, "flat": 0, "down": 0}, "first and second, both up"
    outcome = events[-1]
    assert outcome["at"] == "2026-10-12T09:05:00+09:00"
    scored = {(p["algorithm"], p["bar_close"]) for p in outcome["outcomes"]}
    assert ("consensus-final", "2026-10-12T09:01:00+09:00") in scored
    assert all(p["outcome"] is not None for p in outcome["outcomes"])


async def test_a_status_change_from_another_process_is_streamed(tmp_path: Path) -> None:
    engine, _ = engine_at(tmp_path / "engine.db", at("09:00:00"))

    async with TestClient(TestServer(create_app(engine, stream_poll_seconds=0.01))) as client:
        ws = await client.ws_connect("/v1/stream")
        await asyncio.sleep(0.05)  # the stream is polling
        command = PredictionStore(tmp_path / "engine.db")  # `krx-quant retire second`
        command.change_status("second", at("09:00:00"), Status.ACTIVE, Status.RETIRED, "retired by command")
        command.close()
        [event] = await receive_until(ws, lambda e: e["type"] == "lifecycle")
        await ws.close()

    assert event["algorithm"] == "second"
    assert event["change"] == {
        "at": "2026-10-12T09:00:00+09:00",
        "from": "active",
        "to": "retired",
        "reason": "retired by command",
        "simulated": False,
    }


async def test_the_kis_status_is_sent_on_connecting_and_when_it_changes(tmp_path: Path) -> None:
    engine, _ = engine_at(tmp_path / "engine.db", at("09:30:00"))
    frame = "2026-10-12T09:29:59+09:00"
    kis: dict[str, Any] = {"connected": True, "last_frame_at": frame, "seconds_since_frame": 1.0, "feeds": []}
    app = create_app(engine, mode="live", live_status=lambda: kis, stream_poll_seconds=0.01)

    async with TestClient(TestServer(app)) as client:
        ws = await client.ws_connect("/v1/stream")
        [now] = await receive_until(ws, lambda e: e["type"] == "health")
        kis = {
            "connected": False,
            "last_frame_at": frame,
            "seconds_since_frame": 61.0,
            "feeds": [
                {
                    "feed": "H0UNASP0",
                    "key": SYMBOL,
                    "last_received_at": "2026-10-12T09:29:58+09:00",
                    "seconds_since": 2.0,
                    "gap": "disconnected: closed",
                    "short_frames": 0,
                }
            ],
        }
        [changed] = await receive_until(ws, lambda e: e["type"] == "health")
        await ws.close()

    assert now["kis"]["connected"] is True
    assert changed["kis"]["connected"] is False
    assert changed["kis"]["feeds"][0]["gap"] == "disconnected: closed"


OTHER = "000660"


async def test_the_bar_being_made_is_sent_once_a_poll_with_its_last_values(tmp_path: Path) -> None:
    store = PredictionStore(tmp_path / "engine.db")
    clock = FakeClock(at("09:30:00"))
    engine = PredictionEngine(store, CALENDAR, (SYMBOL, OTHER), FEE, clock, algorithms=[])
    app = create_app(engine, stream_poll_seconds=0.3)

    def trades(*made: tuple[str, str, int, int]) -> None:
        """At once, between two polls: no await in between."""
        for symbol, when, price, qty in made:
            clock.set(at(when))
            engine.on_event(*trade(at(when), price, qty, symbol=symbol))

    async with TestClient(TestServer(app)) as client:
        ws = await client.ws_connect("/v1/stream")
        await asyncio.sleep(0.05)
        trades(
            (SYMBOL, "09:30:01", 81_200, 10),
            (SYMBOL, "09:30:02", 81_400, 5),
            (SYMBOL, "09:30:03", 81_300, 1),
            (OTHER, "09:30:03", 201_000, 2),
        )
        first = await receive_until(ws, lambda e: e["type"] == "bar")
        second = await receive_until(ws, lambda e: e["type"] == "bar")
        await asyncio.sleep(0.7)  # two more polls: nothing changed, nothing sent
        trades((SYMBOL, "09:31:00.5", 81_100, 3))  # the next minute begins
        third = await receive_until(ws, lambda e: e["type"] == "bar")
        await ws.close()

    sent = {e["symbol"]: e["bar"] for e in (*first, *second)}
    assert sent[SYMBOL] == {
        "start": "2026-10-12T09:30:00+09:00",
        "open": 81_200,
        "high": 81_400,
        "low": 81_200,
        "close": 81_300,
        "volume": 16,
        "trades": 3,
    }, "one event for three trades, with the last values"
    assert sent[OTHER]["close"] == 201_000, "each symbol on its own"
    assert len(first) == len(second) == 1
    assert [e["type"] for e in third] == ["bar"], "no event while nothing changed"
    assert third[0]["symbol"] == SYMBOL
    assert third[0]["bar"]["start"] == "2026-10-12T09:31:00+09:00"


async def test_clients_are_told_when_the_engine_shuts_down(tmp_path: Path) -> None:
    engine, _ = engine_at(tmp_path / "engine.db", at("09:30:00"))
    server = TestServer(create_app(engine))
    await server.start_server()
    async with aiohttp.ClientSession() as session, session.ws_connect(server.make_url("/v1/stream")) as ws:
        await server.close()
        message = await asyncio.wait_for(ws.receive(), 5)

    assert message.type is aiohttp.WSMsgType.CLOSE
    assert message.data == aiohttp.WSCloseCode.GOING_AWAY
