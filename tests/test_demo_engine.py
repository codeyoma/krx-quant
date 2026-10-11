"""``krx-quant demo-engine``: a made-up morning through the real engine and Engine API, for
building screens without the server.

Input: nothing but a database path. Output: the Engine API responses a screen reads.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from krx_quant.algorithms import ALGORITHMS
from krx_quant.cli import main
from krx_quant.consensus import NAMES as CONSENSUS
from krx_quant.demo import DEMO_SYMBOLS, demo_app
from krx_quant.macro import INDICATORS
from tests.replay_events import get_json
from tests.test_api_stream import receive_until


async def test_the_demo_engine_answers_what_a_screen_reads(tmp_path: Path) -> None:
    app = await demo_app(tmp_path / "demo.sqlite3")

    async with TestClient(TestServer(app)) as client:
        status = await get_json(client, "/v1/status")
        symbols = await get_json(client, "/v1/symbols")
        latest = {s: await get_json(client, f"/v1/symbols/{s}/latest") for s in DEMO_SYMBOLS}
        macro = await get_json(client, "/v1/macro")
        newton = await get_json(client, "/v1/algorithms/newton")

    assert status["mode"] == "demo"
    assert status["phase"] == "continuous"
    assert status["last_bar_close"] == "2026-10-12T10:30:00+09:00"
    assert [w["kind"] for w in status["special"]] == ["vi"], "a VI, so a screen shows one"
    assert status["training"]["day"] == "2026-10-09"
    assert [s["symbol"] for s in symbols["symbols"]] == list(DEMO_SYMBOLS)
    assert (macro["day"], macro["regime"]) == ("2026-10-12", "risk_off"), "a macro row to show"
    assert len(macro["readings"]) == len(INDICATORS)
    assert status["kis"]["connected"] is True
    assert [f["key"] for f in status["kis"]["feeds"] if f["gap"]] == ["000660"], (
        "a gap, so a screen shows one"
    )
    assert [h["why"] for h in newton["model_history"]] == ["fit", "rollback from newton-20261008-5e1f0a"]
    assert all(s["mid"] is not None for s in symbols["symbols"])
    for body in latest.values():
        assert body["bar_close"] == "2026-10-12T10:30:00+09:00"
        made = {p["algorithm"]: p for p in body["predictions"]}
        assert set(made) >= {a.name for a in ALGORITHMS} | set(CONSENSUS)
        assert all(made[name]["status"] == "ok" for name in CONSENSUS), "the algorithms are active"


async def test_the_demo_is_the_same_every_time(tmp_path: Path) -> None:
    first, second = await demo_app(tmp_path / "a.sqlite3"), await demo_app(tmp_path / "b.sqlite3")

    async with TestClient(TestServer(first)) as a, TestClient(TestServer(second)) as b:
        for path in ("/v1/symbols", "/v1/symbols/005930/predictions?limit=20&algorithm=newton"):
            assert await get_json(a, path) == await get_json(b, path)


async def test_a_paced_demo_streams_its_last_half_hour_as_it_goes(tmp_path: Path) -> None:
    paced = await demo_app(tmp_path / "paced.sqlite3", pace=0.05)

    async with TestClient(TestServer(paced)) as client:
        ws = await client.ws_connect("/v1/stream")
        events = await receive_until(ws, _a_final_consensus_and_an_outcome_seen(), timeout=30)
        await ws.close()
        status = await _until_the_demo_ends(client)
        made = await get_json(client, "/v1/symbols/005930/predictions?limit=40&algorithm=newton")

    assert any(e["type"] == "prediction" for e in events)
    assert all(e["at"] >= "2026-10-12T10:00:00+09:00" for e in events if e["type"] != "health"), "after 10:00"
    assert status["last_bar_close"] == "2026-10-12T10:30:00+09:00", "it goes on to 10:30, then stays"
    unpaced = await demo_app(tmp_path / "unpaced.sqlite3")
    async with TestClient(TestServer(unpaced)) as client:
        assert await get_json(client, "/v1/symbols/005930/predictions?limit=40&algorithm=newton") == made, (
            "the same morning as without the pace"
        )


async def test_a_paced_demo_starts_at_10(tmp_path: Path) -> None:
    paced = await demo_app(tmp_path / "paced.sqlite3", pace=60)

    async with TestClient(TestServer(paced)) as client:
        status = await get_json(client, "/v1/status")

    assert status["now"] == "2026-10-12T10:00:00+09:00"
    assert status["last_bar_close"] == "2026-10-12T10:00:00+09:00"


def _a_final_consensus_and_an_outcome_seen() -> Callable[[dict[str, Any]], bool]:
    seen: set[tuple[str, str | None]] = set()

    def done(event: dict[str, Any]) -> bool:
        seen.add((event["type"], event.get("kind")))
        return {("consensus", "final"), ("outcome", None)} <= seen

    return done


async def _until_the_demo_ends(client: TestClient[Any, Any]) -> dict[str, Any]:
    async with asyncio.timeout(30):
        while True:
            status: dict[str, Any] = await get_json(client, "/v1/status")
            if status["now"] >= "2026-10-12T10:30:00+09:00":
                return status
            await asyncio.sleep(0.05)


@pytest.mark.parametrize("name", ["engine.sqlite3", "engine.sqlite3-wal", "ENGINE.SQLITE3"])
async def test_the_demo_never_replaces_the_live_engines_database(tmp_path: Path, name: str) -> None:
    live = tmp_path / name
    live.write_text("live")

    with pytest.raises(ValueError, match="live engine"):
        await demo_app(live)

    assert live.read_text() == "live"


def test_a_taken_port_is_a_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen()
    port = taken.getsockname()[1]
    monkeypatch.chdir(tmp_path)
    try:
        code = main(["demo-engine", "--port", str(port), "--db", str(tmp_path / "demo.sqlite3")])
    finally:
        taken.close()

    assert code == 1
    assert f"demo Engine API not started on 127.0.0.1:{port}" in capsys.readouterr().err


@pytest.mark.parametrize("pace", ["0", "-1"])
def test_a_pace_is_more_than_0_seconds(tmp_path: Path, pace: str, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["demo-engine", "--pace", pace, "--db", str(tmp_path / "demo.sqlite3")])

    assert code == 1
    assert "--pace is seconds per minute of the demo morning: more than 0" in capsys.readouterr().err
