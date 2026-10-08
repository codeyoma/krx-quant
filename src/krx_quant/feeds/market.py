"""Market context feeds: sector indices, KOSPI200 and USD futures, US daytime trades, option IV."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pyarrow as pa

from krx_quant.feeds.base import (
    EVENT_TIME,
    KST,
    RECEIVED_AT,
    Feed,
    RestFeed,
    Row,
    kst,
    kst_or_received,
    to_float,
    to_int,
)

# H0UPCNT0, sector / market index ticks (KIS: "국내지수 실시간체결"). Keys are 4-digit index
# codes; live data on 2026-10-08 flowed for 0001 (KOSPI), 2001 (KOSPI200), 0013.
INDEX_COLUMNS = (
    "bstp_cls_code bsop_hour prpr_nmix prdy_vrss_sign bstp_nmix_prdy_vrss acml_vol acml_tr_pbmn "
    "pcas_vol pcas_tr_pbmn prdy_ctrt oprc_nmix nmix_hgpr nmix_lwpr oprc_vrss_nmix_prpr "
    "oprc_vrss_nmix_sign hgpr_vrss_nmix_prpr hgpr_vrss_nmix_sign lwpr_vrss_nmix_prpr "
    "lwpr_vrss_nmix_sign prdy_clpr_vrss_oprc_rate prdy_clpr_vrss_hgpr_rate prdy_clpr_vrss_lwpr_rate "
    "uplm_issu_cnt ascn_issu_cnt stnr_issu_cnt down_issu_cnt lslm_issu_cnt qtqt_ascn_issu_cnt "
    "qtqt_down_issu_cnt tick_vrss"
).split()

INDEX_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("event_time", EVENT_TIME),
        ("received_at", RECEIVED_AT),
        ("value", pa.float64()),
        ("change_pct", pa.float64()),
        ("cum_volume", pa.int64()),
        ("cum_value", pa.int64()),
        ("advancers", pa.int64()),
        ("decliners", pa.int64()),
        ("unchanged", pa.int64()),
    ]
)


def _normalize_index(f: dict[str, str], received_at: datetime) -> Row:
    return {
        "symbol": f["bstp_cls_code"],
        "event_time": kst_or_received(f["bsop_hour"], received_at),
        "received_at": received_at,
        "value": to_float(f["prpr_nmix"]),
        "change_pct": to_float(f["prdy_ctrt"]),
        "cum_volume": to_int(f["acml_vol"]),
        "cum_value": to_int(f["acml_tr_pbmn"]),
        "advancers": to_int(f["ascn_issu_cnt"]) + to_int(f["uplm_issu_cnt"]),
        "decliners": to_int(f["down_issu_cnt"]) + to_int(f["lslm_issu_cnt"]),
        "unchanged": to_int(f["stnr_issu_cnt"]),
    }


INDEX = Feed("H0UPCNT0", tuple(INDEX_COLUMNS), INDEX_SCHEMA, _normalize_index)


def _normalize_index_rest(body: dict[str, Any], key: str, received_at: datetime) -> list[Row]:
    out = body["output"]
    return [
        {
            "symbol": key,
            "event_time": received_at.astimezone(KST).replace(microsecond=0),
            "received_at": received_at,
            "value": to_float(out["bstp_nmix_prpr"]),
            "change_pct": to_float(out["bstp_nmix_prdy_ctrt"]),
            "cum_volume": to_int(out["acml_vol"]),
            "cum_value": to_int(out["acml_tr_pbmn"]),
            "advancers": to_int(out["ascn_issu_cnt"]) + to_int(out["uplm_issu_cnt"]),
            "decliners": to_int(out["down_issu_cnt"]) + to_int(out["lslm_issu_cnt"]),
            "unchanged": to_int(out["stnr_issu_cnt"]),
        }
    ]


# FHPUP02100000, "국내업종 현재지수": the index as a REST snapshot.
INDEX_REST = RestFeed(
    "FHPUP02100000",
    "/uapi/domestic-stock/v1/quotations/inquire-index-price",
    INDEX_SCHEMA,
    lambda code: {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": code},
    _normalize_index_rest,
)


# H0IFCNT0 (KOSPI200 futures, "지수선물 실시간체결가") and H0CFCNT0 (commodity / currency
# futures incl. USD, "상품선물 실시간체결가") share this layout. Keys are new-style codes
# such as A01612 (KOSPI200 Dec 2026) and A75610 (USD Oct 2026), read from the KIS masters.
FUTURES_COLUMNS = (
    "futs_shrn_iscd bsop_hour futs_prdy_vrss prdy_vrss_sign futs_prdy_ctrt futs_prpr futs_oprc "
    "futs_hgpr futs_lwpr last_cnqn acml_vol acml_tr_pbmn hts_thpr mrkt_basis dprt nmsc_fctn_stpl_prc "
    "fmsc_fctn_stpl_prc spead_prc hts_otst_stpl_qty otst_stpl_qty_icdc oprc_hour oprc_vrss_prpr_sign "
    "oprc_vrss_nmix_prpr hgpr_hour hgpr_vrss_prpr_sign hgpr_vrss_nmix_prpr lwpr_hour "
    "lwpr_vrss_prpr_sign lwpr_vrss_nmix_prpr shnu_rate cttr esdg otst_stpl_rgbf_qty_icdc thpr_basis "
    "futs_askp1 futs_bidp1 askp_rsqn1 bidp_rsqn1 seln_cntg_csnu shnu_cntg_csnu ntby_cntg_csnu "
    "seln_cntg_smtn shnu_cntg_smtn total_askp_rsqn total_bidp_rsqn prdy_vol_vrss_acml_vol_rate "
    "dscs_bltr_acml_qty dynm_mxpr dynm_llam dynm_prc_limt_yn"
).split()

FUTURES_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("event_time", EVENT_TIME),
        ("received_at", RECEIVED_AT),
        ("price", pa.float64()),
        ("qty", pa.int64()),
        ("cum_volume", pa.int64()),
        ("open_interest", pa.int64()),
        ("theory_price", pa.float64()),
        ("market_basis", pa.float64()),
        ("ask1", pa.float64()),
        ("bid1", pa.float64()),
        ("ask_qty1", pa.int64()),
        ("bid_qty1", pa.int64()),
        ("total_ask_qty", pa.int64()),
        ("total_bid_qty", pa.int64()),
    ]
)


def _normalize_futures(f: dict[str, str], received_at: datetime) -> Row:
    return {
        "symbol": f["futs_shrn_iscd"],
        "event_time": kst_or_received(f["bsop_hour"], received_at),
        "received_at": received_at,
        # KIS sends float noise for currency futures (e.g. 1337.20000117); 2 decimals cover every tick.
        "price": round(to_float(f["futs_prpr"]), 2),
        "qty": to_int(f["last_cnqn"]),
        "cum_volume": to_int(f["acml_vol"]),
        "open_interest": to_int(f["hts_otst_stpl_qty"]),
        "theory_price": round(to_float(f["hts_thpr"]), 2),
        "market_basis": round(to_float(f["mrkt_basis"]), 2),
        "ask1": round(to_float(f["futs_askp1"]), 2),
        "bid1": round(to_float(f["futs_bidp1"]), 2),
        "ask_qty1": to_int(f["askp_rsqn1"]),
        "bid_qty1": to_int(f["bidp_rsqn1"]),
        "total_ask_qty": to_int(f["total_askp_rsqn"]),
        "total_bid_qty": to_int(f["total_bidp_rsqn"]),
    }


KOSPI200_FUTURES = Feed("H0IFCNT0", tuple(FUTURES_COLUMNS), FUTURES_SCHEMA, _normalize_futures)
USD_FUTURES = Feed("H0CFCNT0", tuple(FUTURES_COLUMNS), FUTURES_SCHEMA, _normalize_futures)


# HDFSCNT0, US stock trades (KIS: "해외주식 실시간지연체결가"; US quotes are real-time).
# Keys: "R" + BAQ/BAY/BAA + ticker for the US daytime session (e.g. RBAQNVDA), "D" + NAS/NYS/AMS
# + ticker for the regular session. Live frames have 26 fields, starting with RSYM (the key).
US_TRADE_COLUMNS = (
    "RSYM SYMB ZDIV TYMD XYMD XHMS KYMD KHMS OPEN HIGH LOW LAST SIGN DIFF RATE PBID PASK VBID VASK "
    "EVOL TVOL TAMT BIVL ASVL STRN MTYP"
).split()

US_TRADE_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("event_time", EVENT_TIME),
        ("received_at", RECEIVED_AT),
        ("key", pa.string()),
        ("local_date", pa.string()),
        ("local_time", pa.string()),
        ("price", pa.float64()),
        ("qty", pa.int64()),
        ("cum_volume", pa.int64()),
        ("bid", pa.float64()),
        ("ask", pa.float64()),
        ("bid_qty", pa.int64()),
        ("ask_qty", pa.int64()),
        ("session", pa.string()),
    ]
)


def _normalize_us_trade(f: dict[str, str], received_at: datetime) -> Row:
    kymd = f["KYMD"]
    day = datetime(int(kymd[0:4]), int(kymd[4:6]), int(kymd[6:8])).date()
    return {
        "symbol": f["SYMB"],
        "event_time": kst(day, f["KHMS"]),
        "received_at": received_at,
        "key": f["RSYM"],
        "local_date": f["XYMD"],
        "local_time": f["XHMS"],
        "price": to_float(f["LAST"]),
        "qty": to_int(f["EVOL"]),
        "cum_volume": to_int(f["TVOL"]),
        "bid": to_float(f["PBID"]),
        "ask": to_float(f["PASK"]),
        "bid_qty": to_int(f["VBID"]),
        "ask_qty": to_int(f["VASK"]),
        # MTYP: 1 regular, 2 pre-market, 3 after-hours (KIS docs).
        "session": f["MTYP"],
    }


US_TRADE = Feed("HDFSCNT0", tuple(US_TRADE_COLUMNS), US_TRADE_SCHEMA, _normalize_us_trade)


_DAYTIME_EXCHANGES = {"NAS": "BAQ", "NYS": "BAY", "AMS": "BAA"}


def us_daytime_key(symbol: str) -> str:
    """KIS key for the US daytime session: ``NVDA`` -> ``RBAQNVDA`` (NASDAQ by default);
    other exchanges as ``NYS:XYZ`` -> ``RBAYXYZ`` or ``AMS:XYZ`` -> ``RBAAXYZ``.
    """
    exchange, _, ticker = symbol.rpartition(":")
    return f"R{_DAYTIME_EXCHANGES[exchange or 'NAS']}{ticker}"


# H0IOCNT0, KOSPI200 option trades with implied volatility and greeks
# (KIS: "지수옵션 실시간체결가"). Keys are option codes near the money, from the KIS master.
OPTION_COLUMNS = (
    "optn_shrn_iscd bsop_hour optn_prpr prdy_vrss_sign optn_prdy_vrss prdy_ctrt optn_oprc optn_hgpr "
    "optn_lwpr last_cnqn acml_vol acml_tr_pbmn hts_thpr hts_otst_stpl_qty otst_stpl_qty_icdc "
    "oprc_hour oprc_vrss_prpr_sign oprc_vrss_nmix_prpr hgpr_hour hgpr_vrss_prpr_sign "
    "hgpr_vrss_nmix_prpr lwpr_hour lwpr_vrss_prpr_sign lwpr_vrss_nmix_prpr shnu_rate prmm_val "
    "invl_val tmvl_val delta gama vega theta rho hts_ints_vltl esdg otst_stpl_rgbf_qty_icdc "
    "thpr_basis unas_hist_vltl cttr dprt mrkt_basis optn_askp1 optn_bidp1 askp_rsqn1 bidp_rsqn1 "
    "seln_cntg_csnu shnu_cntg_csnu ntby_cntg_csnu seln_cntg_smtn shnu_cntg_smtn total_askp_rsqn "
    "total_bidp_rsqn prdy_vol_vrss_acml_vol_rate avrg_vltl dscs_lrqn_vol dynm_mxpr dynm_llam "
    "dynm_prc_limt_yn"
).split()

OPTION_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("event_time", EVENT_TIME),
        ("received_at", RECEIVED_AT),
        ("price", pa.float64()),
        ("qty", pa.int64()),
        ("cum_volume", pa.int64()),
        ("open_interest", pa.int64()),
        ("implied_vol", pa.float64()),
        ("delta", pa.float64()),
        ("gamma", pa.float64()),
        ("vega", pa.float64()),
        ("theta", pa.float64()),
        ("theory_price", pa.float64()),
        ("ask1", pa.float64()),
        ("bid1", pa.float64()),
    ]
)


def _normalize_option(f: dict[str, str], received_at: datetime) -> Row:
    return {
        "symbol": f["optn_shrn_iscd"],
        "event_time": kst_or_received(f["bsop_hour"], received_at),
        "received_at": received_at,
        "price": round(to_float(f["optn_prpr"]), 2),
        "qty": to_int(f["last_cnqn"]),
        "cum_volume": to_int(f["acml_vol"]),
        "open_interest": to_int(f["hts_otst_stpl_qty"]),
        "implied_vol": to_float(f["hts_ints_vltl"]),
        "delta": to_float(f["delta"]),
        "gamma": to_float(f["gama"]),
        "vega": to_float(f["vega"]),
        "theta": to_float(f["theta"]),
        "theory_price": round(to_float(f["hts_thpr"]), 2),
        "ask1": round(to_float(f["optn_askp1"]), 2),
        "bid1": round(to_float(f["optn_bidp1"]), 2),
    }


OPTION = Feed("H0IOCNT0", tuple(OPTION_COLUMNS), OPTION_SCHEMA, _normalize_option)
