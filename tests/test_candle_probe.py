"""``krx-quant probe-candles`` (#111): what KIS's period chart query gives, measured.

Input: the fake KIS answering the query as documented (100 candles a call, newest first, no
integrated (UN) or NXT candles for an ETF). Output: the printed report.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant import candle_probe
from krx_quant.candle_probe import probe_candles
from krx_quant.candles import CANDLES_TR
from krx_quant.fake_kis import FakeKis
from tests.test_collector import settings_for

TODAY = date(2026, 10, 13)
LISTED = {"D": date(1990, 1, 1), "W": date(1990, 1, 1), "M": date(1990, 1, 1), "Y": date(1990, 1, 1)}
STEP = {"D": 1, "W": 7, "M": 30, "Y": 365}


def candles_api(etf: str) -> Any:
    """Candles from ``FID_INPUT_DATE_2`` back to ``FID_INPUT_DATE_1``, 100 at most, newest first."""

    def respond(params: dict[str, str]) -> dict[str, Any]:
        market, symbol, period = (
            params[k] for k in ("FID_COND_MRKT_DIV_CODE", "FID_INPUT_ISCD", "FID_PERIOD_DIV_CODE")
        )
        if symbol == etf and market != "J":
            return {"rt_cd": "0", "msg_cd": "MCA00000", "output1": {}, "output2": [{}]}
        start = max(datetime.strptime(params["FID_INPUT_DATE_1"], "%Y%m%d").date(), LISTED[period])
        end = datetime.strptime(params["FID_INPUT_DATE_2"], "%Y%m%d").date()
        traded = 50_000 if params["FID_ORG_ADJ_PRC"] == "0" else 2_500_000
        # The period's candles on fixed dates back from today, newest first, 100 at most.
        rows: list[dict[str, str]] = []
        day = TODAY
        while day >= start and len(rows) < 100:
            if day <= end:
                rows.append(
                    {"stck_bsop_date": day.strftime("%Y%m%d"), "stck_clpr": str(traded), "acml_vol": "10"}
                )
            day -= timedelta(days=STEP[period])
        return {"rt_cd": "0", "msg_cd": "MCA00000", "output1": {"hts_kor_isnm": "삼성전자"}, "output2": rows}

    return respond


async def test_the_probe_reports_markets_history_and_adjusted_prices(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(candle_probe, "PACE", 0)
    kis.rest_responses[CANDLES_TR] = candles_api("133690")
    settings = replace(
        settings_for(kis, tmp_path), symbols=("005930",), etf_symbols=("133690",), rest_max_per_second=1000
    )
    lines: list[str] = []

    calls = await probe_candles(settings, TODAY, lines.append)

    report = "\n".join(lines)
    assert "markets for 133690 (D, the last 30 days):\n  UN: 0 rows, -\n  J: 31 rows" in report
    # Each period paged back to its first candle (1990 here), 100 a call; the last call finds none.
    assert "  D: 13435 candles in 136 calls (at most 100 a call), oldest 1990-01-0" in report
    assert "  Y: 37 candles in 2 calls (at most 37 a call), oldest 1990-" in report
    assert "  FID_ORG_ADJ_PRC=0: 20180423:50000, " in report
    assert "  FID_ORG_ADJ_PRC=1: 20180423:2500000, " in report
    assert calls == len([r for r in kis.rest_requests if r[1] == CANDLES_TR])
