"""REST polling behaviour, observed through the fake KIS seam."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

from krx_quant.collector import run_collector
from krx_quant.fake_kis import FakeKis
from tests.test_collector import FIXED_NOW, read, settings_for

INQUIRE_PRICE = "FHKST01010100"


def price_response(params: dict[str, str]) -> dict[str, Any]:
    return {
        "rt_cd": "0",
        "msg_cd": "MCA00000",
        "msg1": "정상처리 되었습니다.",
        "output": {"stck_prpr": "15230", "acml_vol": "812345", "prdy_ctrt": "1.25"},
    }


async def run_for(kis: FakeKis, settings: Any, seconds: float) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=lambda: FIXED_NOW))
    await asyncio.sleep(seconds)
    stop.set()
    await asyncio.wait_for(task, 5)


async def test_rest_calls_never_exceed_twenty_in_any_one_second_window(kis: FakeKis, tmp_path: Path) -> None:
    for tr_id in (INQUIRE_PRICE, "FHKST01010200", "FHKST01010600"):
        kis.rest_responses[tr_id] = price_response
    # No WebSocket room: every symbol's trades, book and member flow fall back to REST.
    symbols = tuple(f"{i:06d}" for i in range(400000, 400020))
    settings = replace(
        settings_for(kis, tmp_path, symbols=symbols), ws_max_subscriptions=0, fallback_poll_seconds=0.05
    )

    await run_for(kis, settings, 2.5)

    times = sorted(t for t, _, path, _ in kis.rest_requests if "/quotations/" in path)
    assert len(times) > 30, "demand far exceeds the limit, so polling should run at the cap"
    busiest = max(sum(1 for u in times if t <= u < t + 1.0) for t in times)
    assert busiest <= 20


async def test_a_poll_job_survives_dropped_connections(kis: FakeKis, tmp_path: Path) -> None:
    calls = {"n": 0}

    def flaky(params: dict[str, str]) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise ConnectionResetError("KIS dropped the connection")
        return price_response(params)

    kis.rest_responses[INQUIRE_PRICE] = flaky
    settings = replace(settings_for(kis, tmp_path), fallback_poll_seconds=0.05, ws_max_subscriptions=0)

    await run_for(kis, settings, 0.6)

    assert calls["n"] > 2, "the job kept polling after the dropped connections"
    assert read(tmp_path, "events", INQUIRE_PRICE)
