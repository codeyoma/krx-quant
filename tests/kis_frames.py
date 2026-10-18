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

import io
import zipfile
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


PROGRAM_COLUMNS = (
    "MKSC_SHRN_ISCD STCK_CNTG_HOUR SELN_CNQN SELN_TR_PBMN SHNU_CNQN SHNU_TR_PBMN NTBY_CNQN "
    "NTBY_TR_PBMN SELN_RSQN SHNU_RSQN WHOL_NTBY_QTY"
).split()


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


INDEX_COLUMNS = (
    "bstp_cls_code bsop_hour prpr_nmix prdy_vrss_sign bstp_nmix_prdy_vrss acml_vol acml_tr_pbmn "
    "pcas_vol pcas_tr_pbmn prdy_ctrt oprc_nmix nmix_hgpr nmix_lwpr oprc_vrss_nmix_prpr "
    "oprc_vrss_nmix_sign hgpr_vrss_nmix_prpr hgpr_vrss_nmix_sign lwpr_vrss_nmix_prpr "
    "lwpr_vrss_nmix_sign prdy_clpr_vrss_oprc_rate prdy_clpr_vrss_hgpr_rate prdy_clpr_vrss_lwpr_rate "
    "uplm_issu_cnt ascn_issu_cnt stnr_issu_cnt down_issu_cnt lslm_issu_cnt qtqt_ascn_issu_cnt "
    "qtqt_down_issu_cnt tick_vrss"
).split()


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


# KRX야간선물 실시간종목체결 (H0MFCNT0), examples_llm/domestic_futureoption/krx_ngt_futures_ccnl:
# the day futures' list without dscs_bltr_acml_qty.
NIGHT_FUTURES_COLUMNS = (
    "futs_shrn_iscd bsop_hour futs_prdy_vrss prdy_vrss_sign futs_prdy_ctrt futs_prpr futs_oprc "
    "futs_hgpr futs_lwpr last_cnqn acml_vol acml_tr_pbmn hts_thpr mrkt_basis dprt nmsc_fctn_stpl_prc "
    "fmsc_fctn_stpl_prc spead_prc hts_otst_stpl_qty otst_stpl_qty_icdc oprc_hour oprc_vrss_prpr_sign "
    "oprc_vrss_nmix_prpr hgpr_hour hgpr_vrss_prpr_sign hgpr_vrss_nmix_prpr lwpr_hour "
    "lwpr_vrss_prpr_sign lwpr_vrss_nmix_prpr shnu_rate cttr esdg otst_stpl_rgbf_qty_icdc thpr_basis "
    "futs_askp1 futs_bidp1 askp_rsqn1 bidp_rsqn1 seln_cntg_csnu shnu_cntg_csnu ntby_cntg_csnu "
    "seln_cntg_smtn shnu_cntg_smtn total_askp_rsqn total_bidp_rsqn prdy_vol_vrss_acml_vol_rate "
    "dynm_mxpr dynm_llam dynm_prc_limt_yn"
).split()

# KRX야간선물 실시간호가 (H0MFASP0), examples_llm/domestic_futureoption/krx_ngt_futures_asking_price.
NIGHT_BOOK_COLUMNS = (
    "futs_shrn_iscd bsop_hour futs_askp1 futs_askp2 futs_askp3 futs_askp4 futs_askp5 futs_bidp1 "
    "futs_bidp2 futs_bidp3 futs_bidp4 futs_bidp5 askp_csnu1 askp_csnu2 askp_csnu3 askp_csnu4 "
    "askp_csnu5 bidp_csnu1 bidp_csnu2 bidp_csnu3 bidp_csnu4 bidp_csnu5 askp_rsqn1 askp_rsqn2 "
    "askp_rsqn3 askp_rsqn4 askp_rsqn5 bidp_rsqn1 bidp_rsqn2 bidp_rsqn3 bidp_rsqn4 bidp_rsqn5 "
    "total_askp_csnu total_bidp_csnu total_askp_rsqn total_bidp_rsqn total_askp_rsqn_icdc "
    "total_bidp_rsqn_icdc"
).split()


US_TRADE_COLUMNS = (
    "RSYM SYMB ZDIV TYMD XYMD XHMS KYMD KHMS OPEN HIGH LOW LAST SIGN DIFF RATE PBID PASK VBID VASK "
    "EVOL TVOL TAMT BIVL ASVL STRN MTYP"
).split()


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


def program_frame(*records: Mapping[str, str]) -> str:
    return frame("H0UNPGM0", PROGRAM_COLUMNS, *records)


def member_frame(*records: Mapping[str, str]) -> str:
    return frame("H0UNMBC0", MEMBER_COLUMNS, *records)


def index_frame(*records: Mapping[str, str]) -> str:
    return frame("H0UPCNT0", INDEX_COLUMNS, *records)


def futures_frame(tr_id: str, *records: Mapping[str, str]) -> str:
    return frame(tr_id, FUTURES_COLUMNS, *records)


def night_futures_frame(*records: Mapping[str, str]) -> str:
    return frame("H0MFCNT0", NIGHT_FUTURES_COLUMNS, *records)


def night_book_frame(*records: Mapping[str, str]) -> str:
    return frame("H0MFASP0", NIGHT_BOOK_COLUMNS, *records)


def us_trade_frame(*records: Mapping[str, str]) -> str:
    return frame("HDFSCNT0", US_TRADE_COLUMNS, *records)


def option_frame(*records: Mapping[str, str]) -> str:
    return frame("H0IOCNT0", OPTION_COLUMNS, *records)


def master_zips(
    kospi200_futures: list[tuple[str, str]],
    usd_futures: list[tuple[str, str]],
    options: list[tuple[str, str, str, bool]],
) -> dict[str, bytes]:
    """KIS master files (fo_idx_code_mts, fo_com_code) holding the given contracts.

    Futures are (short code, month code) pairs; month code "1" marks the nearest month.
    Options are (code, "C"/"P", "YYYYMM strike", is_atm). Formats follow the files KIS
    publishes (inspected 2026-10-08).
    """
    idx = [
        f"1|{code}|KR4{code}0000|F {code}| |00000.00|{month}|2001|KOSPI200"
        for code, month in kospi200_futures
    ]
    for code, kind, month_strike, is_atm in options:
        month, strike = month_strike.split()
        option_type = "5" if kind == "C" else "6"
        atm_flag = "1" if is_atm else "2"
        idx.append(
            f"{option_type}|{code}|KR4{code}|{kind} {month}   {strike}|{atm_flag}|0{strike.replace(',', '')}"
            "| |2001|KOSPI200"
        )
    com = [
        f"21{code:<9}KR4{code}0000미국달러 F 2026xx{' ' * 24}00000000{month}USD미국달러"
        for code, month in usd_futures
    ]
    return {
        "fo_idx_code_mts.mst.zip": _zip("fo_idx_code_mts.mst", "\n".join(idx)),
        "fo_com_code.mst.zip": _zip("fo_com_code.mst", "\n".join(com)),
    }


def _zip(name: str, text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, text.encode("cp949"))
    return buf.getvalue()
