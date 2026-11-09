"""Raw days past 30 days (#607): the archive deletes a raw partition on the server once the store's
copy, downloaded, has its SHA-256, and a reader reads it back from the store or fails, never finding
the day empty.

The stores are the local-directory one and the S3 one (moto); a few wrap one to give other bytes or
none.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
from collections.abc import Iterator
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import boto3
import pyarrow.parquet as pq
import pytest
from moto import mock_aws

import krx_quant
from krx_quant import archive as archiving
from krx_quant import sample_store
from krx_quant.archive import PRUNED, RAW_KEEP_DAYS, Pin, RawMissing, archive, partition_batches, pruned_mark
from krx_quant.collector import run_collector
from krx_quant.fake_kis import FakeKis
from krx_quant.feeds import RAW_SCHEMA
from krx_quant.feeds.base import KST
from krx_quant.object_store import LocalObjectStore, ObjectStore, S3ObjectStore, file_digest
from krx_quant.raw_days import RawDays
from krx_quant.recorder import ParquetRecorder
from tests.collector_doubles import settings_for
from tests.test_archive import DAY, KEY, PARTITION, Counting, messages, record
from tests.waiting import until

OLD_ENOUGH = DAY + timedelta(days=RAW_KEEP_DAYS + 1)  # DAY is 31 days before it: pruned
OTHER = "raw/date=2026-10-08/feed=H0UNCNT0/symbol=000660"
EVENTS = "events/date=2026-10-08/feed=H0UNCNT0/symbol=005930"


@pytest.fixture(params=["local", "s3"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[ObjectStore]:
    if request.param == "local":
        yield LocalObjectStore(tmp_path / "store")
        return
    with mock_aws():
        client = boto3.client("s3", region_name="ap-southeast-2")
        client.create_bucket(
            Bucket="krx-quant-test", CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"}
        )
        yield S3ObjectStore(client, "krx-quant-test", "engine")


def record_other(data: Path, messages: list[str], day: date = DAY) -> None:
    """Raw rows of another key the same day, and an events row, as the collector writes them."""
    writer = ParquetRecorder(data, flush_rows=10, flush_seconds=3600)
    at = datetime.combine(day, datetime.min.time(), KST) + timedelta(hours=9)
    for i, message in enumerate(messages):
        row = {"received_at": at + timedelta(seconds=i), "message": message}
        writer.record("raw", day, "H0UNCNT0", "000660", row, RAW_SCHEMA)
    writer.record("events", day, "H0UNCNT0", "005930", {"received_at": at, "message": "e0"}, RAW_SCHEMA)
    writer.close()


class Corrupted(Counting):
    """A store whose copies come back with other bytes, while the digest it keeps beside each says
    they are the file sent: the metadata is not what the store holds."""

    def get(self, key: str, target: Path) -> None:
        self.store.get(key, target)
        target.write_bytes(target.read_bytes() + b"flipped")


class Unreachable(Counting):
    """A store whose download of ``key`` fails."""

    def __init__(self, store: ObjectStore, key: str) -> None:
        super().__init__(store)
        self.key = key

    def get(self, key: str, target: Path) -> None:
        if key == self.key:
            raise OSError("connection reset")
        self.store.get(key, target)


class Forgetting(Counting):
    """A store that takes uploads and keeps none."""

    def digest(self, key: str) -> str | None:
        return None

    def put(self, key: str, source: Path, digest: str, *, encrypted: bool = False) -> None:
        self.puts.append(key)


# --- pruning ---------------------------------------------------------------------------------------


async def test_a_raw_day_past_30_days_goes_once_its_copy_has_its_sha256(
    store: ObjectStore, tmp_path: Path
) -> None:
    data = tmp_path / "data"
    record(data, ["m0", "m1", "m2"])
    record_other(data, ["n0"])
    await archive(data, store)
    merged = data / PARTITION / "data.parquet"
    size, digest = merged.stat().st_size, file_digest(merged)
    other = (data / OTHER / "data.parquet").stat().st_size

    report = await archive(data, store, today=OLD_ENOUGH)

    assert (report.pruned, report.pruned_bytes, report.raw_left) == (2, size + other, 0)
    assert sorted(p.name for p in (data / PARTITION).iterdir()) == [PRUNED], "the folder and its mark stay"
    mark = pruned_mark(data / PARTITION)
    assert (mark.key, mark.sha256, mark.size, mark.store) == (KEY, digest, size, store.name)
    assert store.digest(KEY) is not None, "the store keeps its copy"
    assert [p.name for p in (data / EVENTS).iterdir() if not p.name.startswith(".")] == ["data.parquet"]

    again = await archive(data, store, today=OLD_ENOUGH)
    assert (again.pruned, again.raw_left, again.failed) == (0, 0, []), "a pruned partition is left alone"


async def test_a_day_30_days_back_or_later_stays(tmp_path: Path) -> None:
    data = tmp_path / "data"
    store = LocalObjectStore(tmp_path / "store")
    record(data, ["m0"])

    report = await archive(data, store, today=DAY + timedelta(days=RAW_KEEP_DAYS))

    assert (report.pruned, report.raw_left, report.not_pruned) == (0, 1, {})
    assert (data / PARTITION / "data.parquet").is_file()
    assert (await archive(data, store, today=OLD_ENOUGH)).pruned == 1


async def test_a_pinned_day_stays(tmp_path: Path) -> None:
    data = tmp_path / "data"
    record(data, ["m0"])
    pins = [Pin(DAY - timedelta(days=1), DAY, 565, "an investigation reads it")]

    report = await archive(data, LocalObjectStore(tmp_path / "store"), today=OLD_ENOUGH, pins=pins)

    assert (report.pruned, report.pinned, report.raw_left) == (0, 1, 1)
    assert (data / PARTITION / "data.parquet").is_file()
    assert not (data / PARTITION / PRUNED).exists()


async def test_a_copy_whose_sha256_differs_keeps_the_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    data = tmp_path / "data"
    store = LocalObjectStore(tmp_path / "store")
    record(data, ["m0", "m1"])
    await archive(data, store)
    (tmp_path / "store" / KEY).write_bytes(b"not the recorded file")  # the upload's record still says sent

    with caplog.at_level(logging.WARNING, logger="krx_quant.archive"):
        report = await archive(data, store, today=OLD_ENOUGH)

    assert (report.pruned, report.raw_left) == (0, 1)
    assert "differs" in report.not_pruned[PARTITION]
    assert (data / PARTITION / "data.parquet").is_file() and not (data / PARTITION / PRUNED).exists()
    assert f"not pruning {PARTITION}: the copy in" in caplog.text


async def test_the_store_s_digest_is_not_taken_for_its_bytes(store: ObjectStore, tmp_path: Path) -> None:
    data = tmp_path / "data"
    record(data, ["m0"])
    corrupted = Corrupted(store)
    await archive(data, corrupted)
    assert corrupted.digest(KEY) == file_digest(data / PARTITION / "data.parquet"), (
        "the metadata says the same"
    )

    report = await archive(data, corrupted, today=OLD_ENOUGH)

    assert report.pruned == 0 and "differs" in report.not_pruned[PARTITION]
    assert (data / PARTITION / "data.parquet").is_file()
    assert not list((data / PARTITION).glob(".*.tmp")), "the downloaded copy is not left behind"


async def test_a_partition_the_store_has_no_copy_of_stays(tmp_path: Path) -> None:
    data = tmp_path / "data"
    record(data, ["m0"])

    report = await archive(data, Forgetting(LocalObjectStore(tmp_path / "store")), today=OLD_ENOUGH)

    assert report.pruned == 0 and "has no copy" in report.not_pruned[PARTITION]
    assert (data / PARTITION / "data.parquet").is_file()


async def test_a_copy_that_cannot_be_downloaded_keeps_the_file_and_the_run_goes_on(tmp_path: Path) -> None:
    data = tmp_path / "data"
    store = LocalObjectStore(tmp_path / "store")
    record(data, ["m0"])
    record_other(data, ["n0"])
    await archive(data, store)

    report = await archive(data, Unreachable(store, KEY), today=OLD_ENOUGH)

    assert (report.pruned, report.raw_left) == (1, 1)
    assert report.not_pruned == {PARTITION: "could not read or check it (OSError)"}
    assert (data / PARTITION / "data.parquet").is_file() and (data / OTHER / PRUNED).is_file()
    assert report.failed == [], "an upload alert is not raised for it"


async def test_the_run_logs_what_it_pruned_and_what_is_left(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    data = tmp_path / "data"
    store = LocalObjectStore(tmp_path / "store")
    record(data, ["m0", "m1"])
    record(data, ["recent"], day=OLD_ENOUGH - timedelta(days=1))
    await archive(data, store)
    old = (data / PARTITION / "data.parquet").stat().st_size
    recent = (data / "raw/date=2026-11-07/feed=H0UNCNT0/symbol=005930/data.parquet").stat().st_size

    with caplog.at_level(logging.INFO, logger="krx_quant.archive"):
        report = await archive(data, store, today=OLD_ENOUGH)

    assert (report.raw_left, report.raw_left_bytes) == (1, recent)
    line = (
        f"raw on the server (#607): pruned 1 partitions, {old / 1e6:.1f} MB; left 1 partitions,"
        f" {recent / 1e6:.1f} MB (0 pinned, 0 past 30 days kept)"
    )
    assert line in caplog.messages
    assert report.raw_line() == line


async def test_a_run_stopped_between_the_mark_and_the_delete_is_finished(tmp_path: Path) -> None:
    data = tmp_path / "data"
    store = LocalObjectStore(tmp_path / "store")
    record(data, ["m0"])
    await archive(data, store)
    kept = (data / PARTITION / "data.parquet").read_bytes()
    await archive(data, store, today=OLD_ENOUGH)
    (data / PARTITION / "data.parquet").write_bytes(kept)  # as if it stopped before the delete

    report = await archive(data, store, today=OLD_ENOUGH)

    assert sorted(p.name for p in (data / PARTITION).iterdir()) == [PRUNED]
    assert report.failed == [] and report.raw_left == 0


async def test_rows_recorded_into_a_pruned_partition_are_not_merged_over_the_store_s_copy(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    store = LocalObjectStore(tmp_path / "store")
    record(data, ["m0", "m1"])
    await archive(data, store, today=OLD_ENOUGH)
    stored = file_digest(tmp_path / "store" / KEY)
    record(data, ["late"])  # a clock set back a month

    report = await archive(data, store, today=OLD_ENOUGH)

    assert report.failed == [PARTITION]
    assert file_digest(tmp_path / "store" / KEY) == stored
    assert not (data / PARTITION / "data.parquet").exists()


async def test_the_collector_s_archive_prunes_a_day_past_30_days(kis: FakeKis, tmp_path: Path) -> None:
    store = tmp_path / "store"
    settings = replace(settings_for(kis, tmp_path / "data"), object_store=str(store))
    record(settings.data_dir, ["a month and more ago"])
    started = datetime.combine(OLD_ENOUGH, datetime.min.time(), KST) + timedelta(hours=6)  # the 06:00 deploy
    stop = asyncio.Event()
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=lambda: started))

    await until(lambda: (settings.data_dir / PARTITION / PRUNED).is_file())
    stop.set()
    await asyncio.wait_for(task, 5)

    assert LocalObjectStore(store).keys("raw/") == [KEY]
    assert not (settings.data_dir / PARTITION / "data.parquet").exists()


# --- reading it back -------------------------------------------------------------------------------


async def test_a_pruned_partition_read_as_if_it_were_there_fails(tmp_path: Path) -> None:
    data = tmp_path / "data"
    store = LocalObjectStore(tmp_path / "store")
    record(data, ["m0", "m1"])
    await archive(data, store, today=OLD_ENOUGH)

    with pytest.raises(RawMissing, match="pruned on the server"):
        list(partition_batches(data / PARTITION, ["message"]))


async def test_a_partition_pruned_while_it_is_read_fails(tmp_path: Path) -> None:
    data = tmp_path / "data"
    store = LocalObjectStore(tmp_path / "store")
    record(data, ["m0", "m1", "m2", "m3"])  # two part files
    await archive(data, store)
    record(data, ["late"])  # a part the merged file does not hold yet
    reading = partition_batches(data / PARTITION, ["message"])
    first = next(reading)

    await archive(data, store, today=OLD_ENOUGH)  # merges the late part, then prunes

    assert first[0].name == "data.parquet"
    with pytest.raises(RawMissing):
        list(reading)  # the part it was to read next is gone, and the mark says why


def test_a_partition_pruned_between_the_check_and_the_listing_of_its_files_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    store = LocalObjectStore(tmp_path / "store")
    record(data, ["m0", "m1"])
    asyncio.run(archive(data, store))
    listing = archiving.partition_files
    ran: list[bool] = []

    def pruned_first(partition: Path) -> list[Path]:
        if not ran:  # the archive's run lands here: the mark is written, then the file goes
            ran.append(True)
            asyncio.run(archive(data, store, today=OLD_ENOUGH))
        return listing(partition)

    monkeypatch.setattr(archiving, "partition_files", pruned_first)

    with pytest.raises(RawMissing):
        list(partition_batches(data / PARTITION, ["message"]))
    assert ran and (data / PARTITION / PRUNED).is_file()


async def test_a_partition_pruned_as_its_last_file_is_read_fails_once_it_is_read(tmp_path: Path) -> None:
    data = tmp_path / "data"
    store = LocalObjectStore(tmp_path / "store")
    record(data, ["m0", "m1"])
    await archive(data, store)
    reading = partition_batches(data / PARTITION, ["message"])
    first = next(reading)  # the merged file, open

    await archive(data, store, today=OLD_ENOUGH)

    assert messages([first]) == ["m0", "m1"]
    with pytest.raises(RawMissing):
        next(reading)


async def test_a_merged_file_gone_without_a_mark_fails_rather_than_reading_as_no_rows(tmp_path: Path) -> None:
    data = tmp_path / "data"
    record(data, ["m0"])
    await archive(data, LocalObjectStore(tmp_path / "store"))
    (data / PARTITION / "data.parquet").unlink()  # deleted by hand: its upload record stays

    with pytest.raises(RawMissing, match="upload record"):
        list(partition_batches(data / PARTITION, ["message"]))


async def test_a_pruned_day_is_read_back_from_the_store(store: ObjectStore, tmp_path: Path) -> None:
    data = tmp_path / "data"
    record(data, ["m0", "m1", "m2"])
    record_other(data, ["n0"])
    await archive(data, store)
    await archive(data, store, today=OLD_ENOUGH)

    with RawDays(data, store) as raw:
        root = raw.feed(DAY, "H0UNCNT0")
        read = {p.name: messages(partition_batches(p, ["message"])) for p in sorted(root.glob("symbol=*"))}
        assert root.parts[-3:] == ("raw", "date=2026-10-08", "feed=H0UNCNT0"), "the data directory's layout"
        assert raw.feeds(DAY) == ["H0UNCNT0"]
        assert raw.feed(DAY, "H0UNCNT0") == root and raw.restored == 2, "downloaded once"

    assert read == {"symbol=000660": ["n0"], "symbol=005930": ["m0", "m1", "m2"]}
    assert not root.exists(), "the copy goes when it closes"


async def test_a_day_with_some_partitions_pruned_reads_the_rest_from_the_data_directory(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    store = LocalObjectStore(tmp_path / "store")
    record(data, ["m0"])
    record_other(data, ["n0"])
    await archive(data, store)
    (tmp_path / "store" / OTHER / "data.parquet").write_bytes(b"other bytes")  # this one stays
    await archive(data, store, today=OLD_ENOUGH)

    with RawDays(data, store) as raw:
        root = raw.feed(DAY, "H0UNCNT0")
        assert messages(partition_batches(root / "symbol=000660", ["message"])) == ["n0"]
        assert messages(partition_batches(root / "symbol=005930", ["message"])) == ["m0"]
        assert raw.restored == 1


def test_a_day_none_of_whose_partitions_is_pruned_is_read_where_it_is(tmp_path: Path) -> None:
    data = tmp_path / "data"
    record(data, ["m0"])

    raw = RawDays(data, Forgetting(LocalObjectStore(tmp_path / "store")))

    assert raw.feed(DAY, "H0UNCNT0") == data / "raw" / "date=2026-10-08" / "feed=H0UNCNT0"
    assert raw.feed(DAY, "H0UNASP0") == data / "raw" / "date=2026-10-08" / "feed=H0UNASP0"  # none recorded
    assert raw.restored == 0


async def test_a_pruned_day_without_a_store_fails_and_says_so(tmp_path: Path) -> None:
    data = tmp_path / "data"
    record(data, ["m0"])
    await archive(data, LocalObjectStore(tmp_path / "store"), today=OLD_ENOUGH)

    with pytest.raises(
        RawMissing, match="1 of its 1 partitions were pruned on the server .*KRX_OBJECT_STORE"
    ):
        RawDays(data).feed(DAY, "H0UNCNT0")


async def test_a_pruned_day_whose_copy_is_gone_or_differs_fails(tmp_path: Path) -> None:
    data = tmp_path / "data"
    store = LocalObjectStore(tmp_path / "store")
    record(data, ["m0"])
    await archive(data, store, today=OLD_ENOUGH)

    (tmp_path / "store" / KEY).write_bytes(b"other bytes")
    with RawDays(data, store) as raw, pytest.raises(RawMissing, match="not the file pruned"):
        raw.feed(DAY, "H0UNCNT0")
    (tmp_path / "store" / KEY).unlink()
    with RawDays(data, store) as raw, pytest.raises(RawMissing, match="has no copy"):
        raw.feed(DAY, "H0UNCNT0")


async def test_an_unreadable_mark_fails(tmp_path: Path) -> None:
    data = tmp_path / "data"
    store = LocalObjectStore(tmp_path / "store")
    record(data, ["m0"])
    await archive(data, store, today=OLD_ENOUGH)
    (data / PARTITION / PRUNED).write_text(json.dumps({"key": KEY}))

    with RawDays(data, store) as raw, pytest.raises(RawMissing, match="its mark cannot be read"):
        raw.feed(DAY, "H0UNCNT0")


async def test_the_copies_of_two_days_are_kept_at_most(tmp_path: Path) -> None:
    data = tmp_path / "data"
    store = LocalObjectStore(tmp_path / "store")
    days = [DAY - timedelta(days=n) for n in range(3)]
    for day in days:
        record(data, [f"on {day}"], day=day)
    await archive(data, store, today=OLD_ENOUGH)

    with RawDays(data, store) as raw:
        roots = [raw.feed(day, "H0UNCNT0") for day in days]
        assert [r.exists() for r in roots] == [False, True, True], "the day asked of longest ago went"
        again = raw.feed(days[0], "H0UNCNT0")
        assert messages(partition_batches(again / "symbol=005930", ["message"])) == [f"on {days[0]}"]
        assert raw.restored == 4


async def test_a_restored_copy_is_the_file_recorded(tmp_path: Path) -> None:
    data = tmp_path / "data"
    store = LocalObjectStore(tmp_path / "store")
    record(data, ["m0", "m1"])
    await archive(data, store)
    recorded = pq.read_table(data / PARTITION / "data.parquet")
    await archive(data, store, today=OLD_ENOUGH)

    with RawDays(data, store) as raw:
        copy = raw.feed(DAY, "H0UNCNT0") / "symbol=005930" / "data.parquet"
        assert pq.read_table(copy).equals(recorded)
        assert file_digest(copy) == pruned_mark(data / PARTITION).sha256


# --- one guard for listing a partition's files ------------------------------------------------------

# The callers of ``archive.partition_files`` besides the guard (``guarded_files``) and the prune, which
# lists under the archive's own lock: each reads a layer the archive never prunes, and its code is the
# sample store's to review (``sample_store._WHOLE``, ``_PARTS``): until #645 it was in the key, and
# routing it through the guard would have made every stored training day again on the next night.
UNGUARDED = {
    ("replay", "recorded_partitions"): "events",
    ("macro", "_read_partition"): "macro",
    ("training", "is_recorded"): "events",
}
ARCHIVE_OWN = {("archive", "guarded_files"), ("archive", "_prune")}


class _Uses(ast.NodeVisitor):
    """Where a module names ``partition_files`` (but to import it): (module, innermost function)."""

    def __init__(self, module: str) -> None:
        self.module, self.stack, self.found = module, ["<module>"], set[tuple[str, str]]()

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == "partition_files":
            self.found.add((self.module, self.stack[-1]))

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == "partition_files":
            self.found.add((self.module, self.stack[-1]))
        self.generic_visit(node)


def test_every_reader_lists_a_partition_s_files_through_the_guard() -> None:
    """A reader that lists a partition with ``partition_files`` itself would read a raw partition the
    archive pruned, or prunes as it lists, as one with no rows (#607). So outside the guard and the
    prune only the sample store's keyed code may, and it reads no raw."""
    root = Path(krx_quant.__file__).parent
    found: set[tuple[str, str]] = set()
    for path in sorted(root.rglob("*.py")):
        uses = _Uses(path.relative_to(root).with_suffix("").as_posix().replace("/", "."))
        uses.visit(ast.parse(path.read_text()))
        found |= uses.found

    assert found == ARCHIVE_OWN | set(UNGUARDED), "list a partition's files with archive.guarded_files"
    for module, function in UNGUARDED:
        assert module in sample_store._WHOLE or function in sample_store._PARTS.get(module, ()), (
            f"{module}.{function} is no longer in the sample store's key: list through archive.guarded_files"
        )


async def test_the_guard_leaves_a_partition_of_another_layer_listed_empty_alone(tmp_path: Path) -> None:
    data = tmp_path / "data"
    record_other(data, ["n0"])
    await archive(data, LocalObjectStore(tmp_path / "store"))
    (data / EVENTS / "data.parquet").unlink()  # an events file gone: no raw is missing

    assert archiving.guarded_files(data / EVENTS) == []
