"""Collector behaviour, observed through the fake KIS seam.

Input: frames pushed by the fake KIS server. Output: the Parquet dataset
the collector records. Nothing here reaches into collector internals.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from krx_quant.fake_kis import FakeKis
from krx_quant.predictions import DB_NAME, PredictionStore
from krx_quant.signals import SignalRule
from tests.collector_doubles import FIXED_NOW, KST, collect, read, settings_for
from tests.kis_frames import KRX_BOOK_COLUMNS, book_frame, krx_book_frame, trade_frame


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


async def test_an_engine_start_is_recorded_with_its_commit_and_settings(kis: FakeKis, tmp_path: Path) -> None:
    kis.script[("H0UNCNT0", "005930")] = []
    settings = replace(
        settings_for(kis, tmp_path),
        commit="0123456789abcdef",
        fee_rate=0.0002,
        signal_rule=SignalRule(0.65, 3),
    )

    await collect(kis, settings)

    store = PredictionStore(tmp_path / "state" / DB_NAME)
    [run] = store.runs("start", FIXED_NOW - timedelta(days=1))
    store.close()
    assert run.at == FIXED_NOW
    assert json.loads(run.detail) == {
        "commit": "0123456789abcdef",
        "fee_rate": 0.0002,
        "signal_rule": {"min_probability": 0.65, "min_agree": 3},
        "algorithm_seconds": 10.0,
        "cpu_budget_seconds": 30.0,
    }


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
                "KMID_PRC": "81250",
                "KMID_TOTAL_RSQN": "120",
                "KMID_CLS_CODE": "1",
                "NMID_PRC": "81250",
                "NMID_TOTAL_RSQN": "45",
                "NMID_CLS_CODE": "2",
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
    # Mid-point order book per venue, carried inside the integrated frame.
    assert (book["krx_midpoint_px"], book["krx_midpoint_qty"], book["krx_midpoint_cls"]) == (81250, 120, "1")
    assert (book["nxt_midpoint_px"], book["nxt_midpoint_qty"], book["nxt_midpoint_cls"]) == (81250, 45, "2")


async def test_book_frame_records_the_auction_s_expected_fill(kis: FakeKis, tmp_path: Path) -> None:
    # #304: during a single-price auction the integrated book carries its expected fill.
    kis.script[("H0UNASP0", "005930")] = [
        book_frame(
            {
                "MKSC_SHRN_ISCD": "005930",
                "BSOP_HOUR": "085530",
                "ANTC_CNPR": "81300",
                "ANTC_CNQN": "41250",
                "ANTC_VOL": "52000",
                "ANTC_EXCH_CLS_CODE": "1",
            }
        ),
        book_frame({"MKSC_SHRN_ISCD": "005930", "BSOP_HOUR": "093016"}),  # no auction on
    ]

    await collect(kis, settings_for(kis, tmp_path))

    auction, continuous = read(tmp_path, "events", "H0UNASP0")
    expected = ("expected_px", "expected_qty", "expected_volume", "expected_exch_cls")
    assert tuple(auction[c] for c in expected) == (81300, 41250, 52000, "1")
    assert tuple(continuous[c] for c in expected) == (0, 0, 0, "0")


async def test_raw_frames_are_recorded_verbatim(kis: FakeKis, tmp_path: Path) -> None:
    sent = trade_frame({"MKSC_SHRN_ISCD": "005930", "STCK_CNTG_HOUR": "093015", "BSOP_DATE": "20261008"})
    kis.script[("H0UNCNT0", "005930")] = [sent]

    await collect(kis, settings_for(kis, tmp_path))

    assert [r["message"] for r in read(tmp_path, "raw", "H0UNCNT0")] == [sent]


async def test_the_inverse_etf_s_krx_book_is_recorded_raw_and_as_best_quotes(
    kis: FakeKis, tmp_path: Path
) -> None:
    # Its layout was probed on 2026-11-06 (#606): the raw frame is kept, and its best quote, price,
    # quantity and time, is an event a record.
    record = {
        "MKSC_SHRN_ISCD": "0193L0",
        "BSOP_HOUR": "093015",
        "HOUR_CLS_CODE": "0",
        "ASKP1": "9010",
        "BIDP1": "9005",
        "ASKP_RSQN1": "120",
        "BIDP_RSQN1": "340",
        "ASKP2": "9015",
    }
    sent = krx_book_frame(record)
    kis.script[("H0STASP0", "0193L0")] = [sent]

    await collect(kis, settings_for(kis, tmp_path))

    assert [r["message"] for r in read(tmp_path, "raw", "H0STASP0", symbol="0193L0")] == [sent]
    [event] = read(tmp_path, "events", "H0STASP0", symbol="0193L0")
    assert event == {
        "symbol": "0193L0",
        "event_time": datetime(2026, 10, 8, 9, 30, 15, tzinfo=KST),
        "received_at": FIXED_NOW,
        "hour_cls": "0",
        "ask_px_1": 9010,
        "ask_qty_1": 120,
        "bid_px_1": 9005,
        "bid_qty_1": 340,
    }


async def test_a_krx_book_frame_of_the_old_layout_is_kept_raw_only(kis: FakeKis, tmp_path: Path) -> None:
    # KIS's example had 59 fields before 2026-09-28; such a frame is short of the 63 and makes no event.
    sent = krx_book_frame(
        {"MKSC_SHRN_ISCD": "0193L0", "BSOP_HOUR": "093015", "ASKP1": "9010"}, columns=KRX_BOOK_COLUMNS[:59]
    )
    kis.script[("H0STASP0", "0193L0")] = [sent]

    await collect(kis, settings_for(kis, tmp_path))

    assert [r["message"] for r in read(tmp_path, "raw", "H0STASP0", symbol="0193L0")] == [sent]
    assert not (tmp_path / "events" / "date=2026-10-08" / "feed=H0STASP0").exists()


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
