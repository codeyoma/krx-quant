"""The engine's memory jumps (#680): a WARNING line when its resident memory grew more than 100 MB
since the heartbeat before, naming each thread's CPU since then, what the allocators hold, and the
lines that hold what was allocated since (``tracemalloc``, started by the jump and stopped at the
next heartbeat without one).

Input: a fake clock, the resident memory each heartbeat reads, each thread's CPU seconds and the
allocators' figures; for ``thread_seconds``, a made-up ``/proc/self/task`` in the kernel's layout.
Output: the lines logged."""

from __future__ import annotations

import logging
import threading
import tracemalloc
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

from krx_quant import heartbeat, memory_jump
from krx_quant.feeds.base import KST
from krx_quant.footprint import Allocated
from krx_quant.memory_jump import JumpWatch, thread_seconds

NOW = datetime(2026, 11, 10, 9, 53, tzinfo=KST)
HELD = Allocated(4_120_001, 880.4, 40.2, 0.0)


class Beats:
    """The heartbeats' readings: the clock (seconds) and each thread's CPU seconds, set by the test."""

    def __init__(self) -> None:
        self.at = 0.0
        self.threads: dict[int, tuple[str, float]] = {}

    def watch(self) -> JumpWatch:
        return JumpWatch(elapsed=lambda: self.at, threads=lambda: dict(self.threads), allocated=lambda: HELD)


@pytest.fixture(autouse=True)
def untraced() -> Iterator[None]:
    """No test leaves tracemalloc on for the next."""
    yield
    tracemalloc.stop()


def said(caplog: pytest.LogCaptureFixture, level: int = logging.WARNING) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == memory_jump.__name__ and r.levelno == level]


def test_a_jump_names_each_thread_s_cpu_and_what_the_allocators_hold(
    caplog: pytest.LogCaptureFixture,
) -> None:
    beats = Beats()
    watch = beats.watch()
    beats.threads = {1: ("MainThread", 100.0), 2: ("asyncio_2", 5.0), 3: ("ThreadPoolExecutor-0_0", 1.0)}
    beats.threads |= {10 + n: (f"worker-{n}", 0.5) for n in range(4)}
    watch.note(631.0)
    beats.at = 60.0
    beats.threads |= {1: ("MainThread", 130.1), 2: ("asyncio_2", 14.0), 3: ("ThreadPoolExecutor-0_0", 3.0)}
    beats.threads |= {20: ("lightgbm", 2.5), 10: ("worker-0", 0.6), 11: ("worker-1", 0.6)}  # 20 started since
    del beats.threads[13]  # ended since

    with caplog.at_level(logging.INFO, logger=memory_jump.__name__):
        watch.note(978.4)

    assert said(caplog) == [
        "memory jump: engine rss 631 -> 978 MB (+347) in 60s; cpu since the heartbeat before 43.8s:"
        " MainThread 30.1s, asyncio_2 9.0s, lightgbm 2.5s, ThreadPoolExecutor-0_0 2.0s, worker-1 0.1s,"
        " 2 more 0.1s; python 4120001 blocks, malloc 880 MB in use and 40 MB free, arrow 0 MB;"
        " allocations: traced from now on"
    ]
    assert tracemalloc.is_tracing()


def test_growth_of_100_mb_or_less_or_over_a_longer_time_is_no_jump(caplog: pytest.LogCaptureFixture) -> None:
    beats = Beats()
    watch = beats.watch()
    with caplog.at_level(logging.INFO, logger=memory_jump.__name__):
        watch.note(None)  # macOS: no /proc
        beats.at = 60.0
        watch.note(500.0)  # nothing to compare with
        beats.at = 120.0
        watch.note(600.0)  # +100 MB: not more than 100
        beats.at = 300.0
        watch.note(900.0)  # +300 MB, but over three minutes (the heartbeats stopped meanwhile)
        beats.at = 360.0
        watch.note(None)
        beats.at = 420.0
        watch.note(1200.0)  # the heartbeat before could not read it

    assert said(caplog) == [] and said(caplog, logging.INFO) == []
    assert not tracemalloc.is_tracing()


