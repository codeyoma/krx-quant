"""Real-time feeds: KIS column layouts and their normalized event schemas.

Each feed turns one KIS record (a list of string fields in the documented
column order) into one normalized event row. Event schemas are documented
in docs/events.md; keep the two in sync.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow as pa

KST = ZoneInfo("Asia/Seoul")

Row = dict[str, Any]


@dataclass(frozen=True)
class Feed:
    tr_id: str
    columns: tuple[str, ...]
    schema: pa.Schema
    normalize: Callable[[dict[str, str], datetime], Row]


def _kst(day: date, hhmmss: str) -> datetime:
    return datetime.combine(day, time(int(hhmmss[0:2]), int(hhmmss[2:4]), int(hhmmss[4:6])), KST)


def _int(value: str) -> int:
    """KIS sends blank numeric fields when a value does not apply; record those as 0."""
    return int(value) if value.strip() else 0


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


def _normalize_trade(f: dict[str, str], received_at: datetime) -> Row:
    bsop = f["BSOP_DATE"]
    day = date(int(bsop[0:4]), int(bsop[4:6]), int(bsop[6:8]))
    return {
        "symbol": f["MKSC_SHRN_ISCD"],
        "event_time": _kst(day, f["STCK_CNTG_HOUR"]),
        "received_at": received_at,
        # The integrated feed does not tag fills by venue; per-venue feeds will set KRX / NXT.
        "venue": "UN",
        "price": _int(f["STCK_PRPR"]),
        "qty": _int(f["CNTG_VOL"]),
        "side": _SIDES.get(f["CNTG_CLS_CODE"], "unknown"),
        "cum_volume": _int(f["ACML_VOL"]),
        "ask1": _int(f["ASKP1"]),
        "bid1": _int(f["BIDP1"]),
        "session_code": f["NEW_MKOP_CLS_CODE"],
        "halted": f["TRHT_YN"] == "Y",
        "hour_cls": f["HOUR_CLS_CODE"],
        "vi_ref_price": _int(f["VI_STND_PRC"]),
        # Kept verbatim until its meaning is confirmed; it may identify the venue.
        "market_cls_code": f["MARKET_CLS_CODE"],
    }


TRADE = Feed("H0UNCNT0", tuple(TRADE_COLUMNS), TRADE_SCHEMA, _normalize_trade)


# H0UNASP0, integrated (KRX + NXT) real-time order book (KIS: "국내주식 실시간호가 (통합)").
# Column order per the KIS Open API docs.
_LEVELS = range(1, 11)
BOOK_COLUMNS = (
    ["MKSC_SHRN_ISCD", "BSOP_HOUR", "HOUR_CLS_CODE"]
    + [f"ASKP{i}" for i in _LEVELS]
    + [f"BIDP{i}" for i in _LEVELS]
    + [f"ASKP_RSQN{i}" for i in _LEVELS]
    + [f"BIDP_RSQN{i}" for i in _LEVELS]
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
        *[(f"ask_px_{i}", pa.int64()) for i in _LEVELS],
        *[(f"bid_px_{i}", pa.int64()) for i in _LEVELS],
        *[(f"ask_qty_{i}", pa.int64()) for i in _LEVELS],
        *[(f"bid_qty_{i}", pa.int64()) for i in _LEVELS],
        ("total_ask_qty", pa.int64()),
        ("total_bid_qty", pa.int64()),
        ("cum_volume", pa.int64()),
    ]
)


def _normalize_book(f: dict[str, str], received_at: datetime) -> Row:
    # Book frames carry only a time of day; take the date from the receipt time in KST.
    day = received_at.astimezone(KST).date()
    return {
        "symbol": f["MKSC_SHRN_ISCD"],
        "event_time": _kst(day, f["BSOP_HOUR"]),
        "received_at": received_at,
        "hour_cls": f["HOUR_CLS_CODE"],
        **{f"ask_px_{i}": _int(f[f"ASKP{i}"]) for i in _LEVELS},
        **{f"bid_px_{i}": _int(f[f"BIDP{i}"]) for i in _LEVELS},
        **{f"ask_qty_{i}": _int(f[f"ASKP_RSQN{i}"]) for i in _LEVELS},
        **{f"bid_qty_{i}": _int(f[f"BIDP_RSQN{i}"]) for i in _LEVELS},
        "total_ask_qty": _int(f["TOTAL_ASKP_RSQN"]),
        "total_bid_qty": _int(f["TOTAL_BIDP_RSQN"]),
        "cum_volume": _int(f["ACML_VOL"]),
    }


BOOK = Feed("H0UNASP0", tuple(BOOK_COLUMNS), BOOK_SCHEMA, _normalize_book)

STREAM_FEEDS: tuple[Feed, ...] = (TRADE, BOOK)
FEEDS_BY_TR_ID = {f.tr_id: f for f in STREAM_FEEDS}

RAW_SCHEMA = pa.schema([("received_at", pa.timestamp("us", tz="UTC")), ("message", pa.string())])
