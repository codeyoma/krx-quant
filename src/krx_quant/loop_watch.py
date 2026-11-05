"""How long a bar close keeps the event loop from its other work (#538).

The collector's event loop also reads KIS and streams the traded symbol's best quotes to the trader
(``trader/quotes.py``), which stops new entries when none come for 5 s. A bar close's work runs on
that loop between the awaits that leave the CPU to the algorithms' worker threads, so a step that
grows (the kinds' answers over the longest horizon's hour of quotes) or slows (store calls waiting
for the GIL while the algorithms' threads hold it) stops the quotes with it.

``LoopWatch`` measures one bar close: a heartbeat task takes a turn on the loop every
``TICK_SECONDS``, and the longest time between two of its turns is the longest the loop went without
one. The bar close times its steps on the loop (``step``: synchronous code only, never an await);
those that ran in that longest stretch are what held it, and the rest of it is ``other`` (the loop's
other work, or the bar close's between its steps). Over ``LIMIT_SECONDS`` the engine logs one
WARNING with them (``held``). The live engine watches its bar closes (``watch_loop``); replays and
pre-validations do not.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator

# A bar close that keeps the loop this long without a turn is told (#538): the trader's quotes stall
# at 5 s, and on a quiet server a bar close's steps take milliseconds each.
LIMIT_SECONDS = 1.0
# How often the heartbeat asks for a turn. A stretch is measured to within this.
TICK_SECONDS = 0.05
OTHER = "other"
_SHOWN = 0.01  # a step's seconds in the stretch, below which the WARNING leaves it out


class LoopWatch:
    """One bar close's longest stretch without a turn of the event loop, and the steps in it.
    Made in the running loop, where it starts its heartbeat; ``stop`` ends it."""

    def __init__(self) -> None:
        loop = asyncio.get_running_loop()
        self._time = loop.time
        self._last = self._time()  # the heartbeat's last turn; the bar close's start at first
        self._longest = (self._last, self._last)
        self._steps: list[tuple[str, float, float]] = []
        self._beat = loop.create_task(self._beating())

    async def _beating(self) -> None:
        while True:
            self._turn(self._time())
            await asyncio.sleep(TICK_SECONDS)

    def _turn(self, now: float) -> None:
        start, end = self._longest
        if now - self._last > end - start:
            self._longest = (self._last, now)
        self._last = now

    @contextlib.contextmanager
    def step(self, name: str) -> Iterator[None]:
        """Time a synchronous step of the bar close on the loop, by ``name``."""
        start = self._time()
        try:
            yield
        finally:
            self._steps.append((name, start, self._time()))

    def stop(self) -> None:
        """End the heartbeat; the stretch since its last turn counts too."""
        self._beat.cancel()
        self._turn(self._time())

    def held(self) -> tuple[float, list[tuple[str, float]]] | None:
        """The longest stretch without a turn of the loop, in seconds, and the steps that ran in it
        with their seconds there, the longest first, then ``other`` (each from 0.01 s); None when it
        is within ``LIMIT_SECONDS``."""
        start, end = self._longest
        if end - start <= LIMIT_SECONDS:
            return None
        spent: dict[str, float] = {}
        for name, began, ended in self._steps:
            overlap = min(ended, end) - max(began, start)
            if overlap > 0:
                spent[name] = spent.get(name, 0.0) + overlap
        rest = end - start - sum(spent.values())
        parts = [*sorted(spent.items(), key=lambda p: -p[1]), (OTHER, rest)]
        return end - start, [(name, s) for name, s in parts if s >= _SHOWN]


def held_text(what: str, seconds: float, parts: list[tuple[str, float]]) -> str:
    """The WARNING's text: ``<what> held the event loop <s>s (over <limit>s): <step> <s>s, …``."""
    named = ", ".join(f"{name} {s:.2f}s" for name, s in parts)
    return f"{what} held the event loop {seconds:.2f}s (over {LIMIT_SECONDS:g}s): {named}"
