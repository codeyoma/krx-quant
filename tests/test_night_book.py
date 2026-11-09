"""The night futures' book's bytes a night and the rule that keeps or stops it (#605, ``night_book.py``).

Input: nights of book frames recorded raw and normalized as the collector records them (a partition by
the KST day each came), merged by the archive; made-up modules for the scan of who names the book.
Output: each night's bytes, rows, longest silence and whether it counts; the medians, the verdict and
the jobs that run them.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from krx_quant import archive as archiving
from krx_quant import night_book as book
from krx_quant.archive import archive
from krx_quant.cli import main
from krx_quant.feeds import NIGHT_FUTURES_BOOK, RAW_SCHEMA
from krx_quant.feeds.base import KST
from krx_quant.market_calendar import MarketCalendar
from krx_quant.night_book import (
    FIRST_EVENING,
    MIB,
    NIGHTS,
    STATE,
    VERDICT_DAY,
    Layer,
    NightBytes,
    Readers,
    complete_nights,
    counted,
    evenings,
    judge,
    measure,
    measure_night,
    names_book,
    readers,
    report,
    save,
    verdict_report,
)
from krx_quant.object_store import LocalObjectStore
from krx_quant.process_log import Ran
from krx_quant.raw_days import RawDays
from krx_quant.recorder import ParquetRecorder
from krx_quant.scheduled import JOBS, NIGHT_BOOK_BYTES, NIGHT_BOOK_VERDICT, JobRecord, Night

# 2026-11-06 is a Friday; the next two weeks trade on weekdays.
CALENDAR = MarketCalendar(
    {d: d.weekday() < 5 for d in (FIRST_EVENING + timedelta(days=n) for n in range(30))}
)
TRADING = [d for d in (FIRST_EVENING + timedelta(days=n) for n in range(30)) if d.weekday() < 5]
EVERY = timedelta(minutes=4)
# Made-up prices and quantities, none of which may be printed.
PRICES = ("487.65", "487.70")
QTY = "1371"
NO_VALUES = (*PRICES, QTY, "4876", "48765")


def at(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm), KST)


def frame(received: datetime) -> tuple[str, dict[str, str]]:
    """A one-record book frame received at ``received``, and its fields."""
    fields = {c: QTY for c in NIGHT_FUTURES_BOOK.columns}
    fields |= {"futs_shrn_iscd": "A01612", "bsop_hour": received.astimezone(KST).strftime("%H%M%S")}
    fields |= {f"futs_askp{i}": PRICES[1] for i in range(1, 6)} | {
        f"futs_bidp{i}": PRICES[0] for i in range(1, 6)
    }
    message = f"0|{NIGHT_FUTURES_BOOK.tr_id}|001|" + "^".join(fields[c] for c in NIGHT_FUTURES_BOOK.columns)
    return message, fields


def record_night(
    data: Path,
    evening: date,
    *,
    first: str = "18:00",
    skip: tuple[str, str] | None = None,
    events: bool = True,
) -> int:
    """The book's frames of the night after ``evening`` every four minutes from ``first`` to 06:00, but in
    ``skip``, raw and normalized as the collector records them; their count."""
    writer = ParquetRecorder(data, flush_rows=40, flush_seconds=3600)  # a part every 40 rows
    end = datetime.combine(evening + timedelta(days=1), time(6, 0), KST)
    received, count = at(evening, first), 0
    while received <= end:
        local = received.astimezone(KST)
        if skip is None or not (at(evening, skip[0]) <= received < at(evening, skip[1])):
            message, fields = frame(received)
            day = local.date()
            utc = received.astimezone(UTC)
            writer.record(
                "raw",
                day,
                NIGHT_FUTURES_BOOK.tr_id,
                "A01612",
                {"received_at": utc, "message": message},
                RAW_SCHEMA,
            )
            if events:
                row = NIGHT_FUTURES_BOOK.normalize(fields, utc)
                writer.record(
                    "events", day, NIGHT_FUTURES_BOOK.tr_id, "A01612", row, NIGHT_FUTURES_BOOK.schema
                )
            count += 1
        received += EVERY
    writer.close()
    return count


def merge(data: Path, tmp_path: Path) -> None:
    asyncio.run(archive(data, LocalObjectStore(tmp_path / "store")))


def merged_bytes(data: Path, layer: str) -> int:
    return sum(
        p.stat().st_size
        for p in (data / layer).glob(f"date=*/feed={NIGHT_FUTURES_BOOK.tr_id}/symbol=*/data.parquet")
    )


NIGHT_FRAMES = 181  # 18:00 to 06:00 every four minutes, both ends
SESSION_FRAMES = NIGHT_FRAMES - 1  # the one at 06:00:00 is the night's row, past its session's end


def merged(data: Path, layer: str, day: date) -> Path:
    partition = data / layer / f"date={day}" / f"feed={NIGHT_FUTURES_BOOK.tr_id}" / "symbol=A01612"
    return partition / "data.parquet"


def test_a_night_s_bytes_are_its_rows_share_of_the_merged_files_of_both_days(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Row groups of 50 rows from parts of 40: the file the two nights share holds several, one of them
    # both nights'.
    monkeypatch.setattr(archiving, "_ROW_GROUP_ROWS", 50)
    data = tmp_path / "data"
    monday, tuesday = date(2026, 11, 9), date(2026, 11, 10)
    assert record_night(data, monday) == record_night(data, tuesday) == NIGHT_FRAMES
    merge(data, tmp_path)

    nights = [measure_night(data, d) for d in (monday, tuesday)]

    for night in nights:
        assert night.complete and night.why_not == []
        assert night.raw.rows == night.parsed.rows == NIGHT_FRAMES and night.frames == SESSION_FRAMES
        assert night.raw.unmerged == night.parsed.unmerged == 0
        assert night.silence == EVERY
    # Every row is one night's: the nights' shares add up to the merged files (Tuesday's morning is
    # alone in Wednesday's partition).
    assert sum(n.raw.bytes for n in nights) == pytest.approx(merged_bytes(data, "raw"))
    assert sum(n.parsed.bytes for n in nights) == pytest.approx(merged_bytes(data, "events"))
    # Monday's evening is alone in Monday's file: that whole file is Monday's, with its share of Tuesday's.
    own, shared = merged(data, "raw", monday), merged(data, "raw", tuesday)
    meta = pq.ParquetFile(shared).metadata
    groups = [meta.row_group(i) for i in range(meta.num_row_groups)]
    sizes = [sum(g.column(c).total_compressed_size for c in range(g.num_columns)) for g in groups]
    times = pq.read_table(shared, columns=["received_at"]).column(0).to_pylist()
    mine, start, mixed = 0.0, 0, 0
    for group, size in zip(groups, sizes, strict=True):
        mondays = sum(1 for t in times[start : start + group.num_rows] if t < at(tuesday, "12:00"))
        mine += size * mondays / group.num_rows
        mixed += 0 < mondays < group.num_rows
        start += group.num_rows
    assert mixed == 1  # Monday's morning and Tuesday's evening share one row group
    mine += (shared.stat().st_size - sum(sizes)) * sum(1 for t in times if t < at(tuesday, "12:00")) / start
    assert nights[0].raw.bytes == pytest.approx(own.stat().st_size + mine)


def test_a_night_pruned_on_the_server_is_measured_from_the_store_s_copy(tmp_path: Path) -> None:
    """#607: 30 days on the archive has deleted the raw night on the server; its copy in the store is the
    merged file byte for byte, so the night measures the same."""
    data, store = tmp_path / "data", LocalObjectStore(tmp_path / "store")
    monday = date(2026, 11, 9)
    record_night(data, monday)
    merge(data, tmp_path)
    before = measure_night(data, monday)
    asyncio.run(archive(data, store, today=monday + timedelta(days=archiving.RAW_KEEP_DAYS + 2)))
    assert not merged(data, "raw", monday).exists()

    with pytest.raises(archiving.RawMissing):
        measure_night(data, monday)
    with RawDays(data, store) as raw:
        assert measure_night(data, monday, raw) == before


def test_a_night_pruned_as_it_is_measured_fails_rather_than_measuring_zero_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#607: the archive prunes the raw night after ``RawDays`` found it on the server and before its
    files are listed; the measure fails, it does not count the night as empty."""
    data, store = tmp_path / "data", LocalObjectStore(tmp_path / "store")
    monday = date(2026, 11, 9)
    record_night(data, monday)
    merge(data, tmp_path)
    listing = archiving.partition_files
    ran: list[bool] = []

    def pruned_first(partition: Path) -> list[Path]:
        if not ran and "raw" in partition.parts:
            ran.append(True)
            asyncio.run(archive(data, store, today=monday + timedelta(days=archiving.RAW_KEEP_DAYS + 2)))
        return listing(partition)

    monkeypatch.setattr(archiving, "partition_files", pruned_first)

    with RawDays(data, store) as raw, pytest.raises(archiving.RawMissing):
        measure_night(data, monday, raw)
    assert ran


