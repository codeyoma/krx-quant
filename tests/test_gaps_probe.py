"""``krx-quant probe-gaps`` (#344): the recording's gaps day by day and the closing auction window,
read from files the collector's own writers made, without printing a market value."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from krx_quant.backfill import BAR_SCHEMA, stock_bars_path
from krx_quant.cli import main
from krx_quant.collector import silence
from krx_quant.feeds import BOOK, KRX_TRADE, NIGHT_FUTURES, OPTION, RAW_SCHEMA, TRADE, Feed
from krx_quant.feeds.base import KST
from krx_quant.gaps_probe import report
from krx_quant.health import GAP_SCHEMA, FeedHealth
from krx_quant.market_calendar import MarketCalendar
from krx_quant.recorder import ParquetRecorder
from tests.kis_frames import TRADE_COLUMNS, book_frame, trade_frame

DAY = date(2026, 10, 21)  # a Wednesday
SUNDAY = date(2026, 10, 18)
CALENDAR = MarketCalendar({SUNDAY: False, DAY: True})
NIGHT = datetime.combine(DAY, time(22, 30), KST)
STOCKS, ETFS = ("005930", "000660"), ("133690",)
PRICES = ("81500", "81600", "190600")
OPTION_KEY = "B01611892"


def at(hhmmss: str, day: date = DAY) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmmss), KST)


class Clock:
    def __init__(self) -> None:
        self.now = at("09:00:00")

    def __call__(self) -> datetime:
        return self.now


def subscriptions() -> list[tuple[str, str]]:
    stocks = [(feed, s) for feed in (TRADE.tr_id, BOOK.tr_id) for s in STOCKS]
    return [*stocks, *((KRX_TRADE.tr_id, s) for s in (*STOCKS, *ETFS)), (OPTION.tr_id, OPTION_KEY)]


def record_gaps(data: Path) -> None:
    """The day's gaps as the collector's feed health writes them: a stale option key, a disconnect
    KIS made at 11:30:39, the watchdog's silence from 15:29:33, and a Sunday night's disconnect."""
    recorder = ParquetRecorder(data, flush_rows=1_000, flush_seconds=3600)
    clock = Clock()
    health = FeedHealth(recorder, clock, CALENDAR)
    keys = subscriptions()
    for key in keys:
        health.watch(key)
    clock.now = at("09:31:00")  # the option key has not delivered for its 30 minutes, the rest has
    for key in keys[:-1]:
        health.received(key)
    assert health.check_stale() == [(OPTION.tr_id, OPTION_KEY)]
    clock.now = at("10:02:30")
    health.received((OPTION.tr_id, OPTION_KEY))
    clock.now = at("11:30:39")
    health.connection_lost(keys, "websocket closed by KIS (code 1006)")
    for key, acknowledged in zip(
        keys, ("11:30:41", "11:30:42", *["11:30:43"] * (len(keys) - 2)), strict=True
    ):
        clock.now = at(acknowledged)
        health.subscribed(key)
    clock.now = at("15:30:33")  # the watchdog noticed: its gaps start at the last frame
    health.connection_lost(keys, silence(60), since=at("15:29:33"))
    for key in keys[:-1]:
        clock.now = at("15:30:36")
        health.subscribed(key)
    health.close()  # the option key's gap still open: written without an end
    sunday = FeedHealth(recorder, lambda: at("03:00:00", SUNDAY), CALENDAR)
    sunday.connection_lost([(NIGHT_FUTURES.tr_id, "A01612")], "websocket closed by KIS (code 1000)")
    sunday.close()
    recorder.close()


def record_raw(data: Path, rows: list[tuple[str, str, str, str]]) -> None:
    recorder = ParquetRecorder(data, flush_rows=1_000, flush_seconds=3600)
    for feed, key, hhmmss, message in rows:
        row = {"received_at": at(hhmmss).astimezone(UTC), "message": message}
        recorder.record("raw", DAY, feed, key, row, RAW_SCHEMA)
    recorder.close()


def record_trades(data: Path, feed: Feed, trades: list[tuple[str, str, str, str]]) -> None:
    """Trades through the feed's own normalizer: (symbol, exchange time, receipt time, market
    operation code)."""
    recorder = ParquetRecorder(data, flush_rows=1_000, flush_seconds=3600)
    for i, (symbol, hhmmss, received, session_code) in enumerate(trades):
        fields = {c: "0" for c in TRADE_COLUMNS} | {
            "MKSC_SHRN_ISCD": symbol,
            "STCK_CNTG_HOUR": hhmmss.replace(":", ""),
            "STCK_PRPR": PRICES[i % 2],
            "BSOP_DATE": DAY.strftime("%Y%m%d"),
            "NEW_MKOP_CLS_CODE": session_code,
            "HOUR_CLS_CODE": "0",
        }
        row = feed.normalize(fields, at(received).astimezone(UTC))
        recorder.record("events", DAY, feed.tr_id, symbol, row, feed.schema)
    recorder.close()


def record_bars(data: Path, symbol: str, minutes: range) -> None:
    rows = [
        {"symbol": symbol, "bar_time": at(f"15:{m:02d}:00"), "venue": "UN", "open": 81_500}
        | {"high": 81_600, "low": 81_500, "close": 81_600, "volume": 10, "cum_value": 190_600}
        for m in minutes
    ]
    path = stock_bars_path(data, DAY, symbol)
    path.parent.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=BAR_SCHEMA), path)


@pytest.fixture
def recorded(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    record_gaps(data)
    book = book_frame({"MKSC_SHRN_ISCD": "005930"})
    record_raw(
        data,
        [
            (BOOK.tr_id, "005930", "15:15:00", book),  # before the window: the silence reads it
            *((BOOK.tr_id, "005930", f"15:{m}:00", book) for m in range(20, 30)),  # a minute apart
            (BOOK.tr_id, "005930", "15:29:33", book),
            (BOOK.tr_id, "000660", "15:29:20", book),
            (TRADE.tr_id, "005930", "15:19:58", trade_frame({"MKSC_SHRN_ISCD": "005930"})),
            ("_system", "005930", "15:30:35", '{"header": {"tr_id": "H0UNASP0"}}'),
            (BOOK.tr_id, "005930", "15:30:40", book),
            *((BOOK.tr_id, "005930", f"15:{m}:00", book) for m in range(31, 34)),  # after the window
            (NIGHT_FUTURES.tr_id, "A01612", "02:00:00", "0|H0MFCNT0|001|A01612"),
        ],
    )
    record_trades(
        data,
        KRX_TRADE,
        [
            ("005930", "15:30:00", "15:30:00.4", "0"),
            ("005930", "15:40:00", "15:40:00.2", "20"),
            ("000660", "15:40:00", "15:40:00.3", "20"),
        ],
    )
    record_trades(
        data,
        TRADE,
        [
            ("005930", "15:30:01", "15:30:41", "0"),
            ("000660", "15:19:59", "15:19:59.5", "0"),
        ],
    )
    record_bars(data, "005930", range(19, 32))  # 15:19 to 15:31: eleven in the window
    return data


def test_the_probe_summarizes_each_day_s_gaps_and_closing_window_without_a_price(recorded: Path) -> None:
    output = report(recorded, CALENDAR, STOCKS, ETFS, NIGHT)

    assert output.startswith(
        "WebSocket 결측과 종가 단일가 녹화 (#344): 2026-10-18 – 2026-10-21, 녹화한 날 2일(거래일 1일)"
    )
    assert "장 마감(보통 15:30:00) 뒤 1분 안의 첫 체결을 그것으로 본다" in output, "the rule is said"
    assert (
        "  2026-10-18 휴장일: 결측 구간 1개 · disconnected: websocket closed by KIS (code 1000) 1번(구간 1개)"
        in output
    )
    assert (
        "  2026-10-21 거래일: 결측 구간 17개 · disconnected: silent 60s 1번(구간 8개) · "
        "disconnected: websocket closed by KIS (code 1006) 1번(구간 8개) · stale 1번(구간 1개) · "
        "종가 단일가 15:20–15:31: 가장 긴 침묵 62.0초(15:29:33–15:30:35), 겹친 끊김 1번"
    ) in output
    for price in PRICES:
        assert price not in output.replace(",", "")


def test_a_disconnect_s_gaps_are_one_line_with_its_feeds_keys_and_lengths(recorded: Path) -> None:
    output = report(recorded, CALENDAR, STOCKS, ETFS, NIGHT)
    details = output.split("3. 결측 구간")[1]

    assert (
        "    11:30:39 disconnected: websocket closed by KIS (code 1006) · 피드 4개, 키 8개 · "
        "길이 2.0초–4.0초 · H0IOCNT0: B01611892 / H0STCNT0: 000660 005930 133690 / "
        "H0UNASP0 H0UNCNT0: 000660 005930"
    ) in details
    # The watchdog's gaps start at the last frame; the option key never came back before the stop.
    assert (
        "    15:29:33 disconnected: silent 60s · 피드 4개, 키 8개 · 길이 1분 3초, 끝 없음(수집기가 멈춤) 1"
    ) in details
    assert "    09:00:00 stale · 피드 1개, 키 1개 · 길이 1시간 2분 · H0IOCNT0: B01611892" in details
    lines = details.splitlines()
    assert lines.index("  2026-10-21") < next(i for i, x in enumerate(lines) if "09:00:00 stale" in x), (
        "by start"
    )
    assert "    03:00:00 disconnected: websocket closed by KIS (code 1000) · 피드 1개, 키 1개" in details


def test_the_closing_window_shows_frames_silence_trades_and_bars(recorded: Path) -> None:
    output = report(recorded, CALENDAR, STOCKS, ETFS, NIGHT)
    window = output.split("2. 종가 단일가 구간")[1].split("3. 결측 구간")[0]

    assert "  2026-10-21 15:20:00–15:31:00" in window
    assert (
        "    WebSocket 프레임(피드별 마지막 받은 시각 · 수): H0UNASP0 15:30:40 · 13 / _system 15:30:35 · 1"
    ) in window
    assert "    이 구간에 프레임이 없는 피드(그날 녹화는 있다): H0MFCNT0 H0UNCNT0" in window
    assert "): 62.0초(15:29:33–15:30:35)" in window
    assert "    겹친 결측: 15:29:33 disconnected: silent 60s · 피드 4개, 키 8개" in window
    assert (
        "    마감(15:30:00) 뒤 1분 안의 첫 체결 KRX H0STCNT0: "
        "005930 15:30:00(받음 15:30:00, 장운영 '0' 시간 '0') · 000660 없음(마감 뒤 첫 체결 15:40:00) · "
        "133690 녹화 없음"
    ) in window
    assert (
        "    마감(15:30:00) 뒤 1분 안의 첫 체결 통합 H0UNCNT0: "
        "005930 15:30:01(받음 15:30:41, 장운영 '0' 시간 '0') · 000660 없음(마감 뒤 체결 없음)"
    ) in window
    assert (
        "    1분봉 채움 bars FHKST03010230: 005930 있음(이 구간 봉 11개) · 000660 없음 · 133690 없음"
        in window
    )


def books(*times: str) -> list[tuple[str, str, str, str]]:
    return [(BOOK.tr_id, "005930", t, book_frame({"MKSC_SHRN_ISCD": "005930"})) for t in times]


# Frames every half minute from before the window to 15:29:33, then none.
TO_15_29_33 = (*(f"15:{m}:{s:02d}" for m in range(19, 30) for s in (0, 30)), "15:29:33")


def silence_of(output: str) -> str:
    return output.split("가장 긴 침묵 ")[1].split(", 겹친 끊김")[0]


def test_a_silence_running_past_the_window_shows_that_no_frame_came(tmp_path: Path) -> None:
    data = tmp_path / "data"
    record_raw(data, books(*TO_15_29_33))

    output = report(data, CALENDAR, STOCKS, (), NIGHT)

    assert silence_of(output) == "15:29:33부터 15:40:00까지 프레임 없음(10분 27초 넘게)"
    assert "  2026-10-21 거래일: 결측 없음" in output
    assert "    겹친 결측: 없음" in output
    assert "005930 녹화 없음" in output


def test_a_silence_that_began_before_the_window_counts_from_the_day_s_last_frame(tmp_path: Path) -> None:
    # Codex review on #345: frames from 15:10 to 15:30 none, then every second; read from 15:20 only,
    # the window would look healthy.
    data = tmp_path / "data"
    after = [f"15:30:{s:02d}" for s in range(60)] + [f"15:31:{s:02d}" for s in range(0, 60, 5)]
    record_raw(data, books("09:00:00", "15:10:00", *after))

    assert silence_of(report(data, CALENDAR, STOCKS, (), NIGHT)) == "1200.0초(15:10:00–15:30:00)"


def test_a_day_whose_first_frame_is_after_the_window_says_none_came_before(tmp_path: Path) -> None:
    data = tmp_path / "data"
    record_raw(data, books("15:35:00", "15:35:01"))

    output = report(data, CALENDAR, STOCKS, (), NIGHT)

    assert silence_of(output) == "15:35:00 전에는 그날 프레임 없음"
    assert "    WebSocket 프레임(피드별 마지막 받은 시각 · 수): 없음" in output


def test_a_run_before_the_read_s_end_measures_to_now(tmp_path: Path) -> None:
    # Codex review on #345: at 15:32 the frames to 15:40 are not in yet.
    data = tmp_path / "data"
    record_raw(data, books(*TO_15_29_33))

    output = report(data, CALENDAR, STOCKS, (), at("15:32:00"))

    assert silence_of(output) == "15:29:33부터 15:32:00까지 프레임 없음(2분 27초 넘게)"


def test_a_window_not_over_yet_is_said_so(recorded: Path) -> None:
    output = report(recorded, CALENDAR, STOCKS, ETFS, at("15:00:00"))

    assert "종가 단일가 15:20–15:31: 아직 지나지 않음" in output
    assert output.split("2. 종가 단일가 구간")[1].split("3. 결측 구간")[0].strip().endswith("없음")


def test_a_file_unlike_the_schema_is_counted_not_read(recorded: Path) -> None:
    partition = recorded / "gaps" / f"date={DAY}" / "feed=H0UNCNT0" / "symbol=005930"
    names = [n for n in GAP_SCHEMA.names if n != "reason"]
    table = pa.table({n: pa.array([None], GAP_SCHEMA.field(n).type) for n in names})
    pq.write_table(table, partition / "part-235900-odd.parquet")
    raw = recorded / "raw" / f"date={DAY}" / "feed=H0UNASP0" / "symbol=000660"
    pq.write_table(pa.table({"received_at": ["15:29:59"], "message": ["x"]}), raw / "part-235900-odd.parquet")

    output = report(recorded, CALENDAR, STOCKS, ETFS, NIGHT)

    assert "스키마와 다른 파일(열이 없거나 타입이 다르다)" in output
    assert "  gaps H0UNCNT0 reason 없음(읽지 않음): 파일 1개" in output
    assert "  raw H0UNASP0 received_at 타입 string(읽지 않음): 파일 1개" in output
    assert "결측 구간 17개" in output, "the rows of the files read"


def test_without_a_recording_the_probe_says_so(tmp_path: Path) -> None:
    assert report(tmp_path, CALENDAR, STOCKS, ETFS, NIGHT) == (
        "WebSocket 결측과 종가 단일가 녹화 (#344): raw에 WebSocket 프레임이 있거나 gaps가 있는 날이 없다"
    )


def test_the_command_prints_the_report(
    recorded: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(recorded)  # no .env
    monkeypatch.setenv("KRX_DATA_DIR", str(recorded))
    monkeypatch.setenv("KRX_STATE_DIR", str(recorded / "state"))
    monkeypatch.setenv("KRX_SYMBOLS", ",".join(STOCKS))

    assert main(["probe-gaps"]) == 0

    out = capsys.readouterr().out
    assert out.startswith("WebSocket 결측과 종가 단일가 녹화 (#344): 2026-10-18 – ")
    assert "11:30:39 disconnected: websocket closed by KIS (code 1006)" in out
