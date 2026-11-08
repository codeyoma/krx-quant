"""``krx-quant probe-inverse-book`` (#606): what a recorded day of the inverse ETF's KRX book held.

Input: a data directory as the collector writes it (``raw`` frames of ``H0STASP0`` in the example's
63 and 59 fields, KIS's answers in ``_system``, ``gaps``), all values made up. Output: the report's
sections and its exit code; the scheduled job that ran it has left ``JOBS``.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from krx_quant import cli
from krx_quant.config import Settings
from krx_quant.feeds import RAW_SCHEMA
from krx_quant.feeds.base import KST
from krx_quant.footprint import Meter
from krx_quant.health import GAP_SCHEMA
from krx_quant.inverse_book_probe import (
    NO_FRAMES,
    REPEATS,
    RSS_BOUND_MB,
    measure_burden,
    probe_inverse_book,
    report,
    verdict,
)
from krx_quant.inverse_replay import probed_day
from krx_quant.market_calendar import MarketCalendar
from krx_quant.raw_days import RawDays
from krx_quant.recorder import ParquetRecorder
from krx_quant.scheduled import JOBS
from tests.kis_frames import KRX_BOOK_COLUMNS, krx_book_frame

DAY = date(2026, 10, 8)  # a Thursday: 09:00–15:30, the closing auction from 15:20
NEXT = date(2026, 10, 12)
SATURDAY = date(2026, 10, 10)
CALENDAR = MarketCalendar({DAY - timedelta(days=1): True, DAY: True, date(2026, 10, 9): False, NEXT: True})
ETF = "0193L0"
OLD = KRX_BOOK_COLUMNS[:59]  # the example's layout before 2026-09-28
# Made-up prices; none may show in the report.
PRICES = ("9010", "9005", "9020", "9000", "9012")


def at(hms: str, ms: int = 300) -> datetime:
    h, m, s = (int(x) for x in hms.split(":"))
    return datetime(DAY.year, DAY.month, DAY.day, h, m, s, ms * 1000, tzinfo=KST)


def quote(hms: str, ask: int, bid: int, ask_qty: int = 7, bid_qty: int = 9, **extra: str) -> dict[str, str]:
    """A record at ``hms`` with ten levels a side, 5 won apart from ``ask`` and ``bid``."""
    record = {"MKSC_SHRN_ISCD": ETF, "BSOP_HOUR": hms.replace(":", ""), "HOUR_CLS_CODE": "0"}
    for i in range(1, 11):
        record[f"ASKP{i}"] = str(ask + 5 * (i - 1))
        record[f"BIDP{i}"] = str(bid - 5 * (i - 1))
        record[f"ASKP_RSQN{i}"] = str(ask_qty)
        record[f"BIDP_RSQN{i}"] = str(bid_qty)
    record["ACML_VOL"] = "120"
    return record | extra


def ack(tr_id: str, key: str, message: str, ok: bool = True) -> str:
    code = "OPSP0000" if ok else "OPSP0008"
    body = {"rt_cd": "0" if ok else "1", "msg_cd": code, "msg1": message}
    return json.dumps({"header": {"tr_id": tr_id, "tr_key": key, "encrypt": "N"}, "body": body})


class Resident(Meter):
    """Resident memory as a pass sees it (time is a system boundary, and so is ``/proc``): 100 MB just
    before each pass, then 100 MB plus that pass's growth: none for the warm-up pass, then in turn
    each of ``growth``'s rows (read only, raw, raw and events), one row a repeat. No high-water
    mark. ``None``: a system where it cannot be read."""

    def __init__(self, *growth: tuple[float, float, float]) -> None:
        super().__init__(Path("/nonexistent"))
        rows = growth * (REPEATS // len(growth)) if growth else ()
        self._growth = [0.0, *(g for row in rows for g in row)] if growth else None
        self._pass, self._first = -1, True

    def reset(self) -> None:
        self._pass += 1
        self._first = True

    def rss_mb(self) -> float | None:
        if self._growth is None:
            return None
        before, self._first = self._first, False
        return 100.0 if before else 100.0 + self._growth[self._pass]


class Day:
    """A recorded day, written with the collector's recorder."""

    def __init__(self, root: Path) -> None:
        self.data = root / "data"
        self.recorder = ParquetRecorder(self.data, 5000, 30.0)

    def raw(self, feed: str, key: str, when: datetime, message: str) -> None:
        row = {"received_at": when.astimezone(UTC), "message": message}
        self.recorder.record("raw", DAY, feed, key, row, RAW_SCHEMA)

    def book(self, when: datetime, message: str) -> None:
        self.raw("H0STASP0", ETF, when, message)

    def settings(self) -> Settings:
        self.recorder.close()
        return Settings(
            kis_app_key="k",
            kis_app_secret="s",
            data_dir=self.data,
            state_dir=self.data.parent / "state",
            flush_rows=3,  # small, so the burden's buffers are written by rows too
        )


