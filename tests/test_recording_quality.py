"""``krx-quant recording-quality`` (#570): a trading day's recording quality, counted from files the
collector's own writers made (its feed health, recorder and feed normalizers), and the nightly job
that posts it on #571, labelling the issue only past a threshold.

Input: a day's gaps, trades, KIS bars, raw frames, the night futures' frames and the engine's
``/v1/status`` polls. Output: the report's lines and its ``에이전트 차례:`` reasons, with no price
or volume in it.
"""

from __future__ import annotations

import asyncio
import json
import random
import tracemalloc
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from krx_quant import recording_quality as quality
from krx_quant.archive import RAW_KEEP_DAYS, RawMissing, archive
from krx_quant.backfill import BAR_SCHEMA, stock_bars_path
from krx_quant.cli import main
from krx_quant.collector import silence
from krx_quant.doctor import Status
from krx_quant.feeds import (
    BOOK,
    KRX_TRADE,
    MEMBER,
    NIGHT_FUTURES,
    NIGHT_FUTURES_BOOK,
    RAW_SCHEMA,
    TRADE,
    WS_FEEDS,
    Feed,
)
from krx_quant.feeds.base import KST
from krx_quant.footprint import PEAK_START, PEAK_STEP, Meter
from krx_quant.health import FeedHealth, Gap, Unread, read_batches
from krx_quant.market_calendar import MarketCalendar
from krx_quant.object_store import LocalObjectStore
from krx_quant.process_log import Ran
from krx_quant.raw_days import RawDays
from krx_quant.recorder import ParquetRecorder
from krx_quant.recording_quality import Polls, polls_of, report, report_day
from krx_quant.reference import BarBuilder
from krx_quant.scheduled import JOBS, RECORDING_QUALITY, Facts, JobRecord, Night, recording_quality_calls
from krx_quant.trader.report import AGENT
from tests.kis_frames import BOOK_COLUMNS, MEMBER_COLUMNS, TRADE_COLUMNS, book_frame, frame, trade_frame

DAY = date(2026, 11, 5)  # a Thursday
LAST = date(2026, 11, 4)  # the evening whose night ended that morning
BEFORE = (date(2026, 11, 2), date(2026, 11, 3))  # the nights subscribed for before it
SATURDAY = date(2026, 11, 7)
CALENDAR = MarketCalendar(
    {
        **{date(2026, 10, d): True for d in (28, 29, 30)},
        date(2026, 10, 31): False,
        date(2026, 11, 1): False,
        **{date(2026, 11, d): True for d in (2, 3, 4, 5, 6)},
        SATURDAY: False,
    }
)
STOCK, ETF, UNFILLED = "005930", "133690", "000660"
# Made-up prices, volumes and cumulative volumes, each distinct, none of which may be printed.
PRICES = (81_537, 81_629)
VOLUMES = (1_237, 2_474, 3_711, 4_948, 6_185, 7_422)
NO_VALUES = ("81537", "81,537", "81629", "81,629", "1237", "1,237", "2474", "3711", "4948", "6185", "7422")


def at(hhmmss: str, day: date = DAY) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmmss), KST)


def recorder(data: Path) -> ParquetRecorder:
    return ParquetRecorder(data, flush_rows=10_000, flush_seconds=3600)


class Clock:
    def __init__(self) -> None:
        self.now = at("07:00:00")

    def __call__(self) -> datetime:
        return self.now


KEYS = [(TRADE.tr_id, STOCK), (BOOK.tr_id, STOCK), (KRX_TRADE.tr_id, ETF)]


def record_gaps(data: Path) -> None:
    """The day's disconnects as the collector's feed health writes them: one before the open (07:24),
    then four in the session, the watchdog's 60-second silence among them (14:02), as on 2026-11-05."""
    writer = recorder(data)
    clock = Clock()
    health = FeedHealth(writer, clock, CALENDAR)
    for key in KEYS:
        health.watch(key)

    def drop(start: str, back: str, *, since: str | None = None) -> None:
        clock.now = at(start)
        if since is None:
            health.connection_lost(KEYS, "websocket closed by KIS (code 1006)")
        else:
            health.connection_lost(KEYS, silence(60), since=at(since))
        clock.now = at(back)
        for key in KEYS:
            health.subscribed(key)

    drop("07:24:01", "07:24:03")
    drop("09:06:38", "09:06:41")
    drop("14:03:09", "14:03:11", since="14:02:09")
    drop("14:58:52", "14:58:54")
    drop("14:59:41", "14:59:43")
    health.close()
    writer.close()


def trade(
    feed: Feed, symbol: str, hhmmss: str, received: str, price: int, qty: int, cum: int
) -> dict[str, Any]:
    fields = {c: "0" for c in TRADE_COLUMNS} | {
        "MKSC_SHRN_ISCD": symbol,
        "STCK_CNTG_HOUR": hhmmss.replace(":", ""),
        "STCK_PRPR": str(price),
        "CNTG_VOL": str(qty),
        "ACML_VOL": str(cum),
        "BSOP_DATE": DAY.strftime("%Y%m%d"),
    }
    return feed.normalize(fields, at(received).astimezone(UTC))


def record_trades(data: Path, feed: Feed, rows: Iterable[dict[str, Any]]) -> None:
    writer = recorder(data)
    for row in rows:
        writer.record("events", DAY, feed.tr_id, row["symbol"], row, feed.schema)
    writer.close()


def write_bars(data: Path, symbol: str, venue: str, bars: Mapping[str, tuple[int, int]]) -> None:
    """KIS's stored bars of ``symbol``: minute -> (close, volume)."""
    rows = [
        {"symbol": symbol, "bar_time": at(minute), "venue": venue, "open": close, "high": close}
        | {"low": close, "close": close, "volume": volume, "cum_value": 0}
        for minute, (close, volume) in bars.items()
    ]
    path = stock_bars_path(data, DAY, symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=BAR_SCHEMA), path)