def test_a_night_with_a_long_silence_or_a_late_start_or_nothing_does_not_count(tmp_path: Path) -> None:
    data = tmp_path / "data"
    record_night(data, date(2026, 11, 9), skip=("23:00", "23:20"))  # 22:56 to 23:20 without a frame
    record_night(data, date(2026, 11, 10), first="18:20")  # the collector came back at 18:20
    record_night(data, date(2026, 11, 11), skip=("23:00", "23:01"))  # 22:56 to 23:04: still whole
    merge(data, tmp_path)

    silent, late, short, closed = (measure_night(data, date(2026, 11, d)) for d in (9, 10, 11, 12))

    assert silent.why_not == ["22:56:00부터 24분 동안 프레임 없음(기준 10분)"]
    assert late.why_not == ["18:00:00부터 20분 동안 프레임 없음(기준 10분)"]
    assert short.complete and short.silence == timedelta(minutes=8)
    assert closed.frames == 0 and closed.why_not == ["18:00–06:00 프레임 없음(KRX가 쉰 밤이거나 받지 못했다)"]
    assert closed.total == 0


def test_a_night_with_rows_not_merged_yet_waits_for_the_archive(tmp_path: Path) -> None:
    data = tmp_path / "data"
    monday = date(2026, 11, 9)
    record_night(data, monday)

    night = measure_night(data, monday)

    assert night.why_not == [f"아직 합치지 않음(part 파일에 raw {NIGHT_FRAMES}행, events {NIGHT_FRAMES}행)"]
    assert night.raw.bytes == night.parsed.bytes == 0 and night.frames == SESSION_FRAMES
    merge(data, tmp_path)
    assert measure_night(data, monday).complete