def recorded(tmp_path: Path) -> Day:
    day = Day(tmp_path)
    day.raw("_system", ETF, at("06:00:01"), ack("H0STASP0", ETF, "SUBSCRIBE SUCCESS"))
    day.raw("_system", "009150", at("06:00:01"), ack("H0NXCNT0", "009150", "MAX SUBSCRIBE OVER", ok=False))
    day.raw("_system", "_", at("06:00:11"), json.dumps({"header": {"tr_id": "PINGPONG"}}))
    day.book(at("08:59:58"), krx_book_frame(quote("08:59:58", 9010, 9005, HOUR_CLS_CODE="B")))
    day.book(at("09:00:02"), krx_book_frame(quote("09:00:02", 9010, 9005)))  # 1 tick
    day.book(at("09:00:30"), krx_book_frame(quote("09:00:30", 9020, 9005), quote("09:00:30", 9020, 9005, 8)))
    day.book(at("09:01:00"), krx_book_frame(quote("09:01:00", 9005, 9005), columns=OLD))  # locked, 59 fields
    asks_out = quote("09:01:03", 9012, 9000) | {"ASKP2": "9000"}  # off the tick, asks out of order
    day.book(at("09:01:03"), krx_book_frame(asks_out))
    day.book(at("09:01:04"), krx_book_frame(quote("09:01:04", 9000, 9005)))  # crossed
    day.book(at("09:01:05"), "0|H0STASP0|001|" + "^".join(["0"] * 61))  # short of fields
    day.book(at("15:30:31"), krx_book_frame(quote("15:30:31", 9010, 9005, BSOP_HOUR="888888")))
    row = {
        "feed": "H0STASP0",
        "symbol": ETF,
        "start": at("10:00:00", 0),
        "end": at("10:00:40", 0),
        "reason": "disconnected: websocket closed by KIS (code 1006)",
    }
    day.recorder.record("gaps", DAY, "H0STASP0", ETF, row, GAP_SCHEMA)
    return day


def section(text: str, n: int) -> str:
    start = text.index(f"\n{n}. ")
    end = text.find(f"\n{n + 1}. ", start)
    return text[start : end if end > 0 else None]


def test_the_report_counts_the_frames_fields_times_and_best_quotes(tmp_path: Path) -> None:
    out, code = report(recorded(tmp_path).settings(), CALENDAR, DAY, Resident((1.5, 2.0, 4.5)))

    assert code == 0
    assert out.startswith("인버스 ETF 호가 확인 (#606): 0193L0 KRX 호가(H0STASP0), 2026-10-08(목)")
    acks = section(out, 1)
    assert "H0STASP0 0193L0: SUBSCRIBE SUCCESS 1번" in acks
    assert "세션 한도로 거절된 구독: H0NXCNT0 009150 1번" in acks

    frames = section(out, 2)
    assert "프레임 8 · 레코드 9 · 프레임당 레코드: 1개 7, 2개 1" in frames, (
        "the short frame: one record of 61"
    )
    assert "레코드당 필드 수: 59개 1, 61개 1, 63개 7" in frames
    assert "키(symbol 파티션)별 프레임: 0193L0 8" in frames
    assert "때(받은 시각)별 레코드: 시가 단일가 1 · 연속매매 7 · 장 마감 뒤 1" in frames

    fields = section(out, 3)
    assert "필드 63개인 레코드 7개" in fields and "필드 59개인 레코드 1개" in fields
    assert "MID_PRC" in fields.split("필드 59개인")[0], "the four new fields are always 0 here"
    assert "HOUR_CLS_CODE 값: 0 6, B 1" in fields
    assert "MKSC_SHRN_ISCD 값: 0193L0 7" in fields

    times = section(out, 4)
    assert "HHMMSS 7" in times and "6자리, 시각이 아님 1" in times
    assert "시각이 아닌 값: 888888 1" in times
    assert "중앙 0.3초" in times, "received 0.3 s into the second the record carries"

    best = section(out, 5)
    assert "연속매매 레코드 6개 중 매도·매수 1호가가 다 있는 것 6(100.0%)" in best
    assert "1틱 1, 2틱 1, 3틱 2" in best and "0(맞물림) 1" in best and "음수(뒤집힘) 1" in best
    assert "배수가 아닌 1호가나 스프레드: 1" in best
    assert "호가 순서가 어긋난 레코드: 매도 1 · 매수 0" in best
    assert "가격이 바뀜 4 · 잔량만 바뀜 1 · 그대로 0" in best
    # From the short frame's record at 09:01:05.3 to the 15:20 closing auction nothing more came.
    assert "가장 긴 침묵(연속매매 안, 모든 레코드): 22734.7초(그 앞 레코드 09:01:05)" in best
    # Stale: 09:00:07.3–09:00:30.3, 09:00:35.3–09:01:00.3, and from 09:01:10.3 to 15:20.
    assert "오래된 시간: 22777.7초(연속매매의 99.9%)" in best
    assert "봉 마감(매분 0초) 380번 중 380번(100.0%)" in best, "09:01:00.0 came before its record"

    assert "0193L0: 10:00:00 – 10:00:40 (disconnected: websocket closed by KIS (code 1006))" in section(
        out, 6
    )

    burden = section(out, 7)
    assert "같은 프레임을 읽기만 한 실행은 1.5 MB 늘었다" in burden
    assert "지금처럼 raw만: CPU" in burden and "RSS 증가 2.0 MB(읽기를 빼면 0.5 MB)" in burden
    raw_line, events_line = (line for line in burden.splitlines() if "RSS 증가 " in line and "CPU " in line)
    assert raw_line.endswith("기준 안(기준 CPU 5%, RSS 증가 32 MiB)")
    assert "raw와 1호가 이벤트(레코드마다 1행, 8행)" in events_line, "the 63- and 59-field records"
    assert "RSS 증가 4.5 MB(읽기를 빼면 3.0 MB)" in events_line and "기준 안" in events_line
    assert "8. 실행" in out
    for price in PRICES:
        assert price not in out


