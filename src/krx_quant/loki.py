"""Log lines to Grafana Cloud Loki (#358): where each process's lines are kept, an agent queries
them (docs/agents/server-logs.md) and Grafana's alert rules read them (docs/operations.md
"Grafana Cloud Loki로 보내는 로그 (#358)").

Beside it there is only stdout, which ``docker compose logs`` keeps until the 06:00 deploy makes a
new container (the files under ``state/logs``, #356, went in #360). Each process sends its own
lines over HTTPS to ``<KRX_LOKI_URL>/loki/api/v1/push`` (basic auth ``KRX_LOKI_USER`` and
``KRX_LOKI_TOKEN``), from a thread of its own: no collecting agent runs beside the engine (the
server has 2 GB), and nothing listens on the server for it. All three settings set, or nothing is
sent.

- What goes: the engine's lines (``collect``), those of a command run by hand, and the output of
  the processes the engine starts (the nightly training, the scheduled jobs), as the engine reads
  it (``process_log.run_logged``), stdout and stderr as printed. A process the engine started
  sends nothing itself (``process_log.BY_PARENT``).
- The labels are few and fixed (Loki indexes each set as a stream): ``service="krx-quant"``,
  ``process`` (``engine``, ``training``, ``job``, ``command``) and ``level`` (``debug`` …
  ``critical``; a line without a level of its own, such as a traceback's or a record's later
  lines, takes the one before it). What varies goes in the line: a job's starts with
  ``job=<name> night=<night>``, a training's with ``night=<night>``, a command's with
  ``command=<name>``; the rest is the stdout line as it is. A record of several lines goes as one
  entry a line.
- A line over ``LINE_BYTES`` is cut at its last space within them before it waits (``bounded``),
  then masked by ``doctor``'s rules (``redaction.redact`` with the settings' secrets) and cut to
  ``LINE_BYTES`` again when it goes (``cut``). A record logged with ``extra=TEMPLATE_ONLY`` goes as
  its template, its values ``…``: what it may carry (a Toss figure in an exception's message)
  stays on the server (design.md 4.7).
- Sent in batches of ``BATCH_LINES`` lines or ``BATCH_BYTES``, or what has waited
  ``BATCH_SECONDS``; one batch at a time, in order. A failure is tried again after 1, 2, 4 … up to
  ``MAX_RETRY`` seconds (jittered); a batch Loki refuses for what it is (a 4xx other than 408 and
  429) is dropped. The buffer holds at most ``MAX_LINES`` lines and ``MAX_BYTES`` (UTF-8): past
  either, the oldest lines are dropped and counted. A line is handed over and the thread sends it:
  nothing waits for Loki, and no failure of Loki's reaches the process.
- At the process's end what is left is sent within ``EXIT_SECONDS``; the engine asks for a child's
  lines to be sent at once when the child ends. Two gaps the user accepted (2026-10-23): what
  could not be sent by then or was dropped past the cap while Loki was unreachable, and the last
  lines of a process killed outright (SIGKILL, the kernel's out-of-memory kill), never reach Loki.
  Only stdout had them: ``docker compose logs``, until the next deploy.
- The engine also sends a line of its own every ``HEARTBEAT_SECONDS`` from its event loop
  (``keep_heartbeat``), to Loki only: the log-absence alert reads it when the engine is quiet.
- ``status_json`` is ``/v1/status``'s ``loki`` and ``doctor``'s section 8.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import re
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from krx_quant.feeds.base import KST
from krx_quant.redaction import redact

log = logging.getLogger(__name__)

SERVICE = "krx-quant"
ENGINE, TRAINING, JOB, COMMAND = "engine", "training", "job", "command"  # the ``process`` label
TRADER = "trader"  # the trader's process (#394, ``krx-quant trade``)
URL, USER, TOKEN = "KRX_LOKI_URL", "KRX_LOKI_USER", "KRX_LOKI_TOKEN"
PUSH = "/loki/api/v1/push"
BATCH_LINES = 1000
BATCH_BYTES = 512 * 1024
BATCH_SECONDS = 3.0  # a line waits at most this long for others to go with
MAX_LINES = 20_000  # the buffer's cap: past it the oldest lines are dropped
MAX_BYTES = 16 * 1024 * 1024
LINE_BYTES = 64 * 1024  # a longer line is cut (Loki refuses lines over 256 KB)
FIRST_RETRY = 1.0  # seconds before the first try again, doubled each failure in a row
MAX_RETRY = 60.0
REQUEST_SECONDS = 10.0
EXIT_SECONDS = 5.0  # what is left is sent within this at the process's end
HEARTBEAT_SECONDS = 60.0
# ``log.warning(..., extra=TEMPLATE_ONLY)``: the line goes to Loki as its template, the values
# ``…``; stdout has it whole.
VALUES = "loki_values"
TEMPLATE_ONLY: Mapping[str, Any] = {VALUES: False}
LEVELS = {"DEBUG": "debug", "INFO": "info", "WARNING": "warning", "ERROR": "error", "CRITICAL": "critical"}
_LOCAL = frozenset({"localhost", "127.0.0.1", "::1"})  # http:// is allowed for these only (tests)
# A log line's start as ``process_log.FORMAT`` writes it: its time and level.
_STAMPED = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} (DEBUG|INFO|WARNING|ERROR|CRITICAL) ")
# A printf conversion in a log template (``%s``, ``%.1f``, ``%(name)s``, ``%%``), as doctor's.
_SPEC = re.compile(r"%(?:\([^)]*\))?[#0\- +]*(?:\*|\d+)?(?:\.(?:\*|\d+))?[hlL]?([diouxXeEfFgGcrsa%])")
_OVERHEAD = 64  # bytes a buffered line is counted beside its text
_CUT_NOTE = 48  # room kept for the note a cut line ends with


@dataclass(frozen=True)
class Target:
    """Where the lines go: the stack's Loki address (without the push path), its user and token."""

    url: str
    user: str = field(repr=False)
    token: str = field(repr=False)

    @property
    def push_url(self) -> str:
        return self.url + PUSH

    @property
    def authorization(self) -> str:
        return "Basic " + base64.b64encode(f"{self.user}:{self.token}".encode()).decode()


