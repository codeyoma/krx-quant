"""Live mode predicts through the same engine: the collector on the fake KIS seam.

Input: frames pushed by the fake KIS server and a fake Session Clock. Output: the
Engine API the collector serves.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import aiohttp

from krx_quant.clock import FakeClock
from krx_quant.collector import run_collector
from krx_quant.fake_kis import FakeKis
from tests.kis_frames import book_frame
from tests.replay_events import PREDICTORS, at
from tests.test_collector import settings_for


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


async def poll(url: str, ready: Callable[[Any], bool], timeout: float = 5.0) -> Any:
    """GET ``url`` until its JSON body is ``ready``."""
    async with aiohttp.ClientSession() as http, asyncio.timeout(timeout):
        while True:
            try:
                async with http.get(url) as response:
                    body = await response.json()
                    if response.status == 200 and ready(body):
                        return body
            except aiohttp.ClientConnectionError:
                pass  # not listening yet
            await asyncio.sleep(0.02)


async def test_the_collector_predicts_from_live_events_and_serves_them(kis: FakeKis, tmp_path: Path) -> None:
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

        clock.set(at("09:31:00.2"))  # the 09:30 bar closes

        base = f"http://127.0.0.1:{port}/v1"
        latest = await poll(f"{base}/symbols/005930/latest", lambda b: b["bar_close"] is not None)
        status = await poll(f"{base}/status", lambda b: True)
        symbols = await poll(f"{base}/symbols", lambda b: True)
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)

    assert latest["bar_close"] == "2026-10-12T09:31:00+09:00"
    assert {p["algorithm"] for p in latest["predictions"]} == PREDICTORS
    assert {p["displayed_at"] for p in latest["predictions"]} == {"2026-10-12T09:31:00.200000+09:00"}
    # The engine took the book from the live stream.
    assert symbols["symbols"] == [{"symbol": "005930", "mid": 81_250, "tick": 100, "flat_ticks": 2}]
    assert status["mode"] == "live"
    assert status["phase"] == "continuous"
    assert status["kis"]["connected"] is True
    [book_feed] = [f for f in status["kis"]["feeds"] if f["feed"] == "H0UNASP0"]
    assert book_feed["last_received_at"] == "2026-10-12T09:30:10+09:00"
