"""A process's log lines beyond stdout (#356, #358): its own from a start line to an end line with
its exit code, and the output of the processes the engine starts, in Grafana Cloud Loki.

Every process logs to stdout (``cli.main``'s ``logging.basicConfig``, ``FORMAT``, the time in KST),
which ``docker compose logs`` keeps until the 06:00 deploy makes a new container. When Loki is set
(``loki.py``), the same lines go there too, and Loki is where they are kept:

- A process's own lines (the engine, ``krx-quant collect``; a command run by hand, ``docker
  compose run --rm engine <command>``, ``docker compose exec engine krx-quant <command>``), from a
  line saying what started (``krx-quant <arguments> started (pid N, commit C)``) to one with its
  exit code, or the traceback that ended it (``open_process_log``). The start and end lines go to
  Loki only, as they went to the files before.
- A process the engine starts (the nightly training, ``swap._train``; a scheduled job,
  ``scheduled.py``) is run by ``run_logged``: its whole output (stdout and stderr, as printed: what
  logging never sees too, such as a traceback) is read as it comes and sent, labelled as ``Child``
  says (``process="training"``, ``night=<night>``; ``process="job"``, ``job=<name>
  night=<night>``), between a line saying what started and one with its exit code (or the time
  limit that stopped it). The engine tells it so (``BY_PARENT``): it sends nothing itself. Its
  output also comes back whole (``Ran``): the exit code, the last step the training started.

This module was ``log_files.py`` (#356), which also wrote each process's lines to files under
``state/logs`` and packed them for an agent (``krx-quant logs-bundle``). Those went in #360: the
user chose to keep the logs in Grafana Cloud Loki only (2026-10-23). docs/operations.md says what
that leaves out.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shlex
import sys
import time
from asyncio.subprocess import DEVNULL, PIPE, STDOUT
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from krx_quant import loki
from krx_quant.feeds.base import KST
from krx_quant.training_nights import exit_text

log = logging.getLogger(__name__)

# The lines' format, the stdout lines' (``cli.main``'s ``logging.basicConfig``).
FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
# Set for a process the engine starts: the engine sends what it writes.
BY_PARENT = "KRX_LOG_BY_PARENT"
# A process the engine starts, as its command line shows it: ``krx-quant <arguments>``.
KRX_QUANT = (sys.executable, "-m", "krx_quant.cli")
_DRAIN_SECONDS = 5.0  # how long a killed process's output is still read (a child may hold the pipe)


def _kst(seconds: float | None) -> time.struct_time:
    return datetime.fromtimestamp(time.time() if seconds is None else seconds, KST).timetuple()


def formatter() -> logging.Formatter:
    """The stdout lines' format, the time in KST whatever the machine's zone (the server's is
    KST, ``TZ`` in the Dockerfile)."""
    made = logging.Formatter(FORMAT)
    made.converter = _kst
    return made


def own_line(level: int, text: str, error: BaseException | None = None) -> logging.LogRecord:
    """A line of the process's own (its start and its end), for its Loki handler only."""
    exc_info = None if error is None else (type(error), error, error.__traceback__)
    return logging.LogRecord(__name__, level, __file__, 0, text, None, exc_info)


class ProcessLog:
    """A process's own lines in Loki (#358), from its start line to its end line."""

    def __init__(self, shipped: loki.LokiHandler, what: str) -> None:
        self.shipped = shipped
        self.what = what
        self._began = time.monotonic()

    def close(self, code: int | None, why: BaseException | str | None = None) -> None:
        """The end line (the exit code and why, or the exception that ended it), and what is left
        sent within ``loki.EXIT_SECONDS``; then no more. What Loki did not get is said on stdout,
        which has those lines."""
        after = duration(time.monotonic() - self._began)
        if isinstance(why, BaseException):
            level, error = logging.ERROR, why
            text = f"{self.what} ended by {type(why).__name__} after {after}"
        else:
            reason = f" ({why})" if why else ""
            level = logging.INFO if code == 0 else logging.ERROR
            text, error = f"{self.what} ended: {_exit(code)}{reason} after {after}", None
        self.shipped.handle(own_line(level, text, error))
        unsent = loki.stop()
        if unsent:
            log.warning("%d log lines were not sent to Loki; only stdout had them", unsent)


