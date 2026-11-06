"""``krx-quant probe-market-halt`` (#625): what a morning's recorded frames say of a market-wide halt.

Input: a data directory as the collector writes it (``raw`` frames of the trades, the book, the
index and the futures, the VI status's answers, KIS's answers in ``_system``), in the order of
2026-11-06's halt (trading stops, the book comes back for the single-price auction, the trades
after it), all values made up. Output: the report's sections and its exit code, and the scheduled
job that runs it.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest

from krx_quant import cli
from krx_quant.archive import RAW_KEEP_DAYS, archive
from krx_quant.config import Settings
from krx_quant.feeds import RAW_SCHEMA
from krx_quant.feeds.base import KST
from krx_quant.market_calendar import MarketCalendar
from krx_quant.market_halt_probe import (
    MAX_RUNS,
    NO_RECORDS,
    Hold,
    Series,
    best_quotes,
    holds,
    longest_silence,
    probe_market_halt,
    report,
    runs,
)
from krx_quant.object_store import LocalObjectStore
from krx_quant.recorder import ParquetRecorder
from krx_quant.scheduled import FAILED, JOBS, OK, Facts, JobRecord, Night
from krx_quant.scheduled.probes import MARKET_HALT, MARKET_HALT_ISSUE
from tests.kis_frames import book_frame, futures_frame, index_frame, trade_frame

DAY = date(2026, 11, 6)  # a Friday
SATURDAY = date(2026, 11, 7)
SUNDAY = date(2026, 11, 8)
CALENDAR = MarketCalendar({DAY: True, SATURDAY: False, SUNDAY: False})
START, END = time(9, 0), time(9, 40)
SYMBOL, FUTURES = "005930", "A01612"
# Made-up market values; none may show in the report.
VALUES = ("71300", "71200", "71250", "2468.13", "331.45", "-8.31", "-5.27", "4321")


def at(hms: str, ms: int = 300) -> datetime:
    h, m, s = (int(x) for x in hms.split(":"))
    return datetime(DAY.year, DAY.month, DAY.day, h, m, s, ms * 1000, tzinfo=KST)


def trade(hms: str, **extra: str) -> dict[str, str]:
    record = {
        "MKSC_SHRN_ISCD": SYMBOL,
        "STCK_CNTG_HOUR": hms.replace(":", ""),
        "STCK_PRPR": "71250",
        "ASKP1": "71300",
        "BIDP1": "71200",
        "CNTG_VOL": "4321",
        "CNTG_CLS_CODE": "1",
        "BSOP_DATE": "20261106",
        "NEW_MKOP_CLS_CODE": "20",
        "TRHT_YN": "N",
        "MRKT_TRTM_CLS_CODE": " ",
        "MARKET_CLS_CODE": "2",
    }
    return record | extra


def book(hms: str, **extra: str) -> dict[str, str]:
    record = {"MKSC_SHRN_ISCD": SYMBOL, "BSOP_HOUR": hms.replace(":", ""), "ASKP1": "71300", "BIDP1": "71200"}
    return record | extra


def index(hms: str, change: str) -> str:
    return index_frame(
        {
            "bstp_cls_code": "0001",
            "bsop_hour": hms.replace(":", ""),
            "prpr_nmix": "2468.13",
            "prdy_ctrt": change,
        }
    )


def futures(hms: str, change: str) -> str:
    record = {"futs_shrn_iscd": FUTURES, "bsop_hour": hms.replace(":", ""), "futs_prpr": "331.45"}
    return futures_frame("H0IFCNT0", record | {"futs_prdy_ctrt": change, "dynm_prc_limt_yn": "N"})


def ack(tr_id: str, key: str, message: str = "SUBSCRIBE SUCCESS", ok: bool = True) -> str:
    code = "OPSP0000" if ok else "OPSP0008"
    body = {"rt_cd": "0" if ok else "1", "msg_cd": code, "msg1": message}
    return json.dumps({"header": {"tr_id": tr_id, "tr_key": key, "encrypt": "N"}, "body": body})


def vi_answer(*codes: str) -> str:
    rows = [{"vi_cls_code": code, "vi_prc": "71250"} for code in codes]
    return json.dumps({"tr_id": "FHPST01390000", "params": {}, "body": {"output": rows}})


class Day:
    """A recorded day, written with the collector's recorder."""

    def __init__(self, root: Path) -> None:
        self.data = root / "data"
        self.recorder = ParquetRecorder(self.data, 5000, 30.0)

    def raw(self, feed: str, key: str, when: datetime, message: str) -> None:
        row = {"received_at": when.astimezone(UTC), "message": message}
        self.recorder.record("raw", DAY, feed, key, row, RAW_SCHEMA)

    def settings(self) -> Settings:
        self.recorder.close()
        return Settings(
            kis_app_key="k", kis_app_secret="s", data_dir=self.data, state_dir=self.data.parent / "state"
        )