def test_a_file_without_the_received_time_leaves_its_night_out(tmp_path: Path) -> None:
    data = tmp_path / "data"
    monday = date(2026, 11, 9)
    record_night(data, monday)
    merge(data, tmp_path)
    odd = merged(data, "events", date(2026, 11, 10)).parents[1] / "symbol=A01613" / "data.parquet"
    odd.parent.mkdir(parents=True)
    pq.write_table(pa.table({"symbol": ["A01613"]}), odd)

    night = measure_night(data, monday)

    assert night.why_not == ["읽지 못한 파일 1개"]
    assert (
        "읽지 못한 파일: events/date=2026-11-10/feed=H0MFASP0/symbol=A01613/data.parquet: received_at 열이"
        in report([night])
    )


def test_the_nights_measured_are_the_trading_evenings_from_the_first_whose_rows_are_in() -> None:
    assert evenings(CALENDAR, at(date(2026, 11, 7), "11:59")) == []
    assert evenings(CALENDAR, at(date(2026, 11, 7), "12:00")) == [FIRST_EVENING]
    # Friday's night, then Monday's and Tuesday's; Wednesday's ends at noon on Thursday.
    assert evenings(CALENDAR, at(date(2026, 11, 11), "22:30")) == [
        FIRST_EVENING,
        date(2026, 11, 9),
        date(2026, 11, 10),
    ]


