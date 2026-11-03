"""Sliding-window rate limiter for KIS REST calls."""

from __future__ import annotations

import asyncio
import time
from collections import deque

# How long a call that goes last (``acquire(last=True)``) waits before it looks again, while there
# is room but another call is waiting for it: that call wakes for it at once.
_YIELD_SECONDS = 0.01


class RateLimiter:
    """Allow at most ``max_calls`` per ``period`` seconds, over any window.

    The window is widened by ``margin`` so that network jitter cannot make
    KIS see more than ``max_calls`` arrivals within one of its own windows.
    """

    def __init__(self, max_calls: int, period: float = 1.0, margin: float = 0.1) -> None:
        self._max_calls = max_calls
        self._window = period + margin
        self._calls: deque[float] = deque()
        self._waiting = 0  # calls waiting for room, but those that go last

    async def acquire(self, *, last: bool = False) -> float:
        """Wait for room for one more call; return when it was let through (``time.monotonic()``).

        The limit holds for these moments exactly. Arrivals at KIS can bunch by the
        network's (or the machine's) delays, which the margin absorbs up to its size.

        ``last`` (#484: the trader's fill notice recorder's approval key) lets every other call
        waiting go first: it takes room only while no other call waits for it.
        """
        if not last:
            self._waiting += 1
        try:
            while True:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= self._window:
                    self._calls.popleft()
                if len(self._calls) < self._max_calls:
                    if not last or not self._waiting:
                        self._calls.append(now)
                        return now
                    await asyncio.sleep(_YIELD_SECONDS)
                    continue
                await asyncio.sleep(self._calls[0] + self._window - now)
        finally:
            if not last:
                self._waiting -= 1
