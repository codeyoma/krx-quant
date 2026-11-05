"""Archiving recorded days to the Object Store.

During the day the recorder writes many small part files per partition. Archiving
merges each partition's part files into one ``data.parquet`` with large row groups,
streaming so a big day never sits in memory, and uploads the partition's final files
(``data.parquet``, the backfill's ``bars.parquet``) under the same layout. The day candles
(``candles/period=<period>/symbol=<symbol>/candles.parquet``, ``candles``) are not by day:
they are uploaded on every run when changed.

Running it again is always safe and gives the same objects:

- the merged file lists, in its Parquet metadata, the part files it holds, and those
  parts are deleted only after it is written and synced to disk, so a part a crash left
  behind is never merged twice;
- parts written after a run (late rows of the day) are merged on the next run;
- each partition remembers, per store, what it uploaded (``.uploaded.json``), so an
  unchanged file is not uploaded again. ``recheck`` compares every file with the store
  instead, and uploads what the store lacks.

Leftover ``.tmp`` files older than ten minutes (a process died while writing) are
deleted; younger ones may still be in use. One run at a time holds a lock on the data
directory, so a manual run waits for the collector's scheduled one and the other way round.

**Raw past 30 days (#607).** The ``raw`` layer is most of the server's data and grows every day;
the store keeps it for good. So a run given its day (``today``) then deletes, under the same lock
and a partition at a time, the merged file of each ``raw`` partition of a day more than
``RAW_KEEP_DAYS`` calendar days before it that no pin holds (``RAW_PINS``), once the store's copy,
downloaded, has the local file's SHA-256: an upload's record (``.uploaded.json``) and the digest the
store keeps beside an object say what was sent, not what the store holds. Before the file goes, a
mark (``.pruned.json``: the key, SHA-256 and size) is written in its folder, which stays, so the
day's partitions are still listed. Reading a pruned partition here fails (``RawMissing``), never
reads as empty; ``raw_days.RawDays`` reads it back from the store. ``events``, ``gaps``, ``bars``,
``macro`` and the candles are never deleted.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from krx_quant.config import Settings, SettingsError
from krx_quant.feeds.base import KST
from krx_quant.object_store import ObjectStore, file_digest, open_store
from krx_quant.recorder import PART_PREFIX

log = logging.getLogger(__name__)

# The collector archives each day again at this time (KST), after the NXT and KRX
# after-market close and the 20:30 backfill: the day's bars are in the store from then on.
NIGHTLY_ARCHIVE = clock_time(21, 0)
MERGED = "data.parquet"
_PARTS_KEY = b"krx_quant.parts"
_UPLOADED = ".uploaded.json"
_STALE_TMP_SECONDS = 600.0
# Rows per row group in a merged file; the recorder's parts hold a few thousand at most.
_ROW_GROUP_ROWS = 256_000
# Rows per batch read from a partition (``partition_batches``), as arrow's default; a reader of
# wide rows over a whole day asks for fewer (#570).
BATCH_ROWS = 65_536

RAW = "raw"
# A raw day stays on the server this many calendar days after it, then goes once the store's copy is
# checked (#607): the nightly readers of raw read the last week's at most (the recording quality
# report's night and the nights before it, ``recording_quality``), and an older day is read back from
# the store (``raw_days``). Thirty days are about 3 GB of the server's disk (2.4 GB of 3.3 GB on
# 2026-10-27 was raw).
RAW_KEEP_DAYS = 30
PRUNED = ".pruned.json"  # in a pruned raw partition's folder: what was deleted, and its SHA-256
_CHECK = "check.tmp"  # the store's copy downloaded beside the file it is checked against


@dataclass(frozen=True)
class Pin:
    """Raw days the archive keeps on the server past ``RAW_KEEP_DAYS``, ``first`` to ``last``, for an
    investigation of ``issue`` not finished: ``why``."""

    first: date
    last: date
    issue: int
    why: str

    def holds(self, day: date) -> bool:
        return self.first <= day <= self.last


# The pinned raw days. An agent adds one in a PR when an open issue needs a day older than
# ``RAW_KEEP_DAYS`` on the server's own disk (a reader of raw needs none: ``raw_days.RawDays`` reads a
# pruned day back from the store), and the PR that closes the issue takes it out. None now: the
# sample-key probes (#565) read the latest stored day's partitions and no raw ones as inputs, and the
# night book's measure (#605) reads the nights before its 2026-11-20 verdict, within 30 days.
RAW_PINS: tuple[Pin, ...] = ()


class RawMissing(Exception):
    """Raw rows asked for that are not on the server: the archive pruned their partition (#607), and
    they were read as if they were there (``partition_batches``), or could not be read back from the
    store (``raw_days.RawDays``)."""


@dataclass(frozen=True)
class Pruned:
    """A pruned raw partition's mark (``PRUNED``): its merged file's key in the store, SHA-256 and
    size, and when and against which store it was checked."""

    key: str
    sha256: str
    size: int
    at: str
    store: str


@dataclass
class ArchiveReport:
    uploaded: int = 0
    unchanged: int = 0
    failed: list[str] = field(default_factory=list)
    # The raw partitions (#607): deleted on the server this run and their bytes; those on the server
    # after it and theirs; of those, the pinned ones, and the ones past ``RAW_KEEP_DAYS`` kept, with why.
    pruned: int = 0
    pruned_bytes: int = 0
    raw_left: int = 0
    raw_left_bytes: int = 0
    pinned: int = 0
    not_pruned: dict[str, str] = field(default_factory=dict)

    def raw_line(self) -> str:
        """What the run deleted of raw and what is left, for the log and ``krx-quant archive``."""
        return (
            f"raw on the server (#607): pruned {self.pruned} partitions, {_mb(self.pruned_bytes)}; "
            f"left {self.raw_left} partitions, {_mb(self.raw_left_bytes)} "
            f"({self.pinned} pinned, {len(self.not_pruned)} past {RAW_KEEP_DAYS} days kept)"
        )


def _mb(size: int) -> str:
    return f"{size / 1e6:.1f} MB"


async def archive(
    data_dir: Path,
    store: ObjectStore,
    *,
    before: date | None = None,
    recheck: bool = False,
    today: date | None = None,
    pins: Sequence[Pin] = RAW_PINS,
) -> ArchiveReport:
    """Merge and upload every partition of every recorded day, or of the days before ``before``;
    then, given ``today`` (KST), prune the raw partitions more than ``RAW_KEEP_DAYS`` before it that
    none of ``pins`` holds, each once the store's copy is checked (module doc).

    A partition that fails is logged and reported, and the others go on.
    """
    return await asyncio.to_thread(_archive, data_dir, store, before, recheck, today, pins)


async def run_archive(settings: Settings, *, recheck: bool = False) -> ArchiveReport:
    """One archive run outside the collector (``krx-quant archive``)."""
    store = open_store(settings)
    if store is None:
        raise SettingsError("KRX_OBJECT_STORE is not set")
    return await archive(settings.data_dir, store, recheck=recheck, today=datetime.now(KST).date())


def _archive(
    data_dir: Path,
    store: ObjectStore,
    before: date | None,
    recheck: bool,
    today: date | None,
    pins: Sequence[Pin],
) -> ArchiveReport:
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / ".archive.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        report = ArchiveReport()
        for partition in _partitions(data_dir, before):
            try:
                _clean_tmp(partition)
                if (partition / PRUNED).is_file():
                    _check_pruned(partition)
                    continue
                _merge(partition)
                for path in sorted(partition.glob("*.parquet")):
                    if path.name.startswith(PART_PREFIX):
                        continue
                    if _upload(data_dir, path, store, recheck):
                        report.uploaded += 1
                    else:
                        report.unchanged += 1
            except Exception:
                log.exception("could not archive %s", partition)
                report.failed.append(partition.relative_to(data_dir).as_posix())
        if today is not None:
            _prune_raw(data_dir, store, today, pins, report)
            log.info("%s", report.raw_line())
        return report


def _check_pruned(partition: Path) -> None:
    """A pruned partition is not merged or uploaded again: rows written into it since (none can be: a
    day is recorded on that day) would make a merged file without the pruned rows, which would replace
    the store's copy. Raises so the run reports it."""
    if any(partition.glob(f"{PART_PREFIX}*.parquet")):
        raise RuntimeError(f"rows were recorded into {partition} after it was pruned (#607): not merged")


# --- raw past ``RAW_KEEP_DAYS`` (#607) --------------------------------------------------------------


def _prune_raw(
    data_dir: Path, store: ObjectStore, today: date, pins: Sequence[Pin], report: ArchiveReport
) -> None:
    """Prune each raw partition of a day before ``today - RAW_KEEP_DAYS`` that no pin holds, one at a
    time; count those left and their bytes."""
    oldest = today - timedelta(days=RAW_KEEP_DAYS)  # the oldest day kept
    for day, partition in _raw_partitions(data_dir):
        name = partition.relative_to(data_dir).as_posix()
        size = 0
        try:
            size = sum(p.stat().st_size for p in partition.glob("*.parquet"))  # its rows' files
            if (partition / PRUNED).is_file():
                if (partition / MERGED).is_file():  # a run stopped between the mark and the delete
                    _finish_pruning(partition, name)
                continue  # rows recorded into it since are the run's failure (``_check_pruned``)
            if not size or day >= oldest:
                why = ""
            elif any(pin.holds(day) for pin in pins):
                report.pinned += 1
                why = ""
            else:
                why = _prune(data_dir, partition, store) or ""
                if not why:
                    log.debug("pruned %s (%s): its copy in %s has its SHA-256", name, _mb(size), store.name)
                    report.pruned += 1
                    report.pruned_bytes += size
                    continue
        except Exception as e:
            log.exception("could not prune %s", name)
            why = f"could not read or check it ({type(e).__name__})"
        if why:
            log.warning("not pruning %s: %s", name, why)
            report.not_pruned[name] = why
        report.raw_left += 1
        report.raw_left_bytes += size


def _raw_partitions(data_dir: Path) -> Iterator[tuple[date, Path]]:
    """Every ``raw/date=<day>/feed=<feed>/symbol=<symbol>`` folder with its day, in order."""
    for day_dir in sorted((data_dir / RAW).glob("date=*")):
        try:
            day = date.fromisoformat(day_dir.name.removeprefix("date="))
        except ValueError:
            continue
        for partition in sorted(p for p in day_dir.glob("feed=*/symbol=*") if p.is_dir()):
            yield day, partition


def _prune(data_dir: Path, partition: Path, store: ObjectStore) -> str | None:
    """Delete ``partition``'s merged file once the store's copy, downloaded, has its SHA-256, after a
    mark of it; None if it went, else why not."""
    merged = partition / MERGED
    if partition_files(partition) != [merged]:
        return "part files not merged yet"
    if any(partition.glob(".*.tmp")):
        return "a file is being written in it"
    stat = merged.stat()
    local = file_digest(merged)
    key = merged.relative_to(data_dir).as_posix()
    if store.digest(key) is None:
        return f"{store.name} has no copy"
    copy = partition / f".{MERGED}.{uuid.uuid4().hex[:8]}.{_CHECK}"
    try:
        store.get(key, copy)
        stored = file_digest(copy)
    finally:
        copy.unlink(missing_ok=True)
    if stored != local:
        return f"the copy in {store.name} differs (SHA-256 {stored[:12]}, the file's {local[:12]})"
    now = merged.stat()
    if (now.st_size, now.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
        return "it changed while it was checked"
    mark = {
        "key": key,
        "sha256": local,
        "size": stat.st_size,
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "store": store.name,
    }
    tmp = partition / f"{PRUNED}.{uuid.uuid4().hex[:8]}.tmp"
    tmp.write_text(json.dumps(mark))
    _fsync(tmp)
    tmp.replace(partition / PRUNED)
    _fsync(partition)  # the mark is on disk before the file goes
    merged.unlink()
    (partition / _UPLOADED).unlink(missing_ok=True)
    _fsync(partition)
    return None


def _finish_pruning(partition: Path, name: str) -> None:
    """Delete the merged file a run left beside its mark, if it is the file the mark is of."""
    merged = partition / MERGED
    if file_digest(merged) != pruned_mark(partition).sha256:
        log.error("%s has a prune mark and a file it is not of: left as it is", name)
        return
    merged.unlink()
    (partition / _UPLOADED).unlink(missing_ok=True)
    _fsync(partition)
    log.info("pruned %s: a run before stopped between its mark and the delete", name)


def pruned_mark(partition: Path) -> Pruned:
    """The mark of a pruned raw partition (``PRUNED``); ``RawMissing`` if it cannot be read."""
    try:
        data = json.loads((partition / PRUNED).read_text())
        return Pruned(
            str(data["key"]), str(data["sha256"]), int(data["size"]), str(data["at"]), str(data["store"])
        )
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise RawMissing(
            f"{partition}: pruned on the server (#607), and its mark cannot be read ({e})"
        ) from e


def _pruned(partition: Path) -> RawMissing:
    return RawMissing(
        f"{partition}: pruned on the server (#607), its rows are only in the Object Store;"
        " read it through raw_days.RawDays"
    )


def _partitions(data_dir: Path, before: date | None) -> Iterator[Path]:
    """``<layer>/date=<day>/feed=<feed>/symbol=<symbol>`` directories, in order, then the
    candles' ``candles/period=<period>/symbol=<symbol>`` (only finished candles are stored)."""
    for layer in sorted(p for p in data_dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
        for day_dir in sorted(layer.glob("date=*")):
            try:
                day = date.fromisoformat(day_dir.name.removeprefix("date="))
            except ValueError:
                log.warning("skipping %s: not a date folder", day_dir)
                continue
            if before is None or day < before:
                yield from sorted(p for p in day_dir.glob("feed=*/symbol=*") if p.is_dir())
    yield from sorted(p for p in (data_dir / "candles").glob("period=*/symbol=*") if p.is_dir())


def _clean_tmp(partition: Path) -> None:
    for tmp in partition.glob(".*.tmp"):
        if time.time() - tmp.stat().st_mtime > _STALE_TMP_SECONDS:
            log.info("removing leftover %s", tmp)
            tmp.unlink(missing_ok=True)


def _conform(batch: pa.RecordBatch, schema: pa.Schema) -> pa.RecordBatch:
    """``batch`` with ``schema``'s columns in its order, those it lacks all null."""
    columns = [
        batch.column(f.name) if f.name in batch.schema.names else pa.nulls(batch.num_rows, f.type)
        for f in schema
    ]
    return pa.RecordBatch.from_arrays(columns, schema=schema)


def _merge(partition: Path) -> None:
    # In the order written: several parts can share the second in their names.
    parts = sorted(partition.glob(f"{PART_PREFIX}*.parquet"), key=lambda p: (p.stat().st_mtime_ns, p.name))
    if not parts:
        return
    merged = partition / MERGED
    held: list[str] = _held_parts(merged) if merged.exists() else []
    new = [p for p in parts if p.name not in held]
    if new:
        sources = ([merged] if merged.exists() else []) + new
        # A column added to a feed (and missing from files recorded before) is null there.
        schema = pa.unify_schemas([pq.read_schema(s).remove_metadata() for s in sources])
        names = held + [p.name for p in new]
        tmp = partition / f".{MERGED}.{uuid.uuid4().hex[:8]}.tmp"
        with pq.ParquetWriter(tmp, schema.with_metadata({_PARTS_KEY: json.dumps(names)})) as writer:
            pending: list[pa.RecordBatch] = []
            pending_rows = 0
            for source in sources:
                for batch in pq.ParquetFile(source).iter_batches():
                    pending.append(_conform(batch, schema))
                    pending_rows += batch.num_rows
                    if pending_rows >= _ROW_GROUP_ROWS:
                        writer.write_table(pa.Table.from_batches(pending), row_group_size=_ROW_GROUP_ROWS)
                        pending, pending_rows = [], 0
            if pending:
                writer.write_table(pa.Table.from_batches(pending), row_group_size=_ROW_GROUP_ROWS)
        _fsync(tmp)
        tmp.replace(merged)
        _fsync(partition)  # the rename is on disk before the parts go
    for part in parts:
        part.unlink(missing_ok=True)


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def partition_files(partition: Path) -> list[Path]:
    """The files holding a partition's rows, each row once: the merged file, then the part
    files it does not hold, in the order written.
    """
    merged = partition / MERGED
    held = set(_held_parts(merged)) if merged.exists() else set()
    parts = sorted(partition.glob(f"{PART_PREFIX}*.parquet"), key=lambda p: (p.stat().st_mtime_ns, p.name))
    return ([merged] if merged.exists() else []) + [p for p in parts if p.name not in held]


def partition_batches(
    partition: Path,
    columns: Sequence[str] | Callable[[Path, pa.Schema], Sequence[str] | None],
    batch_rows: int = BATCH_ROWS,
) -> Iterator[tuple[Path, pa.RecordBatch]]:
    """A partition's rows, a batch at a time, each row once (``partition_files``), with the file
    each batch is from: for reading beside the collector, whose archive runs merge the parts.

    ``columns`` are the columns read; or, called with each file and its schema, it gives the
    columns read from that file, None to leave the file out. A batch holds ``batch_rows`` rows at
    most. A file merged away while the partition is read, or read as it was written, is left out
    and the rest still counts; the batches it gave before stay given. A partition the archive pruned
    (#607), before or while it is read, raises ``RawMissing``: its rows are not there to count. The
    mark is written before the file goes, so it is looked for before the files are listed, after (a
    prune between the two leaves the listing empty), on a file gone and once they are read; and a
    listing found empty while the upload record (``.uploaded.json``) names the merged file raises too.
    """
    _check_not_pruned(partition)
    files = partition_files(partition)
    _check_not_pruned(partition)
    if not files and _merged_was_stored(partition):
        raise RawMissing(f"{partition}: its upload record says {MERGED} was stored, and it is not there")
    for path in files:
        try:
            chosen = columns(path, pq.read_schema(path)) if callable(columns) else columns
            if chosen is None:
                continue
            for batch in pq.ParquetFile(path).iter_batches(batch_size=batch_rows, columns=list(chosen)):
                yield path, batch
        except (FileNotFoundError, pa.ArrowInvalid, OSError):
            _check_not_pruned(partition)
            continue  # merged away in between, or written as it was read: the rest still counts
    _check_not_pruned(partition)


def _check_not_pruned(partition: Path) -> None:
    if (partition / PRUNED).is_file():
        raise _pruned(partition)


def _merged_was_stored(partition: Path) -> bool:
    """Whether the partition's upload record names its merged file, for any store."""
    try:
        marks = json.loads((partition / _UPLOADED).read_text())
    except (OSError, ValueError):
        return False
    return isinstance(marks, dict) and any(isinstance(m, dict) and MERGED in m for m in marks.values())


def _held_parts(merged: Path) -> list[str]:
    metadata = pq.read_schema(merged).metadata or {}
    names: list[str] = json.loads(metadata.get(_PARTS_KEY, b"[]"))
    return names


def _upload(data_dir: Path, path: Path, store: ObjectStore, recheck: bool) -> bool:
    """Upload ``path`` unless the store already has its content; True if uploaded."""
    marks_path = path.parent / _UPLOADED
    try:
        marks: dict[str, dict[str, dict[str, int | str]]] = json.loads(marks_path.read_text())
    except (OSError, ValueError):
        marks = {}
    stat = path.stat()
    mark = marks.get(store.name, {}).get(path.name)
    unchanged = mark is not None and (mark.get("size"), mark.get("mtime_ns")) == (
        stat.st_size,
        stat.st_mtime_ns,
    )
    if unchanged and not recheck:
        return False
    digest = file_digest(path)
    key = path.relative_to(data_dir).as_posix()
    uploaded = store.digest(key) != digest
    if uploaded:
        store.put(key, path, digest)
    marks.setdefault(store.name, {})[path.name] = {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": digest,
    }
    tmp = marks_path.with_name(f"{_UPLOADED}.tmp")
    tmp.write_text(json.dumps(marks))
    tmp.replace(marks_path)
    return uploaded