def test_the_first_seven_complete_nights_count_and_the_measure_stops_at_them(tmp_path: Path) -> None:
    data = tmp_path / "data"
    nights = TRADING[:9]
    record_night(data, nights[1], first="19:00")  # Monday's does not count
    for evening in (*nights[:1], *nights[2:]):
        record_night(data, evening)
    merge(data, tmp_path)

    found = measure(data, CALENDAR, at(VERDICT_DAY, "22:30"))

    assert [n.evening for n in found] == nights[:8]  # the eighth is the seventh complete one
    assert [n.evening for n in counted(found)] == [nights[0], *nights[2:8]]
    out = report(found)
    assert out.startswith(f"{book.TITLE}: {nights[0]} – {nights[7]}의 밤 8개, 완전한 밤 7/7\n")
    assert f"완전한 밤 {NIGHTS}개를 다 쟀다: 측정 작업은 더 돌지 않는다. 판정은 {VERDICT_DAY} 밤이다." in out
    assert f"{nights[1]}(월) 세지 않음: 18:00:00부터 1시간 0분 동안 프레임 없음(기준 10분)" in out
    assert not any(value in out for value in NO_VALUES)


def night_of(evening: date, mib: float, *, complete: bool = True) -> NightBytes:
    """A night measured at ``mib`` MiB, raw and parsed half each."""
    half = mib * MIB / 2
    silence = timedelta(minutes=2) if complete else timedelta(hours=2)
    since = at(evening, "23:00")
    return NightBytes(evening, Layer(half, 1), Layer(half, 1), 100, silence, since)


def test_the_medians_are_the_counted_nights_against_the_threshold() -> None:
    nights = [
        night_of(d, mib) for d, mib in zip(TRADING[:NIGHTS], (90, 95, 105, 110, 120, 130, 140), strict=True)
    ]
    nights.insert(1, night_of(date(2026, 11, 8), 900, complete=False))

    out = report(nights)

    assert (
        "2026-11-08(일) 세지 않음: 23:00:00부터 2시간 0분 동안 프레임 없음(기준 10분) · raw 450.0 MiB" in out
    )
    assert (
        "의 중앙값: raw 55.0 MiB · parsed 55.0 MiB · 합 110.0 MiB(기준 100.0 MiB를 넘는다)"
        " · 30일이면 3.22 GiB" in out
    )


NOBODY = Readers(("collector.py", "feeds/market.py", "night_book.py"), (), 200)


def test_the_rule_stops_the_book_only_over_the_threshold_with_no_live_module_naming_it() -> None:
    over = [night_of(d, 101) for d in TRADING[:NIGHTS]]
    under = [night_of(d, 100) for d in TRADING[:NIGHTS]]  # the median at the threshold is not over it

    assert judge(over, NOBODY).stop
    assert (
        judge(over, NOBODY).why
        == "합 중앙값 101.0 MiB가 기준 100.0 MiB를 넘고, 이 호가를 읽는 실전 모델이 없다"
    )
    assert not judge(under, NOBODY).stop
    assert judge(under, NOBODY).why == "합 중앙값 100.0 MiB가 기준 100.0 MiB를 넘지 않는다"
    model = Readers((*NOBODY.modules, "algorithms/night/__init__.py"), (), 200)
    assert not judge(over, model).stop
    assert judge(over, model).why.endswith(
        "기록·리포트 밖의 모듈이 이 호가를 부른다(algorithms/night/__init__.py)"
    )
    unread = Readers(NOBODY.modules, ("broken.py: invalid syntax",), 199)
    assert not judge(over, unread).stop and "코드를 다 읽지 못해" in judge(over, unread).why
    assert not judge(over, Readers((), (), 0)).stop  # no source at all: nothing known
    six = over[:-1] + [night_of(TRADING[NIGHTS], 500, complete=False)]
    assert not judge(six, NOBODY).stop
    assert judge(six, NOBODY).why == f"완전한 밤이 6개뿐이라(기준 {NIGHTS}개) 규칙의 중앙값이 없다"


