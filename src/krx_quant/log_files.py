"""Log files in the state folder (#356): every process's log lines, a file a day, kept across
deploys.

Docker keeps a container's output (``json-file``, 10 MB × 5) only while the container lives,
and the 06:00 deploy makes a new one every day. So each process also writes its lines, in the
format of its stdout lines (``FORMAT``, the time in KST), under ``state/logs/``:

- ``engine/<day>.log``: the engine (``krx-quant collect``), a file a KST day; a record goes to
  the file of the day it was made on, so the file changes at midnight KST.
- ``training/<night>.log``: the nightly training the engine starts, the day it trains on
  (``swap._train``); ``jobs/<job>/<night>.log``: a scheduled job's run, the night it ran on
  (``scheduled.py``). The file is that night's even past midnight. The engine copies the
  process's whole output into it as it comes (stdout and stderr, as printed: what logging never
  sees too, such as a traceback), between a line saying what started and one with its exit code
  (or the time limit that stopped it). A process the engine starts is told so (``BY_PARENT``)
  and writes no file of its own.
- ``commands/<command>/<day>.log``: a command run by hand (``docker compose run --rm engine
  <command>``, ``docker compose exec engine krx-quant <command>``), a file a KST day: its log
  lines between a start line and one with its exit code, or the traceback that ended it. What it
  prints (its report) goes to the terminal only.

The files are kept ``KRX_LOG_DAYS`` days (14, today's included) and within ``KRX_LOG_MAX_MB``
(500 MB): past it the oldest go first. Today's and yesterday's are never removed (last night's
training and jobs write theirs past midnight). A process prunes when it starts, and the engine
every hour (``keep_logs``); each removal is logged. ``krx-quant logs-bundle`` packs them, masked,
into ``bundles/`` (``logs_bundle.py``), which is not pruned here and keeps its own few.
``KRX_LOG_DAYS=off`` writes and removes nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shlex
import signal
import sys
import time
from asyncio.subprocess import DEVNULL, PIPE, STDOUT
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TextIO

from krx_quant.config import DEFAULT_LOG_DAYS, DEFAULT_LOG_MAX_MB, Settings, SettingsError, log_keep
from krx_quant.feeds.base import KST

log = logging.getLogger(__name__)

LOGS = "logs"  # under the state directory
ENGINE, TRAINING, JOBS, COMMANDS = "engine", "training", "jobs", "commands"
KINDS = (ENGINE, TRAINING, JOBS, COMMANDS)
NESTED = frozenset({JOBS, COMMANDS})  # a folder of its own for each job and command
BUNDLES = "bundles"
# The lines' format, the stdout lines' (``cli.main``'s ``logging.basicConfig``).
FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
# Set for a process the engine starts: the engine writes what it writes.
BY_PARENT = "KRX_LOG_BY_PARENT"
PRUNE_SECONDS = 3600.0  # how often the engine prunes
MB = 1024 * 1024
# A process the engine starts, as its command line shows it: ``krx-quant <arguments>``.
KRX_QUANT = (sys.executable, "-m", "krx_quant.cli")
_DAY_FILE = re.compile(r"\d{4}-\d{2}-\d{2}\.log")
_DRAIN_SECONDS = 5.0  # how long a killed process's output is still read (a child may hold the pipe)
_LISTED = 10  # the removed files a log line names, at most


def _kst(seconds: float | None) -> time.struct_time:
    return datetime.fromtimestamp(time.time() if seconds is None else seconds, KST).timetuple()


def formatter() -> logging.Formatter:
    """The stdout lines' format, the time in KST whatever the machine's zone (the server's is
    KST, ``TZ`` in the Dockerfile)."""
    made = logging.Formatter(FORMAT)
    made.converter = _kst
    return made


def root_of(settings: Settings) -> Path | None:
    """Where the log files are (``state/logs``); None when ``KRX_LOG_DAYS`` is off."""
    return None if settings.log_days is None else settings.state_dir / LOGS


def night_file(root: Path | None, kind: str, night: date, name: str | None = None) -> Path | None:
    """The file of a night's process: ``training/<night>.log``, ``jobs/<name>/<night>.log``."""
    if root is None:
        return None
    folder = root / kind if name is None else root / kind / name
    return folder / f"{night.isoformat()}.log"


