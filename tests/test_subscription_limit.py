"""Subscription-limit handling, observed through the fake KIS seam."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from krx_quant.collector import measure_subscription_limit, run_collector
from krx_quant.fake_kis import FakeKis
from tests.test_collector import FIXED_NOW, read, settings_for

QUOTE, BOOK_SNAPSHOT = "FHKST01010100", "FHKST01010200"


def ok(output: dict[str, Any] | None, **extra: Any) -> dict[str, Any]:
    return {"rt_cd": "0", "msg_cd": "MCA00000", "msg1": "정상처리 되었습니다.", "output": output, **extra}


def serve_snapshots(kis: FakeKis) -> None:
    kis.rest_responses[QUOTE] = lambda q: ok({"stck_prpr": "190500", "acml_vol": "1000", "prdy_ctrt": "0.5"})
    book = {f"askp{i}": str(190500 + 500 * i) for i in range(1, 11)}
    book |= {f"bidp{i}": str(190500 - 500 * (i - 1)) for i in range(1, 11)}
    book |= {f"askp_rsqn{i}": "10" for i in range(1, 11)} | {f"bidp_rsqn{i}": "20" for i in range(1, 11)}
    book |= {"aspr_acpt_hour": "093016", "total_askp_rsqn": "100", "total_bidp_rsqn": "200"}
    kis.rest_responses[BOOK_SNAPSHOT] = lambda q: ok(None, output1=book, output2={"stck_prpr": "190500"})


async def until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    async def poll() -> None:
        while not condition():
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), timeout)


def polled(kis: FakeKis, tr_id: str, symbol: str) -> bool:
    return any(t == tr_id and q.get("FID_INPUT_ISCD") == symbol for _, t, _, q in kis.rest_requests)


async def run_until(kis: FakeKis, settings: Any, condition: Callable[[], bool]) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=lambda: FIXED_NOW))
    try:
        await until(condition)
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)


async def test_subscriptions_rejected_for_the_limit_fall_back_to_rest_polling(
    kis: FakeKis, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    kis.max_subscriptions = 2
    serve_snapshots(kis)
    settings = replace(settings_for(kis, tmp_path, symbols=("005930", "000660")), fallback_poll_seconds=0.1)

    with caplog.at_level(logging.WARNING):
        await run_until(
            kis, settings, lambda: polled(kis, QUOTE, "000660") and polled(kis, BOOK_SNAPSHOT, "000660")
        )

    assert not polled(kis, QUOTE, "005930"), "accepted subscriptions are not polled"
    [snapshot, *_] = read(tmp_path, "events", BOOK_SNAPSHOT, symbol="000660")
    assert (snapshot["ask_px_1"], snapshot["bid_px_1"], snapshot["ask_qty_1"], snapshot["bid_qty_1"]) == (
        191000,
        190500,
        10,
        20,
    )
    assert "H0UNCNT0 000660" in caplog.text and "REST" in caplog.text


async def test_configured_subscription_cap_sends_the_rest_straight_to_rest(
    kis: FakeKis, tmp_path: Path
) -> None:
    serve_snapshots(kis)
    settings = replace(
        settings_for(kis, tmp_path, symbols=("005930", "000660")),
        ws_max_subscriptions=2,
        fallback_poll_seconds=0.1,
    )

    await run_until(
        kis, settings, lambda: polled(kis, QUOTE, "000660") and polled(kis, BOOK_SNAPSHOT, "000660")
    )

    assert kis.subscribed_keys() == {("H0UNCNT0", "005930"), ("H0UNASP0", "005930")}


async def test_measuring_the_limit_reports_how_many_registrations_kis_accepts(
    kis: FakeKis, tmp_path: Path
) -> None:
    kis.max_subscriptions = 7

    assert await measure_subscription_limit(settings_for(kis, tmp_path)) == 7
