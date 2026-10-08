"""Builders for KIS real-time data frames, used as test input.

Column orders are copied from the KIS Open API documentation
(koreainvestment/open-trading-api, examples_llm/domestic_stock/ccnl_total
and asking_price_total). A frame looks like ``0|<tr_id>|<count>|f1^f2^...``
where the fields of ``count`` records are concatenated.

The column lists are deliberately a separate copy from the production ones in
``krx_quant.feeds``: a copying mistake in either place makes the tests fail
instead of passing by construction.
"""

from __future__ import annotations

from collections.abc import Mapping

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

BOOK_COLUMNS = (
    ["MKSC_SHRN_ISCD", "BSOP_HOUR", "HOUR_CLS_CODE"]
    + [f"ASKP{i}" for i in range(1, 11)]
    + [f"BIDP{i}" for i in range(1, 11)]
    + [f"ASKP_RSQN{i}" for i in range(1, 11)]
    + [f"BIDP_RSQN{i}" for i in range(1, 11)]
    + (
        "TOTAL_ASKP_RSQN TOTAL_BIDP_RSQN OVTM_TOTAL_ASKP_RSQN OVTM_TOTAL_BIDP_RSQN ANTC_CNPR ANTC_CNQN "
        "ANTC_VOL ANTC_CNTG_VRSS ANTC_CNTG_VRSS_SIGN ANTC_CNTG_PRDY_CTRT ACML_VOL TOTAL_ASKP_RSQN_ICDC "
        "TOTAL_BIDP_RSQN_ICDC OVTM_TOTAL_ASKP_ICDC OVTM_TOTAL_BIDP_ICDC STCK_DEAL_CLS_CODE KMID_PRC "
        "KMID_TOTAL_RSQN KMID_CLS_CODE NMID_PRC NMID_TOTAL_RSQN NMID_CLS_CODE ANTC_EXCH_CLS_CODE"
    ).split()
)


def _record(columns: list[str], values: Mapping[str, str]) -> list[str]:
    unknown = set(values) - set(columns)
    assert not unknown, f"unknown columns: {unknown}"
    return [values.get(c, "0") for c in columns]


def frame(tr_id: str, columns: list[str], *records: Mapping[str, str]) -> str:
    fields: list[str] = []
    for r in records:
        fields += _record(columns, r)
    return f"0|{tr_id}|{len(records):03d}|{'^'.join(fields)}"


def trade_frame(*records: Mapping[str, str]) -> str:
    return frame("H0UNCNT0", TRADE_COLUMNS, *records)


def book_frame(*records: Mapping[str, str]) -> str:
    return frame("H0UNASP0", BOOK_COLUMNS, *records)
