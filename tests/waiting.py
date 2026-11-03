"""Waiting for a condition, never for a time (CODING_STANDARDS.md, "Tests wait for conditions").

A test that checks that nothing happened waits first for the event that marks the moment (the
next poll, the next check), then checks."""

from __future__ import annotations

import asyncio
from collections.abc import Callable


async def until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    """Return once ``condition()`` holds, asking it every 20 ms; TimeoutError after ``timeout`` s."""

    async def poll() -> None:
        while not condition():
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), timeout)
