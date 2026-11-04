"""The calls a probe (``krx-quant probe-*``) makes to one source: spaced and counted (#510).

A probe asks KIS, KRX or Toss many times beside the running collector, which shares their limits.
So its calls go one after another, ``pace`` seconds apart, and it stops asking a source after
some failures in a row: the source is down or refusing it, and the rest would fail the same way.
What a call is, which answer is a failure, the probe's budgets (the failures in a row it stands,
its time) and the words its report stops with stay with each probe.
"""

from __future__ import annotations

import asyncio

import aiohttp

# A call that did not get through, or whose answer could not be read: aiohttp's errors, OSError
# (timeouts too: ``TimeoutError`` is one) and ValueError (a body that is not JSON). A probe catches
# its source's own refusal (``KisError``, ``TossError``) before these.
CALL_ERRORS = (aiohttp.ClientError, OSError, ValueError)


class Calls:
    """One source's calls: ``pace`` seconds apart, counted, with their failures in all and in a row.

    ``pace`` None leaves the spacing to the source's client (``toss.TossClient`` spaces its own).
    ``max_failed_in_a_row`` None never stops.
    """

    def __init__(self, pace: float | None, max_failed_in_a_row: int | None = None) -> None:
        self.pace = pace
        self.max_failed_in_a_row = max_failed_in_a_row
        self.made = 0
        self.failed = 0
        self.failed_in_a_row = 0

    @property
    def stopped(self) -> bool:
        """Whether ``max_failed_in_a_row`` calls in a row failed: nothing more is to be asked."""
        return self.max_failed_in_a_row is not None and self.failed_in_a_row >= self.max_failed_in_a_row

    async def turn(self) -> None:
        """Wait for the next call's turn, ``pace`` after the turn before (the first goes at once),
        and count the call."""
        if self.made and self.pace is not None:
            await asyncio.sleep(self.pace)
        self.made += 1

    def answered(self, ok: bool) -> None:
        """Count the call's outcome: a failure, or an answer that ends the failures in a row."""
        if ok:
            self.failed_in_a_row = 0
        else:
            self.failed += 1
            self.failed_in_a_row += 1
