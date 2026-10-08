"""Domestic stock feeds: trades, order book, program trading, member flow, quotes."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pyarrow as pa

from krx_quant.feeds.base import (
    EVENT_TIME,
    KST,
    LEVELS,
    RECEIVED_AT,
    Feed,
    RestFeed,
    Row,
    kst,
    kst_day,
    to_float,
    to_int,
)

# H0UNCNT0, integrated (KRX + NXT) real-time trades (KIS: "국내주식 실시간체결가 (통합)").
# Column order per the KIS Open API docs.
TRADE_COLUMNS = (
    "MKSC_SHRN_ISCD STCK_CNTG_HOUR STCK_PRPR PRDY_VRSS_SIGN PRDY_VRSS PRDY_CTRT WGHN_AVRG_STCK_PRC "
    "STCK_OPRC STCK_HGPR STCK_LWPR ASKP1 BIDP1 CNTG_VOL ACML_VOL ACML_TR_PBMN SELN_CNTG_CSNU "
    "SHNU_CNTG_CSNU NTBY_CNTG_CSNU CTTR SELN_CNTG_SMTN SHNU_CNTG_SMTN CNTG_CLS_CODE SHNU_RATE "
    "PRDY_VOL_VRSS_ACML_VOL_RATE OPRC_HOUR OPRC_VRSS_PRPR_SIGN OPRC_VRSS_PRPR HGPR_HOUR "
    "HGPR_VRSS_PRPR_SIGN HGPR_VRSS_PRPR LWPR_HOUR LWPR_VRSS_PRPR_SIGN LWPR_VRSS_PRPR BSOP_DATE "
    "NEW_MKOP_CLS_CODE TRHT_YN ASKP_RSQN1 BIDP_RSQN1 TOTAL_ASKP_RSQN TOTAL_BIDP_RSQN VOL_TNRT "
    "PRDY_SMNS_HOUR_ACML_VOL PRDY_SMNS_HOUR_ACML_VOL_RATE HOUR_CLS_CODE MRKT_TRTM_CLS_CODE "
    "VI_STND_PRC MARKET_CLS_CODE"
).split()

_SIDES = {"1": "buy", "5": "sell"}

TRADE_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("event_time", pa.timestamp("s", tz="Asia/Seoul")),
        ("received_at", pa.timestamp("us", tz="UTC")),
        ("venue", pa.string()),
        ("price", pa.int64()),
        ("qty", pa.int64()),
        ("side", pa.string()),
        ("cum_volume", pa.int64()),
        ("ask1", pa.int64()),
        ("bid1", pa.int64()),
        ("session_code", pa.string()),
        ("halted", pa.bool_()),
        ("hour_cls", pa.string()),
        ("vi_ref_price", pa.int64()),
        ("market_cls_code", pa.string()),
    ]
)


def _normalize_trade(f: dict[str, str], received_at: datetime, venue: str = "UN") -> Row:
    bsop = f["BSOP_DATE"]
    day = date(int(bsop[0:4]), int(bsop[4:6]), int(bsop[6:8]))
    return {
        "symbol": f["MKSC_SHRN_ISCD"],
        "event_time": kst(day, f["STCK_CNTG_HOUR"]),
        "received_at": received_at,
        # The integrated feed does not tag fills by venue ("UN"); the per-venue feeds do.
        "venue": venue,
        "price": to_int(f["STCK_PRPR"]),
        "qty": to_int(f["CNTG_VOL"]),
        "side": _SIDES.get(f["CNTG_CLS_CODE"], "unknown"),
        "cum_volume": to_int(f["ACML_VOL"]),
        "ask1": to_int(f["ASKP1"]),
        "bid1": to_int(f["BIDP1"]),
        "session_code": f["NEW_MKOP_CLS_CODE"],
        "halted": f["TRHT_YN"] == "Y",
        "hour_cls": f["HOUR_CLS_CODE"],
        "vi_ref_price": to_int(f["VI_STND_PRC"]),
        # Kept verbatim; on live data (2026-10-08) it was "2" for every integrated fill.
        "market_cls_code": f["MARKET_CLS_CODE"],
    }


TRADE = Feed("H0UNCNT0", tuple(TRADE_COLUMNS), TRADE_SCHEMA, _normalize_trade)
# Per-venue trade feeds share the layout; they tell KRX fills from NXT fills.
KRX_TRADE = Feed("H0STCNT0", tuple(TRADE_COLUMNS), TRADE_SCHEMA, lambda f, at: _normalize_trade(f, at, "KRX"))
NXT_TRADE = Feed("H0NXCNT0", tuple(TRADE_COLUMNS), TRADE_SCHEMA, lambda f, at: _normalize_trade(f, at, "NXT"))


# H0UNASP0, integrated (KRX + NXT) real-time order book (KIS: "국내주식 실시간호가 (통합)").
# Column order per the KIS Open API docs.
BOOK_COLUMNS = (
    ["MKSC_SHRN_ISCD", "BSOP_HOUR", "HOUR_CLS_CODE"]
    + [f"ASKP{i}" for i in LEVELS]
    + [f"BIDP{i}" for i in LEVELS]
    + [f"ASKP_RSQN{i}" for i in LEVELS]
    + [f"BIDP_RSQN{i}" for i in LEVELS]
    + (
        "TOTAL_ASKP_RSQN TOTAL_BIDP_RSQN OVTM_TOTAL_ASKP_RSQN OVTM_TOTAL_BIDP_RSQN ANTC_CNPR ANTC_CNQN "
        "ANTC_VOL ANTC_CNTG_VRSS ANTC_CNTG_VRSS_SIGN ANTC_CNTG_PRDY_CTRT ACML_VOL TOTAL_ASKP_RSQN_ICDC "
        "TOTAL_BIDP_RSQN_ICDC OVTM_TOTAL_ASKP_ICDC OVTM_TOTAL_BIDP_ICDC STCK_DEAL_CLS_CODE KMID_PRC "
        "KMID_TOTAL_RSQN KMID_CLS_CODE NMID_PRC NMID_TOTAL_RSQN NMID_CLS_CODE ANTC_EXCH_CLS_CODE"
    ).split()
)

BOOK_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("event_time", pa.timestamp("s", tz="Asia/Seoul")),
        ("received_at", pa.timestamp("us", tz="UTC")),
        ("hour_cls", pa.string()),
        *[(f"ask_px_{i}", pa.int64()) for i in LEVELS],
        *[(f"bid_px_{i}", pa.int64()) for i in LEVELS],
        *[(f"ask_qty_{i}", pa.int64()) for i in LEVELS],
        *[(f"bid_qty_{i}", pa.int64()) for i in LEVELS],
        ("total_ask_qty", pa.int64()),
        ("total_bid_qty", pa.int64()),
        ("cum_volume", pa.int64()),
        # Resting mid-point orders per venue (KMID_* = KRX, NMID_* = NXT).
        ("krx_midpoint_px", pa.int64()),
        ("krx_midpoint_qty", pa.int64()),
        ("krx_midpoint_cls", pa.string()),
        ("nxt_midpoint_px", pa.int64()),
        ("nxt_midpoint_qty", pa.int64()),
        ("nxt_midpoint_cls", pa.string()),
    ]
)


def _normalize_book(f: dict[str, str], received_at: datetime) -> Row:
    # Book frames carry only a time of day; take the date from the receipt time in KST.
    day = received_at.astimezone(KST).date()
    return {
        "symbol": f["MKSC_SHRN_ISCD"],
        "event_time": kst(day, f["BSOP_HOUR"]),
        "received_at": received_at,
        "hour_cls": f["HOUR_CLS_CODE"],
        **{f"ask_px_{i}": to_int(f[f"ASKP{i}"]) for i in LEVELS},
        **{f"bid_px_{i}": to_int(f[f"BIDP{i}"]) for i in LEVELS},
        **{f"ask_qty_{i}": to_int(f[f"ASKP_RSQN{i}"]) for i in LEVELS},
        **{f"bid_qty_{i}": to_int(f[f"BIDP_RSQN{i}"]) for i in LEVELS},
        "total_ask_qty": to_int(f["TOTAL_ASKP_RSQN"]),
        "total_bid_qty": to_int(f["TOTAL_BIDP_RSQN"]),
        "cum_volume": to_int(f["ACML_VOL"]),
        "krx_midpoint_px": to_int(f["KMID_PRC"]),
        "krx_midpoint_qty": to_int(f["KMID_TOTAL_RSQN"]),
        "krx_midpoint_cls": f["KMID_CLS_CODE"],
        "nxt_midpoint_px": to_int(f["NMID_PRC"]),
        "nxt_midpoint_qty": to_int(f["NMID_TOTAL_RSQN"]),
        "nxt_midpoint_cls": f["NMID_CLS_CODE"],
    }


BOOK = Feed("H0UNASP0", tuple(BOOK_COLUMNS), BOOK_SCHEMA, _normalize_book)


QUOTE_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("received_at", pa.timestamp("us", tz="UTC")),
        ("price", pa.int64()),
        ("cum_volume", pa.int64()),
        ("change_pct", pa.float64()),
    ]
)


def _normalize_quote(body: dict[str, Any], key: str, received_at: datetime) -> list[Row]:
    out = body["output"]
    return [
        {
            "symbol": key,
            "received_at": received_at,
            "price": to_int(out["stck_prpr"]),
            "cum_volume": to_int(out["acml_vol"]),
            "change_pct": to_float(out["prdy_ctrt"]),
        }
    ]


# FHKST01010100, "주식현재가 시세": a price snapshot of the integrated market (KRX + NXT).
QUOTE = RestFeed(
    "FHKST01010100",
    "/uapi/domestic-stock/v1/quotations/inquire-price",
    QUOTE_SCHEMA,
    lambda symbol: {"FID_COND_MRKT_DIV_CODE": "UN", "FID_INPUT_ISCD": symbol},
    _normalize_quote,
)
# The same snapshot for KRX alone (J), used for ETFs: they do not trade on NXT.
KRX_QUOTE = RestFeed(
    QUOTE.tr_id,
    QUOTE.path,
    QUOTE_SCHEMA,
    lambda symbol: {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol},
    _normalize_quote,
)


BOOK_SNAPSHOT_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("event_time", pa.timestamp("s", tz="Asia/Seoul")),
        ("received_at", pa.timestamp("us", tz="UTC")),
        ("price", pa.int64()),
        *[(f"ask_px_{i}", pa.int64()) for i in LEVELS],
        *[(f"bid_px_{i}", pa.int64()) for i in LEVELS],
        *[(f"ask_qty_{i}", pa.int64()) for i in LEVELS],
        *[(f"bid_qty_{i}", pa.int64()) for i in LEVELS],
        ("total_ask_qty", pa.int64()),
        ("total_bid_qty", pa.int64()),
        ("vi_cls_code", pa.string()),
    ]
)


def _normalize_book_snapshot(body: dict[str, Any], key: str, received_at: datetime) -> list[Row]:
    book, quote = body["output1"], body["output2"]
    return [
        {
            "symbol": key,
            "event_time": kst(received_at.astimezone(KST).date(), book["aspr_acpt_hour"]),
            "received_at": received_at,
            "price": to_int(quote["stck_prpr"]),
            **{f"ask_px_{i}": to_int(book[f"askp{i}"]) for i in LEVELS},
            **{f"bid_px_{i}": to_int(book[f"bidp{i}"]) for i in LEVELS},
            **{f"ask_qty_{i}": to_int(book[f"askp_rsqn{i}"]) for i in LEVELS},
            **{f"bid_qty_{i}": to_int(book[f"bidp_rsqn{i}"]) for i in LEVELS},
            "total_ask_qty": to_int(book["total_askp_rsqn"]),
            "total_bid_qty": to_int(book["total_bidp_rsqn"]),
            "vi_cls_code": str(quote.get("vi_cls_code", "")),
        }
    ]


# FHKST01010200, "주식현재가 호가/예상체결": a 10-level book snapshot plus price and VI status.
BOOK_SNAPSHOT = RestFeed(
    "FHKST01010200",
    "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
    BOOK_SNAPSHOT_SCHEMA,
    lambda symbol: {"FID_COND_MRKT_DIV_CODE": "UN", "FID_INPUT_ISCD": symbol},
    _normalize_book_snapshot,
)


# H0UNPGM0, integrated program trading per stock (KIS: "국내주식 실시간프로그램매매 (통합)").
PROGRAM_COLUMNS = (
    "MKSC_SHRN_ISCD STCK_CNTG_HOUR SELN_CNQN SELN_TR_PBMN SHNU_CNQN SHNU_TR_PBMN NTBY_CNQN "
    "NTBY_TR_PBMN SELN_RSQN SHNU_RSQN WHOL_NTBY_QTY"
).split()

PROGRAM_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("event_time", EVENT_TIME),
        ("received_at", RECEIVED_AT),
        ("sell_qty", pa.int64()),
        ("sell_value", pa.int64()),
        ("buy_qty", pa.int64()),
        ("buy_value", pa.int64()),
        ("net_qty", pa.int64()),
        ("net_value", pa.int64()),
        ("sell_order_qty", pa.int64()),
        ("buy_order_qty", pa.int64()),
        ("whole_net_qty", pa.int64()),
    ]
)


def _normalize_program(f: dict[str, str], received_at: datetime) -> Row:
    # Cumulative program trading for the day; the frame carries a time of day only.
    return {
        "symbol": f["MKSC_SHRN_ISCD"],
        "event_time": kst(kst_day(received_at), f["STCK_CNTG_HOUR"]),
        "received_at": received_at,
        "sell_qty": to_int(f["SELN_CNQN"]),
        "sell_value": to_int(f["SELN_TR_PBMN"]),
        "buy_qty": to_int(f["SHNU_CNQN"]),
        "buy_value": to_int(f["SHNU_TR_PBMN"]),
        "net_qty": to_int(f["NTBY_CNQN"]),
        "net_value": to_int(f["NTBY_TR_PBMN"]),
        "sell_order_qty": to_int(f["SELN_RSQN"]),
        "buy_order_qty": to_int(f["SHNU_RSQN"]),
        "whole_net_qty": to_int(f["WHOL_NTBY_QTY"]),
    }


PROGRAM = Feed("H0UNPGM0", tuple(PROGRAM_COLUMNS), PROGRAM_SCHEMA, _normalize_program)


# H0UNMBC0, integrated top-5 member firms per stock (KIS: "국내주식 실시간회원사 (통합)").
MEMBER_COLUMNS = (
    "MKSC_SHRN_ISCD SELN2_MBCR_NAME1 SELN2_MBCR_NAME2 SELN2_MBCR_NAME3 SELN2_MBCR_NAME4 "
    "SELN2_MBCR_NAME5 BYOV_MBCR_NAME1 BYOV_MBCR_NAME2 BYOV_MBCR_NAME3 BYOV_MBCR_NAME4 BYOV_MBCR_NAME5 "
    "TOTAL_SELN_QTY1 TOTAL_SELN_QTY2 TOTAL_SELN_QTY3 TOTAL_SELN_QTY4 TOTAL_SELN_QTY5 TOTAL_SHNU_QTY1 "
    "TOTAL_SHNU_QTY2 TOTAL_SHNU_QTY3 TOTAL_SHNU_QTY4 TOTAL_SHNU_QTY5 SELN_MBCR_GLOB_YN_1 "
    "SELN_MBCR_GLOB_YN_2 SELN_MBCR_GLOB_YN_3 SELN_MBCR_GLOB_YN_4 SELN_MBCR_GLOB_YN_5 "
    "SHNU_MBCR_GLOB_YN_1 SHNU_MBCR_GLOB_YN_2 SHNU_MBCR_GLOB_YN_3 SHNU_MBCR_GLOB_YN_4 "
    "SHNU_MBCR_GLOB_YN_5 SELN_MBCR_NO1 SELN_MBCR_NO2 SELN_MBCR_NO3 SELN_MBCR_NO4 SELN_MBCR_NO5 "
    "SHNU_MBCR_NO1 SHNU_MBCR_NO2 SHNU_MBCR_NO3 SHNU_MBCR_NO4 SHNU_MBCR_NO5 SELN_MBCR_RLIM1 "
    "SELN_MBCR_RLIM2 SELN_MBCR_RLIM3 SELN_MBCR_RLIM4 SELN_MBCR_RLIM5 SHNU_MBCR_RLIM1 SHNU_MBCR_RLIM2 "
    "SHNU_MBCR_RLIM3 SHNU_MBCR_RLIM4 SHNU_MBCR_RLIM5 SELN_QTY_ICDC1 SELN_QTY_ICDC2 SELN_QTY_ICDC3 "
    "SELN_QTY_ICDC4 SELN_QTY_ICDC5 SHNU_QTY_ICDC1 SHNU_QTY_ICDC2 SHNU_QTY_ICDC3 SHNU_QTY_ICDC4 "
    "SHNU_QTY_ICDC5 GLOB_TOTAL_SELN_QTY GLOB_TOTAL_SHNU_QTY GLOB_TOTAL_SELN_QTY_ICDC "
    "GLOB_TOTAL_SHNU_QTY_ICDC GLOB_NTBY_QTY GLOB_SELN_RLIM GLOB_SHNU_RLIM SELN2_MBCR_ENG_NAME1 "
    "SELN2_MBCR_ENG_NAME2 SELN2_MBCR_ENG_NAME3 SELN2_MBCR_ENG_NAME4 SELN2_MBCR_ENG_NAME5 "
    "BYOV_MBCR_ENG_NAME1 BYOV_MBCR_ENG_NAME2 BYOV_MBCR_ENG_NAME3 BYOV_MBCR_ENG_NAME4 "
    "BYOV_MBCR_ENG_NAME5"
).split()

_TOP5 = range(1, 6)

MEMBER_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("received_at", RECEIVED_AT),
        *[
            (f"{side}_member_{i}_{field}", kind)
            for side in ("sell", "buy")
            for i in _TOP5
            for field, kind in (
                ("no", pa.string()),
                ("name", pa.string()),
                ("qty", pa.int64()),
                ("foreign", pa.bool_()),
            )
        ],
        ("foreign_sell_qty", pa.int64()),
        ("foreign_buy_qty", pa.int64()),
        ("foreign_net_qty", pa.int64()),
    ]
)


def _member_row(symbol: str, received_at: datetime, get: Any) -> Row:
    """Build a member-flow row; ``get(kind, side, i)`` reads one field from either layout."""
    row: Row = {"symbol": symbol, "received_at": received_at}
    for side in ("sell", "buy"):
        for i in _TOP5:
            row[f"{side}_member_{i}_no"] = str(get("no", side, i)).strip()
            row[f"{side}_member_{i}_name"] = str(get("name", side, i)).strip()
            row[f"{side}_member_{i}_qty"] = to_int(get("qty", side, i))
            row[f"{side}_member_{i}_foreign"] = str(get("foreign", side, i)).strip() == "Y"
    row["foreign_sell_qty"] = to_int(get("foreign_total", "sell", 0))
    row["foreign_buy_qty"] = to_int(get("foreign_total", "buy", 0))
    row["foreign_net_qty"] = to_int(get("foreign_net", "", 0))
    return row


_WS_MEMBER_FIELDS = {
    ("no", "sell"): "SELN_MBCR_NO{i}",
    ("no", "buy"): "SHNU_MBCR_NO{i}",
    ("name", "sell"): "SELN2_MBCR_NAME{i}",
    ("name", "buy"): "BYOV_MBCR_NAME{i}",
    ("qty", "sell"): "TOTAL_SELN_QTY{i}",
    ("qty", "buy"): "TOTAL_SHNU_QTY{i}",
    ("foreign", "sell"): "SELN_MBCR_GLOB_YN_{i}",
    ("foreign", "buy"): "SHNU_MBCR_GLOB_YN_{i}",
    ("foreign_total", "sell"): "GLOB_TOTAL_SELN_QTY",
    ("foreign_total", "buy"): "GLOB_TOTAL_SHNU_QTY",
    ("foreign_net", ""): "GLOB_NTBY_QTY",
}


def _normalize_member(f: dict[str, str], received_at: datetime) -> Row:
    # The frame has no timestamp; received_at is the only time.
    return _member_row(
        f["MKSC_SHRN_ISCD"], received_at, lambda kind, side, i: f[_WS_MEMBER_FIELDS[kind, side].format(i=i)]
    )


MEMBER = Feed("H0UNMBC0", tuple(MEMBER_COLUMNS), MEMBER_SCHEMA, _normalize_member)


def _normalize_member_rest(body: dict[str, Any], key: str, received_at: datetime) -> list[Row]:
    # KIS answers with a one-element list under "output" (seen live 2026-10-08).
    output = body["output"]
    out = output[0] if isinstance(output, list) else output
    names = {
        ("no", "sell"): "seln_mbcr_no{i}",
        ("no", "buy"): "shnu_mbcr_no{i}",
        ("name", "sell"): "seln_mbcr_name{i}",
        ("name", "buy"): "shnu_mbcr_name{i}",
        ("qty", "sell"): "total_seln_qty{i}",
        ("qty", "buy"): "total_shnu_qty{i}",
        ("foreign", "sell"): "seln_mbcr_glob_yn_{i}",
        ("foreign", "buy"): "shnu_mbcr_glob_yn_{i}",
        ("foreign_total", "sell"): "glob_total_seln_qty",
        ("foreign_total", "buy"): "glob_total_shnu_qty",
        ("foreign_net", ""): "glob_ntby_qty",
    }
    return [_member_row(key, received_at, lambda kind, side, i: out.get(names[kind, side].format(i=i), ""))]


# FHKST01010600, "주식현재가 회원사": the same top-5 member flow as a REST snapshot.
MEMBER_REST = RestFeed(
    "FHKST01010600",
    "/uapi/domestic-stock/v1/quotations/inquire-member",
    MEMBER_SCHEMA,
    lambda symbol: {"FID_COND_MRKT_DIV_CODE": "UN", "FID_INPUT_ISCD": symbol},
    _normalize_member_rest,
)


FOREIGN_ESTIMATE_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("received_at", RECEIVED_AT),
        ("slot", pa.string()),
        ("foreign_net_qty", pa.int64()),
        ("institution_net_qty", pa.int64()),
        ("total_net_qty", pa.int64()),
    ]
)


def _normalize_foreign_estimate(body: dict[str, Any], key: str, received_at: datetime) -> list[Row]:
    return [
        {
            "symbol": key,
            "received_at": received_at,
            "slot": str(r.get("bsop_hour_gb", "")),
            "foreign_net_qty": to_int(r.get("frgn_fake_ntby_qty", "")),
            "institution_net_qty": to_int(r.get("orgn_fake_ntby_qty", "")),
            "total_net_qty": to_int(r.get("sum_fake_ntby_qty", "")),
        }
        for r in body.get("output2") or []
    ]


# HHPTJ04160200, "종목별 외인기관 추정가집계": broker estimates of foreign / institutional net
# buying, published a few times a day (foreign 09:30, 11:20, 13:20, 14:30).
FOREIGN_ESTIMATE = RestFeed(
    "HHPTJ04160200",
    "/uapi/domestic-stock/v1/quotations/investor-trend-estimate",
    FOREIGN_ESTIMATE_SCHEMA,
    lambda symbol: {"MKSC_SHRN_ISCD": symbol},
    _normalize_foreign_estimate,
)