def test_the_verdict_says_its_conclusion_what_follows_and_the_modules_naming_the_book() -> None:
    over = [night_of(d, 150) for d in TRADING[:NIGHTS]]

    stop = verdict_report(over, NOBODY, VERDICT_DAY, 1.5)
    keep = verdict_report(over[:3], NOBODY, VERDICT_DAY)

    lines = stop.splitlines()
    assert lines[0] == f"{book.VERDICT_TITLE}: {VERDICT_DAY}"
    assert (
        lines[1] == "결론: H0MFASP0 구독을 멈춘다(합 중앙값 150.0 MiB가 기준 100.0 MiB를 넘고,"
        " 이 호가를 읽는 실전 모델이 없다)"
    )
    assert lines[2].startswith(
        "할 일(에이전트, PR 하나): 수집기의 구독 계획(collector.subscription_plan)에서 H0MFASP0를 빼고"
    )
    assert "야간 체결(H0MFCNT0)과 지난 기록은 그대로 둔다" in lines[2]
    assert "\n3. 이 호가를 이름으로 부르는 모듈(설치된 krx_quant의 모듈 200개의 구문 트리:" in stop
    assert (
        "  collector.py: 구독과 녹화\n" in stop
        and "  기록·리포트 밖(실전 모델의 입력일 수 있다): 없음" in stop
    )
    assert stop.endswith("\n4. 실행\n  1.5초")
    assert keep.splitlines()[1] == "결론: 계속 기록한다(완전한 밤이 3개뿐이라(기준 7개) 규칙의 중앙값이 없다)"
    assert keep.splitlines()[2].startswith("할 일: 없음. H0MFASP0를 그대로 기록한다.")


def test_a_module_names_the_book_by_its_tr_id_feed_schema_or_the_night_feeds_not_in_a_docstring() -> None:
    assert names_book('ROOT = f"events/date={day}/feed=H0MFASP0"')
    assert names_book("from krx_quant.feeds import NIGHT_FUTURES_BOOK")
    assert names_book("from krx_quant.health import NIGHT_FEEDS as nights")
    assert names_book("import krx_quant.feeds\nx = krx_quant.feeds.NIGHT_BOOK_SCHEMA")
    assert names_book("from krx_quant.night_probe import FEEDS as NIGHT_FEEDS")
    assert not names_book('"""Reads nothing of H0MFASP0."""\nX = 1\n"""Nor does X: H0MFASP0."""')
    assert not names_book("# H0MFASP0\nfrom krx_quant.feeds import NIGHT_FUTURES\n")


def test_no_module_outside_the_recording_names_the_book_today() -> None:
    found = readers()

    assert found.unread == () and found.files > 100
    assert found.others == []
    assert {"collector.py", "feeds/market.py", "health.py", "recording_quality.py", "night_book.py"} <= set(
        found.modules
    )


def test_the_scan_lists_a_model_module_naming_the_book_and_one_it_cannot_read(tmp_path: Path) -> None:
    package = tmp_path / "krx_quant"
    (package / "algorithms" / "night").mkdir(parents=True)
    (package / "collector.py").write_text("from krx_quant.feeds import NIGHT_FUTURES_BOOK\n")
    (package / "algorithms" / "night" / "__init__.py").write_text('FEED = "H0MFASP0"\n')
    (package / "engine.py").write_text('"""Not the book: H0MFASP0."""\n')
    (package / "broken.py").write_text("def (:\n")

    found = readers(package)

    assert found.modules == ("algorithms/night/__init__.py", "collector.py")
    assert found.others == ["algorithms/night/__init__.py"]
    assert found.files == 3 and [u.split(":")[0] for u in found.unread] == ["broken.py"]


def test_the_state_file_counts_the_complete_nights_for_the_job_s_condition(tmp_path: Path) -> None:
    state = tmp_path / "state"
    assert complete_nights(state) == 0  # none yet: the measure runs
    nights = [night_of(d, 50) for d in TRADING[:3]] + [night_of(TRADING[3], 50, complete=False)]

    save(state, nights, at(date(2026, 11, 11), "22:31"))

    assert complete_nights(state) == 3
    saved = json.loads((state / STATE).read_text())
    assert saved["nights"][0] == {
        "evening": "2026-11-06",
        "complete": True,
        "raw_bytes": 25 * MIB,
        "parsed_bytes": 25 * MIB,
        "why_not": [],
    }
    (state / STATE).write_text("{")
    assert complete_nights(state) == 0  # unreadable: measured again


