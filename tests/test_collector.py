"""Collector behaviour, observed through the fake KIS seam.

Input: frames pushed by the fake KIS server. Output: the Parquet dataset
the collector records. Nothing here reaches into collector internals.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow.parquet as pq
import pytest

from krx_quant.collector import run_collector
from krx_quant.config import Settings
from krx_quant.fake_kis import FakeKis
from krx_quant.kis import KisError
from tests.kis_frames import book_frame, trade_frame

KST = ZoneInfo("Asia/Seoul")
FIXED_NOW = datetime(2026, 10, 8, 9, 30, 16, tzinfo=KST)


@pytest.fixture
async def kis() -> AsyncIterator[FakeKis]:
    fake = FakeKis()
    await fake.start()
    yield fake
    await fake.stop()


def settings_for(kis: FakeKis, data_dir: Path, symbols: tuple[str, ...] = ("005930",)) -> Settings:
    return Settings(
        kis_app_key=kis.app_key,
        kis_app_secret=kis.app_secret,
        kis_rest_url=kis.rest_url,
        kis_ws_url=kis.ws_url,
        symbols=symbols,
        data_dir=data_dir,
    )


async def collect(kis: FakeKis, settings: Settings) -> None:
    """Run the collector until every scripted frame has been handled, then stop it."""
    stop = asyncio.Event()
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=lambda: FIXED_NOW))
    await kis.wait_for_subscriptions(set(kis.script))
    await kis.round_trip()
    stop.set()
    await asyncio.wait_for(task, 5)


def read(data_dir: Path, layer: str, feed: str, symbol: str = "005930") -> list[dict[str, Any]]:
    path = data_dir / layer / "date=2026-10-08" / f"feed={feed}" / f"symbol={symbol}"
    rows: list[dict[str, Any]] = pq.read_table(path).to_pylist()
    return rows


async def test_trade_frame_is_recorded_as_normalized_trade_event(kis: FakeKis, tmp_path: Path) -> None:
    kis.script[("H0UNCNT0", "005930")] = [
        trade_frame(
            {
                "MKSC_SHRN_ISCD": "005930",
                "STCK_CNTG_HOUR": "093015",
                "STCK_PRPR": "81200",
                "ASKP1": "81300",
                "BIDP1": "81200",
                "CNTG_VOL": "37",
                "ACML_VOL": "1234567",
                "CNTG_CLS_CODE": "1",
                "BSOP_DATE": "20261008",
                "MARKET_CLS_CODE": "J",
            }
        )
    ]

    await collect(kis, settings_for(kis, tmp_path))

    [trade] = read(tmp_path, "events", "H0UNCNT0")
    assert trade["symbol"] == "005930"
    assert trade["event_time"] == datetime(2026, 10, 8, 9, 30, 15, tzinfo=KST)
    assert trade["venue"] == "UN"
    assert trade["price"] == 81200
    assert trade["qty"] == 37
    assert trade["side"] == "buy"
    assert trade["cum_volume"] == 1234567
    assert (trade["ask1"], trade["bid1"]) == (81300, 81200)
    assert trade["market_cls_code"] == "J"


async def test_book_frame_is_recorded_with_ten_levels_per_side(kis: FakeKis, tmp_path: Path) -> None:
    levels = {f"ASKP{i}": str(81200 + 100 * i) for i in range(1, 11)}
    levels |= {f"BIDP{i}": str(81200 - 100 * (i - 1)) for i in range(1, 11)}
    levels |= {f"ASKP_RSQN{i}": str(1000 + i) for i in range(1, 11)}
    levels |= {f"BIDP_RSQN{i}": str(2000 + i) for i in range(1, 11)}
    kis.script[("H0UNASP0", "005930")] = [
        book_frame(
            {
                "MKSC_SHRN_ISCD": "005930",
                "BSOP_HOUR": "093016",
                **levels,
                "TOTAL_ASKP_RSQN": "300000",
                "TOTAL_BIDP_RSQN": "350000",
                "ACML_VOL": "1234604",
            }
        )
    ]

    await collect(kis, settings_for(kis, tmp_path))

    [book] = read(tmp_path, "events", "H0UNASP0")
    assert book["symbol"] == "005930"
    # Book frames carry only a time of day; the date comes from when the frame was received.
    assert book["event_time"] == datetime(2026, 10, 8, 9, 30, 16, tzinfo=KST)
    assert [book[f"ask_px_{i}"] for i in (1, 2, 10)] == [81300, 81400, 82200]
    assert [book[f"bid_px_{i}"] for i in (1, 2, 10)] == [81200, 81100, 80300]
    assert [book[f"ask_qty_{i}"] for i in (1, 10)] == [1001, 1010]
    assert [book[f"bid_qty_{i}"] for i in (1, 10)] == [2001, 2010]
    assert (book["total_ask_qty"], book["total_bid_qty"]) == (300000, 350000)
    assert book["cum_volume"] == 1234604


async def test_raw_frames_are_recorded_verbatim(kis: FakeKis, tmp_path: Path) -> None:
    sent = trade_frame({"MKSC_SHRN_ISCD": "005930", "STCK_CNTG_HOUR": "093015", "BSOP_DATE": "20261008"})
    kis.script[("H0UNCNT0", "005930")] = [sent]

    await collect(kis, settings_for(kis, tmp_path))

    assert [r["message"] for r in read(tmp_path, "raw", "H0UNCNT0")] == [sent]


async def test_frame_with_several_records_yields_one_event_per_record(kis: FakeKis, tmp_path: Path) -> None:
    kis.script[("H0UNCNT0", "005930")] = [
        trade_frame(
            {
                "MKSC_SHRN_ISCD": "005930",
                "STCK_CNTG_HOUR": "093015",
                "STCK_PRPR": "81200",
                "CNTG_VOL": "5",
                "CNTG_CLS_CODE": "1",
                "BSOP_DATE": "20261008",
            },
            {
                "MKSC_SHRN_ISCD": "005930",
                "STCK_CNTG_HOUR": "093015",
                "STCK_PRPR": "81100",
                "CNTG_VOL": "9",
                "CNTG_CLS_CODE": "5",
                "BSOP_DATE": "20261008",
            },
        )
    ]

    await collect(kis, settings_for(kis, tmp_path))

    trades = read(tmp_path, "events", "H0UNCNT0")
    assert [(t["price"], t["qty"], t["side"]) for t in trades] == [(81200, 5, "buy"), (81100, 9, "sell")]


async def test_every_configured_symbol_gets_trade_and_book_subscriptions(
    kis: FakeKis, tmp_path: Path
) -> None:
    kis.script[("H0UNCNT0", "000660")] = []
    kis.script[("H0UNASP0", "005930")] = []

    await collect(kis, settings_for(kis, tmp_path, symbols=("005930", "000660")))

    assert kis.subscribed_keys() == {
        ("H0UNCNT0", "005930"),
        ("H0UNASP0", "005930"),
        ("H0UNCNT0", "000660"),
        ("H0UNASP0", "000660"),
    }


async def test_unparseable_messages_are_kept_raw_and_do_not_stop_recording(
    kis: FakeKis, tmp_path: Path
) -> None:
    good = trade_frame(
        {
            "MKSC_SHRN_ISCD": "005930",
            "STCK_CNTG_HOUR": "093015",
            "STCK_PRPR": "81200",
            "BSOP_DATE": "20261008",
        }
    )
    kis.script[("H0UNCNT0", "005930")] = ["0|H0UNCNT0|001|005930^truncated", "not a kis message", good]

    await collect(kis, settings_for(kis, tmp_path))

    assert [r["message"] for r in read(tmp_path, "raw", "H0UNCNT0")] == [
        "0|H0UNCNT0|001|005930^truncated",
        good,
    ]
    assert [r["message"] for r in read(tmp_path, "raw", "_unparsed", symbol="_")] == ["not a kis message"]
    assert [t["price"] for t in read(tmp_path, "events", "H0UNCNT0")] == [81200]


async def test_collector_fails_when_kis_closes_the_connection(kis: FakeKis, tmp_path: Path) -> None:
    kis.script[("H0UNCNT0", "005930")] = []
    task = asyncio.create_task(
        run_collector(settings_for(kis, tmp_path), stop=asyncio.Event(), clock=lambda: FIXED_NOW)
    )
    await kis.wait_for_subscriptions(set(kis.script))

    await kis.close_connections()

    with pytest.raises(KisError, match="closed"):
        await asyncio.wait_for(task, 5)