@dataclass(frozen=True)
class Keep:
    """How long the files are kept and how much they may take (``KRX_LOG_DAYS``,
    ``KRX_LOG_MAX_MB``)."""

    days: int
    max_bytes: int

    @classmethod
    def of(cls, settings: Settings) -> Keep:
        return cls(settings.log_days or DEFAULT_LOG_DAYS, settings.log_max_mb * MB)


class DayFile(logging.Handler):
    """This process's log records in a file a KST day, ``<folder>/<YYYY-MM-DD>.log``: a record
    goes to the file of the day it was made on (``created``), so the file changes at midnight KST.

    A file that cannot be written (a full disk) is said once (``handleError``) and left until the
    next day; stdout still has the lines."""

    def __init__(self, folder: Path) -> None:
        super().__init__()
        self.folder = folder
        self.setFormatter(formatter())
        self._day: date | None = None
        self._stream: TextIO | None = None
        self._failed = False

    def emit(self, record: logging.LogRecord) -> None:
        try:
            day = datetime.fromtimestamp(record.created, KST).date()
            if day != self._day:
                self._close_file()
                self._day, self._failed = day, False
            if self._failed:
                return
            try:
                if self._stream is None:
                    self.folder.mkdir(parents=True, exist_ok=True)
                    path = self.folder / f"{day.isoformat()}.log"
                    self._stream = path.open("a", encoding="utf-8", errors="replace")
                self._stream.write(self.format(record) + "\n")
                self._stream.flush()
            except OSError:
                self._failed = True
                raise
        except Exception:
            self.handleError(record)

    def note(self, level: int, text: str, error: BaseException | None = None) -> None:
        """A line of the process's own, in its file only (its start and its end)."""
        exc_info = None if error is None else (type(error), error, error.__traceback__)
        self.handle(logging.LogRecord(__name__, level, __file__, 0, text, None, exc_info))

    def _close_file(self) -> None:
        if self._stream is not None:
            with contextlib.suppress(OSError):
                self._stream.close()
            self._stream = None

    def close(self) -> None:
        self.acquire()
        try:
            self._close_file()
        finally:
            self.release()
        super().close()


class ProcessLog:
    """A process's own file (the engine's, a command's) from its start line to its end line."""

    def __init__(self, handler: DayFile, what: str) -> None:
        self.handler = handler
        self.what = what
        self._began = time.monotonic()

    def close(self, code: int | None, why: BaseException | str | None = None) -> None:
        """The end line (the exit code and why, or the exception that ended it), and no more lines."""
        after = duration(time.monotonic() - self._began)
        if isinstance(why, BaseException):
            self.handler.note(logging.ERROR, f"{self.what} ended by {type(why).__name__} after {after}", why)
        else:
            reason = f" ({why})" if why else ""
            level = logging.INFO if code == 0 else logging.ERROR
            self.handler.note(level, f"{self.what} ended: exit code {code}{reason} after {after}")
        logging.getLogger().removeHandler(self.handler)
        self.handler.close()


