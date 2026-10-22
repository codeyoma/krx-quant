"""Log files in the state folder (#356): a file a day per kind of process, kept within days and a
cap, the engine's children's output in theirs, and the masked bundle an agent is handed.

The secret-looking values here are put together at run time, so the repository's own check
(``scripts/check_forbidden.py``) does not find them in this file.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tarfile
from collections.abc import Iterator
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from functools import partial
from pathlib import Path

import aiohttp
import pytest

from krx_quant import cli
from krx_quant.cli import main
from krx_quant.clock import FakeClock
from krx_quant.config import DEFAULT_LOG_DAYS, DEFAULT_LOG_MAX_MB, Settings, SettingsError
from krx_quant.doctor import Status, report
from krx_quant.feeds.base import KST
from krx_quant.github import GitHub
from krx_quant.log_files import (
    BY_PARENT,
    FORMAT,
    KEEP_BUNDLES,
    MB,
    DayFile,
    Keep,
    day_logs,
    formatter,
    keep_logs,
    open_process_log,
    prune,
    run_logged,
)
from krx_quant.logs_bundle import Period, make_bundle
from krx_quant.market_calendar import MarketCalendar
from krx_quant.redaction import MASK, redact, redact_counted
from krx_quant.scheduled import Job, JobState, Scheduler, Target, run_command
from krx_quant.swap import _train
from tests.test_subscription_limit import until

TODAY = date(2026, 10, 23)
NIGHT = date(2026, 10, 22)
APP_KEY = "P" + "k" * 35
GITHUB_TOKEN = "github_pat_" + "B" * 22 + "_" + "C" * 59
BIND_ADDRESS = "100.101.102.103"
TAILNET = "krx-quant.tail" + "1234" + ".ts.net"
PRICE = "71234.5"  # market values: the bundle keeps them
VOLUME = "1234567"


def at(day: date, hhmm: str, seconds: float = 0.0) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm), KST) + timedelta(seconds=seconds)


def record(
    when: datetime, message: str, level: int = logging.INFO, name: str = "krx_quant.collector"
) -> logging.LogRecord:
    """A record made at ``when``: the fake clock's time, not the machine's."""
    made = logging.LogRecord(name, level, __file__, 1, message, None, None)
    made.created = when.timestamp()
    made.msecs = int((made.created - int(made.created)) * 1000) + 0.0
    return made


def lines(path: Path) -> list[str]:
    return path.read_text().splitlines()


def write(path: Path, text: str = "x\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# A file a day


def test_a_record_goes_to_its_kst_day_s_file_and_the_file_changes_at_midnight(tmp_path: Path) -> None:
    handler = DayFile(tmp_path / "engine")
    try:
        handler.handle(record(at(NIGHT, "23:59:59", 0.5), "before midnight"))
        handler.handle(record(at(TODAY, "00:00:00", 0.25), "after midnight", logging.WARNING))
        handler.handle(record(at(TODAY, "09:00:01"), "later the same day"))
    finally:
        handler.close()
    assert lines(tmp_path / "engine" / "2026-10-22.log") == [
        "2026-10-22 23:59:59,500 INFO krx_quant.collector: before midnight"
    ]
    assert lines(tmp_path / "engine" / "2026-10-23.log") == [
        "2026-10-23 00:00:00,250 WARNING krx_quant.collector: after midnight",
        "2026-10-23 09:00:01,000 INFO krx_quant.collector: later the same day",
    ]


def test_the_lines_are_the_stdout_lines_format_in_kst() -> None:
    assert FORMAT == "%(asctime)s %(levelname)s %(name)s: %(message)s"
    made = record(datetime(2026, 10, 22, 15, 0, 0, tzinfo=KST), "x")  # 06:00 UTC
    assert formatter().format(made) == "2026-10-22 15:00:00,000 INFO krx_quant.collector: x"


class Counted(DayFile):
    """A day file that counts the errors it would print."""

    errors = 0

    def handleError(self, record: logging.LogRecord) -> None:
        self.errors += 1


def test_a_file_it_cannot_write_is_said_once_and_the_next_day_tries_again(tmp_path: Path) -> None:
    blocked = write(tmp_path / "engine")  # a file where the folder should be
    handler = Counted(blocked)
    try:
        handler.handle(record(at(NIGHT, "10:00"), "one"))
        handler.handle(record(at(NIGHT, "10:01"), "two"))
        assert handler.errors == 1
        blocked.unlink()
        handler.handle(record(at(TODAY, "10:00"), "the next day"))
    finally:
        handler.close()
    assert lines(tmp_path / "engine" / "2026-10-23.log")[0].endswith("the next day")


# This process's own file


def values(tmp_path: Path, **more: str) -> dict[str, str]:
    return {
        "KRX_STATE_DIR": str(tmp_path / "state"),
        "KRX_LOG_DAYS": "14",
        "KRX_COMMIT": "abcdef0123456789",
        **more,
    }


def test_the_engine_writes_engine_and_a_command_writes_its_own_folder(tmp_path: Path) -> None:
    for command, folder in (("collect", "engine"), ("doctor", "commands/doctor")):
        files = open_process_log(command, [command], values(tmp_path))
        assert files is not None
        logging.getLogger("krx_quant.test").warning("a line of %s", command)
        files.close(0)
        (path,) = (tmp_path / "state" / "logs" / folder).iterdir()
        start, line, end = lines(path)
        assert start.endswith(f"krx-quant {command} started (pid {os.getpid()}, commit abcdef012345)")
        assert line.endswith(f"WARNING krx_quant.test: a line of {command}")
        assert " INFO krx_quant.log_files: " in end and f"krx-quant {command} ended: exit code 0 after" in end


def test_no_file_of_its_own_for_a_process_the_engine_started_nor_when_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert open_process_log("doctor", ["doctor"], values(tmp_path, KRX_LOG_DAYS="off")) is None
    monkeypatch.setenv(BY_PARENT, "1")
    assert open_process_log("doctor", ["doctor"], values(tmp_path)) is None
    assert not (tmp_path / "state").exists()


def test_a_command_s_file_ends_with_its_exit_code_and_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_LOG_DAYS", "14")
    monkeypatch.delenv("KRX_OBJECT_STORE", raising=False)
    assert main(["train"]) == 2
    assert "configuration error" in capsys.readouterr().err
    (path,) = (tmp_path / "state" / "logs" / "commands" / "train").iterdir()
    start, end = lines(path)
    assert f"INFO krx_quant.log_files: krx-quant train started (pid {os.getpid()}, commit " in start
    assert " ERROR krx_quant.log_files: krx-quant train ended: exit code 2 (configuration error: " in end


def test_a_command_ended_by_an_error_leaves_its_traceback_in_its_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_LOG_DAYS", "14")

    def broken(args: object) -> int:
        raise RuntimeError("no such thing")

    monkeypatch.setattr(cli, "_command", broken)
    with pytest.raises(RuntimeError):
        main(["doctor"])
    (path,) = (tmp_path / "state" / "logs" / "commands" / "doctor").iterdir()
    text = path.read_text()
    assert "ERROR krx_quant.log_files: krx-quant doctor ended by RuntimeError after" in text
    assert "Traceback (most recent call last):" in text and "RuntimeError: no such thing" in text
    assert not [h for h in logging.getLogger().handlers if isinstance(h, DayFile)], "the handler is gone"


# Retention


def kinds_of_days(root: Path, days: list[date], size: int = 10) -> None:
    for day in days:
        write(root / "engine" / f"{day}.log", "e" * size)
        write(root / "training" / f"{day}.log", "t" * size)
        write(root / "jobs" / "probe-x" / f"{day}.log", "j" * size)
        write(root / "commands" / "doctor" / f"{day}.log", "c" * size)


def test_files_older_than_the_days_kept_are_removed_and_said(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "logs"
    kinds_of_days(root, [TODAY - timedelta(days=n) for n in range(20)])
    write(root / "jobs" / "old-job" / "2026-10-01.log")
    write(root / "bundles" / "krx-logs-20261001-000000.tar.gz")  # not a day file: its own rule
    write(root / "engine" / "notes.txt")
    caplog.set_level(logging.INFO, "krx_quant.log_files")
    pruned = prune(root, Keep(14, 100 * MB), TODAY)
    left = {f.day for f in day_logs(root)}
    assert min(left) == TODAY - timedelta(days=13) and max(left) == TODAY  # 14 days, today's the first
    assert len(pruned.old) == 6 * 4 + 1 and pruned.over == ()
    assert not (root / "jobs" / "old-job").exists(), "a job's folder left empty goes too"
    assert (root / "bundles" / "krx-logs-20261001-000000.tar.gz").exists()
    assert (root / "engine" / "notes.txt").exists()
    (said,) = [r.getMessage() for r in caplog.records if "older than" in r.getMessage()]
    assert said.startswith("removed 25 log files older than 14 days (KRX_LOG_DAYS): ")
    assert "commands/doctor/2026-10-04.log" in said and "and 15 more" in said


def test_over_the_cap_the_oldest_go_first_but_never_today_s_or_yesterday_s(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "logs"
    kinds_of_days(root, [TODAY - timedelta(days=n) for n in range(4)], size=MB // 4)  # 1 MB a day
    pruned = prune(root, Keep(14, 2 * MB), TODAY)
    assert {f.day for f in pruned.over} == {TODAY - timedelta(days=3), TODAY - timedelta(days=2)}
    assert {f.day for f in day_logs(root)} == {TODAY, TODAY - timedelta(days=1)}
    assert not pruned.still_over
    assert any("to keep the logs within 2 MB (KRX_LOG_MAX_MB)" in r.getMessage() for r in caplog.records)
    # Today's and yesterday's alone over the cap: kept, and said.
    pruned = prune(root, Keep(14, MB), TODAY)
    assert pruned.over == () and pruned.still_over and len(day_logs(root)) == 8


async def test_the_engine_prunes_every_hour_and_says_once_a_day_what_it_cannot(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "logs"
    kinds_of_days(root, [TODAY, TODAY - timedelta(days=30)], size=MB)
    caplog.set_level(logging.INFO, "krx_quant.log_files")
    task = asyncio.create_task(keep_logs(root, Keep(14, MB), lambda: at(TODAY, "12:00"), every=0.01))
    try:
        await until(lambda: len(day_logs(root)) == 4)
        await until(lambda: any("alone take 4 MB" in r.getMessage() for r in caplog.records))
        await asyncio.sleep(0.05)  # more prunes: the warning stays one a day
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert sum("alone take" in r.getMessage() for r in caplog.records) == 1


def test_the_days_and_the_cap_are_settings() -> None:
    keys = {"KIS_APP_KEY": "k", "KIS_APP_SECRET": "s"}
    default = Settings.from_env(keys, dotenv_path=None)
    assert (default.log_days, default.log_max_mb) == (DEFAULT_LOG_DAYS, DEFAULT_LOG_MAX_MB) == (14, 500)
    assert Settings.from_env({**keys, "KRX_LOG_DAYS": "off"}, dotenv_path=None).log_days is None
    assert (
        Settings.from_env({**keys, "KRX_LOG_DAYS": "30", "KRX_LOG_MAX_MB": "50"}, dotenv_path=None).log_max_mb
        == 50
    )
    for bad, why in (
        ({"KRX_LOG_DAYS": "1"}, "KRX_LOG_DAYS must be at least 2"),
        ({"KRX_LOG_DAYS": "two weeks"}, "KRX_LOG_DAYS must be whole days or off"),
        ({"KRX_LOG_MAX_MB": "0"}, "KRX_LOG_MAX_MB must be at least 1"),
        ({"KRX_LOG_MAX_MB": "1.5"}, "KRX_LOG_MAX_MB must be whole MB"),
    ):
        with pytest.raises(SettingsError, match=why):
            Settings.from_env({**keys, **bad}, dotenv_path=None)


# A process the engine starts

CHILD = (
    "import logging, sys\n"
    f"logging.basicConfig(level=logging.INFO, format={FORMAT!r})\n"
    "print('a report line', flush=True)\n"
    "logging.getLogger('krx_quant.training').info('step %s done', 'newton')\n"
    "print('no newline at the end', end='', flush=True)\n"
    "sys.exit(3)\n"
)


async def test_a_child_s_output_lands_in_its_file_between_its_start_and_its_exit_code(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "jobs" / "probe-x" / "2026-10-22.log"
    ran = await run_logged([sys.executable, "-c", CHILD], timedelta(seconds=20), path)
    assert ran.exit_code == 3 and not ran.timed_out
    assert ran.stdout == "a report line\nno newline at the end"
    assert ran.stderr.endswith("INFO krx_quant.training: step newton done\n")
    first, *middle, last = lines(path)
    assert f" INFO krx_quant.log_files: {sys.executable} -c " in first and "started (pid " in first
    assert sorted(middle) == sorted(["a report line", "no newline at the end", ran.stderr.strip()])
    assert " ERROR krx_quant.log_files: " in last and "ended: exit code 3 after" in last


async def test_a_child_stopped_at_its_time_limit_says_so_in_its_file(tmp_path: Path) -> None:
    path = tmp_path / "night.log"
    script = "import time; print('started', flush=True); time.sleep(30)"
    ran = await run_logged([sys.executable, "-c", script], timedelta(seconds=1), path, merge=True)
    assert ran.timed_out and ran.stdout == "started\n"
    assert lines(path)[1] == "started"
    assert "stopped at its time limit (0:00:01): exit code -9 (SIGKILL) after" in lines(path)[-1]


async def test_a_child_is_told_the_engine_writes_its_output(tmp_path: Path) -> None:
    script = f"import os; print(os.environ.get({BY_PARENT!r}))"
    ran = await run_logged([sys.executable, "-c", script], timedelta(seconds=20))
    assert ran.stdout == "1\n"


@pytest.mark.timeout(60)  # the real command's imports (pyarrow, lightgbm) take a few seconds
async def test_the_nightly_training_s_whole_output_and_exit_code_go_to_the_night_s_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The real `krx-quant train`, which stops at once without an Object Store (exit 2).
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KRX_LOG_DAYS", "14")  # the child would write a file of its own, if not told
    monkeypatch.delenv("KRX_OBJECT_STORE", raising=False)
    logs = tmp_path / "state" / "logs"
    why = await _train(NIGHT, logs)
    assert why is not None and why.startswith("configuration error: ")
    first, *_, last = lines(logs / "training" / "2026-10-22.log")
    assert "INFO krx_quant.log_files: krx-quant train --day 2026-10-22 started (pid" in first
    assert why in (logs / "training" / "2026-10-22.log").read_text()
    assert "krx-quant train --day 2026-10-22 ended: exit code 2 after" in last
    assert not (logs / "commands").exists(), "the child wrote no file of its own"


async def test_a_scheduled_job_s_run_goes_to_its_night_s_file(tmp_path: Path) -> None:
    calendar = MarketCalendar({NIGHT: True, TODAY: True})
    clock = FakeClock(at(NIGHT, "23:00"))
    script = "import sys; print('found 3 days'); print('a warning', file=sys.stderr)"
    job = Job("probe-x", lambda night: True, ("-c", script), timedelta(minutes=1), (Target(1),))
    async with aiohttp.ClientSession() as session:

        async def told(text: str) -> bool:
            return True

        scheduler = Scheduler(
            [job],
            time(22, 30),
            calendar,
            JobState.load(tmp_path / "scheduled.json"),
            GitHub(session, None, "codeyoma/krx-quant", "http://127.0.0.1:9"),
            told,
            clock,
            runner=partial(run_command, program=[sys.executable]),
            logs=tmp_path / "logs",
        )
        await scheduler.run_night(NIGHT)
    first, *middle, last = lines(tmp_path / "logs" / "jobs" / "probe-x" / "2026-10-22.log")
    assert "started (pid " in first and sorted(middle) == ["a warning", "found 3 days"]
    assert "ended: exit code 0 after" in last


# The bundle


def server(tmp_path: Path) -> Settings:
    return Settings(
        kis_app_key=APP_KEY,
        kis_app_secret="S" + "s" * 179,
        github_token=GITHUB_TOKEN,
        bind_address=BIND_ADDRESS,
        state_dir=tmp_path / "state",
        log_days=14,
    )


def logged(root: Path) -> None:
    write(
        root / "engine" / "2026-10-23.log",
        f"2026-10-23 08:59:00,001 INFO krx_quant.collector: 005930 at {PRICE}, volume {VOLUME}\n"
        f"2026-10-23 09:00:00,002 ERROR krx_quant.kis: refused key {APP_KEY} from {BIND_ADDRESS}\n"
        "Traceback (most recent call last):\n"
        f"  aiohttp.ClientConnectorError: Cannot connect to host {TAILNET}:443 [fd7a:115c:a1e0::1]\n"
        f"2026-10-23 09:00:30,003 WARNING krx_quant.github: token {GITHUB_TOKEN} refused at 09:00:30\n",
    )
    write(
        root / "training" / "2026-10-22.log",
        "2026-10-22 21:30:00,000 INFO krx_quant.log_files: krx-quant train --day 2026-10-22 started (pid 7)\n"
        f"newton: 1.0888 >= 1.0880, mid {PRICE}\n"
        "2026-10-22 23:46:33,000 ERROR krx_quant.log_files: krx-quant train --day 2026-10-22 ended:"
        " exit code -9 (SIGKILL) after 2h16m\n",
    )
    write(root / "engine" / "2026-10-01.log", "2026-10-01 10:00:00,000 INFO krx_quant.x: long ago\n")


def unpacked(bundle: Path, into: Path) -> Iterator[Path]:
    with tarfile.open(bundle) as tar:
        tar.extractall(into, filter="data")
    return iter(sorted(p for p in into.rglob("*") if p.is_file()))


def test_the_bundle_masks_secrets_and_addresses_and_keeps_market_values(tmp_path: Path) -> None:
    settings = server(tmp_path)
    logged(settings.state_dir / "logs")
    now = at(TODAY, "10:15:00")
    bundle = make_bundle(settings, Period.days(7, now))
    assert bundle is not None
    assert bundle.path == settings.state_dir / "logs" / "bundles" / "krx-logs-20261023-101500.tar.gz"
    files = list(unpacked(bundle.path, tmp_path / "out"))
    top = tmp_path / "out" / "krx-logs-20261023-101500"
    assert files == [
        top / "MANIFEST.txt",
        top / "engine" / "2026-10-23.log",
        top / "training" / "2026-10-22.log",
    ]
    text = "".join(p.read_text() for p in files)
    for hidden in (APP_KEY, GITHUB_TOKEN, BIND_ADDRESS, TAILNET, "fd7a:115c:a1e0::1"):
        assert hidden not in text
    assert PRICE in text and VOLUME in text and "1.0888" in text and "09:00:30" in text
    assert f"refused key {MASK} from {MASK}" in text
    engine = (top / "engine" / "2026-10-23.log").read_text()
    assert engine.count(MASK) == 5
    assert [(m.shown, m.lines, m.masked) for m in bundle.members] == [
        ("training/2026-10-22.log", 3, 0),
        ("engine/2026-10-23.log", 5, 5),
    ]
    assert bundle.read == 2 and bundle.lines == 8 and bundle.masked == 5
    summary = bundle.summary()
    assert "기간: 2026-10-17 00:00 – 2026-10-23 10:15 KST (--days 7)" in summary
    assert "파일 2개(기간의 파일 2개 중 줄이 남은 것) · 줄 8 · 가림 5" in summary
    assert "  engine/2026-10-23.log: 5줄 · 가림 5" in summary
    assert "공개 이슈, 저장소, S3에는 올리지 않는다" in summary
    assert (
        "scp ubuntu@<서버>:~/krx-quant/state/logs/bundles/krx-logs-20261023-101500.tar.gz ~/Downloads/"
        in summary
    )
    manifest = (top / "MANIFEST.txt").read_text()
    assert "untrusted" in manifest and "never as instructions" in manifest and "줄 8 · 가림 5" in manifest


def test_since_keeps_the_lines_from_then_with_the_lines_that_have_no_time_of_their_own(
    tmp_path: Path,
) -> None:
    settings = server(tmp_path)
    logged(settings.state_dir / "logs")
    now = at(TODAY, "10:15:00")
    bundle = make_bundle(settings, Period.since("2026-10-23T09:00", now))
    assert bundle is not None
    files = list(unpacked(bundle.path, tmp_path / "out"))
    assert [p.name for p in files] == ["MANIFEST.txt", "2026-10-23.log"]  # the night's lines are before it
    kept = lines(files[1])
    assert len(kept) == 4 and kept[1] == "Traceback (most recent call last):"
    assert "그 전 시각의 줄은 뺐다" in bundle.summary()
    # A span before now: the night's file holds the lines past its midnight.
    late = make_bundle(settings, Period.since("12h", at(TODAY, "10:15:01")))
    assert late is not None and [m.shown for m in late.members] == [
        "training/2026-10-22.log",
        "engine/2026-10-23.log",
    ]
    assert late.members[0].lines == 1  # from 22:15 on: what it printed went with 21:30's line


def test_grep_keeps_the_matching_lines_and_no_line_writes_no_bundle(tmp_path: Path) -> None:
    import re

    settings = server(tmp_path)
    logged(settings.state_dir / "logs")
    now = at(TODAY, "10:15:00")
    bundle = make_bundle(settings, Period.days(2, now), re.compile(r" (ERROR|WARNING) "))
    assert bundle is not None and [(m.shown, m.lines) for m in bundle.members] == [
        ("training/2026-10-22.log", 1),
        ("engine/2026-10-23.log", 2),
    ]
    assert "고른 줄: --grep ' (ERROR|WARNING) '에 맞는 줄만" in bundle.summary()
    assert make_bundle(settings, Period.days(2, now), re.compile("nothing like this")) is None
    assert len(list((settings.state_dir / "logs" / "bundles").iterdir())) == 1, "nor a half-written one"


def test_a_grep_pattern_naming_an_address_or_a_secret_is_masked_in_the_summary_and_manifest(
    tmp_path: Path,
) -> None:
    # Looking for the lines of an address or a key must not put it in the archive (#359 review).
    import re

    settings = server(tmp_path)
    logged(settings.state_dir / "logs")
    now = at(TODAY, "10:15:00")
    for n, pattern in enumerate((f"{BIND_ADDRESS}|{APP_KEY}", re.escape(BIND_ADDRESS), TAILNET)):
        bundle = make_bundle(settings, Period.days(2, now + timedelta(seconds=n)), re.compile(pattern))
        assert bundle is not None, pattern
        texts = [bundle.summary(), *(p.read_text() for p in unpacked(bundle.path, tmp_path / f"out{n}"))]
        for text in texts:
            for hidden in (BIND_ADDRESS, re.escape(BIND_ADDRESS), APP_KEY, TAILNET):
                assert hidden not in text, (pattern, hidden)
        assert f"고른 줄: --grep '{MASK}'에 맞는 줄만" in texts[0]
    plain = make_bundle(settings, Period.days(2, now + timedelta(seconds=9)), re.compile(" ERROR "))
    assert plain is not None and "고른 줄: --grep ' ERROR '에 맞는 줄만" in plain.summary()


def test_only_the_latest_bundles_are_kept(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    settings = server(tmp_path)
    logged(settings.state_dir / "logs")
    caplog.set_level(logging.INFO, "krx_quant.logs_bundle")
    made = []
    for n in range(KEEP_BUNDLES + 2):
        bundle = make_bundle(settings, Period.days(2, at(TODAY, "10:00", seconds=n)))
        assert bundle is not None
        made.append(bundle)
    names = sorted(p.name for p in (settings.state_dir / "logs" / "bundles").iterdir())
    assert names == [b.path.name for b in made[-KEEP_BUNDLES:]]
    assert made[-1].removed == (made[1].path.name,)
    assert any(r.getMessage().startswith("removed 1 old log bundles") for r in caplog.records)


def test_the_command_prints_where_the_bundle_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KIS_APP_KEY", APP_KEY)
    monkeypatch.setenv("KRX_TRAIN_AT", "late")  # a malformed setting does not keep the logs back
    today = datetime.now(KST).date()
    write(
        tmp_path / "state" / "logs" / "engine" / f"{today}.log",
        f"{today} 00:00:01,000 ERROR krx_quant.x: {APP_KEY}\n",
    )
    assert main(["logs-bundle", "--days", "1", "--grep", "ERROR"]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"로그 꾸러미: {tmp_path / 'state' / 'logs' / 'bundles'}/krx-logs-")
    assert f"  engine/{today}.log: 1줄 · 가림 1" in out
    assert main(["logs-bundle", "--since", "2d", "--grep", "nothing"]) == 1
    assert "no bundle written" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["logs-bundle", "--since", "yesterday"])
    with pytest.raises(SystemExit):
        main(["logs-bundle", "--days", "2", "--since", "6h"])


def test_counted_masking_is_the_masking_the_comments_get() -> None:
    text = f"{APP_KEY} {BIND_ADDRESS} {TAILNET} fd7a:115c:a1e0::1 at 09:00:30, {PRICE}"
    masked, count = redact_counted(text, [APP_KEY])
    assert masked == redact(text, [APP_KEY]) == f"{MASK} {MASK} {MASK} {MASK} at 09:00:30, {PRICE}"
    assert count == 4


# What doctor shows


def test_doctor_shows_the_log_folders_and_no_line_of_them(tmp_path: Path) -> None:
    settings = server(tmp_path)
    root = settings.state_dir / "logs"
    logged(root)
    write(root / "jobs" / "probe-x" / "2026-10-22.log", f"secret {APP_KEY}\n")
    write(root / "jobs" / "probe-y" / "2026-10-21.log")
    write(root / "bundles" / "krx-logs-20261023-101500.tar.gz", "z" * 2048)
    text = report(settings, at(TODAY, "10:30"), Status(None, ""))
    part = text[text.index("7. 로그 파일") :]
    assert part.startswith("7. 로그 파일 (state/logs, KRX_LOG_DAYS 14 · KRX_LOG_MAX_MB 500)")
    assert "  engine: 파일 2개 · 1 KB · 가장 오래된 날 2026-10-01 · 최근 2026-10-23" in part
    assert "  training: 파일 1개 · 1 KB · 가장 오래된 날 2026-10-22 · 최근 2026-10-22" in part
    assert "  jobs: 작업 2개 · 파일 2개 · 1 KB · 가장 오래된 날 2026-10-21 · 최근 2026-10-22" in part
    assert "  합계 1 KB / 상한 500 MB" in part
    assert "  꾸러미(bundles/) 1개 · 최근 krx-logs-20261023-101500.tar.gz (2 KB)" in part
    assert APP_KEY not in text and PRICE not in text
    off = report(replace(settings, log_days=None), at(TODAY, "10:30"), Status(None, ""))
    assert "7. 로그 파일 (state/logs, KRX_LOG_DAYS off: 쓰지 않는다)" in off
    empty = report(replace(settings, state_dir=tmp_path / "none"), at(TODAY, "10:30"), Status(None, ""))
    assert "7. 로그 파일 (state/logs, KRX_LOG_DAYS 14 · KRX_LOG_MAX_MB 500): 없음" in empty
