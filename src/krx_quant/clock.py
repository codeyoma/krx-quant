"""Session Clock: the engine's time, real or fake.

Live mode reads the system clock; replay and tests drive a fake clock forward by hand.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Protocol, runtime_checkable

from krx_quant.feeds.base import KST

# The longest real sleep in one go, so a jump of the system clock (or a machine waking
# from sleep) is noticed within this many seconds.
_MAX_SLEEP_SECONDS = 60.0


@runtime_checkable
class SessionClock(Protocol):
    def now(self) -> datetime: ...

    async def sleep_until(self, at: datetime) -> None: ...


def _system_now() -> datetime:
    return datetime.now(KST)


class SystemClock:
    """Wall-clock time; ``now`` can be swapped for a function, as the collector tests do."""

    def __init__(self, now: Callable[[], datetime] = _system_now) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now()

    async def sleep_until(self, at: datetime) -> None:
        while (remaining := (at - self.now()).total_seconds()) > 0:
            await asyncio.sleep(min(remaining, _MAX_SLEEP_SECONDS))


class FakeClock:
    """Time that only moves when ``set`` is called; sleepers wake once it passes their time."""

    def __init__(self, start: datetime) -> None:
        self._now = start
        self._waiters: list[asyncio.Event] = []

    def now(self) -> datetime:
        return self._now

    def set(self, at: datetime) -> None:
        if at < self._now:
            raise ValueError(f"the clock cannot go back from {self._now} to {at}")
        self._now = at
        waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            waiter.set()

    async def sleep_until(self, at: datetime) -> None:
        while self._now < at:
            waiter = asyncio.Event()
            self._waiters.append(waiter)
            await waiter.wait()