def open_process_log(command: str, argv: Sequence[str], values: Mapping[str, str]) -> ProcessLog | None:
    """This process's file: the engine's (``collect``) or the command's, with a start line; the
    folder pruned first. None if the engine started this process (it writes what this one does)
    or ``KRX_LOG_DAYS`` is off. ``values`` are the raw settings (``config.read_values``): a
    malformed one is the command's to report, and the files meanwhile go by the defaults."""
    if os.environ.get(BY_PARENT):
        return None
    try:
        days, max_mb = log_keep(values)
    except SettingsError:
        days, max_mb = DEFAULT_LOG_DAYS, DEFAULT_LOG_MAX_MB
    if days is None:
        return None
    root = Path(values.get("KRX_STATE_DIR", "state")) / LOGS
    handler = DayFile(root / ENGINE if command == "collect" else root / COMMANDS / command)
    logging.getLogger().addHandler(handler)
    what = command_text(["krx-quant", *argv])
    commit = values.get("KRX_COMMIT", "").strip() or "unknown"
    handler.note(logging.INFO, f"{what} started (pid {os.getpid()}, commit {commit[:12]})")
    try:
        prune(root, Keep(days, max_mb * MB), datetime.now(KST).date())
    except Exception:
        log.exception("could not prune the log files")
    return ProcessLog(handler, what)


# Retention


@dataclass(frozen=True)
class DayLog:
    """A log file: its kind, the job or command (None for the engine's and the training's), its
    day (the KST day, or the night), where it is and its size."""

    kind: str
    name: str | None
    day: date
    path: Path
    size: int

    @property
    def shown(self) -> str:
        """Its path under ``state/logs``: ``jobs/<job>/<night>.log``."""
        parts = [self.kind] if self.name is None else [self.kind, self.name]
        return "/".join([*parts, self.path.name])


def day_logs(root: Path) -> list[DayLog]:
    """Every day's file under ``root``, the oldest first (bundles are not day files)."""
    found: list[DayLog] = []
    for kind in KINDS:
        folder = root / kind
        places = [(p.name, p) for p in _entries(folder) if p.is_dir()] if kind in NESTED else [(None, folder)]
        for name, place in places:
            for path in _entries(place):
                if not _DAY_FILE.fullmatch(path.name):
                    continue
                try:
                    day = date.fromisoformat(path.stem)
                    size = path.stat().st_size
                except (ValueError, OSError):
                    continue
                found.append(DayLog(kind, name, day, path, size))
    return sorted(found, key=lambda f: (f.day, f.shown))


