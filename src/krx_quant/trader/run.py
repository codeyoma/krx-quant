"""``krx-quant trade`` (#394): the trader's process, until stopped.

Three loops in one event loop, all into one ``PaperTrader``: the collector's quotes as they come
(``quotes.follow_quotes``), the cell's new predictions read from the engine's database every
``TraderSettings.poll_seconds`` (``cells.CellReader``, read-only), and the clock every
``TICK_SECONDS`` (a time barrier passed without a quote, a buy whose quote did not come). The
trading days come from the calendar the collector keeps in the state folder
(``state/market_calendar.json``), read again each day; the trader never asks KIS.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta

import aiohttp

from krx_quant.config import Settings
from krx_quant.feeds.base import KST
from krx_quant.market_calendar import MarketCalendar, Session, calendar_store
from krx_quant.predictions import DB_NAME as ENGINE_DB
from krx_quant.trader import SYMBOL
from krx_quant.trader.cells import CellReader
from krx_quant.trader.paper import PaperTrader, Rules
from krx_quant.trader.quotes import follow_quotes
from krx_quant.trader.settings import TraderSettings
from krx_quant.trader.store import DB_NAME, TraderStore

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]
TICK_SECONDS = 0.25
# At a start, the predictions of bar closes this far back are read (a late one is still fresh);
# the older are not acted on (``paper.STALE``).
LOOK_BACK = timedelta(minutes=2)


class _Sessions:
    """A day's session, from the collector's calendar file, read again once a day."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._read_on: date | None = None
        self._calendar = MarketCalendar()

    def __call__(self, day: date) -> Session | None:
        if self._read_on != day:
            self._calendar = calendar_store(self._settings).calendar
            self._read_on = day
        return self._calendar.session(day)


async def run_trader(
    settings: Settings,
    trader: TraderSettings,
    *,
    stop: asyncio.Event,
    now: Clock | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Trade the cell until ``stop`` is set. ``now`` is the clock (KST), ``sleep`` the loops' wait."""
    clock: Clock = now if now is not None else (lambda: datetime.now(KST))
    started = clock()
    store = TraderStore(settings.state_dir / DB_NAME)
    store.add_run(
        started,
        trader.mode,
        {
            "kind": trader.cell.kind,
            "predictor": trader.cell.predictor,
            "threshold": trader.threshold,
            "capital": trader.capital,
            "fee_rate": settings.fee_rate,
            "commit": settings.commit,
        },
    )
    rules = Rules(threshold=trader.threshold, capital=trader.capital, fee_rate=settings.fee_rate)
    book = PaperTrader(store, rules, _Sessions(settings), started, trader.mode)
    reader = CellReader(settings.state_dir / ENGINE_DB, trader.cell, SYMBOL, started - LOOK_BACK)
    log.info(
        "trader: %s trading %s on %s %s (threshold %g, capital %s won), quotes from %s",
        trader.mode,
        SYMBOL,
        trader.cell.kind,
        trader.cell.predictor,
        trader.threshold,
        f"{trader.capital:,}",
        trader.quotes_url,
    )

    async def predictions() -> None:
        while True:
            try:
                signals = await asyncio.to_thread(reader.new)
            except sqlite3.Error as e:
                log.warning("trader: could not read the engine's predictions (%s)", e)
                signals = []
            for signal in signals:
                book.on_signal(signal, clock())
            await sleep(trader.poll_seconds)

    async def ticks() -> None:
        while True:
            book.on_time(clock())
            await sleep(TICK_SECONDS)

    try:
        async with aiohttp.ClientSession() as session:
            tasks = [
                asyncio.create_task(follow_quotes(session, trader.quotes_url, SYMBOL, book.on_quote)),
                asyncio.create_task(predictions()),
                asyncio.create_task(ticks()),
            ]
            waiting = asyncio.create_task(stop.wait())
            try:
                done, _ = await asyncio.wait([waiting, *tasks], return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task is not waiting:
                        task.result()  # a loop that ended raised: let it out
            finally:
                for task in [waiting, *tasks]:
                    task.cancel()
                await asyncio.gather(waiting, *tasks, return_exceptions=True)
    finally:
        store.close()
        log.info("trader: stopped")
