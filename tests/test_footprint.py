"""A step's peak memory by phase (#355), read from a made-up ``/proc/self``.

Input: a folder standing for ``/proc/self``: ``status`` with ``VmHWM`` and ``VmRSS``, and
``clear_refs``, which the meter writes ``5`` to at each phase's start. Output: the figures a
step's report keeps.
"""

from __future__ import annotations

import importlib
import platform
import sys
from pathlib import Path

import pytest

from krx_quant import footprint
from krx_quant.footprint import PEAK_START, PEAK_STEP, DayUsage, Meter, StepUsage


class FakeProc:
    """``/proc/self`` whose peak a test sets, and whose reset takes it down to the resident size."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir()
        (root / "clear_refs").write_text("")
        self.resets = 0
        self.set(peak_kb=100 * 1024, rss_kb=100 * 1024)

    def set(self, *, peak_kb: int, rss_kb: int) -> None:
        (self.root / "status").write_text(f"Name:\tpython\nVmHWM:\t{peak_kb} kB\nVmRSS:\t{rss_kb} kB\n")

    def reset_seen(self) -> bool:
        """Whether the meter asked for a reset since the last look, as the kernel takes it."""
        asked = (self.root / "clear_refs").read_text() == "5"
        (self.root / "clear_refs").write_text("")
        return asked


def test_each_phase_has_its_own_peak_and_the_step_the_highest(tmp_path: Path) -> None:
    proc = FakeProc(tmp_path / "proc")
    usage = StepUsage(Meter(proc.root))
    assert proc.reset_seen(), "the step starts its peak afresh"

    with usage.phase("days"):
        assert proc.reset_seen()
        proc.set(peak_kb=900 * 1024, rss_kb=600 * 1024)
    proc.set(peak_kb=600 * 1024, rss_kb=600 * 1024)  # the kernel's reset: down to the resident
    with usage.phase("fit"):
        assert proc.reset_seen()
        proc.set(peak_kb=700 * 1024, rss_kb=500 * 1024)
    with usage.phase("days"):  # again: its time adds up, its peak is the higher
        proc.set(peak_kb=650 * 1024, rss_kb=500 * 1024)
    usage.days_read = 3

    found = usage.as_dict(days_cached=23)
    assert found["peak_since"] == PEAK_STEP
    assert found["peak_rss_mb"] == 900.0
    assert found["rss_mb"] == 500.0
    assert (found["days_read"], found["days_cached"]) == (3, 23)
    assert found["phases"]["days"]["peak_rss_mb"] == 900.0
    assert found["phases"]["fit"]["peak_rss_mb"] == 700.0
    assert set(found["phases"]) == {"days", "fit"}
    assert all(p["seconds"] is not None and p["seconds"] >= 0 for p in found["phases"].values())


def test_without_a_peak_to_reset_the_peak_is_the_process_s_and_says_so(tmp_path: Path) -> None:
    """macOS, or a kernel without ``clear_refs``: ``ru_maxrss``, the process's since it started."""
    usage = StepUsage(Meter(tmp_path / "no-proc"))
    with usage.phase("fit"):
        pass
    found = usage.as_dict(days_cached=0)
    assert found["peak_since"] == PEAK_START
    assert found["peak_rss_mb"] is not None and found["peak_rss_mb"] > 0
    assert found["rss_mb"] is None, "no VmRSS to read"
    assert found["phases"]["fit"]["peak_rss_mb"] == found["peak_rss_mb"]


def test_without_proc_nor_resource_the_figures_are_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows has no ``resource`` module: the module still loads (the CLI imports it through
    the nightly training), and the memory figures are None."""
    monkeypatch.setitem(sys.modules, "resource", None)  # ``import resource`` raises ImportError
    try:
        reloaded = importlib.reload(footprint)
        usage = reloaded.StepUsage(reloaded.Meter(tmp_path / "no-proc"))
        with usage.phase("fit"):
            pass
        found = usage.as_dict(days_cached=0)
    finally:
        monkeypatch.undo()
        importlib.reload(footprint)
    assert (found["peak_rss_mb"], found["rss_mb"]) == (None, None)
    assert found["phases"]["fit"] == {"seconds": found["phases"]["fit"]["seconds"], "peak_rss_mb": None}


def test_a_replayed_day_s_line_has_its_start_and_each_phase_s_memory_and_peak(tmp_path: Path) -> None:
    """A pre-validation's or a simulation's day (#383): the resident memory it starts with, then
    each phase's at its end, with the phase's own peak."""
    proc = FakeProc(tmp_path / "proc")
    proc.set(peak_kb=300 * 1024, rss_kb=300 * 1024)
    day = DayUsage(Meter(proc.root))
    with day.phase("training"):
        assert proc.reset_seen(), "each phase starts its peak afresh"
        proc.set(peak_kb=700 * 1024, rss_kb=450 * 1024)
    with day.phase("replay"):
        assert proc.reset_seen()
        proc.set(peak_kb=900 * 1024, rss_kb=500 * 1024)

    assert day.phases == {"training": (450.0, 700.0), "replay": (500.0, 900.0)}
    assert day.line() == (
        "300.0 MB at the start; training 450.0 MB (peak 700.0); replay 500.0 MB (peak 900.0); "
        "peaks since each phase began"
    )


def test_a_replayed_day_s_figures_that_cannot_be_read_are_marked(tmp_path: Path) -> None:
    """macOS: no ``VmRSS``, and the peak is the process's since it started."""
    day = DayUsage(Meter(tmp_path / "no-proc"))
    with day.phase("replay"):
        pass
    line = day.line()
    assert line.startswith("? MB at the start; replay ? MB (peak ")
    assert line.endswith("; peaks since the process started")


def test_giving_back_the_memory_freed_runs_wherever_the_engine_does() -> None:
    """``malloc_trim`` on glibc (the server's Linux, CI's); nothing to trim elsewhere (macOS)."""
    footprint.give_back()  # never raises
    trim = footprint._malloc_trim()
    assert (trim is not None) == (sys.platform.startswith("linux") and platform.libc_ver()[0] == "glibc")