def _entries(folder: Path) -> list[Path]:
    try:
        return sorted(folder.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return []


@dataclass(frozen=True)
class Pruned:
    """What a prune removed, past ``KRX_LOG_DAYS`` (``old``) and past ``KRX_LOG_MAX_MB``
    (``over``), what the files take after it, and the cap it kept to."""

    old: tuple[DayLog, ...]
    over: tuple[DayLog, ...]
    left_bytes: int
    max_bytes: int

    @property
    def still_over(self) -> bool:
        """Today's and yesterday's files alone take more than the cap."""
        return self.left_bytes > self.max_bytes


def prune(root: Path, keep: Keep, today: date) -> Pruned:
    """Remove the files older than ``keep.days`` days (today's the first), then the oldest until
    the rest fit in ``keep.max_bytes``; never today's or yesterday's. Each removal is logged."""
    files = day_logs(root)
    recent = today - timedelta(days=1)  # may be written still
    kept_from = min(today - timedelta(days=keep.days - 1), recent)
    old = [f for f in files if f.day < kept_from]
    rest = [f for f in files if f.day >= kept_from]
    left = sum(f.size for f in rest)
    over: list[DayLog] = []
    for f in rest:  # the oldest first
        if left <= keep.max_bytes or f.day >= recent:
            break
        over.append(f)
        left -= f.size
    removed_old, removed_over = _remove(old), _remove(over)
    if removed_old:
        log.info(
            "removed %d log files older than %d days (KRX_LOG_DAYS): %s",
            len(removed_old),
            keep.days,
            _listed(removed_old),
        )
    if removed_over:
        log.warning(
            "removed %d log files to keep the logs within %d MB (KRX_LOG_MAX_MB): %s",
            len(removed_over),
            keep.max_bytes // MB,
            _listed(removed_over),
        )
    left += sum(f.size for f in over if f not in removed_over)
    if removed_old or removed_over:
        for kind in NESTED:  # a job's or command's folder left empty
            for folder in _entries(root / kind):
                with contextlib.suppress(OSError):
                    folder.rmdir()
    return Pruned(tuple(removed_old), tuple(removed_over), left, keep.max_bytes)


def _remove(files: Sequence[DayLog]) -> list[DayLog]:
    removed = []
    for f in files:
        try:
            f.path.unlink()
        except FileNotFoundError:
            continue  # another process's prune took it
        except OSError as e:
            log.warning("could not remove the log file %s: %s", f.shown, e)
            continue
        removed.append(f)
    return removed


def _listed(files: Sequence[DayLog]) -> str:
    names = [f.shown for f in files[:_LISTED]]
    more = len(files) - len(names)
    return ", ".join(names) + (f" and {more} more" if more > 0 else "")


async def keep_logs(
    root: Path, keep: Keep, now: Callable[[], datetime], every: float = PRUNE_SECONDS
) -> None:
    """The engine's prune every ``every`` seconds, until cancelled: the days pass and the cap
    holds between its starts. Today's and yesterday's files alone over the cap are said once a
    day."""
    warned: date | None = None
    while True:
        await asyncio.sleep(every)
        today = now().astimezone(KST).date()
        try:
            pruned = await asyncio.to_thread(prune, root, keep, today)
        except Exception:
            log.exception("could not prune the log files")
            continue
        if pruned.still_over and warned != today:
            warned = today
            log.warning(
                "today's and yesterday's log files alone take %d MB, over KRX_LOG_MAX_MB (%d MB)",
                pruned.left_bytes // MB,
                keep.max_bytes // MB,
            )


# A process the engine starts


@dataclass(frozen=True)
class Ran:
    """A process the engine started, once it ended."""

    exit_code: int | None  # None: it could not be started
    stdout: str
    stderr: str  # empty when it went into stdout (``merge``)
    timed_out: bool = False


class ChildLog:
    """The file of a process the engine started (None: no file): its lines as they come."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._file: TextIO | None = None
        self._format = formatter()
        self._done = path is None

    def note(self, level: int, text: str) -> None:
        """A line of the engine's about the process (its start and its end), as a log line."""
        self.write(self._format.format(logging.LogRecord(__name__, level, __file__, 0, text, None, None)))

    def write(self, line: str) -> None:
        if self._done or self.path is None:
            return
        try:
            if self._file is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._file = self.path.open("a", encoding="utf-8", errors="replace")
            self._file.write(line + "\n")
            self._file.flush()
        except OSError as e:  # the process runs on; its output is still read
            self.close()
            log.warning("could not write the log file %s: %s", self.path, e)

    async def copy(self, stream: asyncio.StreamReader, sink: bytearray) -> None:
        """Read ``stream`` to its end into ``sink``, each whole line into the file too."""
        pending = b""
        while chunk := await stream.read(1 << 16):
            sink += chunk
            *lines, pending = (pending + chunk).split(b"\n")
            for line in lines:
                self.write(line.rstrip(b"\r").decode(errors="replace"))
        if pending:
            self.write(pending.decode(errors="replace"))

    def close(self) -> None:
        self._done = True
        if self._file is not None:
            with contextlib.suppress(OSError):
                self._file.close()
            self._file = None


async def run_logged(
    command: Sequence[str], timeout: timedelta, path: Path | None = None, *, merge: bool = False
) -> Ran:
    """Run ``command``, a process of the engine's (``BY_PARENT``), and read its output; each
    line also goes to ``path``, between a start line and one with its exit code. ``merge`` puts
    stderr into stdout, in the order written. It is killed at ``timeout`` (what it wrote by then
    is kept), and when the engine stops (cancelled)."""
    out = ChildLog(path)
    what = command_text(shown(command))
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=DEVNULL,
            stdout=PIPE,
            stderr=STDOUT if merge else PIPE,
            env={**os.environ, BY_PARENT: "1"},
        )
    except Exception as e:
        out.note(logging.ERROR, f"{what} could not be started: {type(e).__name__}: {e}")
        out.close()
        raise
    began = time.monotonic()
    out.note(logging.INFO, f"{what} started (pid {process.pid})")
    assert process.stdout is not None
    stdout, stderr = bytearray(), bytearray()
    readers = [asyncio.create_task(out.copy(process.stdout, stdout))]
    if process.stderr is not None:
        readers.append(asyncio.create_task(out.copy(process.stderr, stderr)))
    # The process's end is timed too: one that closes its output early still stops at the limit.
    ended = asyncio.create_task(process.wait())
    try:
        try:
            _, pending = await asyncio.wait([*readers, ended], timeout=timeout.total_seconds())
            if pending:
                _kill(process)
                await asyncio.wait(readers, timeout=_DRAIN_SECONDS)
            code = await ended
        finally:
            for task in (*readers, ended):
                task.cancel()
    except BaseException:  # cancelled: it stops with the engine
        _kill(process)
        out.note(
            logging.WARNING, f"{what} stopped with the engine after {duration(time.monotonic() - began)}"
        )
        out.close()
        raise
    after = duration(time.monotonic() - began)
    if pending:
        out.note(
            logging.ERROR,
            f"{what} stopped at its time limit ({timeout}): exit code {exit_text(code)} after {after}",
        )
    else:
        level = logging.INFO if code == 0 else logging.ERROR
        out.note(level, f"{what} ended: exit code {exit_text(code)} after {after}")
    out.close()
    return Ran(
        code, stdout.decode(errors="replace"), stderr.decode(errors="replace"), timed_out=bool(pending)
    )


