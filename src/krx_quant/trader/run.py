"""``krx-quant trade`` (#394, #395): the trader's process, until stopped.

Five loops in one event loop, all into one ``desk.Desk`` (#400: the champion's book and each
running challenger's):

- the picks (``selection.pick``): the cells the cost map passes, read from the Engine API at the
  start and every day at ``PICK_AT`` (08:30), under the champion policy read from the store then
  (#399, ``policy.apply``: its cells among them, in its order, at its θ and λ), and under each
  challenger running that day for its paper book (#400). A cost map that cannot be read is asked
  for again every ``PICK_RETRY_SECONDS``, and no cell trades until it is read: a pick is never
  older than the day's. A champion that cannot be read trades nothing (``NO_POLICY``) until the
  next pick;
- the collector's quotes as they come (``quotes.follow_quotes``), watched for a stall by the desk
  (#407, ``stall.py``);
- the engine's database every ``TraderSettings.poll_seconds`` (``cells.EngineReader``,
  read-only): the picked cells' new predictions, and the special windows the engine kept;
- the clock every ``TICK_SECONDS`` (a rise that waited for the cells ranked above it, a time
  barrier passed without a quote, a buy whose quote did not come, the state written);
- the pauses, resumes and re-arms asked for (#396: the screen's through the Engine API, the
  server's ``krx-quant trade resume|rearm``), read from the trader's store every
  ``limits.CONTROL_SECONDS`` (within a second).

Its halts, pauses, resumes and re-arms go to Telegram (``notify.Alerts.notice``), and in mock
mode its failed orders.

In mock mode (#405, ``KRX_TRADER_MODE=mock``) the champion's book orders on KIS's mock server
(``mock.MockTrader``) through the trader's broker (``kis_orders.trader_broker``: the trader's own
key, ``KRX_TRADER_KIS_*``). The trader does not start, and records no run, when that broker cannot
be made: a key or the account missing, the collector's key, or servers that are not KIS's mock
ones (``SettingsError``, exit 2). Before the loops begin it reconciles the KIS account with the
ledger (#406, ``mock.MockTrader.start``): the ledger's orders read on KIS, the buys whose answer was
lost looked up, what is outside the ledger paused on, the open trade sold past its time barrier or
its take-profit placed again. A trader stopping waits for the order in flight
(``mock.CLOSE_SECONDS``).

The trading days come from the calendar the collector keeps in the state folder
(``state/market_calendar.json``), read again each day; the trader never asks KIS.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import date, datetime, time, timedelta

import aiohttp

from krx_quant import kis
from krx_quant.config import Settings
from krx_quant.feeds.base import KST
from krx_quant.kis_orders import MOCK as MOCK_SERVERS
from krx_quant.kis_orders import Broker, Servers, trader_broker
from krx_quant.market_calendar import MarketCalendar, Session, calendar_store
from krx_quant.notify import Alerts
from krx_quant.predictions import DB_NAME as ENGINE_DB
from krx_quant.trader import COST_MAP_PERIOD, KELLY_FRACTION, MOCK, PICK_AT, SYMBOL
from krx_quant.trader.cells import EngineReader
from krx_quant.trader.desk import Desk
from krx_quant.trader.limits import CONTROL_SECONDS
from krx_quant.trader.mock import Timing
from krx_quant.trader.paper import Rules
from krx_quant.trader.quotes import follow_quotes
from krx_quant.trader.selection import NO_COST_MAP, pick
from krx_quant.trader.settings import TraderSettings
from krx_quant.trader.store import DB_NAME, TraderStore

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]
TICK_SECONDS = 0.25
# At a start, the predictions of bar closes this far back are read (a late one is still fresh);
# the older are not acted on (``paper.STALE``).
LOOK_BACK = timedelta(minutes=2)
# A cost map that could not be read is asked for again this often.
PICK_RETRY_SECONDS = 60.0


def next_pick(now: datetime) -> datetime:
    """The first ``PICK_AT`` after ``now``."""
    due = datetime.combine(now.astimezone(KST).date(), PICK_AT, KST)
    return due if due > now else due + timedelta(days=1)


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
    servers: Servers = MOCK_SERVERS,
    timing: Timing | None = None,
) -> None:
    """Trade the cells picked until ``stop`` is set. ``now`` is the clock (KST), ``sleep`` the loops' wait.
    In mock mode the broker orders on ``servers`` alone (KIS's mock ones; a test's fake KIS), with the
    mock book's waits ``timing``."""
    clock: Clock = now if now is not None else (lambda: datetime.now(KST))
    async with kis.rest_session() as orders:
        # Made before anything is kept: a mock trader that cannot order does not start (#405).
        broker = trader_broker(orders, settings, clock, servers=servers) if trader.mode == MOCK else None
        await _run(settings, trader, stop=stop, clock=clock, sleep=sleep, broker=broker, timing=timing)