def target_of(values: Mapping[str, str]) -> tuple[Target | None, str | None]:
    """The Loki the raw settings name, or None and why not: all three must be set, the address
    an ``https://`` one (with or without the push path)."""
    named = {name: values.get(name, "").strip() for name in (URL, USER, TOKEN)}
    missing = [name for name, value in named.items() if not value]
    if len(missing) == len(named):
        return None, f"{URL}, {USER} and {TOKEN} are not set"
    if missing:
        verb = "is" if len(missing) == 1 else "are"
        return None, f"{' and '.join(missing)} {verb} not set: all three are needed"
    url = named[URL].rstrip("/")
    if url.endswith(PUSH):
        url = url[: -len(PUSH)].rstrip("/")
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        host = None
    if not host or not (parts.scheme == "https" or (parts.scheme == "http" and host in _LOCAL)):
        return None, f"{URL} must be an https:// address"
    return Target(url, named[USER], named[TOKEN]), None


def level_of(line: str, previous: str = "info") -> str:
    """The ``level`` label of a line a child printed: its own (``process_log.FORMAT``), else the
    line before's (a traceback, a report's lines)."""
    found = _STAMPED.match(line)
    return previous if found is None else LEVELS[found.group(1)]


def backoff(failures: int, first: float = FIRST_RETRY, cap: float = MAX_RETRY) -> float:
    """The wait after ``failures`` failures in a row: ``first`` doubled each time, up to ``cap``."""
    return min(cap, first * 2.0 ** min(max(failures, 1) - 1, 30))


