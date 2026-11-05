"""A bar close leaves the event loop free for what else runs on it (#538).

The collector's event loop also reads KIS and streams the best quotes to the trader, which stops
new entries when none come for 5 s. On the server the quotes stalled for 5-6 s right after the
bar closes: the bar close's work ran on the loop, the kinds' answers over the longest horizon's
hour of quotes the heaviest, twice per window.

Input: a test kind of a 15-minute horizon (``drift``, ``tests/kind_doubles.py``) whose answer
function stands in for an hour of a busy symbol's quotes (it takes a fixed time, ``spend``), on a
replayed morning; a kind baseline that holds the loop at one bar close; the collector on the fake
KIS. Output: how long the event loop went without a turn during the bar closes, how many times
each window was answered, the WARNING that names what held the loop, and how soon the collector
hands the GIL over.
"""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time as timer
from collections import Counter
from collections.abc import Awaitable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant.clock import FakeClock
from krx_quant.collector import GIL_SWITCH_SECONDS, run_collector
from krx_quant.engine import PredictionEngine
from krx_quant.fake_kis import FakeKis
from krx_quant.kinds import Answer, Followed, KindForecast, KindInputs
from krx_quant.loop_watch import LIMIT_SECONDS, LOOP_HELD
from krx_quant.predictions import PredictionStore
from krx_quant.replay import replay_into
from krx_quant.touch import TARGET_VERSION
from tests.algorithm_doubles import spend
from tests.collector_doubles import FIXED_NOW, settings_for
from tests.kind_doubles import TEST_KIND, AlwaysHeld, drift_of
from tests.replay_doubles import busy_morning
from tests.replay_events import CALENDAR, DAY, FEE, SYMBOL, at, vi
from tests.waiting import until

ROOT = Path(__file__).resolve().parents[1]
QUARTER = timedelta(minutes=15)
HEAVY_AT = at("09:15:00")  # the window from here ends at the 09:30 bar close
ANSWER_SECONDS = 1.0  # an hour of a busy symbol's quotes, answered on a slow server


async def longest_gap(work: Awaitable[Any]) -> float:
    """The longest the event loop went without a turn while ``work`` ran, in seconds: a
    heartbeat that asks for a turn every 10 ms, less the 10 ms it sleeps."""
    longest = 0.0

    async def beat() -> None:
        nonlocal longest
        last = timer.perf_counter()
        while True:
            await asyncio.sleep(0.01)
            now = timer.perf_counter()
            longest = max(longest, now - last - 0.01)
            last = now

    beating = asyncio.create_task(beat())
    await asyncio.sleep(0)  # the heartbeat starts before the work
    try:
        await work
    finally:
        beating.cancel()
    return longest


def engine_on(db: Path, clock: FakeClock, **options: Any) -> PredictionEngine:
    return PredictionEngine(
        PredictionStore(db),
        CALENDAR,
        (SYMBOL,),
        FEE,
        clock,
        algorithms=[],
        kind_algorithms=[],
        target_version=TARGET_VERSION,
        **options,
    )


async def test_a_bar_close_answers_a_kind_s_window_once_off_the_event_loop(tmp_path: Path) -> None:
    """Input: the 15-minute drift, its answer taking 1 s for the window from 09:15 (answered at
    the 09:30 bar close, where its baselines also read it as the window just over). Output: the
    event loop never goes 0.5 s without a turn, and each window is answered once."""
    answered: Counter[datetime] = Counter()

    def heavy(window: Followed) -> Answer:
        answered[window.bar_close] += 1
        if window.bar_close == HEAVY_AT:
            spend(ANSWER_SECONDS)
        return drift_of(window)

    clock = FakeClock(at("09:00:00"))
    engine = engine_on(
        tmp_path / "engine.db", clock, kinds=(replace(TEST_KIND, answer=heavy, horizon=QUARTER),)
    )
    events = sorted(busy_morning(end="09:32:00"), key=lambda e: e[1]["received_at"])

    gap = await longest_gap(replay_into(engine, clock, events, until=at("09:32:00")))

    assert answered[HEAVY_AT] == 1, "the window just over is answered once, its baselines read that answer"
    assert set(answered.values()) == {1}
    assert gap < ANSWER_SECONDS / 2, f"the loop went {gap:.2f}s without a turn"
    engine.store.close()