def open_process_log(
    command: str, argv: Sequence[str], values: Mapping[str, str], secrets: Sequence[str] = ()
) -> ProcessLog | None:
    """This process's lines sent to Loki too, the engine's (``collect``) or a command's, from a
    start line on (``loki.start``, masked with ``secrets``). None if the engine started this
    process (it sends what this one writes), or Loki is not set (a partial setting is said).
    ``values`` are the raw settings (``config.read_values``): a malformed one is the command's to
    report."""
    if os.environ.get(BY_PARENT):
        return None
    # The engine's (``collect``) and the trader's (``trade``, #394) lines are their own processes';
    # every other command's is labelled ``command``, with its name.
    process = {"collect": loki.ENGINE, "trade": loki.TRADER}.get(command)
    shipped, why_not = loki.start(
        values,
        secrets,
        process or loki.COMMAND,
        "" if process else f"command={command}",
        formatter(),
    )
    if shipped is None:
        if loki.configured(values):
            log.warning("not sending the log lines to Loki: %s", why_not)
        return None
    what = command_text(["krx-quant", *argv])
    commit = values.get("KRX_COMMIT", "").strip() or "unknown"
    shipped.handle(own_line(logging.INFO, f"{what} started (pid {os.getpid()}, commit {commit[:12]})"))
    log.info("sending the log lines to Loki too (process=%s)", shipped.process)
    return ProcessLog(shipped, what)


# A process the engine starts


@dataclass(frozen=True)
class Ran:
    """A process the engine started, once it ended."""

    exit_code: int | None  # None: it could not be started
    stdout: str
    stderr: str  # empty when it went into stdout (``merge``)
    timed_out: bool = False


@dataclass(frozen=True)
class Child:
    """What a process the engine starts is, for its lines in Loki (#358): the ``process`` label,
    and the fields each of its lines starts with (its night, its job)."""

    process: str
    fields: str

    @classmethod
    def training(cls, night: date) -> Child:
        return cls(loki.TRAINING, f"night={night.isoformat()}")

    @classmethod
    def job(cls, name: str, night: date) -> Child:
        return cls(loki.JOB, f"job={name} night={night.isoformat()}")


class ChildLog:
    """The lines of a process the engine started, to Loki as they come, labelled as ``child``
    says (None: none sent)."""

    def __init__(self, child: Child | None = None) -> None:
        self.child = child
        self._format = formatter()
        self._done = False

    def note(self, level: int, text: str) -> None:
        """A line of the engine's about the process (its start and its end), as a log line."""
        line = self._format.format(logging.LogRecord(__name__, level, __file__, 0, text, None, None))
        self.write(line, loki.LEVELS[logging.getLevelName(level)])

    def write(self, line: str, level: str = "info") -> None:
        """A line to Loki at ``level``."""
        if not self._done and self.child is not None and (shipper := loki.active()) is not None:
            shipper.put(self.child.process, level, f"{self.child.fields} {line}")

    async def copy(self, stream: asyncio.StreamReader, sink: bytearray) -> None:
        """Read ``stream`` to its end into ``sink``, each whole line to Loki too; a line without a
        level of its own goes at the one before it."""
        pending = b""
        level = "info"
        while chunk := await stream.read(1 << 16):
            sink += chunk
            *lines, pending = (pending + chunk).split(b"\n")
            for raw in lines:
                line = raw.rstrip(b"\r").decode(errors="replace")
                level = loki.level_of(line, level)
                self.write(line, level)
        if pending:
            line = pending.decode(errors="replace")
            self.write(line, loki.level_of(line, level))

    def close(self) -> None:
        """No more lines; what Loki has not been sent of them goes now."""
        self._done = True
        if self.child is not None and (shipper := loki.active()) is not None:
            shipper.flush()


async def run_logged(
    command: Sequence[str], timeout: timedelta, *, merge: bool = False, child: Child | None = None
) -> Ran:
    """Run ``command``, a process of the engine's (``BY_PARENT``), and read its output; each line
    also goes to Loki as ``child`` labels it (#358), between a start line and one with its exit
    code. ``merge`` puts stderr into stdout, in the order written. It is killed at ``timeout``
    (what it wrote by then is kept), and when the engine stops (cancelled)."""
    out = ChildLog(child)
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
            f"{what} stopped at its time limit ({timeout}): {_exit(code)} after {after}",
        )
    else:
        level = logging.INFO if code == 0 else logging.ERROR
        out.note(level, f"{what} ended: {_exit(code)} after {after}")
    out.close()
    return Ran(
        code, stdout.decode(errors="replace"), stderr.decode(errors="replace"), timed_out=bool(pending)
    )


def _exit(code: int | None) -> str:
    """``exit 3``, ``exit -9 (SIGKILL)`` (the kernel's out-of-memory killer sends it)."""
    return "exit none" if code is None else exit_text(code)


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


def duration(seconds: float) -> str:
    total = round(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{seconds:.1f}s"
