"""Buffered Parquet recorder.

Rows are buffered per partition ``<layer>/date=<YYYY-MM-DD>/feed=<feed>/symbol=<symbol>``
and written as new part files when a partition reaches ``flush_rows``,
when ``flush_seconds`` have passed since its last write, or on close.
Each part file is written to a temporary name and renamed, so a process crash
never leaves a half-written part file (this does not fsync; a power loss can).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

# Part files are named ``part-<HHMMSS>-<id>.parquet``; archiving merges them.
PART_PREFIX = "part-"


@dataclass
class _Partition:
    schema: pa.Schema
    rows: list[dict[str, Any]] = field(default_factory=list)
    last_flush: float = field(default_factory=time.monotonic)


class ParquetRecorder:
    def __init__(self, data_dir: Path, flush_rows: int, flush_seconds: float) -> None:
        self._data_dir = data_dir
        self._flush_rows = flush_rows
        self._flush_seconds = flush_seconds
        self._partitions: dict[Path, _Partition] = {}

    def record(
        self, layer: str, day: date, feed: str, symbol: str, row: dict[str, Any], schema: pa.Schema
    ) -> None:
        path = self._data_dir / layer / f"date={day.isoformat()}" / f"feed={feed}" / f"symbol={symbol}"
        part = self._partitions.setdefault(path, _Partition(schema))
        part.rows.append(row)
        if len(part.rows) >= self._flush_rows:
            self._write(path, part)

    def flush_due(self) -> None:
        now = time.monotonic()
        for path, part in self._partitions.items():
            if part.rows and now - part.last_flush >= self._flush_seconds:
                self._write(path, part)

    def flush(self) -> None:
        """Write every buffered partition; if some fail, still try the rest, then raise."""
        errors: list[Exception] = []
        for path, part in self._partitions.items():
            if part.rows:
                try:
                    self._write(path, part)
                except Exception as e:
                    errors.append(e)
        if errors:
            raise ExceptionGroup("some partitions could not be written", errors)

    def close(self) -> None:
        self.flush()

    def _write(self, path: Path, part: _Partition) -> None:
        path.mkdir(parents=True, exist_ok=True)
        name = f"{PART_PREFIX}{time.strftime('%H%M%S')}-{uuid.uuid4().hex[:8]}.parquet"
        tmp = path / f".{name}.tmp"
        pq.write_table(pa.Table.from_pylist(part.rows, schema=part.schema), tmp)
        tmp.rename(path / name)
        part.rows = []
        part.last_flush = time.monotonic()