async def test_a_situation_learned_while_the_answers_are_made_marks_them_special(tmp_path: Path) -> None:
    """Input: the 15-minute drift's window from 09:15, its answer held in the worker thread at the
    09:30 bar close until a VI of 09:20:10-09:22:20 is learned on the event loop (the status poll's
    answer at 09:30). Output: the window's answer, stored after the VI was learned, is special."""
    started, go = threading.Event(), threading.Event()

    def held(window: Followed) -> Answer:
        if window.bar_close == HEAVY_AT:
            started.set()
            go.wait(5)
        return drift_of(window)

    clock = FakeClock(at("09:00:00"))
    engine = engine_on(
        tmp_path / "engine.db", clock, kinds=(replace(TEST_KIND, answer=held, horizon=QUARTER),)
    )
    events = sorted(busy_morning(end="09:29:59"), key=lambda e: e[1]["received_at"])
    await replay_into(engine, clock, events, until=at("09:29:59"))
    clock.set(at("09:30:00"))

    closing = asyncio.create_task(engine.run_due())
    await until(started.is_set)
    engine.on_event(*vi(at("09:30:00"), "09:20:10", "09:22:20"))
    go.set()
    await closing

    [answer] = [
        a for a in engine.store.kind_answers("drift", SYMBOL) if a["bar_close"] == HEAVY_AT.isoformat()
    ]
    assert answer["special"]
    engine.store.close()


class Stuck(AlwaysHeld):
    """A kind baseline that holds the event loop at the 09:10 bar close (a stand-in for work on
    the loop that has gone slow)."""

    name = "drift-stuck"

    def predict(self, inputs: KindInputs) -> KindForecast:
        if inputs.bar_close == at("09:10:00"):
            timer.sleep(1.2)  # on the loop's thread: nothing else runs meanwhile
        return super().predict(inputs)


async def test_a_bar_close_that_holds_the_loop_over_a_second_says_what_held_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Input: a kind baseline holding the loop 1.2 s at the 09:10 bar close, the engine watching
    its loop. Output: one WARNING, for 09:10, with how long the loop went without a turn and the
    kinds' baselines as what took that time, and the garbage collection in it (#621); none for the
    other bar closes. The same in the store's runs (#589), for the night's report of the bar closes'
    times."""
    clock = FakeClock(at("09:00:00"))
    stuck = replace(TEST_KIND, baselines=(Stuck(),))
    engine = engine_on(tmp_path / "engine.db", clock, kinds=(stuck,), watch_loop=True)
    events = sorted(busy_morning(end="09:12:00"), key=lambda e: e[1]["received_at"])

    with caplog.at_level(logging.WARNING, logger="krx_quant.engine"):
        await replay_into(engine, clock, events, until=at("09:12:00"))

    held = [r.getMessage() for r in caplog.records if "event loop" in r.getMessage()]
    assert len(held) == 1, held
    [line] = held
    assert line.startswith(f"bar close {DAY} 09:10 held the event loop ")
    seconds = float(line.split("held the event loop ")[1].split("s")[0])
    assert seconds >= 1.2
    steps, collected = line.split(": ", 1)[1].split("; ")
    named = dict(part.rsplit(" ", 1) for part in steps.split(", "))
    assert float(named["the kinds' baselines"].rstrip("s")) >= 1.2
    assert collected == "no garbage collection" or re.fullmatch(
        r"garbage collection \d+\.\d\ds \(generation [012]\)", collected
    ), collected
    with sqlite3.connect(tmp_path / "engine.db") as db:
        runs = db.execute("SELECT at, detail FROM runs WHERE kind = ?", (LOOP_HELD,)).fetchall()
    assert [when for when, _ in runs] == [at("09:10:00").isoformat()]
    detail = json.loads(runs[0][1])
    assert detail["seconds"] == pytest.approx(seconds, abs=0.01)
    assert detail["parts"]["the kinds' baselines"] >= 1.2
    assert (detail["gc"] is None) == (collected == "no garbage collection")
    engine.store.close()


class Collecting(AlwaysHeld):
    """A kind baseline that runs a full garbage collection on the event loop at the 09:10 bar
    close; ``slow`` makes that collection's end wait 1.2 s (a stand-in for a pass over the session's
    quote logs on a slow server, #621)."""

    name = "drift-collecting"
    collecting = False

    def predict(self, inputs: KindInputs) -> KindForecast:
        if inputs.bar_close == at("09:10:00"):
            Collecting.collecting = True
            try:
                gc.collect()
            finally:
                Collecting.collecting = False
        return super().predict(inputs)

    @staticmethod
    def slow(phase: str, info: dict[str, Any]) -> None:
        if phase == "stop" and Collecting.collecting and info["generation"] == 2:
            timer.sleep(1.2)  # within the pass, on the loop's thread