def halted(tmp_path: Path) -> Day:
    """A morning in 2026-11-06's order: trading stops at 09:03:33, the book comes back at 09:23:33
    for the single-price auction, the trades at 09:33:37."""
    day = Day(tmp_path)
    for tr_id, key in (
        ("H0UNCNT0", SYMBOL),
        ("H0UNASP0", SYMBOL),
        ("H0UPCNT0", "0001"),
        ("H0IFCNT0", FUTURES),
    ):
        day.raw("_system", key, at("07:20:01"), ack(tr_id, key))
    day.raw("_system", "0193L0", at("07:20:02"), ack("H0STASP0", "0193L0", "MAX SUBSCRIBE OVER", ok=False))
    day.raw("_system", "_", at("09:10:00"), json.dumps({"header": {"tr_id": "PINGPONG"}}))

    day.raw("H0UNCNT0", SYMBOL, at("08:59:59"), trade_frame(trade("08:59:59", CNTG_CLS_CODE="3")))  # before
    # Recorded out of order: the report puts them in the order received.
    day.raw("H0UNCNT0", SYMBOL, at("09:03:33"), trade_frame(trade("09:03:33"), trade("09:03:33")))
    day.raw("H0UNCNT0", SYMBOL, at("09:00:10"), trade_frame(trade("09:00:10")))
    day.raw("H0UNCNT0", SYMBOL, at("09:33:37", 800), trade_frame(trade("09:33:37", CNTG_CLS_CODE="3")))
    day.raw("H0UNCNT0", SYMBOL, at("09:33:40"), trade_frame(trade("09:33:40", CNTG_CLS_CODE="5")))
    day.raw("H0UNCNT0", SYMBOL, at("09:33:41"), "0|H0UNCNT0|001|" + "^".join(["0"] * 45))  # short
    day.raw("H0UNCNT0", SYMBOL, at("09:40:00", 0), trade_frame(trade("09:40:00")))  # the window's end

    day.raw("H0UNASP0", SYMBOL, at("09:00:10"), book_frame(book("09:00:10", HOUR_CLS_CODE="0")))
    day.raw("H0UNASP0", SYMBOL, at("09:03:33"), book_frame(book("09:03:33", HOUR_CLS_CODE="0")))
    auction = book("09:23:33", HOUR_CLS_CODE="C", ASKP1="71250", BIDP1="71250", ANTC_CNPR="71250")
    day.raw("H0UNASP0", SYMBOL, at("09:23:33"), book_frame(auction))
    day.raw("H0UNASP0", SYMBOL, at("09:33:38"), book_frame(book("09:33:38", HOUR_CLS_CODE="0")))

    # The index goes on through the halt; at or below -8% from 09:02:30 to before 09:35:00.
    for hms, change in (
        ("09:01:00", "-7.90"),
        ("09:02:30", "-8.31"),
        ("09:03:20", "-8.31"),
        ("09:03:30", "-8.31"),
        ("09:10:00", "-8.31"),
        ("09:35:00", "-7.90"),
    ):
        day.raw("H0UPCNT0", "0001", at(hms), index(hms, change))
    # The futures stop with the market; 5% or more off before it and from their first tick after it.
    for hms, change in (
        ("09:02:00", "-5.27"),
        ("09:03:32", "-5.27"),
        ("09:33:34", "-5.27"),
        ("09:34:34", "-5.27"),
        ("09:36:00", "-4.10"),
    ):
        day.raw("H0IFCNT0", FUTURES, at(hms), futures(hms, change))

    day.raw("FHPST01390000", SYMBOL, at("09:00:15"), vi_answer("Y"))
    day.raw("FHPST01390000", SYMBOL, at("09:02:20"), vi_answer("N"))
    day.raw("_unparsed", "_", at("09:05:00"), "neither a frame nor JSON")
    return day