def error_kind(error: BaseException) -> str:
    """A push's failure as ``doctor`` shows it: ``HTTP 503``, or its type (with its reason's:
    ``URLError(ConnectionRefusedError)``). Never its text, which may hold an address."""
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code}"
    if isinstance(error, urllib.error.URLError) and isinstance(error.reason, BaseException):
        return f"URLError({type(error.reason).__name__})"
    return type(error).__name__


def bounded(line: str, limit: int = LINE_BYTES) -> str:
    """A line of more than ``limit`` bytes (UTF-8) cut before it waits and is masked, at its last
    space within them: the buffer and the masking's work stay bounded, and no value the masking
    takes (none holds a space) is cut in two to show a part of it. A line without such a space goes
    as a note of its length."""
    data = line.encode("utf-8", errors="replace")
    if len(data) <= limit:
        return line
    head = data[: limit - _CUT_NOTE].decode("utf-8", errors="ignore")
    space = max(head.rfind(" "), head.rfind("\t"))
    return (head[:space] if space > 0 else "") + f" … (cut: {len(data):,} bytes)"


def cut(line: str, limit: int = LINE_BYTES) -> str:
    """``line`` within ``limit`` bytes, saying how long it was if cut."""
    data = line.encode("utf-8", errors="replace")
    if len(data) <= limit:
        return line
    return data[: limit - _CUT_NOTE].decode("utf-8", errors="ignore") + f" … (cut: {len(data):,} bytes)"


@dataclass(frozen=True, slots=True)
class _Entry:
    process: str
    level: str
    ns: int  # its time, nanoseconds since the epoch
    line: str
    queued: float  # monotonic
    size: int


