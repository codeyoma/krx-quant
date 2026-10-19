"""``krx-quant daily-check`` (#264): what the server did with the daily kinds, and whether the
KRX day candles hold the KRX auctions' prices, without printing a price."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from krx_quant.auctions import MINUTE_BAR, write_auctions
from krx_quant.candles import CANDLE_SCHEMA, write_candles, write_krx_days
from krx_quant.daily_check import daily_check
from krx_quant.feeds import KRX_TRADE
from krx_quant.kinds import Answer
from krx_quant.market_calendar import MarketCalendar
from krx_quant.open_to_close import FLAT_NAME, OPEN_TO_CLOSE, PERSIST
from krx_quant.predictions import NewKindPrediction, PredictionStore
from krx_quant.recorder import ParquetRecorder
from tests.replay_events import at, krx_trade

DAY = date(2026, 10, 12)
CALENDAR = MarketCalendar({DAY: True})


def candle(symbol: str, venue: str, opened: int, close: int) -> dict[str, object]:
    row: dict[str, object] = {
        "symbol": symbol,
        "date": DAY,
        "venue": venue,
        "open": opened,
        "high": max(opened, close) + 100,
        "low": min(opened, close) - 100,
        "close": close,
        "volume": 1_000,
        "value": 1_000 * close,
    }
    assert set(row) == set(CANDLE_SCHEMA.names)
    return row


def test_the_check_tells_the_predictions_answers_and_auction_prints(tmp_path: Path) -> None:
    """As on 2026-10-19 (#264): the KRX day candle's close is the after-market's last trade, not
    the closing auction's print; the kept closing auction price the answers end at is the print."""
    data, db = tmp_path / "data", tmp_path / "engine.sqlite3"
    write_krx_days(data, "005930", [candle("005930", "KRX", 81_200, 82_300)])
    write_candles(data, "day", "005930", [candle("005930", "UN", 81_600, 82_000)])  # NXT's pre-market open
    write_krx_days(data, "000660", [candle("000660", "KRX", 201_000, 199_500)])
    recorder = ParquetRecorder(data, flush_rows=100, flush_seconds=3600)
    for feed, row in [
        krx_trade(at("08:35:00"), 81_000, bid=0, ask=0),  # the pre-open after-hours trade, at the last close
        krx_trade(at("09:00:00.4"), 81_200, bid=81_100, ask=81_300),  # the opening auction's print
        krx_trade(at("11:00:00"), 81_700, bid=81_600, ask=81_800),
        krx_trade(at("15:19:59"), 81_900, bid=81_800, ask=82_000),
        krx_trade(at("15:30:01"), 82_000, bid=81_900, ask=82_100),  # the closing auction's print
        krx_trade(at("16:05:00"), 82_300, bid=82_200, ask=82_400),  # the after-market
        krx_trade(at("09:00:00.5"), 201_500, bid=201_000, ask=202_000, symbol="000660"),  # not the candle's
        krx_trade(at("15:30:00.7"), 199_500, bid=199_000, ask=200_000, symbol="000660"),
    ]:
        recorder.record("events", DAY, feed, row["symbol"], row, KRX_TRADE.schema)
    recorder.close()
    for symbol, closed in (("005930", 82_000), ("000660", 199_400)):
        write_auctions(
            data,
            symbol,
            {DAY: {"symbol": symbol, "date": DAY, "open": None, "close": closed, "source": MINUTE_BAR}},
        )
    store = PredictionStore(db)
    t = OPEN_TO_CLOSE.predicted_at(DAY)
    store.add_kind_predictions(
        OPEN_TO_CLOSE.name,
        [
            NewKindPrediction(
                s, name, "v1", t, at("08:50:00.2"), {"p_up": 0.0, "p_flat": 1.0, "p_down": 0.0}, band=0.007
            )
            for s in ("005930", "000660")
            for name in (FLAT_NAME, PERSIST)
        ],
    )
    store.add_kind_answers(
        OPEN_TO_CLOSE.name,
        OPEN_TO_CLOSE.answer_version,
        [
            ("005930", t, Answer("up", None, {"source": MINUTE_BAR}), False),
            ("000660", t, Answer("down", None, {"source": MINUTE_BAR}), True),
        ],
        scored_at=at("15:45:00"),
    )
    store.close()

    text = daily_check(data, db, CALENDAR, ("005930", "000660"), DAY, days=1)

    assert "기간: 2026-10-12 – 2026-10-12 (1거래일)" in text
    assert (
        "2026-10-12: 예측 4개 (otc-flat ok 2, otc-persist ok 2), 보합 폭 4/4, 표시 08:50:00–08:50:00" in text
    )
    assert (
        "2026-10-12: 정답 2/2 (up 1, flat 0, down 1) · 출처 krx_minute_bar 2 · 특수 1"
        " · 채점 10-12 15:45–10-12 15:45" in text
    )
    assert "2026-10-12 005930: 시가 같음, 종가 다름, 정답 종가 같음, 통합 일봉 시가 다름" in text
    assert "2026-10-12 000660: 시가 다름, 종가 같음, 정답 종가 다름" in text
    assert (
        "요약: 시가 같음 1, 시가 다름 1, 정답 종가 같음 1, 정답 종가 다름 1, 종가 같음 1, 종가 다름 1,"
        " 통합 시가 다름 1" in text
    )
    assert not re.search(
        r"\d{5,}", text.replace("2026-10-12", "").replace("005930", "").replace("000660", "")
    ), "no price in the output"


def test_the_check_says_what_is_missing(tmp_path: Path) -> None:
    text = daily_check(tmp_path / "data", tmp_path / "engine.sqlite3", CALENDAR, ("005930",), DAY, days=1)
    assert "엔진 DB 없음" in text
    assert "2026-10-12 005930: KRX 일봉 없음" in text
