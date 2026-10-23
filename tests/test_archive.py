"""Archiving recorded days to the Object Store: one object per partition, safe to run again.

The same tests run against the local-directory store and the S3 store (moto).
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
import pyarrow.parquet as pq
import pytest
from moto import mock_aws

from krx_quant.archive import archive
from krx_quant.candles import write_candles
from krx_quant.collector import run_collector
from krx_quant.fake_kis import FakeKis
from krx_quant.feeds import KOSPI200_FUTURES, RAW_SCHEMA
from krx_quant.feeds.base import KST
from krx_quant.object_store import (
    LocalObjectStore,
    ObjectStore,
    S3ObjectStore,
    Stored,
    get_json,
    put_json,
)
from krx_quant.recorder import ParquetRecorder
from tests.kis_frames import trade_frame
from tests.test_collector import settings_for
from tests.test_subscription_limit import until

DAY = date(2026, 10, 8)
PARTITION = "raw/date=2026-10-08/feed=H0UNCNT0/symbol=005930"
KEY = f"{PARTITION}/data.parquet"


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


def record(data_dir: Path, messages: list[str], day: date = DAY) -> None:
    """Record raw rows the way the collector does: a part file every two rows."""
    recorder = ParquetRecorder(data_dir, flush_rows=2, flush_seconds=3600)
    start = datetime(day.year, day.month, day.day, 0, 30, tzinfo=UTC)
    for i, message in enumerate(messages):
        row = {"received_at": start + timedelta(seconds=i), "message": message}
        recorder.record("raw", day, "H0UNCNT0", "005930", row, RAW_SCHEMA)
    recorder.close()


def fetched(store: ObjectStore, key: str, tmp_path: Path) -> list[dict[str, Any]]:
    target = tmp_path / "fetched.parquet"
    store.get(key, target)
    rows: list[dict[str, Any]] = pq.read_table(target).to_pylist()
    return rows


async def test_each_partition_becomes_one_object_with_every_recorded_row(
    store: ObjectStore, tmp_path: Path
) -> None:
    data = tmp_path / "data"
    record(data, ["m0", "m1", "m2", "m3", "m4"])  # three part files

    await archive(data, store)

    assert store.keys() == [KEY]
    assert [r["message"] for r in fetched(store, KEY, tmp_path)] == ["m0", "m1", "m2", "m3", "m4"]
    assert [p.name for p in (data / PARTITION).iterdir() if not p.name.startswith(".")] == ["data.parquet"]


class Counting:
    """Counts uploads to the store it wraps."""

    def __init__(self, store: ObjectStore, fail: bool = False) -> None:
        self.store = store
        self.puts: list[str] = []
        self.fail = fail

    @property
    def name(self) -> str:
        return self.store.name

    def digest(self, key: str) -> str | None:
        return self.store.digest(key)

    def put(self, key: str, source: Path, digest: str) -> None:
        if self.fail:
            raise OSError("store unreachable")
        self.puts.append(key)
        self.store.put(key, source, digest)

    def get(self, key: str, target: Path) -> None:
        self.store.get(key, target)

    def keys(self, prefix: str = "") -> list[str]:
        return self.store.keys(prefix)

    def objects(self, prefix: str = "") -> list[Stored]:
        return self.store.objects(prefix)

    def delete(self, key: str) -> None:
        self.store.delete(key)


async def test_archiving_again_gives_the_same_objects_without_uploading_again(
    store: ObjectStore, tmp_path: Path
) -> None:
    data = tmp_path / "data"
    record(data, ["m0", "m1", "m2"])
    counting = Counting(store)

    await archive(data, counting)
    first = fetched(store, KEY, tmp_path)
    await archive(data, counting)

    assert counting.puts == [KEY], "uploaded once"
    assert fetched(store, KEY, tmp_path) == first
    assert store.keys() == [KEY]


async def test_rows_recorded_after_a_run_are_added_once_on_the_next(
    store: ObjectStore, tmp_path: Path
) -> None:
    data = tmp_path / "data"
    record(data, ["m0", "m1", "m2"])
    await archive(data, store)

    record(data, ["late0", "late1"])  # e.g. NXT after-market trades
    await archive(data, store)

    assert [r["message"] for r in fetched(store, KEY, tmp_path)] == ["m0", "m1", "m2", "late0", "late1"]


async def test_a_part_left_behind_by_a_crash_is_not_merged_twice(tmp_path: Path) -> None:
    data = tmp_path / "data"
    store = LocalObjectStore(tmp_path / "store")
    record(data, ["m0", "m1", "m2"])
    kept = tmp_path / "kept"
    kept.mkdir()
    parts = sorted((data / PARTITION).glob("part-*.parquet"))
    for part in parts:
        (kept / part.name).write_bytes(part.read_bytes())

    await archive(data, store)
    for part in parts:  # as if the run died after writing data.parquet, before deleting parts
        (data / PARTITION / part.name).write_bytes((kept / part.name).read_bytes())
    await archive(data, store)

    assert [r["message"] for r in fetched(store, KEY, tmp_path)] == ["m0", "m1", "m2"]
    assert not list((data / PARTITION).glob("part-*.parquet"))


async def test_leftover_temporary_files_are_removed_once_stale(tmp_path: Path) -> None:
    data = tmp_path / "data"
    record(data, ["m0"])
    stale = data / PARTITION / ".part-093000-abcd1234.parquet.tmp"
    fresh = data / PARTITION / ".part-153000-ef567890.parquet.tmp"
    for tmp in (stale, fresh):
        tmp.write_bytes(b"half written")
    hour_ago = time.time() - 3600
    os.utime(stale, (hour_ago, hour_ago))

    await archive(data, LocalObjectStore(tmp_path / "store"))

    assert not stale.exists()
    assert fresh.exists(), "may still be being written"


async def test_a_failed_upload_is_reported_and_retried_on_the_next_run(tmp_path: Path) -> None:
    data = tmp_path / "data"
    record(data, ["m0", "m1"])
    store = LocalObjectStore(tmp_path / "store")

    report = await archive(data, Counting(store, fail=True))
    assert report.failed == [PARTITION]
    assert store.keys() == []

    report = await archive(data, store)
    assert (report.failed, store.keys()) == ([], [KEY])


async def test_days_from_a_given_day_on_can_be_left_out(tmp_path: Path) -> None:
    data = tmp_path / "data"
    record(data, ["yesterday"], day=DAY - timedelta(days=1))
    record(data, ["today"])
    store = LocalObjectStore(tmp_path / "store")

    await archive(data, store, before=DAY)

    assert store.keys() == ["raw/date=2026-10-07/feed=H0UNCNT0/symbol=005930/data.parquet"]


async def test_day_candles_are_uploaded_on_every_run_they_changed(tmp_path: Path) -> None:
    data = tmp_path / "data"
    record(data, ["today"])
    candle = {
        "symbol": "005930",
        "date": DAY,
        "venue": "UN",
        "open": 1,
        "high": 2,
        "low": 1,
        "close": 2,
        "volume": 3,
        "value": 6,
    }
    write_candles(data, "day", "005930", [candle])
    store = Counting(LocalObjectStore(tmp_path / "store"))
    key = "candles/period=day/symbol=005930/candles.parquet"

    await archive(data, store, before=DAY)  # today's recording is left for later, not the candles
    write_candles(data, "day", "005930", [candle, {**candle, "date": DAY + timedelta(days=1), "close": 3}])
    await archive(data, store, before=DAY)
    await archive(data, store, before=DAY)

    assert store.puts == [key, key], "uploaded when written, again when a candle was added"
    assert [r["close"] for r in fetched(store.store, key, tmp_path)] == [2, 3]


async def test_the_collector_archives_the_day_after_the_close(kis: FakeKis, tmp_path: Path) -> None:
    kis.script[("H0UNCNT0", "005930")] = [
        trade_frame({"MKSC_SHRN_ISCD": "005930", "STCK_CNTG_HOUR": "152900", "BSOP_DATE": "20261008"})
    ]
    store = tmp_path / "store"
    settings = replace(settings_for(kis, tmp_path / "data"), object_store=str(store))
    just_before = datetime(2026, 10, 8, 15, 44, 59, 500_000, tzinfo=KST)  # archiving starts 15:45
    stop = asyncio.Event()
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=lambda: just_before))
    key = "events/date=2026-10-08/feed=H0UNCNT0/symbol=005930/data.parquet"

    await until(lambda: key in LocalObjectStore(store).keys())
    stop.set()
    await asyncio.wait_for(task, 5)

    [trade] = fetched(LocalObjectStore(store), key, tmp_path)
    assert trade["event_time"] == datetime(2026, 10, 8, 15, 29, tzinfo=KST)


async def test_a_day_of_small_parts_is_merged_into_large_row_groups(tmp_path: Path) -> None:
    data = tmp_path / "data"
    record(data, [f"m{i}" for i in range(200)])  # 100 part files

    await archive(data, LocalObjectStore(tmp_path / "store"))

    assert pq.ParquetFile(data / PARTITION / "data.parquet").metadata.num_row_groups == 1


async def test_a_new_store_gets_every_day_even_if_another_store_already_has_it(tmp_path: Path) -> None:
    data = tmp_path / "data"
    record(data, ["m0"])
    await archive(data, LocalObjectStore(tmp_path / "old-bucket"))

    moved = LocalObjectStore(tmp_path / "new-bucket")
    await archive(data, moved)

    assert moved.keys() == [KEY]


async def test_a_recheck_uploads_objects_missing_from_the_store(tmp_path: Path) -> None:
    data = tmp_path / "data"
    record(data, ["m0"])
    store = LocalObjectStore(tmp_path / "store")
    await archive(data, store)
    (tmp_path / "store" / KEY).unlink()  # lost on the store side

    await archive(data, store)
    assert store.keys() == [], "without a recheck the local record is trusted"
    await archive(data, store, recheck=True)
    assert store.keys() == [KEY]


async def test_a_stray_folder_does_not_stop_the_other_days(tmp_path: Path) -> None:
    data = tmp_path / "data"
    record(data, ["m0"])
    (data / "raw" / "date=copy-of-2026-10-08").mkdir()
    store = LocalObjectStore(tmp_path / "store")

    await archive(data, store, before=DAY + timedelta(days=1))

    assert store.keys() == [KEY]


async def test_a_collector_started_after_the_nightly_run_archives_today_at_once(
    kis: FakeKis, tmp_path: Path
) -> None:
    kis.script[("H0UNCNT0", "005930")] = [
        trade_frame({"MKSC_SHRN_ISCD": "005930", "STCK_CNTG_HOUR": "193000", "BSOP_DATE": "20261008"})
    ]
    store = tmp_path / "store"
    settings = replace(settings_for(kis, tmp_path / "data"), object_store=str(store))
    record(settings.data_dir, ["recorded before a restart at 21:10"])  # e.g. an update stopped the 21:00 run
    after_nightly = datetime(2026, 10, 8, 21, 30, tzinfo=KST)
    stop = asyncio.Event()
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=lambda: after_nightly))

    await until(lambda: KEY in LocalObjectStore(store).keys())
    stop.set()
    await asyncio.wait_for(task, 5)


async def test_a_column_added_to_a_feed_during_the_day_is_null_in_the_rows_before(tmp_path: Path) -> None:
    # A collector restarted mid-day with a newer schema: futures events gained change_pct.
    data = tmp_path / "data"
    new = KOSPI200_FUTURES.schema
    old = new.remove(new.get_field_index("change_pct"))
    at = datetime(2026, 10, 8, 9, 30, tzinfo=KST)

    def row(price: float, **extra: Any) -> dict[str, Any]:
        return {"symbol": "A01612", "event_time": at, "received_at": at, "price": price, **extra}

    for schema, rows in ((old, [row(1056.3)]), (new, [row(1057.1, change_pct=-1.2)])):
        recorder = ParquetRecorder(data, flush_rows=1, flush_seconds=3600)
        for r in rows:
            recorder.record("events", DAY, "H0IFCNT0", "A01612", r, schema)
        recorder.close()
    local = LocalObjectStore(tmp_path / "archive")

    report = await archive(data, local)

    assert not report.failed
    key = "events/date=2026-10-08/feed=H0IFCNT0/symbol=A01612/data.parquet"
    assert [(r["price"], r["change_pct"]) for r in fetched(local, key, tmp_path)] == [
        (1056.3, None),
        (1057.1, -1.2),
    ]


def test_objects_can_be_listed_by_prefix_and_kept_as_json(store: ObjectStore) -> None:
    put_json(store, "reports/2026-10-12/newton.json", {"promoted": True})
    put_json(store, "promotions/2026-10-12.json", {"day": "2026-10-12"})

    assert store.keys("reports/") == ["reports/2026-10-12/newton.json"]
    assert get_json(store, "promotions/2026-10-12.json") == {"day": "2026-10-12"}
    assert get_json(store, "promotions/2026-10-13.json") is None


def test_objects_are_listed_with_their_size_and_time_and_can_be_deleted(store: ObjectStore) -> None:
    """#361: the model prune reads each object's size and when it was stored, and deletes it."""
    before = datetime.now(UTC) - timedelta(minutes=5)
    put_json(store, "models/newton/newton-20261012-abc123.json", {"version": "newton-20261012-abc123"})
    put_json(store, "models/newton/newton-20261013-def456.json", {"version": "newton-20261013-def456"})
    put_json(store, "promotions/2026-10-12.json", {"day": "2026-10-12"})

    listed = store.objects("models/")

    assert [o.key for o in listed] == [
        "models/newton/newton-20261012-abc123.json",
        "models/newton/newton-20261013-def456.json",
    ]
    assert all(o.size > 0 and o.modified >= before for o in listed)
    store.delete("models/newton/newton-20261012-abc123.json")
    store.delete("models/newton/never-stored.json")  # nothing there: nothing happens
    assert store.keys("models/") == ["models/newton/newton-20261013-def456.json"]
    assert store.digest("models/newton/newton-20261012-abc123.json") is None
    assert store.keys("promotions/") == ["promotions/2026-10-12.json"]