def section(text: str, n: int) -> str:
    start = text.index(f"\n{n}. ")
    end = text.find(f"\n{n + 1}. ", start)
    return text[start : end if end > 0 else None]


def test_the_report_says_when_each_feed_went_silent_and_each_field_changed(tmp_path: Path) -> None:
    out, code = report(halted(tmp_path).settings(), DAY, START, END)

    assert code == 0
    assert out.startswith("시장 전체 매매 정지 확인 (#625): 2026-11-06(금) 09:00–09:40에 받은 raw 프레임")

    acks = section(out, 1)
    assert "KIS가 성공으로 답한 구독: TR 4개, 키 4건: H0UNCNT0 1, H0UNASP0 1, H0UPCNT0 1, H0IFCNT0 1" in acks
    assert "거절: H0STASP0 0193L0 (MAX SUBSCRIBE OVER)" in acks
    assert "장운영정보 H0STMKO0: 구독 응답 없음 · raw 폴더 없음" in acks
    assert "장운영정보 H0UNMKO0: 구독 응답 없음 · raw 폴더 없음" in acks

    minutes = section(out, 2).splitlines()
    head = next(line for line in minutes if "H0UNCNT0" in line).split()
    assert head == ["시각", "H0UNCNT0", "H0UNASP0", "H0UPCNT0", "H0IFCNT0"]
    by_minute = {line.split()[0]: line.split()[1:] for line in minutes if line.strip()[:2] == "09"}
    assert len(by_minute) == 40 and "09:40" not in by_minute, "the window's end is not in it"
    assert by_minute["09:03"] == ["2", "1", "2", "1"], "a frame of two trades is two records"
    assert by_minute["09:10"] == ["0", "0", "1", "0"], "only the index went on through the halt"
    assert by_minute["09:33"] == ["3", "1", "0", "1"], "the short frame's record is counted"
    assert next(line for line in minutes if line.split()[:1] == ["합"]).split()[1:] == ["6", "4", "6", "5"]

    keys = section(out, 3)
    assert (
        "H0UNCNT0 005930: 6건(프레임 5개, 배치가 다르거나 데이터 프레임이 아닌 행 1개) · 가장 긴 침묵 "
        "1,804.5초: 09:03:33.300(자체 09:03:33) → 09:33:37.800(자체 09:33:37)" in keys
    ), "the trade of 08:59:59 and the one at 09:40:00.000 are outside the window"
    assert "H0UNASP0 005930: 4건(프레임 4개) · 가장 긴 침묵 1,200.0초: 09:03:33.300(자체 09:03:33) → " in keys
    assert "H0IFCNT0 A01612: 5건(프레임 5개) · 가장 긴 침묵 1,802.0초" in keys
    assert "해석하지 못한 프레임(_unparsed): 1개" in keys

    codes = section(out, 4)
    assert "H0UNCNT0 TRHT_YN\n    005930: 'N' 09:00:10.300–09:33:40.300 5건 → '?' 09:33:41.300" in codes
    assert (
        "H0UNCNT0 CNTG_CLS_CODE\n    005930: '1' 09:00:10.300–09:03:33.300 3건"
        " → '3' 09:33:37.800–09:33:37.800 1건 → '5' 09:33:40.300–09:33:40.300 1건"
        " → '?' 09:33:41.300–09:33:41.300 1건" in codes
    ), "in the order received, not the order recorded"
    assert (
        "H0UNASP0 HOUR_CLS_CODE\n    005930: '0' 09:00:10.300–09:03:33.300 2건"
        " → 'C' 09:23:33.300–09:23:33.300 1건 → '0' 09:33:38.300–09:33:38.300 1건" in codes
    )
    assert (
        "H0UNASP0 1호가(만든 값)\n    005930: '양쪽' 09:00:10.300–09:03:33.300 2건 → '맞닿음' 09:23:33.300"
        in codes
    )
    assert (
        "H0UNASP0 예상 체결가(만든 값)\n    005930: '없음' 09:00:10.300–09:03:33.300 2건 → '있음' " in codes
    )
    assert "H0UNASP0 STCK_DEAL_CLS_CODE: 모든 키(1개)에서 '0'뿐" in codes
    assert "H0IFCNT0 dynm_prc_limt_yn: 모든 키(1개)에서 'N'뿐" in codes

    rest = section(out, 5)
    assert "FHPST01390000 005930: 답 2개" in rest
    assert "행: '1행, 걸린 것 1개' 09:00:15.300–09:00:15.300 1건 → '1행, 걸린 것 0개' 09:02:20.300" in rest

    rules = section(out, 6)
    assert (
        "전일 대비 -8% 이하(1단계의 기준): 09:02:30(받음 09:02:30.300)부터 09:35:00 전까지, "
        "부르는 시각 09:03:30(그 틱 09:03:30, 받음 09:03:30.300)" in rules
    ), "held a minute by the tick of 09:03:30, inside the five minutes after the open"
    assert "전일 대비 -15% 이하(2단계의 기준): 창 안에 없음" in rules
    assert (
        "전일 대비 ±5% 이상(사이드카의 기준): 09:02:00(받음 09:02:00.300)부터 09:36:00 전까지, "
        "부르는 시각 09:03:00(그 틱 09:03:32, 받음 09:03:32.300)" in rules
    )
    assert "7. 실행" in out
    for value in VALUES:
        assert value not in out


