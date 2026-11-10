"""The engine's memory jumps, named by what ran and what holds them (#680).

On 2026-11-10 the engine's resident memory rose by about 330 MB between 09:53 and 09:55 KST (631 ->
978 MB, the server's memory available 774 -> 448 MB) and stayed there, and nothing in the logs named
the cause. ``JumpWatch`` reads the engine's resident memory at each heartbeat (``heartbeat.
readings``, a minute apart) and each thread's CPU time. When the memory grew more than ``JUMP_MB``
since the heartbeat before (at most ``WITHIN_SECONDS`` earlier: a minute, or a late beat), it says
one WARNING line::

    memory jump: engine rss 631 -> 978 MB (+347) in 60s; cpu since the heartbeat before 41.3s:
    MainThread 30.1s, asyncio_2 9.0s, ThreadPoolExecutor-0_0 2.0s, 3 more 0.2s; python 4120001
    blocks, malloc 880 MB in use and 40 MB free, arrow 0 MB; allocations: traced from now on

(one line):

- ``cpu``: each thread's CPU time (user and system, ``/proc/self/task/<tid>/stat``) since the
  heartbeat before, the most first, ``TOP_THREADS`` of them, by the name Python gave the thread
  (``threading``), else the kernel's (``comm``: a native library's threads). A thread started
  since counts its whole time; one that ended since is not there.
- ``python``, ``malloc``, ``arrow``: what Python's, glibc's (in use, and freed but kept: what
  ``malloc_trim`` would give back) and Arrow's allocators hold now (``footprint.allocated``).
- ``allocations``: a jump starts ``tracemalloc`` (one frame a block), which only sees what is
  allocated from then on. At the next heartbeat it names the ``TOP_SITES`` lines that hold the
  most of what was allocated since it started and is still held (``held since tracing began 60s
  ago: krx_quant/x.py:12 120 MB, …; tracemalloc's own 9 MB``): in the jump's line when the memory
  jumped again (the 11-10 jump took two heartbeats), else in one INFO line (``memory jump:
  allocation tracing stopped …``), and tracing stops. So it traces a minute or two a jump: while
  it does, each allocation is slower (1.7 times in a loop of small lists, in the image's Python
  on Linux) and tracemalloc keeps about 60 bytes a block it traces (69 MB for 1.2 million).
  Tracing already on (``PYTHONTRACEMALLOC``) is read and left on.

Cheap at each heartbeat: a read of ``/proc/self/task/*/stat`` and ``threading.enumerate`` (a median
of 0.16 ms for 22 threads, in a container of the image's Python on Linux), in a worker thread
(``heartbeat.readings``). Only a jump reads the allocators and the traces: grouping 1.2 million
traces by line took 1.9 s there, in which the event loop went 66 ms at most without a turn.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import tracemalloc
from collections.abc import Callable, Mapping
from pathlib import Path

from krx_quant import footprint

log = logging.getLogger(__name__)

JUMP_MB = 100.0  # the growth since the heartbeat before that is a jump
WITHIN_SECONDS = 120.0  # the heartbeat before at most this far back (a minute apart, or a late beat)
TRACE_FRAMES = 1  # tracemalloc's frames a block: the line that allocated it
TOP_THREADS = 5
TOP_SITES = 5
_MB = 1024 * 1024

Threads = Mapping[int, tuple[str, float]]  # thread id -> its name, its CPU seconds so far


def thread_seconds(proc: Path = footprint.PROC) -> dict[int, tuple[str, float]]:
    """Each thread of the process's CPU time so far (user and system), by the kernel's thread id:
    the name Python gave it, else the kernel's. Empty where ``proc`` has no ``task`` (macOS)."""
    try:
        tasks = list((proc / "task").iterdir())
    except OSError:
        return {}
    named = {t.native_id: t.name for t in threading.enumerate() if t.native_id is not None}
    tick = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    seen: dict[int, tuple[str, float]] = {}
    for task in tasks:
        try:
            stat = (task / "stat").read_text()
            tid = int(task.name)
        except (OSError, ValueError):
            continue  # ended meanwhile
        # ``<tid> (<comm>) <state> …``: the name may hold spaces and brackets, so it ends at the last.
        comm, _, rest = stat.partition(" (")[2].rpartition(")")
        fields = rest.split()
        try:
            ticks = int(fields[11]) + int(fields[12])  # utime, stime: the stat's 14th and 15th
        except (IndexError, ValueError):
            continue
        seen[tid] = (named.get(tid, comm), ticks / tick)
    return seen