def record_the_bars_day(data: Path) -> None:
    """005930's minutes, each one way a recording can differ from KIS's bars; 133690's all alike."""
    low, high = PRICES
    v1, v2, v3, v4, v5, v6 = VOLUMES
    record_trades(
        data,
        TRADE,
        [
            trade(TRADE, STOCK, "10:00:05", "10:00:05", high, v1, 50_001),
            trade(TRADE, STOCK, "10:00:40", "10:00:40", low, v2, 50_002),  # 10:00 alike
            trade(TRADE, STOCK, "10:02:10", "10:02:10", high, v3, 50_003),  # under KIS's volume
            trade(TRADE, STOCK, "10:03:10", "10:03:10", low, v4, 50_004),
            trade(TRADE, STOCK, "10:03:10", "10:03:11", low, v4, 50_004),  # the same trade again
            trade(TRADE, STOCK, "10:04:20", "10:04:20", high, v5, 50_005),  # its close differs
            trade(TRADE, STOCK, "10:05:30", "10:05:30", low, v6, 50_006),  # KIS has no bar
            trade(TRADE, STOCK, "15:20:00", "15:20:01", low, v1, 50_007),  # the closing auction: not read
        ],
    )
    write_bars(
        data,
        STOCK,
        "UN",
        {
            "08:30:00": (low, v1),  # NXT's pre-market: not read
            "09:06:00": (low, v1),  # missing, in the 09:06:38 disconnect's gap
            "10:00:00": (low, v1 + v2),
            "10:01:00": (high, v1),  # missing, outside every gap
            "10:02:00": (high, v3 + v1),
            "10:03:00": (low, v4),
            "10:04:00": (low, v5),
            "10:06:00": (low, 0),  # a bar without a trade, as the recording has none
        },
    )
    record_trades(data, KRX_TRADE, [trade(KRX_TRADE, ETF, "11:00:01", "11:00:01", low, v1, 9_001)])
    write_bars(data, ETF, "KRX", {"11:00:00": (low, v1)})


def record_raw(data: Path, rows: Iterable[tuple[date, str, str, str, str]]) -> None:
    writer = recorder(data)
    for day, feed, key, hhmmss, message in rows:
        row = {"received_at": at(hhmmss, day).astimezone(UTC), "message": message}
        writer.record("raw", day, feed, key, row, RAW_SCHEMA)
    writer.close()


def member(columns: int) -> str:
    return frame(MEMBER.tr_id, MEMBER_COLUMNS[:columns], {"MKSC_SHRN_ISCD": STOCK})


def record_the_frames_day(data: Path, member_short: int = 3) -> None:
    book = book_frame({"MKSC_SHRN_ISCD": STOCK})
    short_trade = frame(TRADE.tr_id, TRADE_COLUMNS[:40], {"MKSC_SHRN_ISCD": STOCK})
    long_book = frame(BOOK.tr_id, [*BOOK_COLUMNS, "MKSC_SHRN_ISCD"], {"MKSC_SHRN_ISCD": STOCK})
    record_raw(
        data,
        [
            *((DAY, BOOK.tr_id, STOCK, f"10:00:0{s}", book) for s in range(5)),
            (DAY, TRADE.tr_id, STOCK, "10:00:01", trade_frame({"MKSC_SHRN_ISCD": STOCK})),
            *((DAY, MEMBER.tr_id, STOCK, f"08:00:0{s}", member(70)) for s in range(member_short)),
            (DAY, MEMBER.tr_id, STOCK, "08:01:00", member(len(MEMBER_COLUMNS))),
            (DAY, TRADE.tr_id, STOCK, "11:11:11", short_trade),
            (DAY, BOOK.tr_id, STOCK, "12:00:00", long_book),
            (DAY, "_unparsed", "_", "13:00:00", "not a frame"),
            (DAY, "_system", "_", "13:00:01", '{"header": {"tr_id": "PINGPONG"}}'),  # not counted
        ],
    )


def ack(day: date, tr_id: str, key: str) -> tuple[date, str, str, str, str]:
    body = {"rt_cd": "0", "msg_cd": "OPSP0000", "msg1": "SUBSCRIBE SUCCESS"}
    message = json.dumps({"header": {"tr_id": tr_id, "tr_key": key}, "body": body})
    return (day, "_system", key, "07:20:00", message)


def night_frame(feed: str) -> str:
    """A night feed's one-record frame in its layout (its fields' values do not matter here)."""
    width = len(WS_FEEDS[feed].columns)
    return f"0|{feed}|001|" + "^".join(["A01612", *["0"] * (width - 1)])


def night_frames(evening: date, feed: str, records: int) -> list[tuple[date, str, str, str, str]]:
    """``records`` one-record frames of ``feed`` in the night after ``evening``, half of them after
    midnight (the next day's partition)."""
    rows = []
    for i in range(records):
        day, hhmmss = (
            (evening, f"22:00:{i:02d}") if i % 2 == 0 else (evening + timedelta(days=1), f"01:00:{i:02d}")
        )
        rows.append((day, feed, "A01612", hhmmss, night_frame(feed)))
    return rows


def record_the_nights(data: Path) -> None:
    """Two earlier nights of 10 and 12 trade records and 40 book records, then the night after
    11-04: two trade records (one of them normalized) and no book."""
    rows = [ack(d, NIGHT_FUTURES.tr_id, "A01612") for d in (*BEFORE, LAST)]
    for evening, trades in zip(BEFORE, (10, 12), strict=True):
        rows += night_frames(evening, NIGHT_FUTURES.tr_id, trades)
        rows += night_frames(evening, NIGHT_FUTURES_BOOK.tr_id, 40)
    rows += night_frames(LAST, NIGHT_FUTURES.tr_id, 2)
    rows += [
        (DAY, NIGHT_FUTURES.tr_id, "A01612", "19:00:00", night_frame(NIGHT_FUTURES.tr_id))
    ]  # not yet over
    record_raw(data, rows)
    writer = recorder(data)
    row = {"symbol": "A01612", "event_time": at("22:00:00", LAST), "received_at": at("22:00:00", LAST)}
    writer.record("events", LAST, NIGHT_FUTURES.tr_id, "A01612", row, NIGHT_FUTURES.schema)
    writer.close()


