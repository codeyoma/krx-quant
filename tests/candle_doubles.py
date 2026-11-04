"""Day candles in KIS's fields and the fake KIS's answer to the period chart query, made of them, for
the candle and daily tests (#511). Every value is made up.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from krx_quant.candles import period_start

PER_QUERY = 100

Candles = Callable[[str, str], list[dict[str, str]]]


def kis_candles(days: list[date], base: int = 50_000) -> list[dict[str, str]]:
    """A candle per day in KIS's fields, oldest first; the close moves with the day's index."""
    rows = []
    for i, day in enumerate(days):
        close = base + 10 * i
        rows.append(
            {
                "stck_bsop_date": day.strftime("%Y%m%d"),
                "stck_oprc": str(close - 50),
                "stck_hgpr": str(close + 100),
                "stck_lwpr": str(close - 100),
                "stck_clpr": str(close),
                "acml_vol": str(1000 + i),
                "acml_tr_pbmn": str(close * (1000 + i)),
                "mod_yn": "N",
            }
        )
    return rows


def kis_periods(days: list[dict[str, str]], code: str, label: str = "last") -> list[dict[str, str]]:
    """KIS's candles of the period ``code`` (D, W, M, Y) made of its day candles, oldest first:
    each labelled with its period's ``label`` ("first" or "last") trading day. The period
    still going holds every day there is, as KIS's does during it."""
    if code == "D":
        return days
    period = {"W": "week", "M": "month", "Y": "year"}[code]
    groups: dict[date, list[dict[str, str]]] = {}
    for r in days:
        groups.setdefault(
            period_start(datetime.strptime(r["stck_bsop_date"], "%Y%m%d").date(), period), []
        ).append(r)
    out = []
    for start in sorted(groups):
        group = groups[start]
        out.append(
            {
                "stck_bsop_date": (group[-1] if label == "last" else group[0])["stck_bsop_date"],
                "stck_oprc": group[0]["stck_oprc"],
                "stck_hgpr": str(max(int(r["stck_hgpr"]) for r in group)),
                "stck_lwpr": str(min(int(r["stck_lwpr"]) for r in group)),
                "stck_clpr": group[-1]["stck_clpr"],
                "acml_vol": str(sum(int(r["acml_vol"]) for r in group)),
                "acml_tr_pbmn": str(sum(int(r["acml_tr_pbmn"]) for r in group)),
                "mod_yn": "N",
            }
        )
    return out


def candles_api(candles: Candles, label: str = "last") -> Callable[[dict[str, str]], dict[str, Any]]:
    """The period chart query: the newest 100 candles of the period asked, made of the day
    candles ``candles(market, symbol)``, labelled from ``FID_INPUT_DATE_1`` to
    ``FID_INPUT_DATE_2``, newest first, padded like KIS."""

    def respond(params: dict[str, str]) -> dict[str, Any]:
        assert params["FID_ORG_ADJ_PRC"] == "0"
        first, last = params["FID_INPUT_DATE_1"], params["FID_INPUT_DATE_2"]
        days = candles(params["FID_COND_MRKT_DIV_CODE"], params["FID_INPUT_ISCD"])
        rows = kis_periods(days, params["FID_PERIOD_DIV_CODE"], label)
        within = [r for r in reversed(rows) if first <= r["stck_bsop_date"] <= last][:PER_QUERY]
        return {
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "정상처리 되었습니다.",
            "output1": {"hts_kor_isnm": "삼성전자"},
            "output2": within or [{"stck_bsop_date": "", "stck_clpr": ""}],
        }

    return respond
