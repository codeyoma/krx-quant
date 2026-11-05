"""A busy morning through the real engine, timed bar close by bar close (#600).

``tests/test_bar_close_loop.py`` runs it in processes of their own, on two CPUs, with the image's
BLAS and OpenMP threads (read from the ``Dockerfile``), and compares what it measures with the
reference. Run by its path, never as ``tests.bar_close_bench``: the package's ``__init__`` sets
NumPy's BLAS threads for the tests (#574), and this measures the engine as the image runs it.

  python tests/bar_close_bench.py fit <models>              the models, fitted into <models>
  python tests/bar_close_bench.py replay <models> [--busy N]  one morning; one JSON line

The morning is #538's reproduction, as #589 timed it: 3 symbols at 40 book updates and 20 trades a
second each, the KOSPI and the KOSPI 200 futures once a second, from 09:00 to 10:31; every bar-close
and kind algorithm active, each with a model fitted on a made-up day before (the training's own
samples and answers of it, at 4 book updates a second), and every kind of every horizon. The hour
from 09:00 fills the quote log the 60-minute barrier's windows read; the bar closes from 10:00 are
measured. As on the server's two vCPUs: the collector's GIL switch interval (#538), asyncio's
default pool as Python sizes it there (min(32, 2 + 4) = 6 threads), and the live engine's watch of
its loop (``watch_loop``). ``--busy N`` spins N processes beside it on the same CPUs (the trader,
KIS frames, API requests).

Each measured bar close: the longest the event loop went without a turn (a heartbeat asking for one
every 10 ms, counted from when it was due), its time on the clock, the process's CPU, and the time
of each of its steps on the loop (``LoopWatch.step``: the engine's names). Besides: each algorithm's
time in its worker thread, the process's peak RSS, and how long a fixed piece of Python takes on
this machine (``calibration_s``).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import gc
import heapq
import json
import math
import os
import platform
import random
import resource
import statistics
import subprocess
import sys
import tempfile
import time as timer
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import krx_quant.engine as engine_module
from krx_quant import demo
from krx_quant.algorithms import (
    ALGORITHMS,
    KIND_ALGORITHMS,
    Algorithm,
    KindAlgorithm,
    NothingToLearn,
    load_models,
    save_model,
)
from krx_quant.clock import FakeClock
from krx_quant.collector import GIL_SWITCH_SECONDS
from krx_quant.engine import Event, PredictionEngine
from krx_quant.features import Leaders
from krx_quant.feeds.base import KST
from krx_quant.kind_registry import KINDS, kind_named
from krx_quant.kinds import DIRECTION
from krx_quant.lifecycle import Status
from krx_quant.loop_watch import LoopWatch
from krx_quant.predictions import PredictionStore
from krx_quant.replay import replay_into
from krx_quant.touch import TARGET_VERSION
from krx_quant.training import TrainingDay, recorded_day

RATE = 40.0  # book updates a second, each symbol; a trade every other one
TRAINING_RATE = 4.0  # the made-up day the models are fitted on
START, MEASURED_FROM, END = time(9, 0), time(10, 0), time(10, 31)
TRAINING_END = time(12, 0)
WORKERS = min(32, 2 + 4)  # asyncio's default pool on the server's two vCPUs
TICK = 0.01  # the heartbeat's
SYMBOLS = demo.DEMO_SYMBOLS
DAY = demo.DEMO_DAY
TRAINING_DAY = demo.PAST_DAYS[-1]


def morning(day: date, rate: float, end: time) -> Iterator[Event]:
    """The day's events from 09:00 to ``end``, in receipt order, made as they are read: a symbol's
    book ``rate`` times a second and a trade every other one (a buy at the ask or a sell at the
    bid), the KOSPI and the futures each second."""

    def symbol(n: int, code: str) -> Iterator[Event]:
        rng = random.Random(n)
        mid, tick = demo._OPENS[code]
        step = timedelta(seconds=1 / rate)
        when = datetime.combine(day, START, KST) + timedelta(milliseconds=7 * n)
        last = datetime.combine(day, end, KST)
        i = 0
        while when <= last:
            mid += tick * rng.choice((-1, 0, 0, 0, 0, 0, 0, 0, 1))
            bid, ask = mid - tick // 2, mid + tick // 2
            yield demo._book(when, code, bid, ask)
            if i % 2 == 0:
                bought = rng.random() < 0.5  # at the ask, else at the bid
                feed, row = demo._trade(when + step / 2, code, ask if bought else bid, rng.randint(1, 50))
                yield feed, {**row, "side": "buy" if bought else "sell"}
            when += step
            i += 1

    def leaders() -> Iterator[Event]:
        rng = random.Random(9)
        level = 0.0
        when, last = datetime.combine(day, START, KST), datetime.combine(day, end, KST)
        while when <= last:
            level += rng.gauss(0, 0.01)
            yield demo._index(when, "0001", 2600 * (1 + level / 100), level)
            yield demo._futures(when + timedelta(milliseconds=300), 350 * (1 + level / 100), level)
            when += timedelta(seconds=1)

    streams = [symbol(n, code) for n, code in enumerate(SYMBOLS)]
    return heapq.merge(*streams, leaders(), key=lambda event: event[1]["received_at"])


def fit(models: Path) -> dict[str, Any]:
    """Every bar-close and kind algorithm's model fitted on the made-up training day, kept in
    ``models`` as the training keeps them; the samples each learned from, by name."""
    every: list[Algorithm | KindAlgorithm] = [*ALGORITHMS, *KIND_ALGORITHMS]
    names = sorted({f for a in every for f in a.features})
    made, answers, left_out = recorded_day(
        morning(TRAINING_DAY, TRAINING_RATE, TRAINING_END),
        demo._CALENDAR,
        TRAINING_DAY,
        SYMBOLS,
        names,
        demo._FEE,
        Leaders(),
        target_version=TARGET_VERSION,
        kinds=[k for k in KINDS if k.name != DIRECTION],
    )
    # The bar closes while the events last; after them the book stands still.
    last = datetime.combine(TRAINING_DAY, TRAINING_END, KST) - timedelta(minutes=60)
    day = TrainingDay("mid", [s for s in made if s.bar_close <= last], answers, left_out)
    learned: dict[str, Any] = {}
    for algorithm in ALGORITHMS:
        try:
            training = day.of(algorithm.features)
            save_model(models, algorithm.name, algorithm.fit(training))
            learned[algorithm.name] = len(training)
        except NothingToLearn as e:
            learned[algorithm.name] = f"default: {e}"
    for kind_algorithm in KIND_ALGORITHMS:
        kind = kind_named(kind_algorithm.kind)
        assert kind is not None
        try:
            answered = day.of_kind(kind, kind_algorithm.features)
            save_model(models, kind_algorithm.name, kind_algorithm.fit(answered))
            learned[kind_algorithm.name] = len(answered)
        except NothingToLearn as e:
            learned[kind_algorithm.name] = f"default: {e}"
    return learned


class Heartbeat:
    """The longest the event loop went without a turn while ``inside``: a task asking for one every
    ``TICK``, each stretch counted from when its turn was due (or from ``open``)."""

    def __init__(self) -> None:
        self._time = asyncio.get_running_loop().time
        self._due = self._since = self._time()
        self.longest = 0.0
        self._inside = False
        self._task = asyncio.get_running_loop().create_task(self._beat())

    async def _beat(self) -> None:
        while True:
            self._due = self._time() + TICK
            await asyncio.sleep(TICK)
            if self._inside:
                self.longest = max(self.longest, self._time() - max(self._due, self._since))

    def open(self) -> None:
        self._since, self.longest, self._inside = self._time(), 0.0, True

    def close(self) -> float:
        self.longest = max(self.longest, self._time() - max(self._due, self._since))
        self._inside = False
        return self.longest

    def stop(self) -> None:
        self._task.cancel()


class Collections:
    """The garbage collector's passes from ``reset`` on: their seconds in all, and the longest
    one's, of the oldest generation alone too (it walks every object the process keeps)."""

    def __init__(self) -> None:
        self._started = 0.0
        self.reset()
        gc.callbacks.append(self._called)

    def reset(self) -> None:
        self.seconds, self.longest, self.oldest = 0.0, 0.0, 0.0

    def _called(self, phase: str, info: dict[str, int]) -> None:
        if phase == "start":
            self._started = timer.perf_counter()
            return
        spent = timer.perf_counter() - self._started
        self.seconds += spent
        self.longest = max(self.longest, spent)
        if info["generation"] == 2:
            self.oldest += spent


