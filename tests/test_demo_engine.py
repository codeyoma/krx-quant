"""``krx-quant demo-engine``: a made-up morning through the real engine and Engine API, for
building screens without the server.

Input: nothing but a database path. Output: the Engine API responses a screen reads.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from krx_quant.algorithms import ALGORITHMS
from krx_quant.cli import main
from krx_quant.consensus import NAMES as CONSENSUS
from krx_quant.demo import DEMO_SYMBOLS, demo_app
from tests.replay_events import get_json


async def test_the_demo_engine_answers_what_a_screen_reads(tmp_path: Path) -> None:
    app = await demo_app(tmp_path / "demo.sqlite3")

    async with TestClient(TestServer(app)) as client:
        status = await get_json(client, "/v1/status")
        symbols = await get_json(client, "/v1/symbols")
        latest = {s: await get_json(client, f"/v1/symbols/{s}/latest") for s in DEMO_SYMBOLS}

    assert status["mode"] == "demo"
    assert status["phase"] == "continuous"
    assert status["last_bar_close"] == "2026-10-12T10:30:00+09:00"
    assert [w["kind"] for w in status["special"]] == ["vi"], "a VI, so a screen shows one"
    assert status["training"]["day"] == "2026-10-09"
    assert [s["symbol"] for s in symbols["symbols"]] == list(DEMO_SYMBOLS)
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