async def test_a_bar_close_held_by_a_garbage_collection_says_how_long_it_took(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Input: a kind baseline whose full garbage collection at the 09:10 bar close takes 1.2 s, the
    engine watching its loop. Output: one WARNING, for 09:10, ending with the collection's seconds
    and its generation (2); the same in the store's run (``gc``), for the night's report (#621)."""
    clock = FakeClock(at("09:00:00"))
    kind = replace(TEST_KIND, baselines=(Collecting(),))
    engine = engine_on(tmp_path / "engine.db", clock, kinds=(kind,), watch_loop=True)
    events = sorted(busy_morning(end="09:12:00"), key=lambda e: e[1]["received_at"])
    gc.callbacks.insert(0, Collecting.slow)  # before the engine's watch: its stop waits for this
    try:
        with caplog.at_level(logging.WARNING, logger="krx_quant.engine"):
            await replay_into(engine, clock, events, until=at("09:12:00"))
    finally:
        gc.callbacks.remove(Collecting.slow)

    [line] = [r.getMessage() for r in caplog.records if "event loop" in r.getMessage()]
    assert line.startswith(f"bar close {DAY} 09:10 held the event loop ")
    collected = re.fullmatch(r".*; garbage collection (\d+\.\d\d)s \(generation (\d)\)", line)
    assert collected is not None, line
    assert float(collected[1]) >= 1.2 and collected[2] == "2"
    with sqlite3.connect(tmp_path / "engine.db") as db:
        [(detail,)] = db.execute("SELECT detail FROM runs WHERE kind = ?", (LOOP_HELD,)).fetchall()
    assert json.loads(detail)["gc"] == {"seconds": float(collected[1]), "generation": 2}
    engine.store.close()


async def test_the_collector_hands_the_gil_over_sooner_while_it_runs(kis: FakeKis, tmp_path: Path) -> None:
    """Input: the collector on the fake KIS. Output: while it runs, a thread waiting for the GIL
    takes it after ``GIL_SWITCH_SECONDS``, a tenth of Python's 5 ms; once it stops, as before."""
    kis.script[("H0UNCNT0", "005930")] = []
    before = sys.getswitchinterval()
    stop = asyncio.Event()
    task = asyncio.create_task(run_collector(settings_for(kis, tmp_path), stop=stop, clock=lambda: FIXED_NOW))
    await kis.wait_for_subscriptions(set(kis.script))

    running = sys.getswitchinterval()
    stop.set()
    await asyncio.wait_for(task, 5)

    assert running == pytest.approx(GIL_SWITCH_SECONDS)
    assert GIL_SWITCH_SECONDS == pytest.approx(0.0005)
    assert sys.getswitchinterval() == before


# A busy morning's bar closes on the server's two CPUs and 2 GB, against the reference (#600). The
# ci workflow's `bar-close` job runs it (BAR_CLOSE_BENCH=1), and a deploy waits for the whole run
# (#541): a commit that slows the bar closes is not deployed. The bench (``bar_close_bench.py``) runs
# in a process of its own with the image's BLAS and OpenMP threads, not the tests' (``__init__``).
TESTS = Path(__file__).resolve().parent
BENCH = TESTS / "bar_close_bench.py"
REFERENCE = TESTS / "bar_close_reference.json"
THREAD_VARIABLES = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")
CPUS = 2  # the server's vCPUs
MEMORY = "2G"  # the server's
BUSY = 1  # a process spinning on the same CPUs: the trader, KIS frames, API requests
# A figure over its limit fails only if the morning measured again is over too: one slow run (a busy
# runner) is not a slower engine.
ATTEMPTS = 2
# The limits, each the reference's figure times its factor. The reference is the median of 50 mornings
# of main on GitHub's runners on 2026-11-06 (three CPU models: EPYC 7763, 9V74, 9V45); of those, the
# median longest gap reached 1.38 times the reference's, the time on the loop 1.22, the bar close
# 1.27 (the CPU model moves it: 0.69-0.86 s on a 9V45, 0.89-1.26 s on a 9V74, and a morning measured
# again runs on the same one) and the peak RSS 1.01. #538's double answering (12 mornings) took the
# median gap to 35-64 times, the time on the loop to 10-19 times and the gaps' p95 to 1.3-2.4 s. The
# gaps' p95 is held to the engine's own WARNING limit (``loop_watch.LIMIT_SECONDS``) instead: a full
# garbage collection held the loop 0.5-0.7 s, and where the few in a morning land moved the p95 of its
# 32 bar closes from 0.04 s to 0.52 s. Since #621 the engine freezes what it keeps after each bar close
# (``garbage.py``) and those passes take milliseconds (p95 0.040-0.049 s in 6 mornings), but a store
# write waiting for the runner's disk moves the p95 the same way (0.35-0.57 s, a few times in 30).
FACTORS = {
    ("longest_gap_s", "median"): 3.0,
    ("on_loop_s", "median"): 3.0,
    ("bar_close_s", "median"): 1.75,
    ("rss_peak_mb",): 1.2,
}


def image_threads() -> dict[str, str]:
    """The BLAS and OpenMP threads the image sets for each of its processes (``Dockerfile``, #589)."""
    text = (ROOT / "Dockerfile").read_text().replace("\\\n", " ")
    lines = [line for line in text.splitlines() if line.startswith("ENV ")]
    return dict(re.findall(r"\b((?:OPENBLAS|OMP|MKL)_NUM_THREADS)=(\S+)", "\n".join(lines)))


def bench(*args: str, limited: bool = False) -> dict[str, Any]:
    """One run of the bench, with the image's threads; ``limited``: on two CPUs (``taskset``) in a
    cgroup of 2 GB (``systemd-run``), as the server's."""
    env = {k: v for k, v in os.environ.items() if k not in THREAD_VARIABLES} | image_threads()
    command = [sys.executable, str(BENCH), *args]
    if limited:
        assert hasattr(os, "sched_getaffinity"), "taskset and systemd-run (Linux)"
        cpus = ",".join(str(cpu) for cpu in sorted(os.sched_getaffinity(0))[:CPUS])
        memory = ["-p", f"MemoryMax={MEMORY}", "-p", "MemorySwapMax=0"]
        command = ["systemd-run", "--user", "--scope", "-q", *memory, "taskset", "-c", cpus, *command]
    done = subprocess.run(command, env=env, capture_output=True, text=True, timeout=900, check=False)
    assert done.returncode == 0, f"{' '.join(command)} ended with {done.returncode}:\n{done.stderr[-3000:]}"
    out: dict[str, Any] = json.loads(done.stdout)
    return out


def figure(figures: dict[str, Any], key: tuple[str, ...]) -> float:
    value: Any = figures
    for part in key:
        value = value[part]
    return float(value)


def over_limits(figures: dict[str, Any], reference: dict[str, Any]) -> list[str]:
    """What of a morning is over its limit: each figure against the reference's times its factor,
    and the gaps' p95 against the WARNING's limit."""
    over = []
    for key, factor in FACTORS.items():
        value, limit = figure(figures, key), factor * figure(reference, key)
        if value > limit:
            over.append(f"{'.'.join(key)} {value:g} over {limit:.4g} ({factor:g} x the reference's)")
    p95 = figures["longest_gap_s"]["p95"]
    if p95 > LIMIT_SECONDS:
        over.append(f"longest_gap_s.p95 {p95:g} over the WARNING's {LIMIT_SECONDS:g}s")
    return over


@pytest.mark.skipif(
    os.environ.get("BAR_CLOSE_BENCH") != "1", reason="minutes on two CPUs: the ci workflow's bar-close job"
)
@pytest.mark.skipif(sys.platform != "linux", reason="taskset and systemd-run (Linux)")
# The models' fit (about a minute) and up to two mornings of 3-6 minutes each on a runner.
@pytest.mark.timeout(1800)
def test_a_busy_morning_s_bar_closes_on_two_cpus_stay_near_the_reference(tmp_path: Path) -> None:
    """Input: the bench's morning, with every algorithm's model fitted on a made-up day, on two
    CPUs and 2 GB beside a busy process, the image's threads. Output: its figures within the
    reference's limits, in the first morning or, if not, in a second; each figure written where
    ``BAR_CLOSE_FIGURES`` says, for the reference's refresh."""
    reference = json.loads(REFERENCE.read_text())
    fitted = bench("fit", str(tmp_path))["fitted"]
    assert all(isinstance(samples, int) for samples in fitted.values()), f"each fitted: {fitted}"
    mornings: list[dict[str, Any]] = []
    for _ in range(ATTEMPTS):
        figures = bench("replay", str(tmp_path), "--busy", str(BUSY), limited=True)
        mornings.append(figures)
        if (written := os.environ.get("BAR_CLOSE_FIGURES")) is not None:
            Path(written).parent.mkdir(parents=True, exist_ok=True)
            Path(written).write_text(json.dumps(mornings, indent=1) + "\n")
        assert figures["fitted_models"] == sorted(fitted), "the engine predicts with the fitted models"
        assert figures["threads"] == {k: image_threads().get(k) for k in figures["threads"]}
        assert len(figures["cpus"]) == CPUS and figures["memory_max_mb"] == 2048, figures
        assert figures["bar_closes"] == 32, "every bar close from 10:00 to 10:31 measured"
        over = over_limits(figures, reference)
        if not over:
            return
    shown = [{k: m[k] for k in ("cpu_model", "longest_gap_s", "on_loop_s", "bar_close_s")} for m in mornings]
    pytest.fail(f"over the reference's limits in {ATTEMPTS} mornings: {over}\n{json.dumps(shown)}")


def test_the_ci_workflow_runs_the_bench_on_a_runner_of_its_own() -> None:
    """The ci workflow's ``bar-close`` job runs the bench's test with ``BAR_CLOSE_BENCH`` (the shards
    skip it) and keeps its figures; the check a merge waits for needs it (``tests/test_ci_shard.py``)."""
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    job = workflow.split("\n  bar-close:\n", 1)[1].split("\n  test:\n", 1)[0]
    name = test_a_busy_morning_s_bar_closes_on_two_cpus_stay_near_the_reference.__name__
    assert f"uv run pytest -q tests/test_bar_close_loop.py::{name}" in job
    assert 'BAR_CLOSE_BENCH: "1"' in job and "BAR_CLOSE_FIGURES: results/bar-close.json" in job
    assert "name: bar-close" in job and "path: results/bar-close.json" in job