def _kill(process: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        process.kill()


def shown(command: Sequence[str]) -> list[str]:
    """A command as a person runs it: ``krx-quant train ...``, not the interpreter's path."""
    if tuple(command[: len(KRX_QUANT)]) == KRX_QUANT:
        return ["krx-quant", *command[len(KRX_QUANT) :]]
    return list(command)


def command_text(parts: Sequence[str]) -> str:
    """A command line as one line: quoted as a shell would, a newline in it shown as ``\\n``."""
    return shlex.join(parts).replace("\n", "\\n")


def exit_text(code: int | None) -> str:
    """An exit code, with the signal that ended the process if one did (-9: SIGKILL, what the
    kernel's out-of-memory killer sends)."""
    if code is None or code >= 0:
        return str(code)
    try:
        return f"{code} ({signal.Signals(-code).name})"
    except ValueError:
        return str(code)


def size_text(size: int) -> str:
    """A file's size for a person: KB under a MB, else MB to a tenth."""
    if size < MB:
        return f"{(size + 1023) // 1024:,} KB"
    return f"{size / MB:,.1f} MB"


def duration(seconds: float) -> str:
    total = round(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{seconds:.1f}s"


def kinds_of(files: Sequence[DayLog]) -> Iterator[tuple[str, list[DayLog]]]:
    """``files`` by kind, in ``KINDS``' order, the kinds with none left out."""
    for kind in KINDS:
        of_kind = [f for f in files if f.kind == kind]
        if of_kind:
            yield kind, of_kind


# The bundles (``logs_bundle.py``)

KEEP_BUNDLES = 5  # the latest bundles kept; the older are removed when one is made
BUNDLE_PREFIX = "krx-logs-"


def bundle_files(root: Path) -> list[Path]:
    """The bundles under ``root/bundles``, the oldest first (their names hold their time)."""
    return [
        p
        for p in _entries(root / BUNDLES)
        if p.name.startswith(BUNDLE_PREFIX) and p.name.endswith(".tar.gz") and p.is_file()
    ]
