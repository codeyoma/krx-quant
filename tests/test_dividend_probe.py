"""``krx-quant probe-dividends`` (#310): KIS's adjusted day candles against the traded ones, the
steps of their ratio counted by size and month with their dates, and the rows' ex-date flags,
with no price."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant import dividend_probe
from krx_quant.candles import CANDLES_TR
from krx_quant.dividend_probe import probe_dividends, steps
from krx_quant.fake_kis import FakeKis
from tests.kis_rest import requests_for
from tests.test_collector import settings_for

TODAY = date(2026, 10, 20)
SPLIT = date(2018, 5, 4)  # 50:1
DIVIDENDS = (date(2024, 12, 27), date(2025, 3, 28), date(2025, 6, 27))


def history(dividends: bool) -> list[dict[str, Any]]:
    """Made-up KRX days from 2016, as traded: 50 times dearer before the split; with ``dividends``
    the adjusted closes before each ex-date are 1% lower (KIS taking them out)."""
    rows = []
    day = date(2016, 10, 20)
    while day <= TODAY:
        if day.weekday() < 5:
            traded = 2_500_000 if day < SPLIT else 60_000
            adjusted = traded / 50 if day < SPLIT else traded
            if dividends:
                adjusted *= 0.99 ** sum(1 for d in DIVIDENDS if day < d)
            code = "02" if day in DIVIDENDS else "00"
            rows.append({"day": day, "traded": traded, "adjusted": round(adjusted), "code": code})
        day += timedelta(days=1)
    return rows


def candles_api(rows: list[dict[str, Any]]) -> Any:
    def respond(params: dict[str, str]) -> dict[str, Any]:
        first = datetime.strptime(params["FID_INPUT_DATE_1"], "%Y%m%d").date()
        last = datetime.strptime(params["FID_INPUT_DATE_2"], "%Y%m%d").date()
        price = "adjusted" if params["FID_ORG_ADJ_PRC"] == "0" else "traded"
        within = [r for r in reversed(rows) if first <= r["day"] <= last][:100]
        out = [
            {
                "stck_bsop_date": r["day"].strftime("%Y%m%d"),
                "stck_clpr": str(r[price]),
                "flng_cls_code": r["code"],
                "mod_yn": "N",
                "revl_issu_reas": "",
                "prtt_rate": "0.00",
            }
            for r in within
        ]
        return {"rt_cd": "0", "msg_cd": "MCA00000", "msg1": "정상처리 되었습니다.", "output2": out or [{}]}

    return respond


@pytest.mark.parametrize("dividends", [True, False], ids=["taken out", "left in"])
async def test_the_probe_tells_whether_the_adjusted_prices_take_dividends_out(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dividends: bool
) -> None:
    monkeypatch.setattr(dividend_probe, "PACE", 0.0)
    kis.rest_responses[CANDLES_TR] = candles_api(history(dividends))
    lines: list[str] = []

    calls, failed = await probe_dividends(settings_for(kis, tmp_path / "data"), TODAY, lines.append)

    text = "\n".join(lines)
    assert failed == 0 and calls == len(requests_for(kis, CANDLES_TR))
    assert {q["FID_ORG_ADJ_PRC"] for q in requests_for(kis, CANDLES_TR)} == {"0", "1"}
    assert {q["FID_COND_MRKT_DIV_CODE"] for q in requests_for(kis, CANDLES_TR)} == {"J"}
    assert "계단 5% 이상 날짜: 2018-05-04" in text, "the split"
    if dividends:
        assert "0.5–2% 3" in text
        assert "계단 0.5–2% 날짜: 2024-12-27, 2025-03-28, 2025-06-27" in text
        assert "flng_cls_code 02: 3일 중 그날 계단 3" in text
    else:
        assert "0.5–2%" not in text
        assert "flng_cls_code 02: 3일 중 그날 계단 0" in text
        assert "날짜 2024-12-27, 2025-03-28, 2025-06-27" in text, "KIS's flag is then the calendar"
    assert "2,500,000" not in text and "60000" not in text and "60,000" not in text, "no price"


def test_the_won_s_rounding_is_no_step() -> None:
    adjusted = {date(2026, 1, d): {"stck_clpr": str(c)} for d, c in ((5, 10_001), (6, 10_000), (7, 9_999))}
    traded = {d: {"stck_clpr": "10000"} for d in adjusted}
    found = steps(adjusted, traded)
    assert (found.days, found.rounding, dict(found.sizes)) == (3, 2, {})
