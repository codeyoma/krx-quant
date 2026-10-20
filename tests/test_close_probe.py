"""``krx-quant probe-close`` (#264): the KRX day candle's close against KRX's one-minute bars and
the recorded closing auction print, counted before and from the after-market, with no price."""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from krx_quant import close_probe
from krx_quant.candles import CANDLES_TR, Replaced, write_candles, write_krx_days
from krx_quant.close_probe import probe_close
from krx_quant.fake_kis import FakeKis
from krx_quant.feeds import KRX_TRADE
from krx_quant.feeds.base import KST
from krx_quant.recorder import ParquetRecorder
from tests.kis_rest import MINUTE_BARS, minute_bars_api, requests_for
from tests.replay_events import at, krx_trade
from tests.test_auctions import candle, krx_bars
from tests.test_collector import settings_for

TODAY = date(2026, 10, 19)
BEFORE, NO_BARS = date(2026, 9, 10), date(2026, 9, 11)
AFTER, RECORDED = date(2026, 10, 15), date(2026, 10, 16)


def kis_day(row: dict[str, Any], close: int | None = None) -> dict[str, str]:
    """A stored candle as KIS's period chart query gives it, with ``close`` in place of its own."""
    return {
        "stck_bsop_date": row["date"].strftime("%Y%m%d"),
        "stck_oprc": str(row["open"]),
        "stck_hgpr": str(row["high"]),
        "stck_lwpr": str(row["low"]),
        "stck_clpr": str(row["close"] if close is None else close),
        "acml_vol": str(row["volume"]),
        "acml_tr_pbmn": str(row["value"]),
    }


async def test_the_probe_tells_the_after_markets_last_price_from_the_closing_auction(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before the after-market the candle's close is the closing auction's (the day's last bar);
    from it, the after-market's last bar's, and the recorded print is the auction's bar. The days
    whose close is not the auction's are listed; KIS's candles as they are now (#264: AFTER
    finalized to the auction, RECORDED still provisional) are set against the stored ones and the
    auction bar; and the file's notes of replaced candles are printed."""
    monkeypatch.setattr(close_probe, "PACE", 0.0)
    data = tmp_path / "data"
    stored = [
        candle(BEFORE, 70_000, 70_800),
        candle(NO_BARS, 70_800, 71_000),
        candle(AFTER, 71_000, 72_300),  # the after-market's last price
        candle(RECORDED, 72_000, 72_900),
    ]
    replaced = Replaced(BEFORE, datetime(2026, 9, 11, 7, 20, tzinfo=KST), ("close",))
    write_krx_days(data, "005930", stored, [replaced])
    write_candles(data, "week", "005930", [])
    bars = {
        BEFORE: krx_bars(BEFORE, 70_000, 70_800),
        AFTER: krx_bars(AFTER, 71_000, 71_900, evening=72_300),
        RECORDED: krx_bars(RECORDED, 72_000, 72_500, evening=72_900),
    }
    kis.rest_responses[MINUTE_BARS] = minute_bars_api(lambda market, symbol, day: bars.get(day, []))
    now = [kis_day(stored[0]), kis_day(stored[1]), kis_day(stored[2], close=71_900), kis_day(stored[3])]
    kis.rest_responses[CANDLES_TR] = lambda params: {
        "rt_cd": "0",
        "msg_cd": "MCA00000",
        "msg1": "정상처리 되었습니다.",
        "output2": [
            r
            for r in reversed(now)
            if params["FID_INPUT_DATE_1"] <= r["stck_bsop_date"] <= params["FID_INPUT_DATE_2"]
        ],
    }
    recorder = ParquetRecorder(data, flush_rows=100, flush_seconds=3600)
    for feed, row in [
        krx_trade(at("09:00:00.2", RECORDED), 72_000, bid=71_900, ask=72_100),
        krx_trade(at("15:30:00.4", RECORDED), 72_500, bid=72_400, ask=72_600),
    ]:
        recorder.record("events", RECORDED, feed, row["symbol"], row, KRX_TRADE.schema)
    recorder.close()
    lines: list[str] = []

    calls, failed = await probe_close(settings_for(kis, data), TODAY, lines.append)

    text = "\n".join(lines)
    assert (calls, failed) == (10, 0), "a day reaching the auction in its latest page asks two pages"
    assert {q["FID_COND_MRKT_DIV_CODE"] for q in requests_for(kis, MINUTE_BARS)} == {"J"}
    [asked] = requests_for(kis, CANDLES_TR)
    assert (asked["FID_COND_MRKT_DIV_CODE"], asked["FID_INPUT_DATE_1"], asked["FID_INPUT_DATE_2"]) == (
        "J",
        "20260910",
        "20261016",
    ), "the newest stored days, at most ten, in one query"
    assert "005930: 저장된 KRX 일봉 4일" in text
    before, after = text.split("애프터마켓 뒤")
    assert "1분봉 있음 1 · 없음 1" in before
    assert "마지막 봉 시각: 15:30 1" in before
    assert "일봉 종가 = 종가 단일가 봉 종가: 같음 1 · 다름 0" in before
    assert "일봉 종가가 같은 것: 둘 다(같은 값) 1" in before
    assert "단일가 봉과 다른 날" not in before
    assert "1분봉 있음 2 · 없음 0" in after
    assert "마지막 봉 시각: 19:59 2" in after
    assert "종가 단일가 봉(마감부터 10분 안의 첫 봉) 시각: 15:30 2" in after
    assert "일봉 종가 = 종가 단일가 봉 종가: 같음 0 · 다름 2" in after
    assert "일봉 종가 = 그날 마지막 봉 종가: 같음 2 · 다름 0" in after
    assert "일봉 종가가 같은 것: 마지막 봉과만 2" in after
    assert "일봉 시가 = 시가 단일가 봉(개장 분의 봉) 시가: 같음 2 · 다름 0" in after
    assert "일봉 종가가 단일가 봉과 다른 날: 2026-10-15, 2026-10-16" in after
    assert (
        "녹화한 종가 단일가(녹화 있는 날 1): = 단일가 봉 종가 같음 1 · 다름 0 · = 일봉 종가 같음 0 · 다름 1"
        in after
    )
    assert "KIS가 지금 주는 KRX 일봉(최근 4일, 다시 받음)" in after
    assert "    2026-09-10: 저장 = KIS 같음 · 종가 같음" in after
    assert "    2026-09-11: 저장 = KIS 같음 · 종가 봉 없음" in after
    assert "    2026-10-15: 저장 = KIS 다름(close) · 종가 같음" in after, "KIS finalized it"
    assert "    2026-10-16: 저장 = KIS 같음 · 종가 다름" in after, "still provisional at KIS"
    assert "    KRX 일봉 1개: 2026-09-10 ← 09-11 07:20 · close" in after
    assert "    통합 일봉: 파일 없음" in after
    assert "    통합 주봉: 없음" in after
    assert "KIS: 10번 조회, 0번 실패" in text
    bare = re.sub(r"\d{4}-\d{2}-\d{2}|005930|FHKST03010230|H0STCNT0", "", text)
    assert not re.search(r"\d{5,}", bare), "no price in the output"