def test_a_window_without_a_record_exits_3(tmp_path: Path) -> None:
    day = Day(tmp_path)
    day.raw("_system", SYMBOL, at("07:20:01"), ack("H0UNCNT0", SYMBOL))
    day.raw("H0UNCNT0", SYMBOL, at("10:00:00"), trade_frame(trade("10:00:00")))

    out, code = report(day.settings(), DAY, START, END)

    assert code == NO_RECORDS == 3
    assert "받은 레코드 없음" in section(out, 2)
    assert "H0UNCNT0 005930: 0건" in section(out, 3)
    assert "KOSPI 지수(H0UPCNT0 0001): 창 안에 틱 없음" in section(out, 6)


def test_a_window_that_ends_before_it_starts_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not before its end"):
        report(Day(tmp_path).settings(), DAY, END, START)


def test_a_field_with_many_runs_is_counted_by_value() -> None:
    series = Series("H0UNCNT0", SYMBOL)
    first = int(at("09:00:00", 0).timestamp()) * 1_000_000
    for i in range(MAX_RUNS + 2):
        series.add(first + i * 1_000_000, -1, i % 2)

    found = runs(series, ["1", "5"])

    assert len(found) == MAX_RUNS + 2 and [r.count for r in found] == [1] * (MAX_RUNS + 2)
    assert (found[0].value, found[1].value, found[0].first, found[1].first) == (
        "1",
        "5",
        first,
        first + 1_000_000,
    )