class Steps(LoopWatch):
    """The engine's watch of its loop, adding up each step's seconds (``spent``)."""

    made: list[Steps] = []

    def __init__(self) -> None:
        super().__init__()
        self.spent: defaultdict[str, float] = defaultdict(float)
        Steps.made.append(self)

    @contextlib.contextmanager
    def step(self, name: str) -> Iterator[None]:
        started = timer.perf_counter()
        try:
            with super().step(name):
                yield
        finally:
            self.spent[name] += timer.perf_counter() - started


def timed_in_thread(
    timings: defaultdict[str, list[tuple[float, float]]], measuring: Callable[[], bool], name_at: int
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Wrap an algorithm's work in its worker thread: its clock and thread CPU seconds, by name
    (the argument at ``name_at``), while ``measuring``."""

    def wrap(work: Callable[..., Any]) -> Callable[..., Any]:
        def run(*args: Any) -> Any:
            wall, cpu = timer.perf_counter(), timer.thread_time()
            try:
                return work(*args)
            finally:
                if measuring():
                    timings[args[name_at].name].append(
                        (timer.perf_counter() - wall, timer.thread_time() - cpu)
                    )

        return run

    return wrap


def calibration() -> float:
    """The best of three runs of a fixed piece of pure Python (dicts, floats, a sort), in seconds:
    how fast this machine is now."""
    best = math.inf
    for _ in range(3):
        started = timer.perf_counter()
        rows = [{"at": i, "mid": i * 1.5, "qty": i % 7} for i in range(200_000)]
        rows.sort(key=lambda r: (r["qty"], -r["mid"]))
        sum(r["mid"] * r["qty"] for r in rows)
        best = min(best, timer.perf_counter() - started)
    return best


def summary(values: list[float]) -> dict[str, float]:
    """Median, p95 (nearest rank) and max, to the millisecond's tenth."""
    ordered = sorted(values)
    p95 = ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]
    return {
        "median": round(statistics.median(ordered), 4),
        "p95": round(p95, 4),
        "max": round(ordered[-1], 4),
    }