def test_a_day_without_a_frame_of_the_book_exits_3(tmp_path: Path) -> None:
    day = Day(tmp_path)
    day.raw("_system", ETF, at("06:00:01"), ack("H0STASP0", ETF, "SUBSCRIBE SUCCESS"))

    out, code = report(day.settings(), CALENDAR, DAY, Resident())

    assert code == NO_FRAMES == 3
    assert "받은 프레임 없음" in section(out, 2)
    assert "H0STASP0 0193L0: SUBSCRIBE SUCCESS 1번" in section(out, 1)


def test_a_recording_over_the_rss_bound_or_not_measured_is_never_within() -> None:
    assert verdict(0.01, 31.0, 28.0) == "기준 안", "within under the upper bound, reading included"
    assert verdict(0.01, 40.0, 33.0) == "기준 넘음", "over even without the reading"
    assert verdict(0.01, 35.0, 30.0).startswith("RSS는 판정 못함"), "over with the reading, within without"
    assert verdict(0.06, 1.0, 0.0) == "기준 넘음", "the CPU bound is 5% of a core"
    assert verdict(0.01, None, None) == "RSS는 재지 못함(CPU는 기준 안)"
    assert verdict(0.06, None, None) == "기준 넘음"


def test_the_burden_counts_what_recording_adds_to_the_read_only_pass(tmp_path: Path) -> None:
    settings = recorded(tmp_path).settings()

    over = 2.0 + RSS_BOUND_MB + 1
    # One raw pass of the three grew far more than the others: each figure is the median.
    resident = Resident((2.0, 2.5, over), (2.0, 60.0, over), (2.0, 2.5, over))
    burden = measure_burden(RawDays(settings.data_dir), DAY, 3, 30.0, resident)

    assert REPEATS == 3 and (burden.read.rss_mb, burden.raw.rss_mb, burden.all.rss_mb) == (2.0, 2.5, over)
    assert burden.without_reading_mb(burden.raw) == 0.5
    assert burden.without_reading_mb(burden.all) == RSS_BOUND_MB + 1, "over: 33 MB more than reading"
    assert (burden.frames, burden.events) == (8, 8)
    out, _ = report(settings, CALENDAR, DAY, Resident((2.0, 2.5, over)))
    events_line = next(line for line in section(out, 7).splitlines() if "1호가 이벤트" in line)
    assert "RSS 증가 35.0 MB(읽기를 빼면 33.0 MB)" in events_line and "기준 넘음" in events_line
    unmeasured, _ = report(settings, CALENDAR, DAY, Resident())
    assert "RSS 증가 모름(이 시스템에서 읽지 못함)" in section(unmeasured, 7)
    assert "RSS는 재지 못함(CPU는 기준 안)" in section(unmeasured, 7)
    assert "· 기준 안(" not in section(unmeasured, 7)


def test_the_day_read_is_the_latest_whose_session_is_over() -> None:
    def kst(day: date, hour: int) -> datetime:
        return datetime(day.year, day.month, day.day, hour, tzinfo=KST)

    assert probed_day(CALENDAR, kst(DAY, 22)) == DAY
    assert probed_day(CALENDAR, kst(DAY + timedelta(days=1), 1)) == DAY, "after midnight: the evening's"
    assert probed_day(CALENDAR, kst(DAY, 12)) == DAY - timedelta(days=1), "the session is not over"
    assert probed_day(CALENDAR, kst(SATURDAY, 23)) == DAY


def test_the_command_reads_the_day_asked_for(tmp_path: Path) -> None:
    settings = recorded(tmp_path).settings()
    out, code = probe_inverse_book(settings, datetime(2026, 10, 13, 22, tzinfo=KST), DAY)
    assert code == 0 and "2026-10-08(목)" in out


# --- the job ------------------------------------------------------------------------------------


def test_the_probe_ran_and_left_the_jobs_and_its_command_stays() -> None:
    # It posted on #606 on 2026-11-06 (#505: a one-off job that has run leaves ``JOBS``).
    assert "probe-inverse-book-606" not in {job.name for job in JOBS}
    assert callable(cli._parser().parse_args(["probe-inverse-book"]).run)
    assert cli._parser().parse_args(["probe-inverse-book", "--day", "2026-10-08"]).day == DAY
