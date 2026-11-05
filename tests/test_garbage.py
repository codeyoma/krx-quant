"""The live engine's garbage collection after each bar close (#621, ``garbage.py``).

Input: objects made before a freeze, one of them in a reference cycle; an engine given the policy
on a replayed morning, then one idle after the session. Output: what is frozen and what is freed,
and when the engine freezes and lets go.
"""

from __future__ import annotations

import gc
import logging
import re
import weakref
from collections.abc import Iterator
from pathlib import Path

import pytest

from krx_quant.clock import FakeClock
from krx_quant.engine import PredictionEngine
from krx_quant.garbage import IDLE, Garbage
from krx_quant.predictions import PredictionStore
from krx_quant.replay import replay_into
from krx_quant.touch import TARGET_VERSION
from tests.replay_doubles import busy_morning
from tests.replay_events import CALENDAR, FEE, SYMBOL, at


class Thing:
    """An object the collector tracks."""

    me: Thing | None = None


@pytest.fixture(autouse=True)
def unfrozen() -> Iterator[None]:
    """The other tests run with nothing frozen, whatever a test here left."""
    yield
    gc.unfreeze()


def test_a_frozen_object_is_freed_with_its_last_reference_and_a_cycle_at_the_release(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Input: an object and a reference cycle, both alive at a settle, then let go. Output: the
    object is freed at once, as a quote a log forgets is; the cycle only at the release, which says
    how many objects it let go and freed."""
    garbage = Garbage()
    plain, cyclic = Thing(), Thing()
    cyclic.me = cyclic
    plain_ref, cyclic_ref = weakref.ref(plain), weakref.ref(cyclic)

    garbage.settle()
    assert garbage.frozen and gc.get_freeze_count() > 0
    del plain, cyclic
    gc.collect()
    assert plain_ref() is None, "freed by its count of references, frozen or not"
    assert cyclic_ref() is not None, "a frozen cycle waits for the release"

    with caplog.at_level(logging.INFO, logger="krx_quant.garbage"):
        garbage.release()

    assert cyclic_ref() is None
    assert not garbage.frozen and gc.get_freeze_count() == 0
    [line] = [r.getMessage() for r in caplog.records if r.name == "krx_quant.garbage"]
    told = re.fullmatch(
        r"garbage collection: let go of (\d+) frozen objects; a full pass freed (\d+) in \d+\.\d\ds", line
    )
    assert told is not None, line
    assert int(told[1]) > 0 and int(told[2]) >= 1
    garbage.release()  # nothing frozen: nothing done, nothing said
    assert len([r for r in caplog.records if r.name == "krx_quant.garbage"]) == 1


def engine_on(db: Path, clock: FakeClock, garbage: Garbage) -> PredictionEngine:
    return PredictionEngine(
        PredictionStore(db),
        CALENDAR,
        (SYMBOL,),
        FEE,
        clock,
        algorithms=[],
        kind_algorithms=[],
        target_version=TARGET_VERSION,
        garbage=garbage,
    )


async def test_the_engine_freezes_after_each_bar_close_and_lets_go_once_idle(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Input: an engine given the policy, replayed from 09:00 to the first bar close (09:01) and on
    to 09:03; then an engine of the same policy at 15:50, its next work over ``IDLE`` away (the next
    day's first bar close: no answer or review left that day). Output: nothing frozen before the
    first bar close, frozen after it, and let go by the idle engine's run."""
    garbage = Garbage()
    clock = FakeClock(at("09:00:00"))
    engine = engine_on(tmp_path / "engine.db", clock, garbage)
    events = sorted(busy_morning(end="09:03:00"), key=lambda e: e[1]["received_at"])
    before = [e for e in events if e[1]["received_at"] < at("09:01:00")]

    await replay_into(engine, clock, before, until=at("09:00:59"))
    assert not garbage.frozen
    await replay_into(engine, clock, events[len(before) :], until=at("09:03:00"))
    assert garbage.frozen and gc.get_freeze_count() > 0
    assert engine.next_due() is not None, "the session goes on: nothing let go"
    engine.store.close()

    idle = engine_on(tmp_path / "idle.db", FakeClock(at("15:50:00")), garbage)
    ahead = idle.next_due()
    assert ahead is None or ahead - at("15:50:00") > IDLE, ahead
    with caplog.at_level(logging.INFO, logger="krx_quant.garbage"):
        await idle.run_due()

    assert not garbage.frozen and gc.get_freeze_count() == 0
    assert any(r.getMessage().startswith("garbage collection: let go of ") for r in caplog.records)
    idle.store.close()