@dataclass(frozen=True)
class Shipped:
    """What a ``Shipper`` did so far: the lines Loki took and those dropped, the lines waiting,
    the last success and the last failure (its kind, ``error_kind``), and the failures in a row."""

    sent: int
    dropped: int
    buffered: int
    last_success_at: datetime | None
    last_error: str | None
    last_error_at: datetime | None
    failures: int


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is a failure: the request carries the token, which goes to Loki only."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class Shipper:
    """Lines to one Loki, from a thread of its own (``put`` never waits nor raises)."""

    def __init__(
        self,
        target: Target,
        secrets: Iterable[str] = (),
        *,
        batch_lines: int = BATCH_LINES,
        batch_bytes: int = BATCH_BYTES,
        every: float = BATCH_SECONDS,
        max_lines: int = MAX_LINES,
        max_bytes: int = MAX_BYTES,
        first_retry: float = FIRST_RETRY,
        max_retry: float = MAX_RETRY,
        request_seconds: float = REQUEST_SECONDS,
        jitter: Callable[[], float] | None = None,
    ) -> None:
        self.target = target
        self._secrets = tuple(secrets)
        self._batch_lines, self._batch_bytes, self._every = batch_lines, batch_bytes, every
        self._max_lines, self._max_bytes = max_lines, max_bytes
        self._first_retry, self._max_retry = first_retry, max_retry
        self._request_seconds = request_seconds
        # Senders that failed together spread out (0.5–1 of the wait).
        self._jitter = jitter or (lambda: 0.5 + random.random() / 2)
        self._cond = threading.Condition()
        self._buffer: deque[_Entry] = deque()
        self._bytes = 0
        self._last_ns = 0
        self._sent = 0
        self._dropped = 0
        self._dropped_said = 0  # the drops said so far (a warning, or the line saying Loki is back)
        self._dropped_by_then = 0  # the drops before the failures in a row began
        self._in_flight = 0
        self._now = False  # send what is buffered without waiting (``flush``)
        self._closing = False
        self._deadline: float | None = None
        self._failures = 0
        self._last_success: float | None = None
        self._last_error: str | None = None
        self._last_error_at: float | None = None
        # Straight to Loki: no proxy from the environment, no redirect.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        self._thread = threading.Thread(target=self._run, name="loki", daemon=True)
        self._thread.start()

    def put(self, process: str, level: str, line: str, at: float | None = None) -> None:
        """Hand ``line`` over (``at``: its time, seconds since the epoch; now if None), cut to
        ``LINE_BYTES`` (``bounded``) and counted in its UTF-8 bytes."""
        ns = time.time_ns() if at is None else int(at * 1_000_000_000)
        line = bounded(line)
        size = len(line.encode("utf-8", errors="replace")) + _OVERHEAD
        with self._cond:
            if self._closing:
                self._dropped += 1
                return
            ns = max(ns, self._last_ns + 1)  # in order, and no two lines at one time
            self._last_ns = ns
            self._buffer.append(_Entry(process, level, ns, line, time.monotonic(), size))
            self._bytes += size
            while self._buffer and (len(self._buffer) > self._max_lines or self._bytes > self._max_bytes):
                self._bytes -= self._buffer.popleft().size
                self._dropped += 1
            if len(self._buffer) <= 1 or self._full():
                self._cond.notify()

    def flush(self) -> None:
        """Send what is buffered now, without waiting for a batch to fill (and without waiting
        here for it to go)."""
        with self._cond:
            self._now = True
            self._cond.notify()

    def close(self, timeout: float | None = None) -> int:
        """Send what is left within ``timeout`` seconds (``EXIT_SECONDS``), then take no more; the
        lines not sent."""
        timeout = EXIT_SECONDS if timeout is None else timeout
        with self._cond:
            if not self._closing:
                self._closing = True
                self._deadline = time.monotonic() + timeout
            dropped_before = self._dropped
            self._cond.notify_all()
        self._thread.join(timeout + 1.0)
        with self._cond:
            unsent = self._dropped - dropped_before
            if self._thread.is_alive():  # a request that outlived its time: the rest is not sent
                left = len(self._buffer) + self._in_flight
                self._dropped += len(self._buffer)
                self._buffer.clear()
                self._bytes = 0
                unsent += left
            return unsent

    def status(self) -> Shipped:
        with self._cond:
            return Shipped(
                self._sent,
                self._dropped,
                len(self._buffer) + self._in_flight,
                _at(self._last_success),
                self._last_error,
                _at(self._last_error_at),
                self._failures,
            )

    # The thread

    def _full(self) -> bool:
        return len(self._buffer) >= self._batch_lines or self._bytes >= self._batch_bytes

    def _run(self) -> None:
        while True:
            with self._cond:
                batch = self._next()
                if batch is None:
                    return
                self._in_flight = len(batch)
                # Said now while Loki takes the lines; while it fails, once it is back.
                newly_dropped = 0 if self._failures else self._dropped - self._dropped_said
                self._dropped_said += newly_dropped
            if newly_dropped:
                log.warning(
                    "Loki: %d lines dropped (the buffer was full or Loki refused them); only stdout had them",
                    newly_dropped,
                )
            try:
                self._ship(batch)
            except Exception:  # the thread goes on whatever a batch did
                with self._cond:
                    self._dropped += len(batch)
                log.exception("Loki: could not send %d lines", len(batch))
            finally:
                with self._cond:
                    self._in_flight = 0

    def _next(self) -> list[_Entry] | None:
        """The next batch, once it is due; None once closing with nothing left. Holds the lock."""
        while True:
            if self._buffer:
                waited = time.monotonic() - self._buffer[0].queued
                if self._closing or self._now or self._full() or waited >= self._every:
                    return self._take()
                self._cond.wait(self._every - waited)
            elif self._closing:
                return None
            else:
                self._now = False
                self._cond.wait()

    def _take(self) -> list[_Entry]:
        batch: list[_Entry] = []
        size = 0
        while self._buffer and len(batch) < self._batch_lines and (not batch or size < self._batch_bytes):
            entry = self._buffer.popleft()
            self._bytes -= entry.size
            size += entry.size
            batch.append(entry)
        if not self._buffer:
            self._now = False
        return batch

    def _ship(self, batch: list[_Entry]) -> None:
        """Send ``batch`` until Loki takes it, refuses it, or the time to close is up."""
        body = self._body(batch)
        while True:
            with self._cond:
                timeout = self._request_seconds
                if self._deadline is not None:
                    left = self._deadline - time.monotonic()
                    if left <= 0:
                        self._dropped += len(batch)
                        return
                    timeout = min(timeout, max(left, 0.1))
            try:
                self._post(body, timeout)
            except Exception as e:
                kind = error_kind(e)
                refused = (
                    isinstance(e, urllib.error.HTTPError) and 400 <= e.code < 500 and e.code not in (408, 429)
                )
                with self._cond:
                    self._last_error, self._last_error_at = kind, time.time()
                    if not self._failures:
                        self._dropped_by_then = self._dropped
                    self._failures += 1
                    failures = self._failures
                    if refused:
                        self._dropped += len(batch)
                if failures == 1:
                    log.warning(
                        "Loki: could not send the log lines (%s); trying again, the lines wait in the buffer",
                        kind,
                    )
                self._pause(backoff(failures, self._first_retry, self._max_retry) * self._jitter())
                if refused:  # sending it again would not help; the next batch waited too
                    return
                continue
            with self._cond:
                self._sent += len(batch)
                self._last_success = time.time()
                recovered, self._failures = self._failures, 0
                meanwhile = self._dropped - self._dropped_by_then
                if recovered:  # the line below says the drops meanwhile
                    self._dropped_said = self._dropped
            if recovered:
                log.info(
                    "Loki: sending again after %d failures in a row; %d lines dropped meanwhile",
                    recovered,
                    meanwhile,
                )
            return

    def _pause(self, seconds: float) -> None:
        """Wait ``seconds``, or until the time to close is up."""
        end = time.monotonic() + seconds
        with self._cond:
            while True:
                stop = end if self._deadline is None else min(end, self._deadline)
                left = stop - time.monotonic()
                if left <= 0:
                    return
                self._cond.wait(left)

    def _body(self, batch: list[_Entry]) -> bytes:
        """The push's JSON: a stream for each label set, its lines masked and cut, in order."""
        streams: dict[tuple[str, str], list[list[str]]] = {}
        for entry in batch:
            line = cut(redact(entry.line, self._secrets))  # bounded when it was put
            streams.setdefault((entry.process, entry.level), []).append([str(entry.ns), line])
        body = {
            "streams": [
                {"stream": {"service": SERVICE, "process": process, "level": level}, "values": values}
                for (process, level), values in streams.items()
            ]
        }
        return json.dumps(body, ensure_ascii=False).encode("utf-8", errors="replace")  # a lone surrogate

    def _post(self, body: bytes, timeout: float) -> None:
        request = urllib.request.Request(
            self.target.push_url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": self.target.authorization,
                "User-Agent": SERVICE,
            },
        )
        with self._opener.open(request, timeout=timeout) as response:
            response.read()