class JumpWatch:
    """The engine's memory at each heartbeat (``note``): a WARNING line when it jumped (the
    module's doc). ``elapsed`` is the clock in seconds, ``threads`` each thread's CPU time and
    ``allocated`` the allocators' figures (tests give theirs)."""

    def __init__(
        self,
        *,
        elapsed: Callable[[], float] = time.monotonic,
        threads: Callable[[], Threads] = thread_seconds,
        allocated: Callable[[], footprint.Allocated] = footprint.allocated,
    ) -> None:
        self._elapsed = elapsed
        self._threads = threads
        self._allocated = allocated
        self._last: tuple[float, float | None, Threads] | None = None  # the heartbeat before
        self._traced_from: float | None = None  # when this watch started tracing
        self._failed = False

    def note(self, rss_mb: float | None) -> None:
        """A heartbeat's reading of the engine's resident memory (None: it could not be read).
        In a worker thread: a jump's traces take seconds to read. One that fails is said once,
        and the heartbeat goes on."""
        try:
            self._note(rss_mb)
        except Exception:
            if not self._failed:
                log.exception("memory jump: could not watch the memory; the heartbeat goes on")
            self._failed = True

    def _note(self, rss_mb: float | None) -> None:
        now, threads = self._elapsed(), self._threads()
        before, self._last = self._last, (now, rss_mb, threads)
        if before is None or rss_mb is None or before[1] is None:
            self._stop_tracing(now)
            return
        since, grew = now - before[0], rss_mb - before[1]
        if grew <= JUMP_MB or since > WITHIN_SECONDS:
            self._stop_tracing(now)
            return
        python, malloc, free, arrow = self._allocated()
        log.warning(
            "memory jump: engine rss %.0f -> %.0f MB (+%.0f) in %.0fs; %s; python %d blocks,"
            " malloc %s MB in use and %s MB free, arrow %s MB; allocations: %s",
            before[1],
            rss_mb,
            grew,
            since,
            _cpu(before[2], threads),
            python,
            _mb(malloc),
            _mb(free),
            _mb(arrow),
            self._sites(now),
        )

    def _sites(self, now: float) -> str:
        """What tracing names (the module's doc); tracing starts if it is off."""
        if self._traced_from is None and not tracemalloc.is_tracing():
            tracemalloc.start(TRACE_FRAMES)
            self._traced_from = now
            return "traced from now on"
        began = (
            "" if self._traced_from is None else f" since tracing began {now - self._traced_from:.0f}s ago"
        )
        return f"held{began}: {_top()}"

    def _stop_tracing(self, now: float) -> None:
        """At a heartbeat without a jump: stop the tracing this watch started, with what it held."""
        if self._traced_from is None:
            return
        began, self._traced_from = self._traced_from, None
        try:
            held = _top()
        finally:
            tracemalloc.stop()
        log.info(
            "memory jump: allocation tracing stopped; held since it began %.0fs ago: %s", now - began, held
        )


def _cpu(before: Threads, now: Threads) -> str:
    """``cpu since the heartbeat before 41.3s: MainThread 30.1s, …, 3 more 0.2s``."""
    spent = sorted(
        (
            (seconds - before[tid][1] if tid in before else seconds, name)
            for tid, (name, seconds) in now.items()
        ),
        reverse=True,
    )
    if not spent:
        return "cpu since the heartbeat before ?"
    shown = [f"{name} {seconds:.1f}s" for seconds, name in spent[:TOP_THREADS]]
    rest = spent[TOP_THREADS:]
    if rest:
        shown.append(f"{len(rest)} more {sum(s for s, _ in rest):.1f}s")
    return f"cpu since the heartbeat before {sum(s for s, _ in spent):.1f}s: " + ", ".join(shown)


def _top() -> str:
    """The ``TOP_SITES`` lines that hold the most of what tracemalloc traced, ``<folder>/<file>:
    <line> <MB> MB``, and tracemalloc's own memory."""
    stats = tracemalloc.take_snapshot().statistics("lineno")
    top = [s for s in stats if s.traceback[0].filename != tracemalloc.__file__][:TOP_SITES]
    sites = ", ".join(
        f"{'/'.join(Path(s.traceback[0].filename).parts[-2:])}:{s.traceback[0].lineno} {s.size / _MB:.0f} MB"
        for s in top
    )
    return f"{sites or 'nothing'}; tracemalloc's own {tracemalloc.get_tracemalloc_memory() / _MB:.0f} MB"


def _mb(value: float | None) -> str:
    return "?" if value is None else f"{value:.0f}"
