"""Archiving recorded days to the Object Store.

During the day the recorder writes many small part files per partition. Archiving
merges each partition's part files into one ``data.parquet`` with large row groups,
streaming so a big day never sits in memory, and uploads the partition's final files
(``data.parquet``, the backfill's ``bars.parquet``) under the same layout.

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
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from krx_quant.config import Settings, SettingsError
from krx_quant.object_store import ObjectStore, file_digest, open_store
from krx_quant.recorder import PART_PREFIX

log = logging.getLogger(__name__)

MERGED = "data.parquet"
_PARTS_KEY = b"krx_quant.parts"
_UPLOADED = ".uploaded.json"
_STALE_TMP_SECONDS = 600.0
# Rows per row group in a merged file; the recorder's parts hold a few thousand at most.
_ROW_GROUP_ROWS = 256_000


@dataclass
class ArchiveReport:
    uploaded: int = 0
    unchanged: int = 0
    failed: list[str] = field(default_factory=list)


async def archive(
    data_dir: Path, store: ObjectStore, *, before: date | None = None, recheck: bool = False
) -> ArchiveReport:
    """Merge and upload every partition of every recorded day, or of the days before ``before``.

    A partition that fails is logged and reported, and the others go on.
    """
    return await asyncio.to_thread(_archive, data_dir, store, before, recheck)


async def run_archive(settings: Settings, *, recheck: bool = False) -> ArchiveReport:
    """One archive run outside the collector (``krx-quant archive``)."""
    store = open_store(settings)
    if store is None:
        raise SettingsError("KRX_OBJECT_STORE is not set")
    return await archive(settings.data_dir, store, recheck=recheck)


def _archive(data_dir: Path, store: ObjectStore, before: date | None, recheck: bool) -> ArchiveReport:
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / ".archive.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        report = ArchiveReport()
        for partition in _partitions(data_dir, before):
            try:
                _clean_tmp(partition)
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
        return report


def _partitions(data_dir: Path, before: date | None) -> Iterator[Path]:
    """``<layer>/date=<day>/feed=<feed>/symbol=<symbol>`` directories, in order."""
    for layer in sorted(p for p in data_dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
        for day_dir in sorted(layer.glob("date=*")):
            try:
                day = date.fromisoformat(day_dir.name.removeprefix("date="))
            except ValueError:
                log.warning("skipping %s: not a date folder", day_dir)
                continue
            if before is None or day < before:
                yield from sorted(p for p in day_dir.glob("feed=*/symbol=*") if p.is_dir())


def _clean_tmp(partition: Path) -> None:
    for tmp in partition.glob(".*.tmp"):
        if time.time() - tmp.stat().st_mtime > _STALE_TMP_SECONDS:
            log.info("removing leftover %s", tmp)
            tmp.unlink(missing_ok=True)


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
        schema = pq.read_schema(sources[0]).remove_metadata()
        names = held + [p.name for p in new]
        tmp = partition / f".{MERGED}.{uuid.uuid4().hex[:8]}.tmp"
        with pq.ParquetWriter(tmp, schema.with_metadata({_PARTS_KEY: json.dumps(names)})) as writer:
            pending: list[pa.RecordBatch] = []
            for source in sources:
                for batch in pq.ParquetFile(source).iter_batches():
                    pending.append(batch)
                    if sum(b.num_rows for b in pending) >= _ROW_GROUP_ROWS:
                        writer.write_table(pa.Table.from_batches(pending), row_group_size=_ROW_GROUP_ROWS)
                        pending = []
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