async def _run(
    settings: Settings,
    trader: TraderSettings,
    *,
    stop: asyncio.Event,
    clock: Clock,
    sleep: Callable[[float], Awaitable[None]],
    broker: Broker | None,
    timing: Timing | None,
) -> None:
    started = clock()
    store = TraderStore(settings.state_dir / DB_NAME)
    store.add_run(
        started,
        trader.mode,
        {
            "threshold": trader.threshold,
            "capital": trader.capital,
            "daily_loss_percent": trader.daily_loss_percent,
            "cumulative_loss_percent": trader.cumulative_loss_percent,
            "fee_rate": settings.fee_rate,
            "kelly_fraction": KELLY_FRACTION,
            "cost_map": {"engine": trader.engine_url, "symbol": SYMBOL, "period": COST_MAP_PERIOD},
            "commit": settings.commit,
        },
    )
    for name in trader.ignored:
        log.warning("trader: %s is no longer read (#395): the cost map picks the cells", name)
    rules = Rules(
        threshold=trader.threshold,
        capital=trader.capital,
        daily_loss_percent=trader.daily_loss_percent,
        cumulative_loss_percent=trader.cumulative_loss_percent,
        fee_rate=settings.fee_rate,
    )
    try:
        async with aiohttp.ClientSession() as session:
            alerts = Alerts(
                session, settings.telegram_bot_token, settings.telegram_chat_id, settings.telegram_api_url
            )
            try:
                book = Desk(
                    store,
                    rules,
                    _Sessions(settings),
                    started,
                    trader.mode,
                    alerts.notice,
                    broker=broker,
                    clock=clock,
                    timing=timing,
                )
                try:
                    await book.start()  # in mock mode the KIS account reconciled with the ledger (#406)
                    await _trade(settings, trader, book, session, stop=stop, clock=clock, sleep=sleep)
                finally:
                    await book.close()  # the order in flight on KIS, waited for (#405)
            finally:
                await alerts.close()
    finally:
        store.close()
        log.info("trader: stopped")


async def _trade(
    settings: Settings,
    trader: TraderSettings,
    book: Desk,
    session: aiohttp.ClientSession,
    *,
    stop: asyncio.Event,
    clock: Clock,
    sleep: Callable[[float], Awaitable[None]],
) -> None:
    reader = EngineReader(settings.state_dir / ENGINE_DB, SYMBOL)
    log.info(
        "trader: %s trading %s on the cells the cost map of %s passes (threshold %g, capital %s won,"
        " loss limits %g%% a day and %g%% in all), quotes from %s",
        trader.mode,
        SYMBOL,
        trader.engine_url,
        trader.threshold,
        f"{trader.capital:,}",
        trader.daily_loss_percent,
        trader.cumulative_loss_percent,
        trader.quotes_url,
    )

    async def picking() -> None:
        failing = False
        while True:
            selection = await pick(session, trader.engine_url)
            at = clock()
            if selection.reason == NO_COST_MAP:
                if not failing:  # kept and logged once a run of failures
                    log.warning(
                        "trader: could not read the cost map (%s): no cell trades until it is read,"
                        " asked for every %gs",
                        selection.detail,
                        PICK_RETRY_SECONDS,
                    )
                    book.select(selection, at)
                    reader.follow(book.cells, at - LOOK_BACK)
                failing = True
                await sleep(PICK_RETRY_SECONDS)
                continue
            if failing:
                log.info("trader: the cost map is read again")
            failing = False
            book.select(selection, at)
            reader.follow(book.cells, at - LOOK_BACK)
            due = next_pick(clock())
            while clock() < due:
                await sleep(min(60.0, (due - clock()).total_seconds()))

    async def predictions() -> None:
        while True:
            today = datetime.combine(clock().astimezone(KST).date(), time(), KST)
            try:
                read = await asyncio.to_thread(reader.read, today)
            except sqlite3.Error as e:
                log.warning("trader: could not read the engine's predictions (%s)", e)
                read = None
            if read is not None:
                windows, signals = read
                book.on_special(windows)
                for signal in signals:
                    book.on_signal(signal, clock())
            await sleep(trader.poll_seconds)

    async def ticks() -> None:
        while True:
            book.on_time(clock())
            await sleep(TICK_SECONDS)

    async def controls() -> None:
        while True:
            try:
                book.on_controls(clock())
            except sqlite3.Error as e:  # the store busy with another writer: read again next time
                log.warning("trader: could not read the controls asked for (%s)", e)
            await sleep(CONTROL_SECONDS)

    tasks = [
        asyncio.create_task(picking()),
        asyncio.create_task(follow_quotes(session, trader.quotes_url, SYMBOL, book.on_quote)),
        asyncio.create_task(predictions()),
        asyncio.create_task(ticks()),
        asyncio.create_task(controls()),
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
