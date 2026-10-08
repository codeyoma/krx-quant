"""Normalized events for each feed, observed through the fake KIS seam."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from krx_quant.collector import run_collector
from krx_quant.fake_kis import FakeKis
from tests.kis_frames import (
    futures_frame,
    index_frame,
    member_frame,
    option_frame,
    program_frame,
    trade_frame,
    us_trade_frame,
)
from tests.test_collector import FIXED_NOW, KST, collect, read, settings_for
from tests.test_subscription_limit import until
from tests.test_subscription_plan import CALLS, serve_masters


async def test_program_trading(kis: FakeKis, tmp_path: Path) -> None:
    kis.script[("H0UNPGM0", "005930")] = [
        program_frame(
            {
                "MKSC_SHRN_ISCD": "005930",
                "STCK_CNTG_HOUR": "093015",
                "SELN_CNQN": "120000",
                "SHNU_CNQN": "150000",
                "NTBY_CNQN": "30000",
                "NTBY_TR_PBMN": "7911000000",
            }
        )
    ]

    await collect(kis, settings_for(kis, tmp_path))

    [p] = read(tmp_path, "events", "H0UNPGM0")
    assert p["event_time"] == datetime(2026, 10, 8, 9, 30, 15, tzinfo=KST)
    assert (p["sell_qty"], p["buy_qty"], p["net_qty"], p["net_value"]) == (120000, 150000, 30000, 7911000000)


async def test_member_flow(kis: FakeKis, tmp_path: Path) -> None:
    kis.script[("H0UNMBC0", "005930")] = [
        member_frame(
            {
                "MKSC_SHRN_ISCD": "005930",
                "SELN2_MBCR_NAME1": "모건스탠리",
                "SELN_MBCR_NO1": "00036",
                "TOTAL_SELN_QTY1": "410000",
                "SELN_MBCR_GLOB_YN_1": "Y",
                "BYOV_MBCR_NAME2": "키움증권",
                "TOTAL_SHNU_QTY2": "220000",
                "SHNU_MBCR_GLOB_YN_2": "N",
                "GLOB_TOTAL_SELN_QTY": "900000",
                "GLOB_TOTAL_SHNU_QTY": "650000",
                "GLOB_NTBY_QTY": "-250000",
            }
        )
    ]

    await collect(kis, settings_for(kis, tmp_path))

    [m] = read(tmp_path, "events", "H0UNMBC0")
    assert (m["sell_member_1_name"], m["sell_member_1_no"], m["sell_member_1_qty"]) == (
        "모건스탠리",
        "00036",
        410000,
    )
    assert m["sell_member_1_foreign"] is True and m["buy_member_2_foreign"] is False
    assert (m["buy_member_2_name"], m["buy_member_2_qty"]) == ("키움증권", 220000)
    assert (m["foreign_sell_qty"], m["foreign_buy_qty"], m["foreign_net_qty"]) == (900000, 650000, -250000)


async def test_sector_index(kis: FakeKis, tmp_path: Path) -> None:
    kis.script[("H0UPCNT0", "0013")] = [
        index_frame(
            {
                "bstp_cls_code": "0013",
                "bsop_hour": "093016",
                "prpr_nmix": "3512.48",
                "prdy_ctrt": "-0.84",
                "ascn_issu_cnt": "12",
                "uplm_issu_cnt": "1",
                "down_issu_cnt": "30",
                "stnr_issu_cnt": "4",
            }
        )
    ]

    await collect(kis, replace(settings_for(kis, tmp_path), index_codes=("0013",)))

    [i] = read(tmp_path, "events", "H0UPCNT0", symbol="0013")
    assert (i["value"], i["change_pct"]) == (3512.48, -0.84)
    assert (i["advancers"], i["decliners"], i["unchanged"]) == (13, 30, 4)


async def test_the_closing_index_record_without_a_time_takes_the_time_received(
    kis: FakeKis, tmp_path: Path
) -> None:
    # After the evening close KIS sends each index's final values with bsop_hour 999999
    # (seen live on 2026-10-08 at 20:05).
    kis.script[("H0UPCNT0", "0001")] = [
        index_frame(
            {"bstp_cls_code": "0001", "bsop_hour": "999999", "prpr_nmix": "6625.93", "prdy_ctrt": "-2.62"}
        )
    ]

    await collect(kis, replace(settings_for(kis, tmp_path), index_codes=("0001",)))

    [i] = read(tmp_path, "events", "H0UPCNT0", symbol="0001")
    assert (i["event_time"], i["value"], i["change_pct"]) == (FIXED_NOW, 6625.93, -2.62)


async def test_an_index_record_with_a_malformed_time_stays_raw_only(kis: FakeKis, tmp_path: Path) -> None:
    kis.script[("H0UPCNT0", "0001")] = [
        index_frame({"bstp_cls_code": "0001", "bsop_hour": "25ab00", "prpr_nmix": "6625.93"})
    ]

    await collect(kis, replace(settings_for(kis, tmp_path), index_codes=("0001",)))

    assert read(tmp_path, "raw", "H0UPCNT0", symbol="0001"), "kept for investigation"
    assert not (tmp_path / "events" / "date=2026-10-08" / "feed=H0UPCNT0").exists()


async def test_usd_futures(kis: FakeKis, tmp_path: Path) -> None:
    serve_masters(kis)
    kis.script[("H0CFCNT0", "A75610")] = [
        futures_frame(
            "H0CFCNT0",
            {
                "futs_shrn_iscd": "A75610",
                "bsop_hour": "093016",
                "futs_prpr": "1388.40",
                "last_cnqn": "25",
                "acml_vol": "40123",
                "hts_otst_stpl_qty": "512345",
                "futs_askp1": "1388.50",
                "futs_bidp1": "1388.40",
            },
        )
    ]

    await collect(kis, settings_for(kis, tmp_path))

    [f] = read(tmp_path, "events", "H0CFCNT0", symbol="A75610")
    assert (f["price"], f["qty"], f["cum_volume"], f["open_interest"]) == (1388.4, 25, 40123, 512345)
    assert (f["ask1"], f["bid1"]) == (1388.5, 1388.4)


async def test_kospi200_futures(kis: FakeKis, tmp_path: Path) -> None:
    serve_masters(kis)
    kis.script[("H0IFCNT0", "A01612")] = [
        futures_frame(
            "H0IFCNT0",
            {
                "futs_shrn_iscd": "A01612",
                "bsop_hour": "093016",
                "futs_prpr": "1056.30",
                "mrkt_basis": "3.77",
                "hts_thpr": "1057.85",
            },
        )
    ]

    await collect(kis, settings_for(kis, tmp_path))

    [f] = read(tmp_path, "events", "H0IFCNT0", symbol="A01612")
    assert (f["price"], f["market_basis"], f["theory_price"]) == (1056.3, 3.77, 1057.85)


async def test_us_daytime_trade(kis: FakeKis, tmp_path: Path) -> None:
    kis.script[("HDFSCNT0", "RBAQNVDA")] = [
        us_trade_frame(
            {
                "RSYM": "RBAQNVDA",
                "SYMB": "NVDA",
                "XYMD": "20261007",
                "XHMS": "203016",
                "KYMD": "20261008",
                "KHMS": "093016",
                "LAST": "192.3400",
                "EVOL": "300",
                "TVOL": "1520000",
                "MTYP": "1",
            }
        )
    ]

    await collect(kis, replace(settings_for(kis, tmp_path), us_symbols=("NVDA",)))

    [t] = read(tmp_path, "events", "HDFSCNT0", symbol="NVDA")
    assert t["event_time"] == datetime(2026, 10, 8, 9, 30, 16, tzinfo=KST)
    assert (t["key"], t["local_date"], t["local_time"]) == ("RBAQNVDA", "20261007", "203016")
    assert (t["price"], t["qty"], t["cum_volume"]) == (192.34, 300, 1520000)


async def test_venue_trades_are_tagged_by_venue(kis: FakeKis, tmp_path: Path) -> None:
    fill = {
        "MKSC_SHRN_ISCD": "005930",
        "STCK_CNTG_HOUR": "093015",
        "STCK_PRPR": "81200",
        "BSOP_DATE": "20261008",
    }
    kis.script[("H0STCNT0", "005930")] = [trade_frame(fill).replace("H0UNCNT0", "H0STCNT0")]
    kis.script[("H0NXCNT0", "005930")] = [trade_frame(fill).replace("H0UNCNT0", "H0NXCNT0")]

    await collect(kis, replace(settings_for(kis, tmp_path), venue_trades=True))

    assert [t["venue"] for t in read(tmp_path, "events", "H0STCNT0")] == ["KRX"]
    assert [t["venue"] for t in read(tmp_path, "events", "H0NXCNT0")] == ["NXT"]


def ok(**body: Any) -> dict[str, Any]:
    return {"rt_cd": "0", "msg_cd": "MCA00000", "msg1": "정상처리 되었습니다.", **body}


async def poll_until(kis: FakeKis, settings: Any, tr_id: str) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=lambda: FIXED_NOW))
    try:
        await until(lambda: any(t == tr_id for _, t, _, _ in kis.rest_requests))
        await asyncio.sleep(0.05)
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)


async def test_option_trade_carries_implied_vol_and_greeks(kis: FakeKis, tmp_path: Path) -> None:
    serve_masters(kis)
    kis.script[("H0IOCNT0", CALLS[3])] = [
        option_frame(
            {
                "optn_shrn_iscd": CALLS[3],
                "bsop_hour": "093016",
                "optn_prpr": "12.35",
                "last_cnqn": "40",
                "hts_ints_vltl": "21.57",
                "delta": "0.5021",
                "gama": "0.0123",
                "hts_otst_stpl_qty": "15230",
            }
        )
    ]

    await collect(kis, settings_for(kis, tmp_path))

    [o] = read(tmp_path, "events", "H0IOCNT0", symbol=CALLS[3])
    assert (o["price"], o["qty"], o["implied_vol"], o["delta"], o["gamma"]) == (
        12.35,
        40,
        21.57,
        0.5021,
        0.0123,
    )
    assert o["open_interest"] == 15230


async def test_foreign_and_institution_estimates(kis: FakeKis, tmp_path: Path) -> None:
    rows = [
        {
            "bsop_hour_gb": "1",
            "frgn_fake_ntby_qty": "-120000",
            "orgn_fake_ntby_qty": "45000",
            "sum_fake_ntby_qty": "-75000",
        },
        {
            "bsop_hour_gb": "2",
            "frgn_fake_ntby_qty": "-80000",
            "orgn_fake_ntby_qty": "10000",
            "sum_fake_ntby_qty": "-70000",
        },
    ]
    kis.rest_responses["HHPTJ04160200"] = lambda q: ok(output2=rows)

    await poll_until(
        kis, replace(settings_for(kis, tmp_path), foreign_estimate_poll_seconds=0.1), "HHPTJ04160200"
    )

    estimates = read(tmp_path, "events", "HHPTJ04160200")
    assert [(e["slot"], e["foreign_net_qty"], e["institution_net_qty"]) for e in estimates[:2]] == [
        ("1", -120000, 45000),
        ("2", -80000, 10000),
    ]


async def test_member_flow_falls_back_to_rest_when_refused_for_the_limit(
    kis: FakeKis, tmp_path: Path
) -> None:
    # KIS answers inquire-member with a one-element list under "output" (seen live 2026-10-08).
    out = {
        "seln_mbcr_name1": "모건스탠리",
        "total_seln_qty1": "410000",
        "seln_mbcr_glob_yn_1": "Y",
        "glob_ntby_qty": "-250000",
    }
    kis.rest_responses["FHKST01010600"] = lambda q: ok(output=[out])
    settings = replace(settings_for(kis, tmp_path), ws_max_subscriptions=0, fallback_poll_seconds=0.1)

    await poll_until(kis, settings, "FHKST01010600")

    [request] = [q for _, t, _, q in kis.rest_requests if t == "FHKST01010600"][:1]
    assert request["FID_COND_MRKT_DIV_CODE"] == "UN", "the integrated (KRX + NXT) market"
    [m, *_] = read(tmp_path, "events", "FHKST01010600")
    assert (m["sell_member_1_name"], m["sell_member_1_qty"], m["sell_member_1_foreign"]) == (
        "모건스탠리",
        410000,
        True,
    )
    assert m["foreign_net_qty"] == -250000


async def test_index_falls_back_to_rest_when_refused_for_the_limit(kis: FakeKis, tmp_path: Path) -> None:
    out = {
        "bstp_nmix_prpr": "748.12",
        "bstp_nmix_prdy_ctrt": "0.31",
        "acml_vol": "1",
        "acml_tr_pbmn": "2",
        "ascn_issu_cnt": "120",
        "uplm_issu_cnt": "0",
        "down_issu_cnt": "70",
        "lslm_issu_cnt": "0",
        "stnr_issu_cnt": "10",
    }
    kis.rest_responses["FHPUP02100000"] = lambda q: ok(output=out)
    settings = replace(
        settings_for(kis, tmp_path), index_codes=("2001",), ws_max_subscriptions=0, fallback_poll_seconds=0.1
    )

    await poll_until(kis, settings, "FHPUP02100000")

    [i, *_] = read(tmp_path, "events", "FHPUP02100000", symbol="2001")
    assert (i["value"], i["advancers"], i["decliners"]) == (748.12, 120, 70)
