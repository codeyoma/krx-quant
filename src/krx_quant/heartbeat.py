"""The memory the engine's heartbeat line says each minute (#547): the server's, and each process's.

Loki had no memory over time: the 2 GB server's headroom was guessed from a few snapshots during the
session and the training's peaks. The engine's heartbeat (``loki.keep_heartbeat``, one line a minute
to Loki from the engine's event loop) now carries it, so no line is added: a line a minute of the
trader's own would have added 1,440 lines a day to the about 2,350 all the processes sent in the day
to 2026-11-05 23:24 (1,440 of them the engine's heartbeats). The line:

    heartbeat: sent 1203, dropped 0, buffered 0; memory available 812 MB, swap used 0 MB; rss engine
    431 MB, trader 182 MB, training 640 MB, job prevalidate-cox 300 MB

(one line). In whole MB:

- ``available``: the server's ``MemAvailable`` (``/proc/meminfo``, which a container reads as the
  server's): what the kernel can hand out without swapping.
- ``swap used``: the server's ``SwapTotal`` less ``SwapFree``.
- ``rss``: resident memory (``VmRSS``): the engine's own; the trader's, as its snapshot says it
  (``trader.store.read_resident``: the trader runs in a container of its own, whose processes the
  engine cannot see, and writes its figure there at least every 5 seconds); then each process the
  engine started that runs now (``process_log.running``), by its name: ``training``, ``job <name>``.
- ``?``: a figure that cannot be read: no ``/proc`` (macOS), the trader not running (no snapshot
  within ``store.ALIVE_SECONDS``) or its store not read within ``READ_SECONDS``, a process that
  ended meanwhile.

Each reading also goes into its clock hour's extremes in ``state/memory-hours.json`` (#623,
``memory_hours.py``): a scheduled job reads them there, since the server cannot read Loki.

The ``/proc`` files are read on the event loop (the kernel answers them from memory, in
microseconds); the trader's store in a worker thread, as the Engine API reads it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

from krx_quant import memory_hours, process_log
from krx_quant.footprint import PROCS, resident_mb, server_mb, swapped_mb
from krx_quant.trader.store import DB_NAME, read_resident

READ_SECONDS = 10.0  # the trader's figure is left out (``?``) when its store takes longer


async def readings(state_dir: Path | None, now: Callable[[], datetime], proc: Path = PROCS) -> str:
    """The heartbeat's memory now (the module's doc): the trader's from its store in ``state_dir``
    (``?`` without one)."""
    children = process_log.running()
    trader = None
    if state_dir is not None:
        try:
            trader = await asyncio.wait_for(
                asyncio.to_thread(read_resident, state_dir / DB_NAME, now()), READ_SECONDS
            )
        except TimeoutError:
            trader = None
    if state_dir is not None:  # each hour's extremes, for the jobs that cannot read Loki (#623)
        memory_hours.note(state_dir, now(), *server_mb(proc), resident_mb(None, proc), swapped_mb(None, proc))
    return line(trader, children, proc)


def line(trader_mb: float | None, children: Mapping[int, str], proc: Path = PROCS) -> str:
    """``memory available 812 MB, swap used 0 MB; rss engine 431 MB, trader 182 MB, job x 300 MB``:
    the server's memory, then this process's, the trader's (``trader_mb``) and each of
    ``children``'s (pid -> its name), in the order they started."""
    available, swap = server_mb(proc)
    rss = [("engine", resident_mb(None, proc)), ("trader", trader_mb)]
    rss += [(name, resident_mb(pid, proc)) for pid, name in children.items()]
    shown = ", ".join(f"{name} {_mb(mb)} MB" for name, mb in rss)
    return f"memory available {_mb(available)} MB, swap used {_mb(swap)} MB; rss {shown}"


def _mb(value: float | None) -> str:
    return "?" if value is None else f"{value:.0f}"