def test_the_next_heartbeat_names_the_lines_that_hold_what_was_allocated_since(
    caplog: pytest.LogCaptureFixture,
) -> None:
    beats = Beats()
    watch = beats.watch()
    watch.note(600.0)
    beats.at = 60.0
    with caplog.at_level(logging.INFO, logger=memory_jump.__name__):
        watch.note(760.0)  # tracing starts
        kept = [bytearray(30 * 1024 * 1024)]  # held by this line, after tracing started
        beats.at = 120.0
        watch.note(900.0)  # jumped again: its line names it
        kept.append(bytearray(20 * 1024 * 1024))
        beats.at = 180.0
        watch.note(905.0)  # calm: tracing stops, naming what it held
    here = f"tests/test_memory_jump.py:{kept_line()}"

    first, second = said(caplog)
    assert first.endswith("allocations: traced from now on")
    assert "allocations: held since tracing began 60s ago: " + here + " 30 MB" in second, second
    [stopped] = said(caplog, logging.INFO)
    assert stopped.startswith("memory jump: allocation tracing stopped; held since it began 120s ago: "), (
        stopped
    )
    assert f"{here} 30 MB" in stopped and "tracemalloc's own " in stopped
    assert not tracemalloc.is_tracing()
    del kept


def kept_line() -> int:
    """The line of ``kept = [bytearray(…)]`` above."""
    source = Path(__file__).read_text().splitlines()
    return next(n for n, text in enumerate(source, 1) if text.strip().startswith("kept = [bytearray("))


def test_tracing_already_on_is_read_and_left_on(caplog: pytest.LogCaptureFixture) -> None:
    tracemalloc.start()  # as PYTHONTRACEMALLOC does
    beats = Beats()
    watch = beats.watch()
    watch.note(600.0)
    beats.at = 60.0
    with caplog.at_level(logging.INFO, logger=memory_jump.__name__):
        watch.note(760.0)
        beats.at = 120.0
        watch.note(761.0)

    [line] = said(caplog)
    assert "allocations: held: " in line and "tracemalloc's own " in line
    assert said(caplog, logging.INFO) == []
    assert tracemalloc.is_tracing()


def test_a_watch_that_fails_says_so_once(caplog: pytest.LogCaptureFixture) -> None:
    def broken() -> dict[int, tuple[str, float]]:
        raise RuntimeError("a bug")

    watch = JumpWatch(threads=broken)
    with caplog.at_level(logging.INFO, logger=memory_jump.__name__):
        watch.note(600.0)
        watch.note(800.0)

    assert said(caplog, logging.ERROR) == ["memory jump: could not watch the memory; the heartbeat goes on"]


def fake_task(proc: Path, tid: int, comm: str, utime: int, stime: int) -> None:
    """``/proc/self/task/<tid>/stat`` as the kernel writes it (probed in the image's Python on
    Linux): ``<tid> (<comm>) <state> <ppid> …``, utime and stime the 14th and 15th fields in
    clock ticks."""
    fields = ["S", "0", "1", "1", "0", "-1", "4194560", "2250", "0", "5", "0", str(utime), str(stime)]
    fields += ["0", "0", "20", "0", "25", "0", "35551189"]
    (proc / "task" / str(tid)).mkdir(parents=True)
    (proc / "task" / str(tid) / "stat").write_text(f"{tid} ({comm}) {' '.join(fields)}\n")


def test_thread_seconds_reads_each_task_by_python_s_name_else_the_kernel_s(tmp_path: Path) -> None:
    proc = tmp_path / "self"
    mine = threading.get_native_id()
    fake_task(proc, mine, "python", 250, 50)
    fake_task(proc, 77, "a) b (c", 100, 0)  # a name with brackets and spaces
    (proc / "task" / "78").mkdir()  # ended while read: no stat

    assert thread_seconds(proc) == {mine: (threading.current_thread().name, 3.0), 77: ("a) b (c", 1.0)}
    assert thread_seconds(tmp_path / "nothing") == {}


async def test_the_heartbeat_gives_the_engine_s_memory_to_its_watch(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    (proc / "meminfo").write_text("MemAvailable:     831488 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n")
    beats = Beats()
    watch = beats.watch()

    async def beat(kb: int) -> str:
        (proc / "self" / "status").write_text(f"Name:\tpython\nVmRSS:\t{kb} kB\n")
        return await heartbeat.readings(None, lambda: NOW, proc, jumps=watch)

    with caplog.at_level(logging.INFO, logger=memory_jump.__name__):
        assert "rss engine 631 MB" in await beat(631 * 1024)
        beats.at = 60.0
        assert "rss engine 978 MB" in await beat(978 * 1024)

    [line] = said(caplog)
    assert line.startswith("memory jump: engine rss 631 -> 978 MB (+347) in 60s; "), line