def peak_rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (2**20 if sys.platform == "darwin" else 2**10), 1)  # bytes on macOS, KiB on Linux


def memory_max_mb() -> float | None:
    """The memory this process's cgroup may use (cgroup v2's ``memory.max``), None without a limit."""
    with contextlib.suppress(OSError, ValueError, IndexError):
        group = Path("/proc/self/cgroup").read_text().splitlines()[0].split(":", 2)[2]
        limit = (Path("/sys/fs/cgroup") / group.lstrip("/") / "memory.max").read_text().strip()
        return None if limit == "max" else round(int(limit) / 2**20, 1)
    return None


def cpu_model() -> str:
    with contextlib.suppress(OSError):
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    return platform.processor() or platform.machine()


async def replay_morning(models: Path) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(WORKERS))
    sys.setswitchinterval(GIL_SWITCH_SECONDS)
    db = Path(tempfile.mkdtemp()) / "engine.db"
    store = PredictionStore(db)
    registered = datetime(2026, 10, 1, tzinfo=KST)
    every: list[Algorithm | KindAlgorithm] = [*ALGORITHMS, *KIND_ALGORITHMS]
    for algorithm in every:
        store.add_status(algorithm.name, registered, None, Status.ACTIVE, "bench")
    store.mark_target(TARGET_VERSION, registered)
    clock = FakeClock(datetime.combine(DAY, time(), KST))
    measured_from = datetime.combine(DAY, MEASURED_FROM, KST)
    heart = Heartbeat()
    passes = Collections()
    closes: list[dict[str, Any]] = []
    timings: defaultdict[str, list[tuple[float, float]]] = defaultdict(list)

    def measuring() -> bool:
        return clock.now() >= measured_from

    engine_module.LoopWatch = Steps  # type: ignore[attr-defined]
    engine_module._predict_symbols = timed_in_thread(timings, measuring, 0)(engine_module._predict_symbols)
    engine_module._predict_kind_symbols = timed_in_thread(timings, measuring, 1)(
        engine_module._predict_kind_symbols
    )

    class Measured(PredictionEngine):
        async def run_due(self) -> None:
            at = self.clock.now()
            Steps.made.clear()
            heart.open()
            passes.reset()
            started, cpu = timer.perf_counter(), timer.process_time()
            try:
                await super().run_due()
            finally:
                longest = heart.close()
                if at >= measured_from and Steps.made:
                    spent: defaultdict[str, float] = defaultdict(float)
                    for watch in Steps.made:
                        for name, seconds in watch.spent.items():
                            spent[name] += seconds
                    closes.append(
                        {
                            "at": f"{at.astimezone(KST):%H:%M}",
                            "longest_gap": longest,
                            "bar_close": timer.perf_counter() - started,
                            "cpu": timer.process_time() - cpu,
                            "steps": dict(spent),
                            "gc": passes.seconds,
                            "gc_longest": passes.longest,
                            "gc_oldest": passes.oldest,
                        }
                    )

    engine = Measured(
        store,
        demo._CALENDAR,
        SYMBOLS,
        demo._FEE,
        clock,
        algorithms=ALGORITHMS,
        kind_algorithms=KIND_ALGORITHMS,
        kinds=KINDS,
        models=load_models(models, every),
        target_version=TARGET_VERSION,
        watch_loop=True,
    )
    events = Counter[str]()

    def counted() -> Iterator[Event]:
        for event in morning(DAY, RATE, END):
            events[event[0]] += 1
            yield event

    replayed = timer.perf_counter()
    await replay_into(engine, clock, counted(), until=datetime.combine(DAY, END, KST))
    replay_s = timer.perf_counter() - replayed
    heart.stop()
    engine.store.close()
    names = sorted({name for c in closes for name in c["steps"]})
    on_loop = [sum(c["steps"].values()) for c in closes]
    return {
        "events": sum(events.values()),
        "replay_s": round(replay_s, 1),
        "bar_closes": len(closes),
        "longest_gap_s": summary([c["longest_gap"] for c in closes]),
        "bar_close_s": summary([c["bar_close"] for c in closes]),
        "cpu_s": summary([c["cpu"] for c in closes]),
        "on_loop_s": summary(on_loop),
        "gc_s": summary([c["gc"] for c in closes]),
        "gc_longest_s": summary([c["gc_longest"] for c in closes]),
        "steps_s": {name: summary([c["steps"].get(name, 0.0) for c in closes]) for name in names},
        "algorithms_s": {
            name: {
                "wall_median": round(statistics.median(w for w, _ in rows), 4),
                "cpu_median": round(statistics.median(c for _, c in rows), 4),
            }
            for name, rows in sorted(timings.items())
        },
        "slowest": sorted(closes, key=lambda c: -c["longest_gap"])[:3],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("what", choices=("fit", "replay"))
    parser.add_argument("models", type=Path)
    parser.add_argument("--busy", type=int, default=0, help="processes spinning beside it")
    args = parser.parse_args()
    if args.what == "fit":
        print(json.dumps({"fitted": fit(args.models)}), flush=True)
        return
    cpus = sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None
    busy = [
        subprocess.Popen([sys.executable, "-c", "while True: pass"]) for _ in range(args.busy)
    ]  # on this process's CPUs: a child keeps its parent's
    try:
        calibration_s = calibration()
        figures = asyncio.run(replay_morning(args.models))
    finally:
        for process in busy:
            process.kill()
            process.wait()
    print(
        json.dumps(
            {
                "threads": {k: os.environ.get(k) for k in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS")},
                "cpus": cpus,
                "cpu_model": cpu_model(),
                "busy": args.busy,
                "calibration_s": round(calibration_s, 4),
                "memory_max_mb": memory_max_mb(),
                "rss_peak_mb": peak_rss_mb(),
                **figures,
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
