"""The scheduler on a fake clock for the tests (#215, #511): the week it runs in (Thursday 15 to
Monday 19 October, the weekend off), stand-in job processes, the notices it sends, and its saved
state.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path

import aiohttp

from krx_quant.clock import FakeClock
from krx_quant.feeds.base import KST
from krx_quant.github import GitHub
from krx_quant.market_calendar import MarketCalendar
from krx_quant.process_log import Child
from krx_quant.scheduled import (
    STATE_FILE,
    Facts,
    Job,
    JobRecord,
    JobState,
    Ran,
    Scheduler,
)
from krx_quant.swap import NightTraining
from tests.fake_github import REPO, TOKEN, FakeGitHub

THURSDAY, FRIDAY, SATURDAY, SUNDAY, MONDAY = (date(2026, 10, d) for d in (15, 16, 17, 18, 19))
CALENDAR = MarketCalendar({THURSDAY: True, FRIDAY: True, SATURDAY: False, SUNDAY: False, MONDAY: True})
AT = time(22, 30)
COMMIT = "0123456789abcdef0123"


def at(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm), KST)


async def settle() -> None:
    """Let every task run until it waits on the clock or a process."""
    for _ in range(20):
        await asyncio.sleep(0.005)


class Runs:
    """Stand-in job processes: each run is recorded with the time it started; ``hold`` keeps
    them going until ``release``."""

    def __init__(self, clock: FakeClock, ran: Ran | None = None) -> None:
        self.clock = clock
        self.ran = ran or Ran(0, "out\n", "")
        self.started: list[tuple[str, datetime]] = []
        self.ended: list[str] = []
        self.held = asyncio.Event()
        self.held.set()

    def hold(self) -> None:
        self.held.clear()

    def release(self) -> None:
        self.held.set()

    def names(self) -> list[str]:
        return [name for name, _ in self.started]

    async def __call__(
        self,
        args: Sequence[str],
        timeout: timedelta,
        child: Child | None = None,
    ) -> Ran:
        self.started.append((args[0], self.clock.now()))
        await self.held.wait()
        self.ended.append(args[0])
        return self.ran


class Told(list[str]):
    async def __call__(self, text: str) -> bool:
        self.append(text)
        return True

    def saying(self, part: str) -> list[str]:
        return [line for line in self if part in line]


@asynccontextmanager
async def scheduling(
    tmp_path: Path,
    start: datetime,
    jobs: Sequence[Job],
    *,
    runs: Runs | None = None,
    github: FakeGitHub | None = None,
    token: str | None = TOKEN,
    training: NightTraining | None = None,
    facts: Facts | Callable[[], Facts] | None = None,
    session_facts: Facts | None = None,
    told: Told | None = None,
    secrets: Sequence[str] = (),
    clock: FakeClock | None = None,
    ran: Callable[[str, JobRecord], None] | None = None,
) -> AsyncIterator[tuple[FakeClock, Runs, Told, JobState]]:
    """A scheduler running on a fake clock until the block ends (the collector stops)."""
    clock = clock or FakeClock(start)
    runs = runs or Runs(clock)
    told = Told() if told is None else told
    state = JobState.load(tmp_path / STATE_FILE)

    async def read_facts() -> Facts:
        if callable(facts):  # read each night, as the collector does
            return facts()
        return facts or Facts()

    async def read_session_facts() -> Facts:
        return session_facts or Facts()

    async with aiohttp.ClientSession() as session:
        hub = GitHub(session, token if github else None, REPO, github.url if github else "http://127.0.0.1:9")
        scheduler = Scheduler(
            jobs,
            AT,
            CALENDAR,
            state,
            hub,
            told,
            clock,
            commit=COMMIT,
            secrets=secrets,
            training=training,
            facts=read_facts,
            session_facts=read_session_facts,
            runner=runs,
            ran=ran,
        )
        task = asyncio.create_task(scheduler.run())
        try:
            await settle()
            yield clock, runs, told, state
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def saved(tmp_path: Path) -> dict[str, object]:
    data: dict[str, object] = json.loads((tmp_path / STATE_FILE).read_text())
    return data
