"""A step's peak memory by phase (#355), read from a made-up ``/proc/self``.

Input: a folder standing for ``/proc/self``: ``status`` with ``VmHWM`` and ``VmRSS``, and
``clear_refs``, which the meter writes ``5`` to at each phase's start. Output: the figures a
step's report keeps.
"""

from __future__ import annotations

from pathlib import Path

from krx_quant.footprint import PEAK_START, PEAK_STEP, Meter, StepUsage


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
