"""Disk Watch: a Telegram alert when the disk under the data or the state folder runs low (#379).

The collector reads the free space under ``settings.data_dir`` and ``settings.state_dir`` at
start and every ``CHECK_SECONDS`` (a disk both folders are on is read once, under the first).
When less than ``settings.disk_min_free_percent`` of a disk is free it raises an alert, once. The
alert clears once at least ``RECOVER_POINTS`` percentage points more are free, so a disk that
hovers at the threshold does not alert at every check. The disks alerted are kept in the state
folder (``STATE_FILE``): a restart (each trading day at 07:20, each deploy) does not alert again
while a disk stays low, and still says when it recovers.

The free space is ``shutil.disk_usage``'s, as ``krx-quant doctor`` shows it (section 6): what a
user other than root may still write. On the server what fills the disk is mostly Docker's build
cache and images, which the engine cannot see from its container; ``deploy/auto-update.sh``
prunes them after each deploy and reports what is left (docs/operations.md 6장 "디스크가 찬다").
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from krx_quant.notify import Alerts

log = logging.getLogger(__name__)

CHECK_SECONDS = 600.0
# How far above the threshold the free space must come back for the alert to clear, in
# percentage points of the disk.
RECOVER_POINTS = 2.0
STATE_FILE = "disk_alert.json"
# The folders watched, by the key they are kept and alerted under.
LABELS = {"data": "데이터 폴더", "state": "상태 폴더"}
_GB = 1024**3  # as doctor shows it


class Usage(Protocol):
    """What ``shutil.disk_usage`` gives, as far as it is read here."""

    @property
    def total(self) -> int: ...

    @property
    def free(self) -> int: ...


@dataclass(frozen=True)
class Reading:
    """The free space of one disk, read under the folder ``name`` (a key of ``LABELS``)."""

    name: str
    path: Path
    free: int
    total: int

    @property
    def percent(self) -> float:
        return 100 * self.free / self.total

    def describe(self) -> str:
        return f"{self.free / _GB:.1f} GB({self.percent:.1f}%)"


def read_disks(dirs: Mapping[str, Path], usage: Callable[[Path], Usage] = shutil.disk_usage) -> list[Reading]:
    """Each disk under ``dirs`` once, under the first folder on it. A folder not there yet holds
    nothing to fill and is skipped; one that cannot be read is skipped and logged."""
    seen: set[int] = set()
    readings: list[Reading] = []
    for name, path in dirs.items():
        try:
            device = os.stat(path).st_dev
            if device in seen:
                continue
            found = usage(path)
        except FileNotFoundError:
            continue
        except OSError as e:
            log.warning("could not read the free space under %s: %s", path, e)
            continue
        seen.add(device)
        if found.total > 0:
            readings.append(Reading(name, path, found.free, found.total))
    return readings


def alerted(state_dir: Path) -> list[str]:
    """The folders whose disk is alerted as low, as kept in the state folder (none if the file
    is missing or unreadable)."""
    try:
        low = json.loads((state_dir / STATE_FILE).read_text())["low"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    return sorted(name for name in low if name in LABELS) if isinstance(low, list) else []


class DiskWatch:
    def __init__(
        self,
        dirs: Mapping[str, Path],
        min_free_percent: float,
        alerts: Alerts,
        state_dir: Path,
        usage: Callable[[Path], Usage] = shutil.disk_usage,
    ) -> None:
        self._dirs = dict(dirs)
        self._min = min_free_percent
        self._alerts = alerts
        self._state_dir = state_dir
        self._usage = usage
        self._low = set(alerted(state_dir))
        for name in self._low:  # alerted before a restart: its recovery is still told
            alerts.mark(_key(name))

    def check(self) -> list[Reading]:
        """Read the disks once and alert on them; what was read."""
        readings = read_disks(self._dirs, self._usage)
        for r in readings:
            where = f"{LABELS[r.name]} {r.path}"
            if r.percent < self._min and r.name not in self._low:
                self._alerts.raise_(
                    _key(r.name),
                    f"서버 디스크의 여유가 {r.describe()}로 기준 {self._min:g}%보다 작습니다({where}). "
                    "무엇이 차지하는지 서버에서 `docker system df`와 `du -sh data/*/ state/*/`로 봅니다"
                    '(docs/operations.md 6장 "디스크가 찬다").',
                )
                self._low.add(r.name)
                self._save()
            elif r.name in self._low and r.percent >= self._min + RECOVER_POINTS:
                self._alerts.resolve(
                    _key(r.name),
                    f"서버 디스크의 여유가 {r.describe()}로 돌아왔습니다(기준 {self._min:g}%, {where}).",
                )
                self._low.discard(r.name)
                self._save()
        return readings

    def _save(self) -> None:
        """Keep the disks alerted; a disk too full to take the file is alerted again after a
        restart, which is fine."""
        path = self._state_dir / STATE_FILE
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"low": sorted(self._low)}))
            tmp.replace(path)
        except OSError as e:
            log.warning("could not keep the disk alert in %s: %s", path, e)


async def keep_disks(watch: DiskWatch, every: float = CHECK_SECONDS) -> None:
    """Check the disks now and every ``every`` seconds until cancelled; a failure is logged and
    the next check goes on."""
    while True:
        try:
            watch.check()
        except Exception:
            log.exception("could not check the free disk space")
        await asyncio.sleep(every)


def _key(name: str) -> str:
    return f"disk:{name}"
