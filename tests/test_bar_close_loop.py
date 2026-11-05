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
import json
import logging
import sqlite3
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
from krx_quant.loop_watch import LOOP_HELD
from krx_quant.predictions import PredictionStore
from krx_quant.replay import replay_into
from krx_quant.touch import TARGET_VERSION
from tests.algorithm_doubles import spend
from tests.collector_doubles import FIXED_NOW, settings_for
from tests.kind_doubles import TEST_KIND, AlwaysHeld, drift_of
from tests.replay_doubles import busy_morning
from tests.replay_events import CALENDAR, DAY, FEE, SYMBOL, at, vi
from tests.waiting import until

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
    kinds' baselines as what took that time; none for the other bar closes. The same in the store's
    runs (#589), for the night's report of the bar closes' times."""
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
    named = dict(part.rsplit(" ", 1) for part in line.split(": ", 1)[1].split(", "))
    assert float(named["the kinds' baselines"].rstrip("s")) >= 1.2
    with sqlite3.connect(tmp_path / "engine.db") as db:
        runs = db.execute("SELECT at, detail FROM runs WHERE kind = ?", (LOOP_HELD,)).fetchall()
    assert [when for when, _ in runs] == [at("09:10:00").isoformat()]
    detail = json.loads(runs[0][1])
    assert detail["seconds"] == pytest.approx(seconds, abs=0.01)
    assert detail["parts"]["the kinds' baselines"] >= 1.2
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