def _at(seconds: float | None) -> datetime | None:
    return None if seconds is None else datetime.fromtimestamp(seconds, KST)


class LokiHandler(logging.Handler):
    """This process's records to its ``Shipper``, as ``formatter`` writes them (stdout's
    format), each line starting with ``fields`` if any."""

    def __init__(self, shipper: Shipper, process: str, fields: str, formatter: logging.Formatter) -> None:
        super().__init__()
        self.shipper = shipper
        self.process = process
        self.fields = fields
        self.setFormatter(formatter)

    def emit(self, record: logging.LogRecord) -> None:
        """The record as stdout has it, an entry a line (a traceback's, a multiline message's)."""
        try:
            shown = record if getattr(record, VALUES, True) else _template(record)
            level = LEVELS.get(record.levelname, "info")
            for line in self.format(shown).split("\n"):
                text = f"{self.fields} {line}" if self.fields else line
                self.shipper.put(self.process, level, text, record.created)
        except Exception:
            self.handleError(record)

    def beat(self) -> None:
        """The heartbeat line, to Loki only (not the root logger: stdout does not have it)."""
        s = self.shipper.status()
        self.handle(
            logging.LogRecord(
                __name__,
                logging.INFO,
                __file__,
                0,
                "heartbeat: sent %d, dropped %d, buffered %d",
                (s.sent, s.dropped, s.buffered),
                None,
            )
        )


