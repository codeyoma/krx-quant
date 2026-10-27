"""The disk alert (#379): told once when the disk under the data or the state folder runs low,
cleared once it has room again, and not told again by a restart while it lasts.

Input: the free space a stand-in for ``shutil.disk_usage`` reports, the threshold, the state
folder. Output: the alerts sent, the disks kept as alerted, and ``doctor``'s line of them.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple, cast

import pytest

from krx_quant.config import Settings, SettingsError
from krx_quant.disk_watch import (
    RECOVER_POINTS,
    STATE_FILE,
    DiskWatch,
    Reading,
    alerted,
    keep_disks,
    read_disks,
)
from krx_quant.doctor import Status, report
from krx_quant.feeds.base import KST
from krx_quant.notify import Alerts

GB = 1024**3
KEYS = {"KIS_APP_KEY": "key", "KIS_APP_SECRET": "secret"}


class FakeUsage(NamedTuple):
    total: int
    free: int


class Disk:
    """A disk of 57 GB whose free space a test sets, counting its reads."""

    def __init__(self, free_percent: float = 50.0) -> None:
        self.total = 57 * GB
        self.free_percent = free_percent
        self.reads: list[Path] = []
        self.fails: OSError | None = None

    def __call__(self, path: Path) -> FakeUsage:
        self.reads.append(path)
        if self.fails is not None:
            raise self.fails
        return FakeUsage(self.total, int(self.total * self.free_percent / 100))


class Sent(Alerts):
    """Alerts without Telegram, keeping what they would send."""

    def __init__(self) -> None:
        super().__init__(cast(Any, None), None, None, "http://127.0.0.1:9")
        self.sent: list[str] = []

    def _send(self, text: str) -> None:
        self.sent.append(text)


def folders(tmp_path: Path) -> dict[str, Path]:
    dirs = {"data": tmp_path / "data", "state": tmp_path / "state"}
    for path in dirs.values():
        path.mkdir(exist_ok=True)
    return dirs


def test_a_low_disk_is_told_once_and_its_recovery_once_it_has_room_again(tmp_path: Path) -> None:
    disk, alerts = Disk(25.0), Sent()
    watch = DiskWatch(folders(tmp_path), 20.0, alerts, tmp_path / "state", disk)

    watch.check()
    disk.free_percent = 19.5
    watch.check()
    watch.check()

    assert len(alerts.sent) == 1, "once while it lasts"
    [low] = alerts.sent
    assert low.startswith("[장애] 서버 디스크의 여유가 11.1 GB(19.5%)로 기준 20%보다 작습니다(데이터 폴더 ")
    assert "docker system df" in low

    disk.free_percent = 20 + RECOVER_POINTS - 0.5  # back over the threshold, not by enough
    watch.check()
    assert len(alerts.sent) == 1, "a disk hovering at the threshold is not told at every check"

    disk.free_percent = 20 + RECOVER_POINTS + 1
    watch.check()
    watch.check()
    assert len(alerts.sent) == 2
    assert alerts.sent[1].startswith("[복구] 서버 디스크의 여유가 13.1 GB(23.0%)로 돌아왔습니다(기준 20%")

    disk.free_percent = 10.0
    watch.check()
    assert len(alerts.sent) == 3 and alerts.sent[2].startswith("[장애]"), "a new crossing is told again"


def test_a_restart_while_the_disk_is_low_does_not_tell_it_again_but_tells_its_recovery(
    tmp_path: Path,
) -> None:
    dirs, disk = folders(tmp_path), Disk(15.0)
    first = Sent()
    DiskWatch(dirs, 20.0, first, tmp_path / "state", disk).check()
    assert len(first.sent) == 1
    assert alerted(tmp_path / "state") == ["data"]

    restarted = Sent()  # the 07:20 restart, or a deploy
    watch = DiskWatch(dirs, 20.0, restarted, tmp_path / "state", disk)
    watch.check()
    assert restarted.sent == []

    disk.free_percent = 30.0
    watch.check()
    assert len(restarted.sent) == 1 and restarted.sent[0].startswith("[복구]")
    assert alerted(tmp_path / "state") == []


def test_folders_on_one_disk_are_read_once_and_a_missing_one_is_skipped(tmp_path: Path) -> None:
    disk = Disk()
    dirs = folders(tmp_path)

    readings = read_disks(dirs, disk)

    assert [r.name for r in readings] == ["data"], "data and state share the disk, as on the server"
    assert disk.reads == [dirs["data"]]
    assert read_disks({"data": tmp_path / "nowhere"}, disk) == []


def test_a_disk_that_cannot_be_read_is_skipped_and_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    disk, alerts = Disk(), Sent()
    disk.fails = PermissionError("denied")
    watch = DiskWatch(folders(tmp_path), 20.0, alerts, tmp_path / "state", disk)

    with caplog.at_level(logging.WARNING, logger="krx_quant.disk_watch"):
        assert watch.check() == []

    assert alerts.sent == []
    assert "could not read the free space under" in caplog.text


def test_a_state_file_that_cannot_be_read_counts_as_no_disk_alerted(tmp_path: Path) -> None:
    for text in ("", "not json", json.dumps({"low": "data"}), json.dumps({"low": ["data", "elsewhere"]})):
        (tmp_path / STATE_FILE).write_text(text)
        assert alerted(tmp_path) in ([], ["data"])
    assert alerted(tmp_path) == ["data"], "a folder it does not watch is dropped"
    assert alerted(tmp_path / "missing") == []


class Flaky(DiskWatch):
    """A watch whose first check fails."""

    calls = 0

    def check(self) -> list[Reading]:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("a bug in one check")
        return super().check()


async def told(watch: DiskWatch, alerts: Sent, every: float) -> None:
    """Run the watch until it sends an alert."""
    task = asyncio.create_task(keep_disks(watch, every))
    try:
        async with asyncio.timeout(10):
            while not alerts.sent:
                await asyncio.sleep(0.01)
    finally:
        task.cancel()


async def test_the_watch_reads_the_disks_at_start(tmp_path: Path) -> None:
    alerts = Sent()

    await told(DiskWatch(folders(tmp_path), 20.0, alerts, tmp_path / "state", Disk(10.0)), alerts, 3600)

    assert alerts.sent[0].startswith("[장애] 서버 디스크의 여유가"), "not an hour later"


async def test_the_watch_goes_on_after_a_check_that_failed(tmp_path: Path) -> None:
    alerts = Sent()
    watch = Flaky(folders(tmp_path), 20.0, alerts, tmp_path / "state", Disk(10.0))

    await told(watch, alerts, 0)

    assert watch.calls >= 2 and alerts.sent[0].startswith("[장애]")


def threshold(env: dict[str, str]) -> float:
    return Settings.from_env({**KEYS, **env}, dotenv_path=None).disk_min_free_percent


def test_the_threshold_is_20_percent_unless_set_and_0_turns_it_off() -> None:
    assert threshold({}) == 20.0
    assert threshold({"KRX_DISK_MIN_FREE_PERCENT": "12.5"}) == 12.5
    assert threshold({"KRX_DISK_MIN_FREE_PERCENT": "0"}) == 0
    for bad in ("-1", "100", "a lot", ""):
        with pytest.raises(SettingsError, match="KRX_DISK_MIN_FREE_PERCENT"):
            Settings.from_env({**KEYS, "KRX_DISK_MIN_FREE_PERCENT": bad}, dotenv_path=None)
    assert Settings("", "").disk_min_free_percent == 0, "a collector built in a test never alerts"


def test_doctor_shows_the_threshold_and_the_disks_alerted_now(tmp_path: Path) -> None:
    settings = Settings(
        kis_app_key="",
        kis_app_secret="",
        data_dir=tmp_path / "data",
        state_dir=tmp_path / "state",
        disk_min_free_percent=20.0,
    )
    folders(tmp_path)
    now = datetime(2026, 10, 27, 10, 0, tzinfo=KST)

    text = report(settings, now, Status(None, "x"))
    assert (
        "  디스크 알림(KRX_DISK_MIN_FREE_PERCENT, #379): 여유가 20%보다 작으면 텔레그램 · 지금 알림 중: 없음"
        in text
    )

    DiskWatch(folders(tmp_path), 20.0, Sent(), settings.state_dir, Disk(5.0)).check()
    text = report(settings, now, Status(None, "x"))
    assert "지금 알림 중: 데이터 폴더" in text

    text = report(replace(settings, disk_min_free_percent=0), now, Status(None, "x"))
    assert "(KRX_DISK_MIN_FREE_PERCENT, #379): 꺼짐 · 지금 알림 중: 데이터 폴더" in text
