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


def serve_snapshots(kis: FakeKis, **quote: str) -> None:
    kis.rest_responses[QUOTE] = lambda q: ok({"stck_prpr": "190500", "acml_vol": "1000", "prdy_ctrt": "0.5"})
    book = {f"askp{i}": str(190500 + 500 * i) for i in range(1, 11)}
    book |= {f"bidp{i}": str(190500 - 500 * (i - 1)) for i in range(1, 11)}
    book |= {f"askp_rsqn{i}": "10" for i in range(1, 11)} | {f"bidp_rsqn{i}": "20" for i in range(1, 11)}
    book |= {"aspr_acpt_hour": "093016", "total_askp_rsqn": "100", "total_bidp_rsqn": "200"}
    output2 = {"stck_prpr": "190500", **quote}
    kis.rest_responses[BOOK_SNAPSHOT] = lambda q: ok(None, output1=book, output2=output2)


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
    kis.max_subscriptions = 4  # the night futures (#264), then 005930's trades and book
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
    assert (snapshot["expected_px"], snapshot["expected_volume"]) == (None, None), "not in this answer"
    assert "H0UNCNT0 000660" in caplog.text and "REST" in caplog.text


async def test_a_book_snapshot_records_the_auction_s_expected_fill_when_its_answer_has_it(
    kis: FakeKis, tmp_path: Path
) -> None:
    # #304: KIS's "호가/예상체결" answer may carry the auction's expected price and volume.
    kis.max_subscriptions = 4
    serve_snapshots(kis, antc_cnpr="190600", antc_vol="5200")
    settings = replace(settings_for(kis, tmp_path, symbols=("005930", "000660")), fallback_poll_seconds=0.1)

    await run_until(kis, settings, lambda: polled(kis, BOOK_SNAPSHOT, "000660"))

    [snapshot, *_] = read(tmp_path, "events", BOOK_SNAPSHOT, symbol="000660")
    assert (snapshot["expected_px"], snapshot["expected_volume"]) == (190600, 5200)


async def test_configured_subscription_cap_sends_the_rest_straight_to_rest(
    kis: FakeKis, tmp_path: Path
) -> None:
    serve_snapshots(kis)
    settings = replace(
        settings_for(kis, tmp_path, symbols=("005930", "000660")),
        ws_max_subscriptions=4,
        fallback_poll_seconds=0.1,
    )

    await run_until(
        kis, settings, lambda: polled(kis, QUOTE, "000660") and polled(kis, BOOK_SNAPSHOT, "000660")
    )

    night = {("H0MFCNT0", "A01612"), ("H0MFASP0", "A01612")}  # first without the masters (#264)
    assert kis.subscribed_keys() == night | {("H0UNCNT0", "005930"), ("H0UNASP0", "005930")}


async def test_measuring_the_limit_reports_how_many_registrations_kis_accepts(
    kis: FakeKis, tmp_path: Path
) -> None:
    kis.max_subscriptions = 7

    assert await measure_subscription_limit(settings_for(kis, tmp_path)) == 7


async def test_etfs_fall_back_to_the_krx_snapshot_and_stocks_to_the_integrated_one(
    kis: FakeKis, tmp_path: Path
) -> None:
    # ETFs do not trade on NXT, so the integrated market has no data for them.
    serve_snapshots(kis)
    settings = replace(
        settings_for(kis, tmp_path),
        etf_symbols=("133690",),
        ws_max_subscriptions=0,
        fallback_poll_seconds=0.1,
    )

    await run_until(kis, settings, lambda: polled(kis, QUOTE, "005930") and polled(kis, QUOTE, "133690"))

    markets = {
        q["FID_INPUT_ISCD"]: q["FID_COND_MRKT_DIV_CODE"] for _, t, _, q in kis.rest_requests if t == QUOTE
    }
    assert markets == {"005930": "UN", "133690": "J"}, "J is KRX alone, UN the integrated market"
    [etf, *_] = read(tmp_path, "events", QUOTE, symbol="133690")
    assert (etf["price"], etf["cum_volume"]) == (190500, 1000)


async def test_a_code_listed_as_both_stock_and_etf_keeps_the_stock_fallbacks(
    kis: FakeKis, tmp_path: Path
) -> None:
    serve_snapshots(kis)
    settings = replace(
        settings_for(kis, tmp_path),
        symbols=("133690",),
        etf_symbols=("133690",),
        ws_max_subscriptions=0,
        fallback_poll_seconds=0.1,
    )

    await run_until(kis, settings, lambda: polled(kis, BOOK_SNAPSHOT, "133690"))

    assert polled(kis, "FHKST01010600", "133690"), "the book and member flow still fall back to REST"