POLLS = Polls(
    (
        {"feed": "FHPST01390000", "key": STOCK, "attempts": 4_000, "failures": 11}
        | {"errors": {"ServerDisconnectedError": 11}, "max_consecutive_failures": 3},
        {"feed": "FHPST01390000", "key": ETF, "attempts": 4_000, "failures": 0}
        | {"errors": {}, "max_consecutive_failures": 0},
        {"feed": "HHPTJ04160200", "key": STOCK, "attempts": 78, "failures": 0}
        | {"errors": {}, "max_consecutive_failures": 0},
    ),
    started_at=at("07:20:01"),
)


@pytest.fixture
def recorded(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    record_gaps(data)
    record_the_bars_day(data)
    record_the_frames_day(data)
    record_the_nights(data)
    return data


def section(text: str, number: int) -> str:
    return text.split(f"\n{number}. ")[1].split(f"\n{number + 1}. ")[0]


def reasons(text: str) -> list[str]:
    line = next(line for line in text.splitlines() if line.startswith(AGENT))
    return line.removeprefix(AGENT).split("; ")


def test_the_day_s_disconnects_in_the_session_are_counted_by_length_and_feed(recorded: Path) -> None:
    text = report(recorded, CALENDAR, DAY, [STOCK], POLLS)

    gaps = section(text, 1)
    assert "장중 끊김 4번(5초 미만 3, 5–60초 0, 60초 이상 1) · 장 밖 끊김 1번" in gaps
    assert "14:02:09 disconnected: silent 60s · 피드 3개, 키 3개 · 길이 1분 2초" in gaps
    assert gaps.count("disconnected: websocket closed by KIS (code 1006)") == 3  # 07:24 is not shown
    # Each feed's one key was cut 3 + 62 + 2 + 2 seconds in the session.
    assert "H0UNASP0: 끊김 4번 69.0초 · stale 0개 0.0초" in gaps
    assert "H0STCNT0: 끊김 4번 69.0초" in gaps
    assert "장중 끊김 4번(기준 3번 이상)" in reasons(text)
    assert "60초 이상이거나 끝 없는 장중 끊김 1번(14:02:09)" in reasons(text)


def test_two_short_disconnects_in_the_session_call_no_agent(tmp_path: Path) -> None:
    writer = recorder(tmp_path)
    clock = Clock()
    health = FeedHealth(writer, clock, CALENDAR)
    for start, back in (("09:06:38", "09:06:41"), ("14:58:52", "14:58:54")):
        clock.now = at(start)
        health.connection_lost(KEYS, "websocket closed by KIS (code 1006)")
        clock.now = at(back)
        for key in KEYS:
            health.subscribed(key)
    writer.close()

    gaps = section(report(tmp_path, CALENDAR, DAY, [], POLLS), 1)

    assert "장중 끊김 2번(5초 미만 2, 5–60초 0, 60초 이상 0)" in gaps
    assert not [r for r in reasons(report(tmp_path, CALENDAR, DAY, [], POLLS)) if "끊김" in r]


def test_a_disconnect_one_subscription_never_came_back_from_is_unfinished(tmp_path: Path) -> None:
    """Two keys taken back at once and one still out when the collector stopped (``FeedHealth.close``
    writes its gap without an end): the disconnect's length is unknown, not the two's 2 seconds."""
    writer = recorder(tmp_path)
    clock = Clock()
    health = FeedHealth(writer, clock, CALENDAR)
    clock.now = at("10:15:00")
    health.connection_lost(KEYS, "websocket closed by KIS (code 1006)")
    clock.now = at("10:15:02")
    for key in KEYS[:-1]:
        health.subscribed(key)
    clock.now = at("10:20:00")
    health.close()
    writer.close()

    text = report(tmp_path, CALENDAR, DAY, [], POLLS)

    gaps = section(text, 1)
    assert "장중 끊김 1번(5초 미만 0, 5–60초 0, 60초 이상 0, 끝 없음 1)" in gaps
    assert "H0STCNT0: 끊김 1번 0.0초 · stale 0개 0.0초 · 끝 없음(수집기가 멈춤, 길이 모름) 1개" in gaps
    assert "60초 이상이거나 끝 없는 장중 끊김 1번(10:15:00)" in reasons(text)


def test_the_recorded_bars_are_set_against_kis_s_minute_by_minute(recorded: Path) -> None:
    text = report(recorded, CALENDAR, DAY, [STOCK, ETF, UNFILLED], POLLS)

    bars = section(text, 2)
    stock = next(line for line in bars.splitlines() if line.strip().startswith(STOCK))
    v1, v2, v3, v4, v5, v6 = VOLUMES
    # Continuous trading's minutes alone: not the 08:30 bar nor the 15:20 auction's trade.
    recorded_volume = v1 + v2 + v3 + 2 * v4 + v5 + v6
    kis_volume = v1 + (v1 + v2) + v1 + (v3 + v1) + v4 + v5
    assert stock.strip() == (
        "005930 통합(H0UNCNT0 ↔ FHKST03010230 UN): 녹화 행 8 · 둘 다 거래한 분 4"
        " · 빠진 분 2(결측 구간 밖 1) · KIS에 없는 분 1(결측 구간 밖 1) · 거래량 모자람 1(결측 구간 밖 1)"
        " · 거래량 넘침 1(결측 구간 밖 1) · 종가 다름 1(결측 구간 밖 1) · 두 번 기록됐을 수 있는 행 1"
        f" · 녹화 거래량 ÷ KIS {recorded_volume / kis_volume:.2%}"
    )
    assert (
        "133690 KRX(H0STCNT0 ↔ FHKST03010230 KRX): 녹화 행 1 · 둘 다 거래한 분 1 · 빠진 분 0(결측 구간 밖 0)"
        in bars
    )
    assert "000660: KIS 1분봉 없음(그날 파일이 아직 없다)" in bars
    found = reasons(text)
    assert "005930 결측 구간 밖의 빠진 분 1개(기준 1개 이상)" in found
    assert "005930 결측 구간 밖에서 거래량이나 종가가 다른 분 3/4(75.0%, 기준 1% 넘게)" in found
    assert "005930 두 번 기록됐을 수 있는 행 1개" in found
    assert "KIS 1분봉 없음: 000660" in found
    assert not [r for r in found if r.startswith(ETF)]


def test_the_differing_minutes_are_counted_by_cause(tmp_path: Path) -> None:
    """#631: each differing minute one way a recording can differ from KIS's bars, the cumulative
    volume a chain from one trade to the next but where a trade never came."""
    low, high = PRICES

    def at_(hhmmss: str, qty: int, cum: int, price: int = low, received: str | None = None) -> dict[str, Any]:
        return trade(TRADE, STOCK, hhmmss, received or hhmmss, price, qty, cum)

    odd = at_("11:20:10", 8, 103) | {"hour_cls": "C"}  # a trade of another hour code
    record_trades(
        tmp_path,
        TRADE,
        [
            at_("11:00:30", 10, 10),  # alike
            at_("11:01:10", 20, 30),  # KIS puts the next trade in 11:01, the recording in 11:02
            at_("11:02:00", 10, 40),
            at_("11:02:30", 5, 45),
            at_("11:10:05", 40, 85),  # the first minute after a halt: KIS counts more
            at_("11:11:05", 7, 92),
            at_("11:19:05", 3, 95),
            odd,
            at_("11:29:05", 2, 105),
            at_("11:30:10", 5, 130),  # a trade of 20 before it never came: the cumulative volume says so
            at_("11:39:05", 1, 131),
            at_("11:40:59", 2, 133, high, "11:40:59"),
            at_("11:40:59", 2, 135, low, "11:41:00"),  # received last in the last second: its close
            at_("11:49:05", 1, 136),
            at_("11:50:05", 6, 142),  # short, and nothing says why
        ],
    )
    write_bars(
        tmp_path,
        STOCK,
        "UN",
        {
            "11:00:00": (low, 10),
            "11:01:00": (low, 30),
            "11:02:00": (low, 5),
            "11:10:00": (low, 50),
            "11:11:00": (low, 7),
            "11:19:00": (low, 3),
            "11:20:00": (low, 9),
            "11:29:00": (low, 2),
            "11:30:00": (low, 25),
            "11:39:00": (low, 1),
            "11:40:00": (high, 4),
            "11:49:00": (low, 1),
            "11:50:00": (low, 9),
        },
    )

    bars = section(report(tmp_path, CALENDAR, DAY, [STOCK], POLLS), 2)

    assert (
        "다른 분 7개의 원인 후보(한 분이 여럿에 들 수 있다): 분 경계(옆 분과 맞바뀜) 2"
        " · 단일가(개장 분, 멈춘 뒤 연 분) 1 · 다른 시간·장운영 구분의 체결 1 · 누적 거래량에만 있는 체결 1"
        " · 누적 거래량보다 더 센 체결 0 · 같은 초의 순서(종가만) 1 · 어느 것도 아님 1"
        " · 누적 거래량 사슬(누적 거래량 순으로 늘어놓은 체결 15행): 체결량보다 더 뛴 곳 1 · 덜 늘어난 곳 0"
    ) in bars


def test_a_minute_missing_inside_a_gap_calls_no_agent(tmp_path: Path) -> None:
    record_gaps(tmp_path)
    write_bars(tmp_path, STOCK, "UN", {"09:06:00": (PRICES[0], VOLUMES[0])})

    text = report(tmp_path, CALENDAR, DAY, [STOCK], POLLS)

    assert "빠진 분 1(결측 구간 밖 0)" in section(text, 2)
    assert not [r for r in reasons(text) if r.startswith(STOCK)]


def test_frames_short_long_or_unparsed_are_counted_by_feed(recorded: Path) -> None:
    text = report(recorded, CALENDAR, DAY, [], POLLS)

    frames = section(text, 3)
    # The book's six, the trades' two, the member feed's four and the night trades' two received that day.
    assert "읽은 데이터 프레임 14개(피드 4개)" in frames
    assert "해석하지 못한 프레임(_unparsed) 1개(첫 13:00:00)" in frames
    assert "H0UNCNT0 1개(키 1개, 첫 11:11:11) · H0UNMBC0 3개(키 1개, 첫 08:00:00)" in frames
    assert "필드가 남는 프레임: H0UNASP0 1개" in frames
    found = reasons(text)
    assert "해석하지 못한 프레임 1개" in found
    assert "H0UNCNT0 필드가 모자란 프레임 1개(기준 0개 넘게)" in found
    assert "H0UNMBC0 필드가 모자란 프레임 3개(기준 2개 넘게)" in found
    assert "필드가 남는 프레임 H0UNASP0 1개" in found


def test_two_short_member_frames_a_day_call_no_agent(tmp_path: Path) -> None:
    record_the_frames_day(tmp_path, member_short=2)

    found = reasons(report(tmp_path, CALENDAR, DAY, [], POLLS))

    assert not [r for r in found if r.startswith(MEMBER.tr_id)]
    assert "H0UNCNT0 필드가 모자란 프레임 1개(기준 0개 넘게)" in found  # another feed's still does


def test_the_rest_failures_are_the_engine_s_of_the_day(recorded: Path) -> None:
    text = report(recorded, CALENDAR, DAY, [], POLLS)

    polls = section(text, 4)
    assert "엔진이 07:20:01에 시작한 뒤의 수" in polls
    assert (
        "FHPST01390000(키 2개): 호출 8,000 · 실패 11(ServerDisconnectedError 11) · 실패한 키 1개"
        " · 가장 길게 잇단 실패 3번"
    ) in polls
    assert "HHPTJ04160200(키 1개): 호출 78 · 실패 0" in polls
    assert "REST 실패 11번(기준 10번 이상)" in reasons(text)
    assert "REST 조회가 잇달아 실패(FHPST01390000 3번, 기준 3번)" in reasons(text)


def status(now: datetime, polls: list[Any] | None = None) -> Status:
    body = {"mode": "live", "now": now.isoformat(), "started_at": at("07:20:01").isoformat()}
    return Status(body | {"kis": {"polls": polls or []}}, "test")


def test_the_polls_are_known_only_while_the_engine_s_day_is_the_report_s() -> None:
    assert polls_of(status(at("22:31:00")), DAY).unknown is None
    after_midnight = polls_of(status(at("00:40:00", DAY + timedelta(days=1))), DAY)
    assert (
        after_midnight.unknown is not None and "그날의 수는 자정에 0부터 다시 센다" in after_midnight.unknown
    )
    down = polls_of(Status(None, "", ("이 컨테이너의 엔진 API: ClientConnectorError",)), DAY)
    assert down.unknown == "엔진 API가 답하지 않았다(이 컨테이너의 엔진 API: ClientConnectorError)"
    assert polls_of(Status({"mode": "demo", "now": at("22:31:00").isoformat()}, "test"), DAY).unknown


def test_unknown_polls_are_said_and_call_no_agent(tmp_path: Path) -> None:
    text = report(tmp_path, CALENDAR, DAY, [], Polls(unknown="엔진 API가 답하지 않았다()"))

    assert "모른다: 엔진 API가 답하지 않았다()" in section(text, 4)
    assert not [r for r in reasons(text) if "REST" in r]


def test_the_night_s_records_are_set_against_the_nights_before(recorded: Path) -> None:
    text = report(recorded, CALENDAR, DAY, [], POLLS)

    night = text.split("\n5. ")[1]
    assert night.startswith("야간 기록량 (2026-11-04(수) 18:00 – 11-05 06:00, 받은 시각)")
    assert "구독: H0MFCNT0 A01612를 KIS가 18:00 전에 받았다" in night
    assert (
        "H0MFCNT0 체결: 프레임 2 · 레코드 2 · events 1(정규화하지 못한 레코드 1) · 앞선 밤 2개 중앙값의 18%"
        in night
    )
    assert (
        "H0MFASP0 호가: 프레임 0 · 레코드 0 · events 0(정규화하지 못한 레코드 0) · 앞선 밤 2개 중앙값의 0%"
        in night
    )
    assert "2026-11-02 체결 10 · 호가 40 / 2026-11-03 체결 12 · 호가 40" in night
    found = reasons(text)
    assert "야간 체결(H0MFCNT0) 레코드가 앞선 밤 중앙값의 18%(기준 25% 미만)" in found
    assert "야간 호가(H0MFASP0) 기록 없음(2026-11-04 밤. KRX가 쉰 밤(긴 연휴 전날)이면 그대로 둔다)" in found
    assert "야간 체결(H0MFCNT0) 정규화하지 못한 레코드 1개" in found


def test_a_night_like_the_ones_before_calls_no_agent(tmp_path: Path) -> None:
    rows = [ack(d, NIGHT_FUTURES.tr_id, "A01612") for d in (*BEFORE, LAST)]
    for evening in (*BEFORE, LAST):
        rows += night_frames(evening, NIGHT_FUTURES.tr_id, 10)
        rows += night_frames(evening, NIGHT_FUTURES_BOOK.tr_id, 30)
    record_raw(tmp_path, rows)

    text = report(tmp_path, CALENDAR, DAY, [], POLLS)

    assert "앞선 밤 2개 중앙값의 100%" in text
    assert not [r for r in reasons(text) if r.startswith("야간") and "레코드가" in r]


def test_the_report_prints_no_price_or_volume(recorded: Path) -> None:
    text = report(recorded, CALENDAR, DAY, [STOCK, ETF, UNFILLED], POLLS)

    for value in NO_VALUES:
        assert value not in text, value


def test_a_clean_day_s_report_calls_no_agent(tmp_path: Path) -> None:
    low = PRICES[0]
    record_trades(tmp_path, TRADE, [trade(TRADE, STOCK, "10:00:05", "10:00:05", low, VOLUMES[0], 7)])
    write_bars(tmp_path, STOCK, "UN", {"10:00:00": (low, VOLUMES[0])})
    rows = [ack(d, NIGHT_FUTURES.tr_id, "A01612") for d in (*BEFORE, LAST)]
    for evening in (*BEFORE, LAST):
        rows += night_frames(evening, NIGHT_FUTURES.tr_id, 4) + night_frames(
            evening, NIGHT_FUTURES_BOOK.tr_id, 4
        )
    record_raw(tmp_path, rows)
    writer = recorder(tmp_path)
    for i in range(4):
        received = at("22:00:00", LAST) + timedelta(seconds=i)
        for feed in (NIGHT_FUTURES, NIGHT_FUTURES_BOOK):
            row = {"symbol": "A01612", "event_time": received, "received_at": received}
            writer.record("events", LAST, feed.tr_id, "A01612", row, feed.schema)
    writer.close()

    text = report(tmp_path, CALENDAR, DAY, [STOCK], Polls(started_at=at("07:20:01")))

    assert reasons(text) == ["아님(기준 안: 기록만, 라벨을 바꾸지 않는다)"]
    assert not recording_quality_calls(Ran(0, text, ""))


def test_a_day_that_does_not_trade_is_said_so(tmp_path: Path) -> None:
    assert report(tmp_path, CALENDAR, SATURDAY, [], POLLS) == "기록 품질 (#570): 2026-11-07는 거래일이 아니다"


@pytest.mark.parametrize(
    ("now", "day"),
    [
        (at("22:31:00"), DAY),  # the job, after the nightly training
        (at("01:30:00", DAY + timedelta(days=1)), DAY),  # still that night
        (at("14:00:00"), LAST),  # in the session: the day before, whose bars are final
        (at("03:00:00", SATURDAY), date(2026, 11, 6)),  # Friday's night
        (at("12:00:00", SATURDAY), date(2026, 11, 6)),
    ],
)
def test_a_run_reports_the_night_s_trading_day_once_its_bars_are_final(now: datetime, day: date) -> None:
    assert report_day(CALENDAR, now) == day


def test_a_peak_over_the_memory_bound_calls_an_agent(recorded: Path) -> None:
    found = quality.measure(recorded, CALENDAR, DAY, [], Polls(started_at=at("07:20:01")))
    assert found is not None

    quality.ran(found, 12.3, quality.MEMORY_MB + 1)

    assert "12.3초 · 최대 RSS 501 MB(기준 500 MB)" in quality.render(found)
    assert "최대 메모리 501 MB(기준 500 MB 넘게)" in found.reasons


class Peaks(Meter):
    """A meter whose peak is ``low`` until it has been reset ``over_at`` times, then ``high``: the
    sections' peaks, one section going over (``footprint.Meter``'s face)."""

    def __init__(self, low: float, high: float, over_at: int, *, resets: bool = True) -> None:
        super().__init__()
        self.low, self.high, self.over_at, self.resets, self.reset_count = low, high, over_at, resets, 0

    @property
    def peak_since(self) -> str:
        return PEAK_STEP if self.resets else PEAK_START

    def reset(self) -> None:
        self.reset_count += 1

    def peak_mb(self) -> float | None:
        return self.high if self.reset_count >= self.over_at else self.low


def test_a_run_over_its_memory_bound_stops_in_the_section_and_says_where(recorded: Path) -> None:
    """#631: the third section's reading goes over the bound: the first two stay, the rest is not read,
    and the reason says where it stopped, once."""
    budget = quality.Budget(500, Peaks(300, 600, over_at=3))
    found = quality.measure(recorded, CALENDAR, DAY, [STOCK], POLLS, budget=budget)
    assert found is not None
    quality.ran(found, 1.0, budget.peak_mb())
    text = quality.render(found)

    assert [heading for heading, _ in found.sections] == [
        "1. 장중 끊김 (gaps, 09:00–15:30)",
        "2. 녹화한 1분봉과 KIS 1분봉 (연속매매 09:00–15:19 봉, 봉 시각은 그 분의 시작)",
        "3. 메모리 기준 넘음",
        "6. 실행",
    ]
    assert "3절을 읽다가 최대 RSS 600 MB(기준 500 MB): 이 절과 그 뒤 절은 읽지 않았다" in section(text, 3)
    found_reasons = reasons(text)
    assert (
        "메모리 기준 넘음: 3절을 읽다가 최대 RSS 600 MB(기준 500 MB 넘게), 그 뒤는 읽지 않았다"
        in found_reasons
    )
    assert not [r for r in found_reasons if r.startswith("최대 메모리")]
    assert "005930 결측 구간 밖의 빠진 분 1개(기준 1개 이상)" in found_reasons  # read before it stopped
    assert "1.0초 · 최대 RSS 600 MB(기준 500 MB)" in text
    assert "절마다 최대 RSS(그 절의 최대): 시작 300 MB · 1절 300 MB · 2절 300 MB · 3절 600 MB" in text


def test_a_section_s_peak_over_the_bound_once_it_is_read_stops_the_next(recorded: Path) -> None:
    """#631: the fourth section reads no batch to check after; its peak over the bound, held to it once
    the section is read and before the fifth's reset forgets it, still stops the run."""
    budget = quality.Budget(500, Peaks(300, 600, over_at=4))
    found = quality.measure(recorded, CALENDAR, DAY, [STOCK], POLLS, budget=budget)
    assert found is not None

    headings = [heading for heading, _ in found.sections]
    assert headings[3:] == ["4. REST 조회 실패 (/v1/status의 kis.polls)", "5. 메모리 기준 넘음"]
    assert found.sections[-1][1] == ["4절을 읽은 뒤 최대 RSS 600 MB(기준 500 MB): 그 뒤 절은 읽지 않았다"]
    assert "메모리 기준 넘음: 4절을 읽은 뒤 최대 RSS 600 MB(기준 500 MB 넘게), 그 뒤는 읽지 않았다" in (
        found.reasons
    )


def test_a_peak_over_the_bound_after_the_last_section_is_said_by_the_run_s_own_section(
    tmp_path: Path,
) -> None:
    record_gaps(tmp_path)  # no night recorded: the fifth section reads no batch
    budget = quality.Budget(500, Peaks(300, 600, over_at=5))
    found = quality.measure(tmp_path, CALENDAR, DAY, [], POLLS, budget=budget)
    assert found is not None
    quality.ran(found, 1.0, budget.peak_mb())

    assert found.stopped is None and found.sections[4][0].startswith("5. 야간 기록량")
    assert "최대 메모리 600 MB(기준 500 MB 넘게)" in found.reasons


def test_a_run_within_its_bound_reads_every_section_and_gives_the_same_report(recorded: Path) -> None:
    unbounded = report(recorded, CALENDAR, DAY, [STOCK, ETF], POLLS)
    budget = quality.Budget(500, Peaks(300, 600, over_at=99, resets=False))
    found = quality.measure(recorded, CALENDAR, DAY, [STOCK, ETF], POLLS, budget=budget)
    assert found is not None

    assert quality.render(found) == unbounded
    quality.ran(found, 1.0, budget.peak_mb())
    assert (
        "절마다 최대 RSS(프로세스가 시작한 뒤의 최대라 절마다 나뉘지 않는다): 시작 300 MB · 1절 300 MB"
        in (quality.render(found))
    )


def old_recorded_bars(data: Path, feed: str, symbol: str) -> tuple[dict[datetime, Any], int, int]:
    """The bars and repeats as the report built them before #631: a Python tuple a row, sorted by
    receipt into the engine's builder, a set of the rows seen."""
    partition = data / "events" / f"date={DAY.isoformat()}" / f"feed={feed}" / f"symbol={symbol}"
    rows: list[tuple[Any, ...]] = []
    for batch in read_batches(
        partition, ("event_time", "received_at", "price", "qty", "cum_volume"), Unread()
    ):
        rows += zip(
            *(batch.column(c).to_pylist() for c in ("received_at", "event_time", "price", "qty")),
            batch.column("cum_volume").to_pylist(),
            strict=True,
        )
    rows.sort(key=lambda r: r[0])
    builder, seen, duplicates = BarBuilder(), set(), 0
    for _, at_, price, qty, cum in rows:
        builder.trade(symbol, at_, price, qty)
        if cum is not None and cum > 0:
            duplicates += (at_, price, qty, cum) in seen
            seen.add((at_, price, qty, cum))
    closed = builder.closed(symbol, datetime.combine(DAY, time(23, 59, 59), KST))
    return {b.start: vars(b) for b in closed}, len(rows), duplicates


def test_the_bars_are_the_ones_built_a_row_at_a_time(tmp_path: Path) -> None:
    """#631: held as columns, the trades give the bars, rows and repeats the Python rows gave: received
    out of exchange order, several in a second, some recorded twice, over several part files."""
    rng = random.Random(631)
    rows = []
    for i in range(3_000):
        second = rng.randrange(60 * 60)
        hhmmss = (datetime.combine(DAY, time(10), KST) + timedelta(seconds=second)).strftime("%H:%M:%S")
        late = datetime.combine(DAY, time(10), KST) + timedelta(seconds=second + rng.choice((0, 0, 1, 70)))
        price, qty = rng.choice(PRICES), rng.randrange(1, 400)
        rows.append(trade(TRADE, STOCK, hhmmss, late.strftime("%H:%M:%S"), price, qty, 10_000 + i))
        if i % 97 == 0:
            rows.append(dict(rows[-1]))  # the same trade again
    writer = ParquetRecorder(tmp_path, flush_rows=700, flush_seconds=3600)
    for row in rows:
        writer.record("events", DAY, TRADE.tr_id, STOCK, row, TRADE.schema)
    writer.close()

    got = quality.recorded_bars(tmp_path, DAY, TRADE.tr_id, STOCK, Unread())

    bars, count, duplicates = old_recorded_bars(tmp_path, TRADE.tr_id, STOCK)
    assert {m: vars(b) for m, b in got.bars.items()} == bars
    assert (got.rows, got.duplicates) == (count, duplicates) and duplicates > 0


def test_a_day_of_trades_is_built_in_little_python_memory(tmp_path: Path) -> None:
    """#631: 300,000 trades of a symbol, traced (tracemalloc: Python's objects and numpy's arrays).
    A Python tuple a row and a set of them held about 100 MB here (400 MB for 2026-11-06's 1.2 million
    of 005930); the columns hold about 44 MB, a slice's rows and a few arrays of a number a row."""
    rows = 300_000
    seconds = [i * 76_000 // rows for i in range(rows)]  # 09:00 to about 15:20
    times = [datetime.combine(DAY, time(9), KST) + timedelta(seconds=s) for s in seconds]
    columns: dict[str, Any] = {f.name: pa.nulls(rows, f.type) for f in TRADE.schema}
    columns |= {
        "symbol": pa.array([STOCK] * rows),
        "event_time": pa.array(times, TRADE.schema.field("event_time").type),
        "received_at": pa.array(times, TRADE.schema.field("received_at").type),
        "price": pa.array([PRICES[i % 2] for i in range(rows)], pa.int64()),
        "qty": pa.array([300 + i % 50 for i in range(rows)], pa.int64()),
        "cum_volume": pa.array(range(1_000, 1_000 + rows), pa.int64()),
        "hour_cls": pa.array(["0"] * rows),
        "session_code": pa.array(["0"] * rows),
    }
    partition = tmp_path / "events" / f"date={DAY.isoformat()}" / f"feed={TRADE.tr_id}" / f"symbol={STOCK}"
    partition.mkdir(parents=True)
    pq.write_table(pa.table(columns, schema=TRADE.schema), partition / "data.parquet", row_group_size=256_000)
    del times, columns

    tracemalloc.start()
    try:
        got = quality.recorded_bars(tmp_path, DAY, TRADE.tr_id, STOCK, Unread())
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert got.rows == rows and got.duplicates == 0
    assert peak < 60 * 2**20, f"{peak / 2**20:.0f} MB"


def test_the_command_reads_the_files_and_says_the_engine_did_not_answer(
    recorded: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(recorded)  # no .env
    monkeypatch.setenv("KRX_DATA_DIR", str(recorded))
    monkeypatch.setenv("KRX_STATE_DIR", str(recorded / "state"))
    monkeypatch.setenv("KRX_SYMBOLS", STOCK)
    monkeypatch.setenv("KRX_ETF_SYMBOLS", ETF)
    monkeypatch.setattr(quality, "MEMORY_MB", float("inf"))  # the test process's peak is not the run's
    # Nothing listens on port 9: the engine's API is out of reach at once.
    monkeypatch.setattr(
        quality, "api_candidates", lambda settings: [("이 컨테이너의 엔진 API", "http://127.0.0.1:9")]
    )

    assert main(["recording-quality", "--day", DAY.isoformat()]) == 0

    out = capsys.readouterr().out
    assert out.startswith("기록 품질 (#570): 2026-11-05(목)\n" + AGENT)
    assert "모른다: 엔진 API가 답하지 않았다(이 컨테이너의 엔진 API: " in out
    assert "005930 통합(H0UNCNT0 ↔ FHKST03010230 UN)" in out and "133690 KRX" in out
    assert "\n6. 실행\n  " in out


async def test_a_day_pruned_on_the_server_is_read_back_from_the_store(recorded: Path, tmp_path: Path) -> None:
    """#607: a month on the archive has deleted the day's raw and the nights' on the server; a run for
    the day (``--day``) reads them back from the store and gives the report it gave before."""
    store = LocalObjectStore(tmp_path / "store")
    await archive(recorded, store)  # merged, as the server's are by then
    before = report(recorded, CALENDAR, DAY, [STOCK], POLLS)
    pruned = await archive(recorded, store, today=DAY + timedelta(days=RAW_KEEP_DAYS + 10))
    assert pruned.pruned and not pruned.raw_left

    with pytest.raises(RawMissing, match="no Object Store is set"):
        report(recorded, CALENDAR, DAY, [STOCK], POLLS)
    with RawDays(recorded, store) as raw:
        assert report(recorded, CALENDAR, DAY, [STOCK], POLLS, raw) == before
        assert raw.restored


def test_the_command_reads_a_pruned_day_from_the_object_store(
    recorded: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = tmp_path / "store"
    asyncio.run(archive(recorded, LocalObjectStore(store)))
    frames = section(report(recorded, CALENDAR, DAY, [STOCK], POLLS), 3)
    asyncio.run(archive(recorded, LocalObjectStore(store), today=DAY + timedelta(days=RAW_KEEP_DAYS + 10)))
    monkeypatch.chdir(recorded)  # no .env
    monkeypatch.setenv("KRX_DATA_DIR", str(recorded))
    monkeypatch.setenv("KRX_STATE_DIR", str(recorded / "state"))
    monkeypatch.setenv("KRX_OBJECT_STORE", str(store))
    monkeypatch.setenv("KRX_SYMBOLS", STOCK)
    monkeypatch.setattr(quality, "MEMORY_MB", float("inf"))  # the test process's peak is not the run's
    monkeypatch.setattr(
        quality, "api_candidates", lambda settings: [("이 컨테이너의 엔진 API", "http://127.0.0.1:9")]
    )

    assert main(["recording-quality", "--day", DAY.isoformat()]) == 0

    assert frames in capsys.readouterr().out


def night(day: date, record: JobRecord | None = None) -> Night:
    return Night(day, CALENDAR.is_trading_day(day), "abc", record, {}, Facts())


def test_the_job_runs_each_trading_night_after_the_trader_s_report() -> None:
    names = [j.name for j in JOBS]
    assert names.index("recording-quality-570") == names.index("trader-report") + 1
    assert RECORDING_QUALITY.command == ("recording-quality",)
    assert [t.issue for t in RECORDING_QUALITY.targets] == [571]
    assert RECORDING_QUALITY.unlabel == ()
    assert RECORDING_QUALITY.due(night(DAY))
    assert not RECORDING_QUALITY.due(night(SATURDAY))
    ran = JobRecord(DAY, "ok", at("22:31:00"), "abc", exit_code=0)
    assert not RECORDING_QUALITY.due(night(DAY, ran))
    assert RECORDING_QUALITY.due(night(date(2026, 11, 6), ran))
    cut = JobRecord(DAY, "interrupted", at("22:31:00"), "abc")
    assert RECORDING_QUALITY.due(night(date(2026, 11, 6), cut))


def test_the_job_labels_the_issue_only_past_a_threshold_or_on_failure() -> None:
    quiet = f"기록 품질 (#570): 2026-11-05(목)\n{AGENT}아님(기준 안: 기록만, 라벨을 바꾸지 않는다)\n"
    past = f"기록 품질 (#570): 2026-11-05(목)\n{AGENT}장중 끊김 4번(기준 3번 이상)\n"
    assert not recording_quality_calls(Ran(0, quiet, ""))
    assert recording_quality_calls(Ran(0, past, ""))
    assert recording_quality_calls(Ran(1, quiet, ""))
    assert recording_quality_calls(Ran(None, "", ""))
    assert recording_quality_calls(Ran(-9, quiet, "", timed_out=True))


def test_a_disconnect_s_gaps_are_one_group_and_one_line() -> None:
    day = date(2026, 11, 5)
    at = datetime(2026, 11, 5, 9, 6, 38, tzinfo=KST)
    reason = "disconnected: websocket closed by KIS (code 1006)"
    gaps = [
        Gap("H0UNASP0", "005930", at, at + timedelta(seconds=2.1), reason),
        Gap("H0UNASP0", "000660", at, at + timedelta(seconds=2.9), reason),
        Gap("H0UNCNT0", "005930", at, None, reason),
        Gap("H0UNCNT0", "005930", at, at + timedelta(minutes=1), "stale: no data 180s"),
    ]
    groups = quality.groups_of(gaps)
    assert [(g.reason, len(g.gaps), g.disconnect) for g in groups] == [
        (reason, 3, True),
        (gaps[3].reason, 1, False),
    ]
    assert quality.group_line(groups[0], day) == (
        f"09:06:38 {reason} · 피드 2개, 키 3개 · 길이 2.1초–2.9초, 끝 없음(수집기가 멈춤) 1"
        " · H0UNASP0: 000660 005930 / H0UNCNT0: 005930"
    )
    assert quality.group_line(groups[0], date(2026, 11, 4)).startswith("11-05 09:06:38 ")
    assert [quality.length_text(timedelta(seconds=s)) for s in (59.94, 62, 3_725)] == [
        "59.9초",
        "1분 2초",
        "1시간 2분",
    ]


def test_the_night_after_a_day_runs_to_six_and_its_late_minutes() -> None:
    day = date(2026, 11, 5)
    start, end = quality.night_window(day)
    assert (start, end) == (datetime(2026, 11, 5, 18, tzinfo=KST), datetime(2026, 11, 6, 6, 10, tzinfo=KST))
    assert quality.evening(datetime(2026, 11, 6, 5, 59, tzinfo=KST)) == day, (
        "before 06:00, the evening before"
    )
    assert quality.evening(datetime(2026, 11, 6, 6, 0, tzinfo=KST)) == date(2026, 11, 6)