def _template(record: logging.LogRecord) -> logging.LogRecord:
    """``record`` with its template for its message, the values ``…``, an exception by its type."""
    if isinstance(record.msg, str):
        text = _SPEC.sub(lambda m: "%" if m.group(1) == "%" else "…", record.msg)
    else:
        text = "…"
    if record.exc_info and record.exc_info[1] is not None:
        text += f" | {type(record.exc_info[1]).__name__}"
    fields = {"msg": text + " (values on the server only)", "args": None, "exc_info": None, "exc_text": None}
    return logging.makeLogRecord({**record.__dict__, **fields, "stack_info": None})


# This process's shipping: started by ``process_log.open_process_log``, stopped at its end.

_handler: LokiHandler | None = None
_off: str = "not started"  # why nothing is sent, while nothing is


def start(
    values: Mapping[str, str],
    secrets: Iterable[str],
    process: str,
    fields: str,
    formatter: logging.Formatter,
    **options: Any,
) -> tuple[LokiHandler | None, str | None]:
    """Send this process's lines from now on (its handler on the root logger), if the raw settings
    name a Loki; else None and why not. ``options`` are the ``Shipper``'s (tests)."""
    global _handler, _off
    stop(0)
    target, why = target_of(values)
    if target is None:
        _off = why or "off"
        return None, why
    handler = LokiHandler(Shipper(target, secrets, **options), process, fields, formatter)
    logging.getLogger().addHandler(handler)
    _handler = handler
    return handler, None


def stop(timeout: float | None = None) -> int:
    """Send what is left within ``timeout`` seconds (``EXIT_SECONDS``) and stop; the lines not
    sent."""
    global _handler, _off
    handler, _handler = _handler, None
    if handler is None:
        return 0
    _off = "stopped"
    logging.getLogger().removeHandler(handler)
    unsent = handler.shipper.close(timeout)
    handler.close()
    return unsent


def configured(values: Mapping[str, str]) -> bool:
    """Whether any of the three settings is set (a partial one is said, not sent with)."""
    return any(values.get(name, "").strip() for name in (URL, USER, TOKEN))


def active() -> Shipper | None:
    """This process's shipper, if it sends."""
    return None if _handler is None else _handler.shipper


def status_json() -> dict[str, Any]:
    """``/v1/status``'s ``loki``: whether this process sends (and why not), the lines sent,
    dropped and waiting, the last success and the last failure's kind."""
    handler = _handler
    if handler is None:
        return {
            "enabled": False,
            "reason": _off,
            "sent": 0,
            "dropped": 0,
            "buffered": 0,
            "last_success_at": None,
            "last_error": None,
            "last_error_at": None,
        }
    s = handler.shipper.status()
    return {
        "enabled": True,
        "reason": None,
        "sent": s.sent,
        "dropped": s.dropped,
        "buffered": s.buffered,
        "last_success_at": None if s.last_success_at is None else s.last_success_at.isoformat(),
        "last_error": s.last_error,
        "last_error_at": None if s.last_error_at is None else s.last_error_at.isoformat(),
    }


async def keep_heartbeat(every: float = HEARTBEAT_SECONDS) -> None:
    """The engine's heartbeat line every ``every`` seconds, until cancelled. From its event loop:
    a loop that stops turning stops it, and the log-absence alert fires (docs/operations.md)."""
    while True:
        await asyncio.sleep(every)
        handler = _handler
        if handler is not None:
            handler.beat()
