"""The state folder's restore lock (#576): a restore never replaces the databases under the engine or
the trader.

``state/.state-restore.lock`` is held shared (``flock``) by the collector and by the trader from
before either opens a database until it ends (``in_use``), and exclusively by a restore of the state
backup (``restoring``, ``state_backup.restore``). A collector or a trader that starts while a restore
holds it waits until the restore is done, then opens what was restored; a restore that finds either
running refuses before it changes anything. The two containers share the state folder on the host,
so the lock holds across them, as the engine lock does (``rescore.EngineLock``). The kernel lets it go
when the process ends.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

log = logging.getLogger(__name__)

LOCK = ".state-restore.lock"
# How often a collector or a trader waiting for a restore looks again.
POLL_SECONDS = 1.0


@asynccontextmanager
async def in_use(state_dir: Path, who: str) -> AsyncIterator[None]:
    """Hold the state folder in use (shared) for the block, ``who`` its user (``the collector``): while
    a restore holds it, wait first, looking every ``POLL_SECONDS``."""
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / LOCK).open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            log.warning("a restore of the server state runs: %s waits for it to end", who)
            while True:
                await asyncio.sleep(POLL_SECONDS)
                try:
                    fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    continue
            log.info("the restore of the server state is over: %s starts", who)
        yield


@contextmanager
def restoring(state_dir: Path) -> Iterator[bool]:
    """Hold the state folder for a restore (exclusive) for the block: True; False, holding nothing, if
    the collector or the trader holds it in use."""
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / LOCK).open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
