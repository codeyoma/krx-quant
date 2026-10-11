"""Normalized events for each feed, observed through the fake KIS seam."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from krx_quant.collector import check_fallbacks, run_collector
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
from tests.test_subscription_plan import CALLS, PUTS, serve_masters


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
                "futs_prdy_ctrt": "-1.24",
                "mrkt_basis": "3.77",
                "hts_thpr": "1057.85",
            },
        )
    ]

    await collect(kis, settings_for(kis, tmp_path))

    [f] = read(tmp_path, "events", "H0IFCNT0", symbol="A01612")
    assert (f["price"], f["change_pct"], f["market_basis"], f["theory_price"]) == (
        1056.3,
        -1.24,
        3.77,
        1057.85,
    )


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


async def poll_until(
    kis: FakeKis,
    settings: Any,
    tr_id: str,
    asked: Callable[[dict[str, str]], bool] = lambda q: True,
    times: int = 1,
) -> None:
    """Run the collector until it has called ``tr_id`` ``times`` times (with a query
    ``asked`` accepts).
    """
    stop = asyncio.Event()
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=lambda: FIXED_NOW))
    try:
        await until(lambda: sum(t == tr_id and asked(q) for _, t, _, q in kis.rest_requests) >= times)
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


def program_row(
    hour: str, sell: tuple[str, str], buy: tuple[str, str], net: tuple[str, str]
) -> dict[str, str]:
    """A row of the program trading trend query: the day's totals up to ``hour``, as
    (shares, won) for sells, buys and net buying.
    """
    return {
        "bsop_hour": hour,
        "stck_prpr": "81200",
        "acml_vol": "9500000",
        "whol_smtn_seln_vol": sell[0],
        "whol_smtn_seln_tr_pbmn": sell[1],
        "whol_smtn_shnu_vol": buy[0],
        "whol_smtn_shnu_tr_pbmn": buy[1],
        "whol_smtn_ntby_qty": net[0],
        "whol_smtn_ntby_tr_pbmn": net[1],
        "whol_ntby_vol_icdc": "10000",
        "whol_ntby_tr_pbmn_icdc": "812000000",
    }


Responder = Callable[[dict[str, str]], dict[str, Any]]


def after_a_first_answer(respond: Responder) -> Responder:
    """``respond`` from each query's second call on. A stand-in takes its first answer only
    as what later ones are compared with: when its values were true is unknown.
    """
    seen: set[tuple[tuple[str, str], ...]] = set()

    def answer(q: dict[str, str]) -> dict[str, Any]:
        query = tuple(sorted(q.items()))
        if query in seen:
            return respond(q)
        seen.add(query)
        return ok()

    return answer


async def test_program_trading_falls_back_to_rest_when_refused_for_the_limit(
    kis: FakeKis, tmp_path: Path
) -> None:
    # The rows' order in KIS's answer is not confirmed ⚠: the latest time is the day's totals.
    rows = [
        program_row("093012", ("100000", "8120000000"), ("120000", "9744000000"), ("20000", "1624000000")),
        program_row("093016", ("120000", "9744000000"), ("150000", "12180000000"), ("30000", "2436000000")),
    ]
    kis.rest_responses["FHPPG04650101"] = after_a_first_answer(lambda q: ok(output=rows))
    settings = replace(settings_for(kis, tmp_path), ws_max_subscriptions=0, fallback_poll_seconds=0.05)

    await poll_until(kis, settings, "FHPPG04650101", times=2)

    [request, *_] = [q for _, t, _, q in kis.rest_requests if t == "FHPPG04650101"]
    assert (request["FID_COND_MRKT_DIV_CODE"], request["FID_INPUT_ISCD"]) == ("UN", "005930")
    # Recorded as the feed it stands in for, so the engine, replays and training read it as that.
    [p] = read(tmp_path, "events", "H0UNPGM0")
    assert p["event_time"] == datetime(2026, 10, 8, 9, 30, 16, tzinfo=KST)
    assert (p["sell_qty"], p["sell_value"], p["buy_qty"], p["buy_value"]) == (
        120000,
        9744000000,
        150000,
        12180000000,
    )
    assert (p["net_qty"], p["net_value"]) == (30000, 2436000000)
    # The resting program orders are not in the answer: unknown, not 0.
    assert (p["sell_order_qty"], p["buy_order_qty"], p["whole_net_qty"]) == (None, None, None)
    assert read(tmp_path, "raw", "FHPPG04650101"), "the answer itself is kept under its own TR"


async def test_a_program_trading_row_of_the_last_session_is_not_taken_for_today(
    kis: FakeKis, tmp_path: Path
) -> None:
    # Before the open, the answer may still hold the last session's rows: timed after now.
    rows = [program_row("153000", ("1", "81200"), ("2", "162400"), ("1", "81200"))]
    kis.rest_responses["FHPPG04650101"] = after_a_first_answer(lambda q: ok(output=rows))
    settings = replace(settings_for(kis, tmp_path), ws_max_subscriptions=0, fallback_poll_seconds=0.05)

    await poll_until(kis, settings, "FHPPG04650101", times=2)

    assert read(tmp_path, "raw", "FHPPG04650101")
    assert not (tmp_path / "events" / "date=2026-10-08" / "feed=H0UNPGM0").exists()


def derivative_quotes(answers: dict[tuple[str, str], dict[str, str]]) -> Any:
    """The futures and options quote query: ``output1`` for (market code, contract)."""

    def respond(q: dict[str, str]) -> dict[str, Any]:
        out = answers.get((q["FID_COND_MRKT_DIV_CODE"], q["FID_INPUT_ISCD"]))
        if out is None:
            return {"rt_cd": "1", "msg_cd": "OPSQ0002", "msg1": "없는 종목"}
        return ok(output1=out, output2={}, output3={})

    return respond


async def test_kospi200_futures_fall_back_to_rest_when_refused_for_the_limit(
    kis: FakeKis, tmp_path: Path
) -> None:
    serve_masters(kis)
    quote = {
        "hts_kor_isnm": "F 202612",
        "futs_prpr": "1056.30",
        "futs_prdy_ctrt": "-1.24",
        "acml_vol": "183402",
        "hts_otst_stpl_qty": "251873",
        "hts_thpr": "1057.85",
        "mrkt_basis": "3.77",
    }
    kis.rest_responses["FHMIF10000000"] = after_a_first_answer(derivative_quotes({("F", "A01612"): quote}))
    settings = replace(settings_for(kis, tmp_path), ws_max_subscriptions=0, fallback_poll_seconds=0.05)

    await poll_until(kis, settings, "FHMIF10000000", lambda q: q["FID_COND_MRKT_DIV_CODE"] == "F", times=2)

    # The quote has no time: the event's is the second it was received.
    [f] = read(tmp_path, "events", "H0IFCNT0", symbol="A01612")
    assert f["event_time"] == FIXED_NOW
    assert (f["price"], f["change_pct"], f["cum_volume"], f["open_interest"]) == (
        1056.3,
        -1.24,
        183402,
        251873,
    )
    assert (f["theory_price"], f["market_basis"]) == (1057.85, 3.77)
    # No single trade nor book in the quote: unknown, not 0.
    unknown = ("qty", "ask1", "bid1", "ask_qty1", "bid_qty1", "total_ask_qty", "total_bid_qty")
    assert [f[c] for c in unknown] == [None] * len(unknown)


async def test_a_derivative_stand_in_is_not_recorded_again_for_the_index_quotes_beside_it(
    kis: FakeKis, tmp_path: Path
) -> None:
    serve_masters(kis)
    quote = {
        "futs_prpr": "1056.30",
        "futs_prdy_ctrt": "-1.24",
        "acml_vol": "183402",
        "hts_otst_stpl_qty": "251873",
        "hts_thpr": "1057.85",
        "mrkt_basis": "3.77",
    }
    calls = 0

    def respond(q: dict[str, str]) -> dict[str, Any]:
        nonlocal calls
        if (q["FID_COND_MRKT_DIV_CODE"], q["FID_INPUT_ISCD"]) != ("F", "A01612"):
            return {"rt_cd": "1", "msg_cd": "OPSQ0002", "msg1": "없는 종목"}
        calls += 1
        # The answer also carries index quotes, which move by the second; the contract's
        # quote changes once, after the first answer.
        index = {"bstp_cls_code": "2001", "bstp_nmix_prpr": f"{1052 + calls / 100:.2f}"}
        contract = quote if calls == 1 else {**quote, "futs_prpr": "1056.35"}
        return ok(output1=contract, output2=index, output3=index)

    kis.rest_responses["FHMIF10000000"] = respond
    settings = replace(settings_for(kis, tmp_path), ws_max_subscriptions=0, fallback_poll_seconds=0.02)

    stop = asyncio.Event()
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=lambda: FIXED_NOW))
    try:
        await until(lambda: calls >= 4)
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)

    assert [f["price"] for f in read(tmp_path, "events", "H0IFCNT0", symbol="A01612")] == [1056.35]


async def test_a_stand_in_first_answer_that_cannot_be_read_is_kept_raw(kis: FakeKis, tmp_path: Path) -> None:
    serve_masters(kis)
    calls = 0

    def respond(q: dict[str, str]) -> dict[str, Any]:
        nonlocal calls
        if (q["FID_COND_MRKT_DIV_CODE"], q["FID_INPUT_ISCD"]) != ("F", "A01612"):
            return {"rt_cd": "1", "msg_cd": "OPSQ0002", "msg1": "없는 종목"}
        calls += 1
        return ok(output1={"hts_kor_isnm": "F 202612"}, output2={}, output3={})  # no price

    kis.rest_responses["FHMIF10000000"] = respond
    settings = replace(settings_for(kis, tmp_path), ws_max_subscriptions=0, fallback_poll_seconds=0.02)

    stop = asyncio.Event()
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=lambda: FIXED_NOW))
    try:
        await until(lambda: calls >= 3)
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)

    # The baseline is read like any answer: one that cannot be is kept raw (once) to look into.
    assert len(read(tmp_path, "raw", "FHMIF10000000", symbol="A01612")) == 1
    assert not (tmp_path / "events" / "date=2026-10-08" / "feed=H0IFCNT0").exists()


async def test_usd_futures_fall_back_to_rest_when_refused_for_the_limit(kis: FakeKis, tmp_path: Path) -> None:
    serve_masters(kis)
    # Currency futures prices come with float noise, as on the WebSocket.
    quote = {
        "futs_prpr": "1388.40000117",
        "futs_prdy_ctrt": "0.21",
        "acml_vol": "40123",
        "hts_otst_stpl_qty": "512345",
        "hts_thpr": "1388.95",
        "mrkt_basis": "0.00",
    }
    kis.rest_responses["FHMIF10000000"] = after_a_first_answer(derivative_quotes({("CF", "A75610"): quote}))
    settings = replace(settings_for(kis, tmp_path), ws_max_subscriptions=0, fallback_poll_seconds=0.05)

    await poll_until(kis, settings, "FHMIF10000000", lambda q: q["FID_COND_MRKT_DIV_CODE"] == "CF", times=2)

    [f] = read(tmp_path, "events", "H0CFCNT0", symbol="A75610")
    assert (f["event_time"], f["price"], f["cum_volume"], f["open_interest"]) == (
        FIXED_NOW,
        1388.4,
        40123,
        512345,
    )


async def test_options_fall_back_to_rest_when_refused_for_the_limit(kis: FakeKis, tmp_path: Path) -> None:
    serve_masters(kis)
    quote = {
        "hts_kor_isnm": "C 202610 1,072.5",
        "futs_prpr": "12.35",
        "acml_vol": "52310",
        "hts_otst_stpl_qty": "15230",
        "hts_thpr": "12.41",
        "hts_ints_vltl": "21.57",
        "delta_val": "0.5021",
        "gama": "0.0123",
        "vega": "1.2034",
        "theta": "-0.8812",
        "rho": "0.1102",
        "acpr": "1072.50",
    }
    kis.rest_responses["FHMIF10000000"] = after_a_first_answer(derivative_quotes({("O", CALLS[3]): quote}))
    settings = replace(settings_for(kis, tmp_path), ws_max_subscriptions=0, fallback_poll_seconds=0.05)

    await poll_until(kis, settings, "FHMIF10000000", lambda q: q["FID_INPUT_ISCD"] == CALLS[3], times=2)

    [o] = read(tmp_path, "events", "H0IOCNT0", symbol=CALLS[3])
    assert (o["event_time"], o["price"], o["cum_volume"], o["open_interest"]) == (
        FIXED_NOW,
        12.35,
        52310,
        15230,
    )
    assert (o["implied_vol"], o["delta"], o["gamma"], o["vega"], o["theta"]) == (
        21.57,
        0.5021,
        0.0123,
        1.2034,
        -0.8812,
    )
    assert o["theory_price"] == 12.41
    assert (o["qty"], o["ask1"], o["bid1"]) == (None, None, None)


def us_quote(last: str, volume: str) -> dict[str, Any]:
    """The US stock quote's answer, with the fields of KIS's example."""
    return ok(
        output={
            "rsym": "RBAQNVDA",
            "zdiv": "4",
            "base": "190.1200",
            "pvol": "1830000",
            "last": last,
            "sign": "2",
            "diff": "2.2200",
            "rate": "+1.17",
            "tvol": volume,
            "tamt": "292356800",
            "ordy": "매수가능",
        }
    )


async def test_us_daytime_trades_fall_back_to_rest_when_refused_for_the_limit(
    kis: FakeKis, tmp_path: Path
) -> None:
    kis.rest_responses["HHDFS00000300"] = after_a_first_answer(lambda q: us_quote("192.3400", "1520000"))
    settings = replace(
        settings_for(kis, tmp_path), us_symbols=("NVDA",), ws_max_subscriptions=0, fallback_poll_seconds=0.05
    )

    await poll_until(kis, settings, "HHDFS00000300", times=2)

    [request, *_] = [q for _, t, _, q in kis.rest_requests if t == "HHDFS00000300"]
    assert (request["EXCD"], request["SYMB"]) == ("BAQ", "NVDA"), "the NASDAQ daytime session"
    # Under the ticker, as the stream records US trades.
    [t] = read(tmp_path, "events", "HDFSCNT0", symbol="NVDA")
    assert (t["event_time"], t["key"], t["price"], t["cum_volume"]) == (
        FIXED_NOW,
        "RBAQNVDA",
        192.34,
        1520000,
    )
    unknown = ("local_date", "local_time", "qty", "bid", "ask", "bid_qty", "ask_qty", "session")
    assert [t[c] for c in unknown] == [None] * len(unknown)


async def test_a_us_quote_without_a_price_records_nothing(kis: FakeKis, tmp_path: Path) -> None:
    kis.rest_responses["HHDFS00000300"] = after_a_first_answer(lambda q: us_quote("", ""))
    settings = replace(
        settings_for(kis, tmp_path), us_symbols=("NVDA",), ws_max_subscriptions=0, fallback_poll_seconds=0.05
    )

    await poll_until(kis, settings, "HHDFS00000300", times=2)

    assert read(tmp_path, "raw", "HHDFS00000300", symbol="RBAQNVDA")
    assert not (tmp_path / "events" / "date=2026-10-08" / "feed=HDFSCNT0").exists()


async def test_a_stand_in_records_only_news_and_not_its_first_answer(kis: FakeKis, tmp_path: Path) -> None:
    calls = 0

    def respond(q: dict[str, str]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return us_quote("192.3400", "1520000") if calls <= 3 else us_quote("192.5000", "1520300")

    kis.rest_responses["HHDFS00000300"] = respond
    settings = replace(
        settings_for(kis, tmp_path), us_symbols=("NVDA",), ws_max_subscriptions=0, fallback_poll_seconds=0.02
    )

    stop = asyncio.Event()
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=lambda: FIXED_NOW))
    try:
        await until(lambda: calls >= 5)
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)

    # The quote carries no time: a repeat is not news, as the stream sends only new trades,
    # and the first answer may be the last session's (before the open), so it is not either.
    assert [t["price"] for t in read(tmp_path, "events", "HDFSCNT0", symbol="NVDA")] == [192.5]
    assert len(read(tmp_path, "raw", "HHDFS00000300", symbol="RBAQNVDA")) == 1


def vi_row(start: str, end: str, *, active: str = "N", kind: str = "2", count: str = "1") -> dict[str, str]:
    """A row of the VI status query, shaped like the live answer for 005930 on 2026-10-08."""
    return {
        "hts_kor_isnm": "삼성전자",
        "mksc_shrn_iscd": "005930",
        "vi_cls_code": active,
        "bsop_date": "20261008",
        "cntg_vi_hour": start,
        "vi_cncl_hour": end,
        "vi_kind_code": kind,
        "vi_prc": "261500",
        "vi_stnd_prc": "0",
        "vi_dprt": "0.00",
        "vi_dmc_stnd_prc": "264500",
        "vi_dmc_dprt": "-1.13",
        "vi_count": count,
    }


async def test_vi_status_is_polled_for_the_day_and_recorded_when_it_changes(
    kis: FakeKis, tmp_path: Path
) -> None:
    # Newest first, like KIS: one VI still on (no release time yet), one released.
    answer = [vi_row("092950", "", active="Y", kind="1", count="2"), vi_row("091200", "091431")]
    kis.rest_responses["FHPST01390000"] = lambda q: ok(output=answer)
    settings = replace(settings_for(kis, tmp_path), vi_poll_seconds=0.02)

    stop = asyncio.Event()
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=lambda: FIXED_NOW))
    try:
        await until(lambda: sum(t == "FHPST01390000" for _, t, _, _ in kis.rest_requests) >= 3)
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)

    [query] = {q["FID_INPUT_DATE_1"] for _, t, _, q in kis.rest_requests if t == "FHPST01390000"}
    assert query == "20261008"
    # The same answer every time: recorded once.
    assert len(read(tmp_path, "raw", "FHPST01390000")) == 1
    on, released = read(tmp_path, "events", "FHPST01390000")
    assert (on["event_time"], on["end_time"], on["active"], on["kind"]) == (
        datetime(2026, 10, 8, 9, 29, 50, tzinfo=KST),
        None,
        True,
        "static",
    )
    assert (released["event_time"], released["end_time"], released["active"], released["kind"]) == (
        datetime(2026, 10, 8, 9, 12, tzinfo=KST),
        datetime(2026, 10, 8, 9, 14, 31, tzinfo=KST),
        False,
        "dynamic",
    )


async def test_vis_are_not_polled_while_the_market_is_closed(kis: FakeKis, tmp_path: Path) -> None:
    kis.rest_responses["FHPST01390000"] = lambda q: ok(output=[])
    kis.rest_responses["HHPTJ04160200"] = lambda q: ok(output2=[])
    settings = replace(settings_for(kis, tmp_path), vi_poll_seconds=0.02, foreign_estimate_poll_seconds=0.02)
    evening = datetime(2026, 10, 8, 20, 30, tzinfo=KST)

    stop = asyncio.Event()
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=lambda: evening))
    try:
        await until(lambda: sum(t == "HHPTJ04160200" for _, t, _, _ in kis.rest_requests) >= 3)
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)

    assert not any(t == "FHPST01390000" for _, t, _, _ in kis.rest_requests)


async def test_each_fallback_of_the_plan_can_be_checked_once_beside_the_collector(
    kis: FakeKis, tmp_path: Path
) -> None:
    serve_masters(kis)
    futures = {
        "futs_prpr": "1056.30",
        "futs_prdy_ctrt": "-1.24",
        "acml_vol": "183402",
        "hts_otst_stpl_qty": "251873",
        "hts_thpr": "1057.85",
        "mrkt_basis": "3.77",
    }
    kis.rest_responses["FHMIF10000000"] = derivative_quotes({("F", "A01612"): futures})
    program = program_row(
        "093016", ("120000", "9744000000"), ("150000", "12180000000"), ("30000", "2436000000")
    )
    kis.rest_responses["FHPPG04650101"] = lambda q: ok(output=[program])
    settings = replace(settings_for(kis, tmp_path), us_symbols=("NVDA",))

    checks = await check_fallbacks(settings, lambda: FIXED_NOW)

    by_feed = {(c.cut_feed, c.key): c for c in checks}
    assert list(by_feed) == [
        ("H0IFCNT0", "A01612"),
        ("H0CFCNT0", "A75610"),
        ("HDFSCNT0", "RBAQNVDA"),
        *[(tr, "005930") for tr in ("H0UNCNT0", "H0UNASP0", "H0UNPGM0", "H0UNMBC0")],
        *[("H0IOCNT0", code) for code in CALLS[1:6] + PUTS[1:6]],
    ], "every registration of the plan that has a REST fallback, in the plan's order"
    kospi200 = by_feed["H0IFCNT0", "A01612"]
    assert (kospi200.rest.tr_id, kospi200.params["FID_COND_MRKT_DIV_CODE"], kospi200.error) == (
        "FHMIF10000000",
        "F",
        None,
    )
    assert [(r["event_time"], r["price"]) for r in kospi200.rows] == [(FIXED_NOW, 1056.3)]
    assert [r["net_value"] for r in by_feed["H0UNPGM0", "005930"].rows] == [2436000000]
    usd = by_feed["H0CFCNT0", "A75610"]
    assert (usd.rows, usd.error is not None) == ([], True), "KIS refused it: the check says why"
    # Each asked once, over REST only, and nothing recorded: it can run beside the collector.
    assert sum(t == "FHPPG04650101" for _, t, _, _ in kis.rest_requests) == 1
    assert kis.subscriptions == []
    assert not (tmp_path / "events").exists()
    assert not (tmp_path / "raw").exists()


async def test_checking_the_fallbacks_fails_for_derivatives_whose_contracts_are_unknown(
    kis: FakeKis, tmp_path: Path
) -> None:
    # No KIS masters and no configured codes: the nearest contracts cannot be found.
    checks = await check_fallbacks(settings_for(kis, tmp_path), lambda: FIXED_NOW)

    unknown = [
        (c.cut_feed, c.error is not None)
        for c in checks
        if c.cut_feed in ("H0IFCNT0", "H0CFCNT0", "H0IOCNT0")
    ]
    assert unknown == [("H0IFCNT0", True), ("H0CFCNT0", True), ("H0IOCNT0", True)]
