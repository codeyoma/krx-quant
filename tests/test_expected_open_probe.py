"""``krx-quant probe-expected-open`` (#304): what the recorded books carried of the auctions'
expected fill, without printing a price."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from krx_quant.candles import write_krx_days
from krx_quant.cli import main
from krx_quant.expected_open_probe import report
from krx_quant.feeds import BOOK, BOOK_SNAPSHOT, RAW_SCHEMA
from krx_quant.feeds.base import KST
from krx_quant.market_calendar import MarketCalendar
from krx_quant.recorder import ParquetRecorder
from tests.kis_frames import book_frame

DAY = date(2026, 10, 12)
CALENDAR = MarketCalendar({DAY: True, date(2026, 10, 13): True})
NIGHT = datetime.combine(DAY, time(22, 30), KST)
PRICES = ("81200", "81300", "82000", "82100", "190600")


def at(hhmmss: str, day: date = DAY) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmmss), KST)


def book(hhmmss: str, expected: str = "0", venue: str = "0", hour: str = "0") -> str:
    fields = {"MKSC_SHRN_ISCD": "005930", "BSOP_HOUR": hhmmss.replace(":", ""), "HOUR_CLS_CODE": hour}
    return book_frame(fields | {"ANTC_CNPR": expected, "ANTC_EXCH_CLS_CODE": venue})


def snapshot(**quote: str) -> str:
    body = {"rt_cd": "0", "output1": {}, "output2": {"stck_prpr": "81100", **quote}}
    return json.dumps({"tr_id": BOOK_SNAPSHOT.tr_id, "params": {}, "body": body})


def record_raw(data: Path, rows: list[tuple[str, str, datetime, str]]) -> None:
    recorder = ParquetRecorder(data, flush_rows=100, flush_seconds=3600)
    for feed, symbol, received, message in rows:
        row = {"received_at": received.astimezone(UTC), "message": message}
        recorder.record("raw", received.date(), feed, symbol, row, RAW_SCHEMA)
    recorder.close()


def candle(opened: int, close: int) -> dict[str, object]:
    return {
        "symbol": "005930",
        "date": DAY,
        "venue": "KRX",
        "open": opened,
        "high": close + 100,
        "low": opened - 100,
        "close": close,
        "volume": 1_000,
        "value": 1_000 * close,
    }


def book_rows(schema: pa.Schema, *rows: dict[str, object]) -> pa.Table:
    full = [{name: row.get(name) for name in schema.names} for row in rows]
    return pa.Table.from_pylist(full, schema=schema)


def test_the_probe_counts_the_expected_fill_by_session_part_and_prints_no_price(tmp_path: Path) -> None:
    data = tmp_path / "data"
    record_raw(
        data,
        [
            (BOOK.tr_id, "005930", at("08:10:00"), book("08:10:00")),
            (BOOK.tr_id, "005930", at("08:31:00"), book("08:31:00", "81200", "1", "A")),
            (BOOK.tr_id, "005930", at("08:59:58"), book("08:59:58", "81300", "1", "A")),
            (BOOK.tr_id, "005930", at("09:30:00"), book("09:30:00")),
            (BOOK.tr_id, "005930", at("15:21:00"), book("15:21:00", "82000", "1", "0")),
            (BOOK.tr_id, "005930", at("15:29:59"), book("15:29:59", "82100", "1", "0")),
            (BOOK.tr_id, "005930", at("16:00:00"), book("16:00:00")),
            (BOOK.tr_id, "005930", at("08:40:00"), "0|H0UNASP0|001|005930^too^short"),
            (BOOK_SNAPSHOT.tr_id, "005930", at("08:45:00"), snapshot(antc_cnpr="81200")),
            (BOOK_SNAPSHOT.tr_id, "005930", at("08:46:00"), snapshot()),
        ],
    )
    write_krx_days(data, "005930", [candle(81_300, 82_000)])
    events = data / "events" / f"date={DAY}" / f"feed={BOOK.tr_id}" / "symbol=005930"
    events.mkdir(parents=True)
    names = ("expected_px", "expected_qty", "expected_volume", "expected_exch_cls")
    old = pa.schema([f for f in BOOK.schema if f.name not in names])
    received = at("08:58:00").astimezone(UTC)
    # Written before #304 (no new columns), then after it.
    before = book_rows(old, {"symbol": "005930", "received_at": received})
    pq.write_table(before, events / "part-085800-a.parquet")
    new = book_rows(BOOK.schema, {"symbol": "005930", "received_at": received, "expected_px": 81_300})
    pq.write_table(new, events / "part-085900-b.parquet")

    output = report(data, CALENDAR, ("005930",), NIGHT)

    assert output.startswith("예상 체결 녹화 확인 (#304): 2026-10-12 – 2026-10-12, 거래일 1일, 종목 005930")
    assert (
        "005930: 장전 1(예상가 0) · 시가 단일가 2(예상가 2) · 연속매매 1(예상가 0) · "
        "종가 단일가 2(예상가 2) · 장 마감 뒤 1(예상가 0) · 읽을 수 없는 프레임 1"
    ) in output
    assert "005930: 시가 단일가 '1' 2 / 종가 단일가 '1' 2" in output
    assert "시가 단일가 'A' 2" in output
    assert "005930 2026-10-12: 시가 단일가 08:31:00–08:59:58, 종가 단일가 15:21:00–15:29:59" in output
    assert "005930 시가 단일가: 같음 1" in output
    assert "005930 종가 단일가: 1틱 1" in output  # 82,100 against a close of 82,000: one 100-won tick
    assert "005930: 시가 단일가 응답 2(antc_cnpr 있음 1, 0이 아님 1)" in output
    assert "005930: 행 2 · 새 열이 있는 행 1 · 예상가가 있는 행 시가 단일가 1" in output
    for price in PRICES:
        assert price not in output.replace(",", "")


def test_a_day_without_an_expected_price_or_a_candle_is_counted_so(tmp_path: Path) -> None:
    data = tmp_path / "data"
    record_raw(data, [(BOOK.tr_id, "005930", at("08:31:00"), book("08:31:00", "81200", "1"))])

    output = report(data, CALENDAR, ("005930",), NIGHT)

    assert "005930 시가 단일가: 일봉 없음 1" in output
    assert "005930 종가 단일가: 예상가 없음 1" in output
    assert "005930 2026-10-12: 시가 단일가 08:31:00–08:31:00" in output
    assert "005930: 응답 없음" in output


def test_the_probe_reads_only_the_trading_days_over_and_the_latest_ones(tmp_path: Path) -> None:
    data = tmp_path / "data"
    later = date(2026, 10, 13)
    record_raw(
        data,
        [
            (BOOK.tr_id, "005930", at("08:31:00"), book("08:31:00", "81200", "1")),
            (BOOK.tr_id, "005930", at("08:31:00", later), book("08:31:00", "81200", "1")),
        ],
    )

    assert "2026-10-12 – 2026-10-12, 거래일 1일" in report(data, CALENDAR, ("005930",), NIGHT)
    tonight = datetime.combine(later, time(22, 30), KST)
    assert "2026-10-13 – 2026-10-13, 거래일 1일" in report(data, CALENDAR, ("005930",), tonight, days=1)
    assert "2026-10-12 – 2026-10-13, 거래일 2일" in report(data, CALENDAR, ("005930",), tonight)


def test_the_probe_reads_at_least_one_day(tmp_path: Path) -> None:
    # A slice of the last 0 days would be every recorded day: refused, not read.
    with pytest.raises(ValueError, match="at least 1"):
        report(tmp_path, CALENDAR, ("005930",), NIGHT, days=0)
    with pytest.raises(SystemExit):
        main(["probe-expected-open", "--days", "0"])


def test_without_recorded_books_the_probe_says_so(tmp_path: Path) -> None:
    assert report(tmp_path, CALENDAR, ("005930",), NIGHT) == (
        "예상 체결 녹화 확인 (#304): raw에 통합 호가(H0UNASP0)가 있는 거래일이 없다"
    )
