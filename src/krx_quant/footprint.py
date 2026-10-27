"""Wall time and peak memory of a long run's steps (#355): the nightly training's; and of a
replayed day (#383): a pre-validation's or a simulated probation's (``DayUsage``).

A step is timed in phases (the nightly training's: reading the days, building the samples,
fitting, validating). Each phase's peak is the process's resident high-water mark
(``VmHWM`` in ``/proc/self/status``), reset at the phase's start by writing ``5`` to
``/proc/self/clear_refs`` (Linux 4.0 and later), so that it is that phase's own; a step's
peak is the highest of its phases'. Where the mark cannot be reset (another system, a
kernel that refuses), the peak is the process's since it started (``ru_maxrss``), and
``peak_since`` says ``start`` instead of ``step``. Without either (Windows has no ``resource``
module), the figures are None. Nothing here raises: a figure that cannot be read is None.

``give_back`` hands the memory a replayed day freed back to the system (#383). The allocators
keep what is freed for later: glibc's ``malloc``, in an arena for each thread that allocated
(the engine reads the stored predictions in worker threads, more of them each replayed day as
its database grows), and Arrow's pool, which read the day's files. Measured on made-up days
shaped like the recorded ones, the process grew by about 50 MB a replayed day while the Python
objects alive stayed the same; glibc held most of it as free memory. ``malloc_trim`` (glibc only)
and the pool's ``release_unused`` return it.
"""

from __future__ import annotations

import ctypes
import functools
import gc
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

PROC = Path("/proc/self")
PEAK_STEP = "step"  # the peaks are the step's and its phases' own
PEAK_START = "start"  # the peaks are the process's since it started
_MB = 1024 * 1024


class Meter:
    """The process's resident memory: now, and its peak since the last ``reset``."""

    def __init__(self, proc: Path = PROC) -> None:
        self._proc = proc
        self._resets: bool | None = None  # whether the peak could be reset, once tried

    @property
    def peak_since(self) -> str:
        return PEAK_STEP if self._resets else PEAK_START

    def reset(self) -> None:
        """Start the peak afresh, where the system lets it be."""
        if self._resets is False:
            return
        try:
            (self._proc / "clear_refs").write_text("5")
        except OSError:
            self._resets = False
        else:
            self._resets = _kb(self._proc / "status", "VmHWM") is not None

    def peak_mb(self) -> float | None:
        """The peak since the last reset (``VmHWM``), else since the process started."""
        if self._resets:
            kb = _kb(self._proc / "status", "VmHWM")
            if kb is not None:
                return round(kb / 1024, 1)
        try:
            import resource  # Unix only: not on Windows, where the engine's CLI also runs
        except ImportError:
            return None
        try:
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        except (OSError, ValueError):
            return None
        # Bytes on macOS, kilobytes on Linux.
        return round(peak / _MB if sys.platform == "darwin" else peak / 1024, 1)

    def rss_mb(self) -> float | None:
        """Resident now (``VmRSS``); None where it cannot be read."""
        kb = _kb(self._proc / "status", "VmRSS")
        return None if kb is None else round(kb / 1024, 1)


class StepUsage:
    """One step's wall time and peak memory, by phase, as its report keeps them."""

    def __init__(self, meter: Meter) -> None:
        self._meter = meter
        self._started = time.monotonic()
        self._peak: float | None = None
        self.phases: dict[str, dict[str, float | None]] = {}
        self.days_read = 0  # the training days this step read (built), not found in the cache
        self.days_made = 0  # of them, those made from their files, not read from the sample store (#370)
        meter.reset()

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        """Time ``name`` and keep its peak; a phase entered again adds to its time."""
        self._peak = _higher(self._peak, self._meter.peak_mb())  # since the step or last phase
        self._meter.reset()
        started = time.monotonic()
        try:
            yield
        finally:
            peak = self._meter.peak_mb()
            self._peak = _higher(self._peak, peak)
            self._meter.reset()
            kept = self.phases.setdefault(name, {"seconds": 0.0, "peak_rss_mb": None})
            kept["seconds"] = round(float(kept["seconds"] or 0.0) + time.monotonic() - started, 1)
            kept["peak_rss_mb"] = _higher(kept["peak_rss_mb"], peak)

    def as_dict(self, days_cached: int) -> dict[str, Any]:
        """The step's figures, at its end: ``days_cached`` are the days the cache holds now."""
        self._peak = _higher(self._peak, self._meter.peak_mb())
        return {
            "seconds": round(time.monotonic() - self._started, 1),
            "peak_rss_mb": self._peak,
            "rss_mb": self._meter.rss_mb(),
            "peak_since": self._meter.peak_since,
            "days_read": self.days_read,
            "days_made": self.days_made,
            "days_cached": days_cached,
            "phases": self.phases,
        }


class DayUsage:
    """A replayed day's memory (#383: a pre-validation's or a simulated probation's day), as its
    log line says it: the process's resident memory when the day starts, then at the end of each
    phase (its training, its replay) with that phase's peak."""

    def __init__(self, meter: Meter) -> None:
        self._meter = meter
        self.start_mb = meter.rss_mb()
        self.phases: dict[str, tuple[float | None, float | None]] = {}  # name -> (resident, peak)

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        """Keep ``name``'s resident memory at its end and its peak."""
        self._meter.reset()
        try:
            yield
        finally:
            self.phases[name] = (self._meter.rss_mb(), self._meter.peak_mb())

    def line(self) -> str:
        """``312.4 MB at the start; training 540.1 MB (peak 612.0); replay 498.7 MB (peak 701.3);
        peaks since each phase began``: in MB, ``?`` for a figure that cannot be read."""
        since = "each phase began" if self._meter.peak_since == PEAK_STEP else "the process started"
        parts = [f"{_mb(self.start_mb)} MB at the start"]
        parts += [f"{name} {_mb(rss)} MB (peak {_mb(peak)})" for name, (rss, peak) in self.phases.items()]
        return "; ".join(parts) + f"; peaks since {since}"


def give_back() -> None:
    """Free what a replayed day left (its engine's reference cycles) and hand the memory freed
    back to the system: Arrow's pool's, then glibc's (``malloc_trim``; another C library has none,
    and nothing is done there)."""
    gc.collect()
    try:
        import pyarrow

        pyarrow.default_memory_pool().release_unused()
    except (ImportError, AttributeError, OSError):
        pass
    trim = _malloc_trim()
    if trim is not None:
        trim(0)


@functools.cache
def _malloc_trim() -> Any:
    """glibc's ``malloc_trim``; None elsewhere (macOS, musl, Windows)."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        trim = ctypes.CDLL(None).malloc_trim  # the C library the process runs on
    except (OSError, AttributeError):
        return None
    trim.argtypes = [ctypes.c_size_t]
    trim.restype = ctypes.c_int
    return trim


def _mb(value: float | None) -> str:
    return "?" if value is None else f"{value:.1f}"


def _higher(a: float | None, b: float | None) -> float | None:
    if a is None:
        return b
    return a if b is None else max(a, b)


def _kb(path: Path, field: str) -> int | None:
    """A ``kB`` field of ``/proc/self/status``."""
    try:
        text = path.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if name == field and len(parts) == 2 and parts[1] == "kB" and parts[0].isdigit():
            return int(parts[0])
    return None
