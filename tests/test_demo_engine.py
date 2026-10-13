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
    usd = status["usd_futures"]
    assert usd["previous_close"] == 1392.2 and usd["change_pct"] is not None, "the USD futures move to show"
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


async def test_the_demo_scrolls_back_into_two_earlier_afternoons(tmp_path: Path) -> None:
    app = await demo_app(tmp_path / "demo.sqlite3")
    before = "before=2026-10-12T09:00:00%2B09:00"

    async with TestClient(TestServer(app)) as client:
        bars = await get_json(client, f"/v1/symbols/005930/bars?{before}&limit=2000")
        made = await get_json(
            client, f"/v1/symbols/005930/predictions?algorithm=consensus-final&{before}&limit=1000"
        )

    starts = [b["start"] for b in bars["bars"]]
    # 14:00–15:19 of the 7th and the 8th (Friday the 9th is Hangul Day), oldest first.
    assert (len(starts), starts[0], starts[-1]) == (
        160,
        "2026-10-07T14:00:00+09:00",
        "2026-10-08T15:19:00+09:00",
    )
    assert starts[79:81] == ["2026-10-07T15:19:00+09:00", "2026-10-08T14:00:00+09:00"]
    closes = [p["bar_close"] for p in made["predictions"]]
    assert (len(closes), closes[0], closes[-1]) == (
        154,
        "2026-10-08T15:16:00+09:00",
        "2026-10-07T14:00:00+09:00",
    )
    assert all(p["status"] == "ok" and p["outcome"] and p["pooled"] for p in made["predictions"]), (
        "made by active members and scored, as on the morning"
    )


async def test_the_demo_has_two_years_of_day_candles_before_the_morning(tmp_path: Path) -> None:
    app = await demo_app(tmp_path / "demo.sqlite3")

    async with TestClient(TestServer(app)) as client:
        latest = await get_json(client, "/v1/symbols/005930/candles?period=day")
        older = await get_json(
            client, "/v1/symbols/005930/candles?period=day&before=2026-10-07T00:00:00%2B09:00&limit=2000"
        )

    dates = [c["date"] for c in latest["candles"]]
    assert (latest["venue"], len(dates), dates[-2:]) == ("UN", 400, ["2026-10-07", "2026-10-08"])
    assert len(older["candles"]) == 498 and older["candles"][-1]["date"] == "2026-10-06"
    last = latest["candles"][-1]
    assert last["low"] <= min(last["open"], last["close"]) <= max(last["open"], last["close"]) <= last["high"]
    assert abs(last["close"] - 81_250) <= 81_250 * 0.001, "the last close is near the morning's open"


async def test_the_demo_is_the_same_every_time(tmp_path: Path) -> None:
    first, second = await demo_app(tmp_path / "a.sqlite3"), await demo_app(tmp_path / "b.sqlite3")

    async with TestClient(TestServer(first)) as a, TestClient(TestServer(second)) as b:
        for path in (
            "/v1/symbols",
            "/v1/symbols/005930/predictions?limit=20&algorithm=newton",
            "/v1/symbols/005930/bars?limit=500",
            "/v1/symbols/005930/candles?period=day",
        ):
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


async def test_a_paced_demo_moves_the_bar_being_made_within_its_minute(tmp_path: Path) -> None:
    paced = await demo_app(tmp_path / "paced.sqlite3", pace=3)  # 10 s of the morning every half second

    async with TestClient(TestServer(paced)) as client:
        ws = await client.ws_connect("/v1/stream")
        bars: list[dict[str, Any]] = []

        def two_of_one_minute(event: dict[str, Any]) -> bool:
            if event["type"] == "bar" and event["symbol"] == "005930":
                bars.append(event["bar"])
            return len(bars) >= 2 and bars[-1]["start"] == bars[-2]["start"]

        await receive_until(ws, two_of_one_minute, timeout=15)
        await ws.close()

    assert bars[-1]["start"] >= "2026-10-12T10:00:00+09:00"
    assert bars[-1]["volume"] > bars[-2]["volume"], "the same minute's bar, grown"


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


@pytest.mark.parametrize("pace", ["0", "-1", "nan", "inf", "1e10000"])
def test_a_pace_is_a_finite_number_of_seconds_more_than_0(
    tmp_path: Path, pace: str, capsys: pytest.CaptureFixture[str]
) -> None:
    # An infinite pace would leave the demo at 10:00 for good, streaming nothing.
    with pytest.raises(SystemExit) as exit:
        main(["demo-engine", "--pace", pace, "--db", str(tmp_path / "demo.sqlite3")])

    assert exit.value.code == 2, "a wrong argument, as the README's exit codes say"
    assert "seconds per minute of the demo morning: a finite number more than 0" in capsys.readouterr().err