def test_the_longest_silence_counts_the_window_s_ends() -> None:
    start = int(at("09:00:00", 0).timestamp()) * 1_000_000
    end = start + 600 * 1_000_000
    series = Series("H0UNASP0", SYMBOL)
    assert longest_silence(series, start, end) == (600.0, None, None)
    for seconds in (10, 20, 100):
        series.add(start + seconds * 1_000_000, -1, 0)
    assert longest_silence(series, start, end) == (500.0, 2, None), "from the last record to the end"
    series.add(end - 1_000_000, -1, 0)
    assert longest_silence(series, start, end) == (499.0, 2, 3)


def test_a_condition_is_called_by_the_first_tick_a_minute_after_it_was_first_met() -> None:
    times = [at(hms, 0) for hms in ("09:00:00", "09:00:30", "09:00:59", "09:01:10", "09:02:00", "09:03:00")]

    assert holds(times, [True, True, True, True, False, True]) == [
        Hold(since=0, called=3, until=4),
        Hold(since=5, called=None, until=None),
    ], "09:01:10 is the first tick a minute after 09:00:00; the last stretch has one tick"
    assert holds(times, [False] * 6) == []
    assert [h.called for h in holds(times, [True, True, True, False, True, True])] == [None, 5]


def test_the_best_quotes_are_told_apart_without_a_price() -> None:
    assert best_quotes("71300", "71200") == "양쪽"
    assert best_quotes("71250", "71250") == "맞닿음"
    assert best_quotes("71200", "71300") == "뒤집힘"
    assert best_quotes("71300", "0") == "한쪽" and best_quotes("", "") == "없음"
    assert best_quotes("x", "71200") == "?"


async def test_a_day_the_archive_pruned_is_read_back_from_the_store(tmp_path: Path) -> None:
    settings = halted(tmp_path).settings()
    store = LocalObjectStore(tmp_path / "store")
    await archive(settings.data_dir, store, today=DAY + timedelta(days=1))
    await archive(settings.data_dir, store, today=DAY + timedelta(days=RAW_KEEP_DAYS + 1))
    assert not list((settings.data_dir / "raw").rglob("*.parquet")), "pruned on the server"

    out, code = probe_market_halt(
        Settings(
            kis_app_key="k",
            kis_app_secret="s",
            data_dir=settings.data_dir,
            state_dir=settings.state_dir,
            object_store=str(tmp_path / "store"),
        ),
        DAY,
        START,
        END,
    )

    assert code == 0 and "H0UNCNT0 005930: 6건" in section(out, 3)


# --- the job ------------------------------------------------------------------------------------


def night(day: date, record: JobRecord | None = None) -> Night:
    return Night(day, CALENDAR.is_trading_day(day), "new", record, {}, Facts())


def ran(day: date, outcome: str = OK, commit: str = "new") -> JobRecord:
    return JobRecord(day, outcome, datetime(day.year, day.month, day.day, 22, 30, tzinfo=KST), commit, 0)


def test_the_job_runs_on_the_first_night_trading_or_not_and_again_after_a_failure_on_other_code() -> None:
    due = MARKET_HALT.due
    assert due(night(SATURDAY)), "the day it reads is recorded already"
    assert not due(night(SUNDAY, ran(SATURDAY)))
    assert not due(night(SUNDAY, ran(SATURDAY, FAILED, "new")))
    assert due(night(SUNDAY, ran(SATURDAY, FAILED, "old")))


def test_the_job_reads_the_halt_s_morning_and_posts_to_the_issue() -> None:
    assert MARKET_HALT in JOBS
    args = cli._parser().parse_args(list(MARKET_HALT.command))
    assert callable(args.run)
    assert (args.day, args.start, args.until) == (DAY, START, END)
    assert [t.issue for t in MARKET_HALT.targets] == [MARKET_HALT_ISSUE] == [625]
    defaults = cli._parser().parse_args(["probe-market-halt", "--day", "2026-11-06"])
    assert (defaults.start, defaults.until) == (START, END)