def night(
    day: date, record: JobRecord | None = None, *, nights: int = 0, verdict: JobRecord | None = None
) -> Night[Any]:
    records = {} if verdict is None else {NIGHT_BOOK_VERDICT.name: verdict}
    return Night(day, CALENDAR.is_trading_day(day), "abc", record, records, nights)


def ran_on(day: date, outcome: str = "ok", commit: str = "abc") -> JobRecord:
    return JobRecord(day, outcome, at(day, "22:31"), commit, exit_code=0 if outcome == "ok" else 1)


def test_the_measure_runs_each_trading_night_until_seven_complete_nights_or_the_verdict() -> None:
    monday, saturday = date(2026, 11, 9), date(2026, 11, 14)
    names = [j.name for j in JOBS]
    assert names.index("night-book-bytes-605") == names.index("recording-quality-570") + 1
    assert names.index("night-book-verdict-605") == names.index("night-book-bytes-605") + 1
    assert NIGHT_BOOK_BYTES.command == ("night-book-bytes",)
    assert [t.issue for t in NIGHT_BOOK_BYTES.targets] == [605] and NIGHT_BOOK_BYTES.unlabel == ()

    assert NIGHT_BOOK_BYTES.due(night(monday))
    assert not NIGHT_BOOK_BYTES.due(night(saturday))
    assert not NIGHT_BOOK_BYTES.due(night(monday, ran_on(monday)))  # once a night
    assert NIGHT_BOOK_BYTES.due(night(date(2026, 11, 10), ran_on(monday), nights=6))
    assert not NIGHT_BOOK_BYTES.due(night(date(2026, 11, 10), ran_on(monday), nights=NIGHTS))
    cut = JobRecord(monday, "interrupted", at(monday, "22:31"), "abc")
    assert NIGHT_BOOK_BYTES.due(night(monday, cut))
    assert not NIGHT_BOOK_BYTES.due(night(date(2026, 11, 23), nights=3, verdict=ran_on(VERDICT_DAY)))


def test_the_verdict_runs_once_on_its_night_and_again_after_a_failure_on_other_code() -> None:
    assert NIGHT_BOOK_VERDICT.command == ("night-book-bytes", "--verdict")
    assert [t.issue for t in NIGHT_BOOK_VERDICT.targets] == [605] and NIGHT_BOOK_VERDICT.label_if is None

    assert not NIGHT_BOOK_VERDICT.due(night(VERDICT_DAY - timedelta(days=1)))
    assert NIGHT_BOOK_VERDICT.due(night(VERDICT_DAY))
    assert NIGHT_BOOK_VERDICT.due(night(VERDICT_DAY + timedelta(days=1)))  # missed: the next night
    assert not NIGHT_BOOK_VERDICT.due(night(VERDICT_DAY + timedelta(days=1), ran_on(VERDICT_DAY)))
    failed = ran_on(VERDICT_DAY, "failed")
    assert not NIGHT_BOOK_VERDICT.due(night(VERDICT_DAY + timedelta(days=1), failed))
    assert NIGHT_BOOK_VERDICT.due(
        night(VERDICT_DAY + timedelta(days=1), ran_on(VERDICT_DAY, "failed", "old"))
    )


def test_the_measure_labels_the_issue_only_when_its_run_failed() -> None:
    calls = NIGHT_BOOK_BYTES.label_if
    assert calls is not None
    assert not calls(Ran(0, "야간 호가 기록량 (#605): ...", ""))
    assert calls(Ran(1, "", "Traceback"))
    assert calls(Ran(None, "", ""))
    assert calls(Ran(-9, "", "", timed_out=True))


def test_the_command_writes_the_state_file_and_prints_no_market_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tmp_path / "data"
    record_night(data, FIRST_EVENING)
    merge(data, tmp_path)
    monkeypatch.chdir(tmp_path)  # no .env
    monkeypatch.setenv("KRX_DATA_DIR", str(data))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))

    assert main(["night-book-bytes", "--verdict"]) == 0

    out = capsys.readouterr().out
    assert out.startswith(f"{book.VERDICT_TITLE}: ")
    assert "\n결론: 계속 기록한다(" in out and "\n4. 실행\n  " in out
    assert not any(value in out for value in NO_VALUES)
    assert (tmp_path / "state" / STATE).exists()
