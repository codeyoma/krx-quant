"""What changes the key of a day a night stored (#565, ``sample_keys_probe.py``).

Input: two recorded days laid out as on the server: the data directory the collector records into
and archives from, the Object Store it uploads to, the night's training cache and the sample store,
each file with the time it would have there. The day before had rows merged into one of its files at
the 06:00 start after its night; the latest day has part files written after its 21:00 archive.
Output: the probe's report (each input stored after its day's night, the part files not merged yet,
the keys changed since the last run, no price) and the jobs that run it on two trading nights.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant import cli
from krx_quant.archive import PRUNED, archive
from krx_quant.config import Settings
from krx_quant.features import Leaders
from krx_quant.feeds import KRX_TRADE, NIGHT_FUTURES, TRADE
from krx_quant.feeds.base import KST
from krx_quant.market_calendar import MarketCalendar
from krx_quant.object_store import LocalObjectStore
from krx_quant.recorder import ParquetRecorder
from krx_quant.sample_keys_probe import STATE, _changes, key_files, probe_sample_keys, read_key
from krx_quant.sample_store import STORE_DIR, DayKey, SampleStore, day_key
from krx_quant.scheduled import FAILED, JOBS, OK, Facts, JobRecord, Night
from krx_quant.scheduled.probes import SAMPLE_KEYS, SAMPLE_KEYS_AGAIN, SAMPLE_KEYS_ISSUE
from krx_quant.training import TrainingDay
from tests.replay_events import at, trade

BEFORE, LATEST, NEXT = date(2026, 11, 5), date(2026, 11, 6), date(2026, 11, 9)
SATURDAY = date(2026, 11, 7)
CALENDAR = MarketCalendar({BEFORE: True, LATEST: True, SATURDAY: False, NEXT: True})
SYMBOLS = ("005930",)
PRICE = 987_654  # every recorded price: the report never shows it
EVERY_TRADE = TRADE.tr_id  # H0UNCNT0, the bars' trades
KRX_ONLY = KRX_TRADE.tr_id  # H0STCNT0


def kst(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm), KST)


def touch(path: Path, when: datetime) -> None:
    os.utime(path, (when.timestamp(), when.timestamp()))


@dataclass(frozen=True)
class Server:
    data: Path
    store: Path
    state: Path

    @property
    def cache(self) -> Path:
        return self.state / "training-cache"

    @property
    def samples(self) -> Path:
        return self.state / STORE_DIR

    def settings(self) -> Settings:
        return Settings(
            kis_app_key="",
            kis_app_secret="",
            symbols=SYMBOLS,
            etf_symbols=(),
            index_codes=(),
            us_symbols=(),
            data_dir=self.data,
            state_dir=self.state,
            object_store=str(self.store),
            train_at=time(21, 30),
        )

    def record(self, day: date, received: datetime, feeds: Sequence[str] = (EVERY_TRADE, KRX_ONLY)) -> None:
        """A trade of the symbol received at ``received`` in each of ``feeds``, recorded under ``day``
        as a part file (the collector's flush)."""
        recorder = ParquetRecorder(self.data, flush_rows=1000, flush_seconds=3600)
        for feed in (TRADE if f == EVERY_TRADE else KRX_TRADE for f in feeds):
            _, row = trade(received, PRICE, feed=feed)
            recorder.record("events", day, feed.tr_id, row["symbol"], row, feed.schema)
        recorder.close()

    def night_futures(self, day: date, received: datetime) -> None:
        recorder = ParquetRecorder(self.data, flush_rows=1000, flush_seconds=3600)
        row = {"symbol": "A01612", "event_time": received, "received_at": received, "price": float(PRICE)}
        recorder.record("events", day, NIGHT_FUTURES.tr_id, "A01612", row, NIGHT_FUTURES.schema)
        recorder.close()

    def archive(self, when: datetime) -> None:
        """An archive run at ``when``: what it merges and uploads is stamped with that time."""
        before = {p: p.stat().st_mtime_ns for p in self.store.rglob("*.parquet")}
        asyncio.run(archive(self.data, LocalObjectStore(self.store)))
        for p in self.store.rglob("*.parquet"):
            if before.get(p) != p.stat().st_mtime_ns:
                touch(p, when)
                touch(self.data / p.relative_to(self.store), when)

    def fetch(self, day: date, when: datetime) -> None:
        """The night's fetch of ``day``'s events into its cache: what differs from the store's."""
        for p in (self.store / "events" / f"date={day.isoformat()}").rglob("*.parquet"):
            copy = self.cache / p.relative_to(self.store)
            if not copy.is_file() or copy.read_bytes() != p.read_bytes():
                copy.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(p, copy)
                touch(copy, when)

    def store_day(self, day: date, when: datetime) -> DayKey:
        """The night's key of ``day`` from its cache, its file written at ``when``; older keys go."""
        key = day_key(self.cache, day, CALENDAR, SYMBOLS, (), 0.0, Leaders(), target_version=3, kinds=())
        store = SampleStore(self.samples)
        for old in self.samples.glob(f"date={day.isoformat()}/*.json.gz"):
            old.unlink()
        assert store.put(key, TrainingDay("mid", []), ())
        touch(store.path(key), when)
        return key


@pytest.fixture
def server(tmp_path: Path) -> Server:
    """The night of 11-06 done: 11-05's file with rows merged at the 06:00 start after its night
    (one late trade, at 23:10), made again tonight; 11-06's parts after the 21:00 archive left."""
    found = Server(tmp_path / "data", tmp_path / "store", tmp_path / "state")
    found.record(BEFORE, at("09:00:01", BEFORE))
    found.archive(kst(BEFORE, "21:01"))
    found.fetch(BEFORE, kst(BEFORE, "21:31"))
    found.store_day(BEFORE, kst(BEFORE, "21:40"))
    found.record(BEFORE, at("23:10:00", BEFORE), feeds=(EVERY_TRADE,))
    found.archive(kst(LATEST, "06:02"))
    found.record(LATEST, at("09:00:01", LATEST))
    found.archive(kst(LATEST, "21:01"))
    found.fetch(BEFORE, kst(LATEST, "21:31"))
    found.fetch(LATEST, kst(LATEST, "21:31"))
    found.store_day(BEFORE, kst(LATEST, "21:40"))
    found.store_day(LATEST, kst(LATEST, "21:52"))
    found.record(LATEST, at("22:05:00", LATEST), feeds=(EVERY_TRADE,))
    found.night_futures(LATEST, at("22:06:00", LATEST))
    return found


def section(report: str, number: int, day: date | None = None) -> str:
    """The paragraph of ``report`` headed ``<number>.`` (and naming ``day``, if given)."""
    for paragraph in report.split("\n\n"):
        first = paragraph.splitlines()[0]
        if first.startswith(f"{number}.") and (day is None or str(day) in first):
            return paragraph
    raise AssertionError(f"no section {number} {day or ''} in:\n{report}")


def partition(day: date, feed: str, symbol: str = SYMBOLS[0]) -> str:
    return f"events/date={day.isoformat()}/feed={feed}/symbol={symbol}"


def test_it_names_the_input_stored_after_the_night_of_its_day(server: Server) -> None:
    report, code = probe_sample_keys(server.settings(), kst(LATEST, "22:31"))

    assert code == 0
    stored = section(report, 1)
    assert "날 2개 (2026-11-05–2026-11-06) · 파일 2개" in stored
    assert "11-06 21:30 뒤에 쓴 파일(그 밤이 만든 날): 2026-11-05 (21:40:00" in stored
    assert "2026-11-06 (21:52:00" in stored
    before = section(report, 2, BEFORE)
    assert "입력 2개: 그 날의 밤(11-05 21:30) 뒤에 Object Store에 올라간 것 1개" in before
    assert "SHA-256이 키와 다르거나 없는 것 0개" in before
    merged = f"{partition(BEFORE, EVERY_TRADE)}/data.parquet"
    (line,) = [line for line in before.splitlines() if line.startswith(f"  {merged} · ")]
    assert "11-06 06:02:00" in line, "the Object Store's copy, merged at the 06:00 start"
    assert line.count("11-06 06:02:00") == 2, "and the data directory's, with no part left"
    assert "11-06 21:31:00" in line, "the cache's, fetched again tonight"
    assert line.endswith("part 0개")
    assert partition(BEFORE, KRX_ONLY) not in before, "stored before its night: not listed"
    latest = section(report, 2, LATEST)
    assert "그 날의 밤(11-06 21:30) 뒤에 Object Store에 올라간 것 0개" in latest
    assert f"그 밤 뒤에 바뀐 입력은 없다(가장 늦게 올라간 입력: {partition(LATEST, EVERY_TRADE)}" in latest


def test_it_tells_the_late_parts_the_samples_read_from_the_rest(server: Server) -> None:
    report, _ = probe_sample_keys(server.settings(), kst(LATEST, "22:31"))

    late = section(report, 3, LATEST)
    assert "part가 남은 파티션 2개" in late
    assert "표본이 읽는 파티션 1개:" in late
    assert f"    {partition(LATEST, EVERY_TRADE)} · part 1개" in late
    assert "표본이 읽지 않는 파티션 1개 (층, 피드별):" in late
    assert f"    events {NIGHT_FUTURES.tr_id} · 파티션 1개 · part 1개" in late


def test_a_raw_partition_pruned_on_the_server_is_no_late_part(server: Server) -> None:
    """#607: the probe only lists part files, which a raw partition the archive pruned never has: its
    folder holds the mark alone, and is neither late nor unreadable."""
    pruned = server.data / "raw" / f"date={LATEST.isoformat()}" / f"feed={EVERY_TRADE}" / "symbol=005930"
    pruned.mkdir(parents=True)
    (pruned / PRUNED).write_text("{}")

    report, code = probe_sample_keys(server.settings(), kst(LATEST, "22:31"))

    late = section(report, 3, LATEST)
    assert code == 0
    assert "part가 남은 파티션 2개" in late and "읽지 못한" not in late


def test_the_next_run_names_what_changed_in_a_key_since_the_last(server: Server) -> None:
    first, _ = probe_sample_keys(server.settings(), kst(LATEST, "22:31"))
    assert section(first, 4).startswith("4. 지난 실행 뒤 키가 바뀐 날: 지난 실행이 없다")
    kept = json.loads((server.state / STATE).read_text())
    assert set(kept["days"]) == {"2026-11-05", "2026-11-06"}
    old = kept["days"]["2026-11-06"]["file"]
    # The weekend's start merges the late trade; Monday's night makes 11-06 again and its new day.
    server.archive(kst(SATURDAY, "06:01"))
    server.record(NEXT, at("09:00:01", NEXT))
    server.archive(kst(NEXT, "21:01"))
    for day in (LATEST, NEXT):
        server.fetch(day, kst(NEXT, "21:31"))
    new = server.store_day(LATEST, kst(NEXT, "21:40"))
    server.store_day(NEXT, kst(NEXT, "21:52"))

    report, code = probe_sample_keys(server.settings(), kst(NEXT, "22:31"))

    assert code == 0
    changes = section(report, 4)
    assert changes.startswith("4. 지난 실행 뒤 키가 바뀐 날 (지난 실행 2026-11-06 22:31 KST")
    merged = f"{partition(LATEST, EVERY_TRADE)}/data.parquet"
    assert f"  2026-11-06: 파일 {old[:12]} → {new.digest[:12]} (11-09 21:40:00에 씀)" in changes
    assert "    inputs: 2개 → 2개 (바뀐 것 1, 더한 것 0, 뺀 것 0)" in changes
    (line,) = [line for line in changes.splitlines() if merged in line]
    was = dict(kept["days"]["2026-11-06"]["parts"]["inputs"])[merged]
    assert line == f"      {merged} · {was[:12]} → {dict(new.parts['inputs'])[merged][:12]}"
    kept_parts = "calendar, day, fee_rate, format, kinds, leaders, names, semantics, symbols, target_version"
    assert f"    그대로인 부분: {kept_parts}" in changes
    assert "  키가 그대로인 날 1개" in changes and "  새 날 1개: 2026-11-09" in changes
    assert "  없어진 날 0개" in changes
    latest = section(report, 2, LATEST)
    assert "그 날의 밤(11-06 21:30) 뒤에 Object Store에 올라간 것 1개" in latest
    assert "11-07 06:01:00" in latest


def test_the_report_holds_no_price(server: Server) -> None:
    report, _ = probe_sample_keys(server.settings(), kst(LATEST, "22:31"))

    assert str(PRICE) not in report and f"{PRICE:,}" not in report
    assert str(PRICE) not in (server.state / STATE).read_text()


def test_a_calendar_change_shows_the_days_that_differ_and_the_order_alone_is_said() -> None:
    def parts(sessions: list[Any], expiry: int, inputs: list[list[str]]) -> dict[str, Any]:
        return {
            "day": "2026-11-06",
            "calendar": {"sessions": sessions, "expiry": expiry, "first_known": None},
            "inputs": inputs,
        }

    open_day = ["2026-11-06T09:00:00+09:00", "2026-11-06T15:30:00+09:00"]
    reach = [None] * 50
    a, b = ["a/data.parquet", "1" * 64], ["b/data.parquet", "2" * 64]
    before = {
        "2026-11-06": {
            "file": "x" * 32,
            "written": "11-06 21:52:00",
            "parts": parts([*reach, open_day], 4, [a, b]),
        }
    }
    now = {
        "2026-11-06": {
            "file": "y" * 32,
            "written": "11-09 21:40:00",
            "parts": parts([*reach, None], 3, [b, a]),
        }
    }

    lines = _changes(before, now)

    assert "    calendar:" in lines
    assert "      2026-11-06: 장 09:00–15:30 → 쉬는 날" in lines
    assert "      expiry: 4 → 3" in lines
    assert "     같은 파일, 같은 SHA-256이고 순서만 다르다" in lines
    assert "    그대로인 부분: day" in lines


def test_a_key_is_read_from_the_head_of_its_file_and_a_bad_one_is_said(server: Server) -> None:
    key = server.store_day(LATEST, kst(LATEST, "21:52"))
    assert read_key(SampleStore(server.samples).path(key)) == key.parts
    bad = server.samples / f"date={LATEST.isoformat()}" / f"{'b' * 32}.json.gz"
    bad.write_bytes(gzip.compress(b'{"something":"else"}'))
    touch(bad, kst(LATEST, "22:00"))  # the newer of the two

    found = key_files(server.samples)[LATEST]
    assert [f.parts is None for f in found] == [False, True]
    with pytest.raises(ValueError, match="not a stored day"):
        read_key(bad)
    report, _ = probe_sample_keys(server.settings(), kst(LATEST, "22:31"))
    assert f"  읽지 못한 파일 {LATEST}/{bad.name}: ValueError: not a stored day" in section(report, 1)
    assert f"파일이 2개인 날 {LATEST}" in section(report, 1)
    assert f"(파일 {key.digest[:12]}, 11-06 21:52:00에 씀)" in section(report, 2, LATEST), "the readable one"


def test_without_a_stored_day_or_a_store_there_is_nothing_to_look_at(tmp_path: Path) -> None:
    empty = Server(tmp_path / "data", tmp_path / "store", tmp_path / "state")
    report, code = probe_sample_keys(empty.settings(), kst(LATEST, "22:31"))
    assert code == 1 and "표본 저장소에 날이 없다: 볼 것이 없다" in report


# --- the jobs ----------------------------------------------------------------------------------


def night(
    day: date, record: JobRecord | None = None, first: JobRecord | None = None, commit: str = "new"
) -> Night:
    records = {} if first is None else {SAMPLE_KEYS.name: first}
    return Night(day, CALENDAR.is_trading_day(day), commit, record, records, Facts())


def ran(day: date, outcome: str = OK, commit: str = "new") -> JobRecord:
    return JobRecord(day, outcome, kst(day, "22:30"), commit, 0 if outcome == OK else 1)


def test_the_first_run_is_on_the_first_trading_night_and_again_after_a_failure_on_other_code() -> None:
    due = SAMPLE_KEYS.due
    assert due(night(LATEST))
    assert not due(night(SATURDAY)), "not on a night that does not trade: no training made a day"
    assert not due(night(NEXT, ran(LATEST)))
    assert not due(night(NEXT, ran(LATEST, FAILED, "new")))
    assert due(night(NEXT, ran(LATEST, FAILED, "old")))


def test_the_second_run_is_on_a_trading_night_after_the_first_ran_to_its_end() -> None:
    due = SAMPLE_KEYS_AGAIN.due
    assert not due(night(LATEST)), "not before the first"
    assert not due(night(LATEST, first=ran(LATEST))), "not on the first's night"
    assert not due(night(SATURDAY, first=ran(LATEST)))
    assert due(night(NEXT, first=ran(LATEST)))
    assert not due(night(NEXT, first=ran(LATEST, FAILED))), "the first failed: it runs again first"
    assert not due(night(NEXT + timedelta(days=1), ran(NEXT), first=ran(LATEST))), "once"
    assert due(night(NEXT + timedelta(days=1), ran(NEXT, FAILED, "old"), first=ran(LATEST)))


def test_the_jobs_run_the_command_and_post_to_the_issue() -> None:
    for job in (SAMPLE_KEYS, SAMPLE_KEYS_AGAIN):
        assert job in JOBS
        assert callable(cli._parser().parse_args(list(job.command)).run)
        assert job.timeout <= timedelta(minutes=30)
        assert [t.issue for t in job.targets] == [SAMPLE_KEYS_ISSUE] == [565]
    args = cli._parser().parse_args(["probe-sample-keys", "--day", "2026-11-06"])
    assert args.day == LATEST
