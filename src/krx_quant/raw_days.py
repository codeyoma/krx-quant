"""Reading a raw day back from the Object Store once the archive pruned it on the server (#607).

The archive keeps a day's ``raw`` partitions on the server for ``archive.RAW_KEEP_DAYS`` calendar
days. After that it deletes each partition's merged file once the store's copy, downloaded, has its
SHA-256, and leaves a mark in the partition's folder (``archive.PRUNED``: the key, SHA-256 and size).
The folders stay, so what was recorded is still listed; the rows are only in the store. Reading a
pruned partition as if it were there fails (``archive.RawMissing``): no reader finds the day empty.

``RawDays`` is where the readers of raw rows (``recording_quality``, ``night_probe``, ``gaps_probe``,
``night_book``) find a day's feed: the data directory's own folder while none of its partitions is
pruned, else a copy in a temporary directory, with the layout of the data directory: the pruned
partitions downloaded from the store, each checked against its mark's SHA-256, and the others linked
to the data directory's. Without a store, or when the store's copy is missing or differs, it raises
``RawMissing`` and says which. It keeps the copies of ``DAYS`` days at most, dropping the one asked of
longest ago, so a reader that reads every day in turn holds two days' downloads on disk; all go when it
closes.

The other readers of the data directory read ``events``, ``gaps``, ``bars`` and ``macro``, which the
archive never deletes: the engine, replay, training and the sample store, re-scoring. ``doctor`` reads
today's raw PINGPONGs only, and the sample-key probe (#565) only lists the latest day's part files,
which a pruned partition never has.
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import date
from pathlib import Path
from types import TracebackType

from krx_quant.archive import MERGED, PRUNED, RAW, RawMissing, pruned_mark
from krx_quant.object_store import ObjectStore, file_digest

DAYS = 2  # days whose copies are kept at once: a night is read from two days' partitions
# The collector's raw partitions of WebSocket frames that are not a feed's data (``collector.py``):
# KIS's control frames (PINGPONG, the replies to registrations) and the frames it could not parse.
SYSTEM, UNPARSED = "_system", "_unparsed"


class RawDays:
    """A data directory's raw days, the pruned ones read back from ``store`` (module doc)."""

    def __init__(self, data_dir: Path, store: ObjectStore | None = None, *, days: int = DAYS) -> None:
        self.data_dir = data_dir
        self._store = store
        self._days = days
        self._tmp: tempfile.TemporaryDirectory[str] | None = None
        self._copies: dict[tuple[date, str], Path] = {}  # in the order made
        self.restored = 0  # partitions downloaded so far

    def __enter__(self) -> RawDays:
        return self

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None
    ) -> None:
        self.close()

    def close(self) -> None:
        """Remove the copies."""
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None
        self._copies.clear()

    def feeds(self, day: date) -> list[str]:
        """The feeds recorded raw on ``day`` (their folders, pruned or not), sorted."""
        root = self.data_dir / RAW / f"date={day.isoformat()}"
        return sorted(p.name.removeprefix("feed=") for p in root.glob("feed=*") if p.is_dir())

    def feed(self, day: date, feed: str) -> Path:
        """The folder of ``day``'s ``feed`` partitions to read (``raw/date=<day>/feed=<feed>``): the
        data directory's own while none of them is pruned (or there is none), else a copy with the
        pruned ones downloaded from the store. ``RawMissing`` if a pruned one cannot be read back."""
        local = self.data_dir / RAW / f"date={day.isoformat()}" / f"feed={feed}"
        partitions = sorted(p for p in local.glob("symbol=*") if p.is_dir())
        pruned = [p for p in partitions if (p / PRUNED).is_file()]
        if not pruned:
            return local
        if (copy := self._copies.get((day, feed))) is not None:
            return copy
        where = local.relative_to(self.data_dir).as_posix()
        if self._store is None:
            raise RawMissing(
                f"{where}: {len(pruned)} of its {len(partitions)} partitions were pruned on the server"
                " (#607), and no Object Store is set to read them back from (KRX_OBJECT_STORE)"
            )
        self._make_room(day)
        target = self._root() / RAW / f"date={day.isoformat()}" / f"feed={feed}"
        target.mkdir(parents=True)
        try:
            for partition in partitions:
                into = target / partition.name
                if partition in pruned:
                    _restore(partition, into, self._store)
                    self.restored += 1
                else:
                    into.symlink_to(partition.resolve(), target_is_directory=True)
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise
        self._copies[(day, feed)] = target
        return target

    def _root(self) -> Path:
        if self._tmp is None:
            self._tmp = tempfile.TemporaryDirectory(prefix="krx-quant-raw-")
        return Path(self._tmp.name)

    def _make_room(self, day: date) -> None:
        """Drop the copies of the day asked of longest ago while ``day``'s would make too many days."""
        held = list(dict.fromkeys(d for d, _ in self._copies))
        while day not in held and len(held) >= self._days:
            gone = held.pop(0)
            for key in [k for k in self._copies if k[0] == gone]:
                shutil.rmtree(self._copies.pop(key), ignore_errors=True)


def _restore(partition: Path, into: Path, store: ObjectStore) -> None:
    """Download the merged file ``partition``'s mark is of into ``into``, checked against its SHA-256."""
    mark = pruned_mark(partition)
    if store.digest(mark.key) is None:
        raise RawMissing(f"{mark.key}: pruned on the server (#607), and {store.name} has no copy of it")
    into.mkdir()
    path = into / MERGED
    store.get(mark.key, path)
    got = file_digest(path)
    if got != mark.sha256:
        raise RawMissing(
            f"{mark.key}: pruned on the server (#607), and the copy in {store.name} is not the file pruned"
            f" (SHA-256 {got[:12]}, the file's {mark.sha256[:12]})"
        )
