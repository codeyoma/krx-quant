"""``krx-quant trade`` (#394, #395): the trader's process, until stopped.

Five loops in one event loop, all into one ``desk.Desk`` (#400: the champion's book and each
running challenger's):

- the picks (``selection.pick``): the cost map's cells, read from the Engine API at the
  start and every day at ``PICK_AT`` (08:30), under the champion policy read from the store then
  (#399, ``policy.apply``: its cells, in its order, at its θ, whatever the cost map's verdict, #472;
  #471: no λ), and under each
  challenger running that day for its paper book (#400). A cost map that cannot be read is asked
  for again every ``PICK_RETRY_SECONDS``, and no cell trades until it is read: a pick is never
  older than the day's. While the engine starts (#508: ``ENGINE_START_GRACE`` after the trader
  starts, until its first answer) a connection refused is asked again every
  ``STARTING_RETRY_SECONDS`` and logged INFO, as the quotes' are (``quotes.follow_quotes``). A
  champion that cannot be read trades nothing (``NO_POLICY``) until the next pick, nor does the
  trader before the first champion (#469, ``NO_CHAMPION``). Each pick is made in the review cycle in
  force (#469, ``review.py``: the evaluation version begun at the trader's first start after its
  deploy, if the nightly job did not begin it first);
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
(``mock.MockTrader``) through the trader's broker (``kis_orders.trader_broker``: the trader's own key,
``KRX_TRADER_KIS_*``); with ``KRX_TRADER_MOCK_PRACTICE=on`` (#427) it practises a cell on a day no
champion cell trades (``desk.Desk``: the cell frozen for the review cycle, #474). The settings refuse
the practice with paper and live, before a live trader falls back to paper. The trader does not start,
and records no run, when that broker cannot be made: a key or the account missing, the collector's key,
or servers that are not KIS's mock ones (``SettingsError``, exit 2). Before the loops begin it
reconciles the KIS account with the ledger (#406, ``mock.MockTrader.start``): the ledger's orders read
on KIS, the buys whose answer was lost looked up, what is outside the ledger paused on, the open trade
sold past its time barrier or its take-profit placed again. A trader stopping waits for the order in
flight (``mock.CLOSE_SECONDS``).

The broker of either mode checks each buy last before it leaves for KIS (#543, ``kis_orders.BuyLimits``):
within ``KRX_TRADER_CAPITAL`` with the buys that may still fill (the live one within the first period's
cap while it holds, told at each judgment, ``LiveWatch``), of 005930 alone, priced near the latest
quote of the stream, which reaches it before the books. A buy it withholds is a buy that could not be
sent (``mock.py``'s failed order). Sells are never checked.

In mock mode the trader also records its mock account's fill notices (#484, ``fill_notices.py``:
with ``KRX_TRADER_KIS_HTS_ID``, on each trading day from 08:20 to 15:40, in
``state/fill_notices.sqlite3``), beside the loops and never in their set: its failures stop neither
the start nor the trading, and no book reads it (the fills are the order inquiry's, #404).
``krx-quant fill-notice-layout`` measures them (``notice_layout.py``; its night job left ``JOBS`` once
it had, #580). Paper and live record none.

In live mode (#408, ``KRX_TRADER_MODE=live``) the trader makes the live broker as it starts
(``kis_orders.trader_live_broker``: the live key, ``KRX_TRADER_LIVE_KIS_*``, on KIS's live servers; it
does not start without it, ``SettingsError``), and judges at its start and at each pick whether the
champion may order on the live account (``live.readiness``: the live gate and the execution checks).
Until then the desk runs in paper (its run kept as ``paper``, ``configured`` ``live``) and the trader
says what is missing on Telegram at its start and once a day; but while something is left on the
live account (``live.exposure``: a live trade, order or lost buy, after a change of the champion)
it runs the live book to wind it down, buying nothing new (``Desk.wind_down``), and starts again as
paper only once nothing is left; once they pass, the champion's book
orders on the live account as the mock one does on the mock account, sized on the first period's cap
(``live.step``). When the answer changes at a pick, the trader stops itself once nothing of the
champion's book is open (``SWITCH``), and Docker starts it again (compose's ``restart:
unless-stopped``), which judges it afresh: a mode is chosen at a start alone. Every mode keeps the
live state as it judged it in its state (``GET /v1/trader``'s ``live``).

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
from krx_quant.feeds.base import KST, kst_day
from krx_quant.kis_orders import LIVE as LIVE_SERVERS
from krx_quant.kis_orders import MOCK as MOCK_SERVERS
from krx_quant.kis_orders import Broker, BuyLimits, Servers, trader_broker, trader_live_broker
from krx_quant.market_calendar import MarketCalendar, Session, calendar_store
from krx_quant.notify import Alerts
from krx_quant.predictions import DB_NAME as ENGINE_DB
from krx_quant.state_lock import in_use
from krx_quant.trader import (
    COST_MAP_PERIOD,
    KIS_MODES,
    LIVE,
    MOCK,
    PAPER,
    PICK_AT,
    SYMBOL,
    fill_notices,
    live,
    review,
)
from krx_quant.trader.cells import EngineReader
from krx_quant.trader.desk import Desk
from krx_quant.trader.limits import CONTROL_SECONDS
from krx_quant.trader.mock import Timing
from krx_quant.trader.paper import Rules
from krx_quant.trader.quotes import BestQuote, follow_quotes
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
# The engine starting (#508): the 06:00 deploy starts the trader and the engine together, and the
# engine's API answered 8–11 s after its start (Loki, 2026-10-28 – 11-04), its quote stream just
# after. For this long after the trader starts, a connection refused before the engine first answered
# is that start: logged INFO, once, and the cost map asked for again every ``STARTING_RETRY_SECONDS``.
# Past it, or once the engine has answered, a failure is a WARNING as before.
ENGINE_START_GRACE = timedelta(minutes=3)
STARTING_RETRY_SECONDS = 5.0
# Why the trader stopped by itself (#408): the live mode's answer changed at a pick, and Docker
# starts it again in the other mode.
SWITCH = "switch"


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
    live_servers: Servers = LIVE_SERVERS,
    timing: Timing | None = None,
    notice_pace: fill_notices.Pace | None = None,
) -> str | None:
    """Trade the cells picked until ``stop`` is set, or until the live mode's answer changes at a pick
    (``SWITCH``, returned; None after ``stop``). ``now`` is the clock (KST), ``sleep`` the loops' wait.
    In mock mode the broker orders on ``servers`` alone (KIS's mock ones; a test's fake KIS), in live
    mode on ``live_servers`` (KIS's live ones; a test's fake KIS), with the KIS book's waits
    ``timing``; in mock mode the fill notice recorder (#484) waits ``notice_pace``."""
    clock: Clock = now if now is not None else (lambda: datetime.now(KST))
    # Before its store is opened: a restore of the state backup (#576) is not run under it.
    async with in_use(settings.state_dir, "the trader"), kis.rest_session() as orders:
        # Made before anything is kept: a mock or live trader that cannot order does not start (#405,
        # #408). The live broker is made whether or not the live mode orders yet: its key is checked.
        # Each checks a buy last against the trader's limits (#543): the capital, its one symbol; the
        # live one on the first period's cap until the ramp says it is lifted (``LiveWatch.judge``).
        broker: Broker | None = None
        limits = BuyLimits(trader.capital, frozenset({SYMBOL}))
        if trader.mode == MOCK:
            broker = trader_broker(orders, settings, clock, servers=servers, limits=limits)
        elif trader.mode == LIVE:
            broker = trader_live_broker(orders, settings, clock, servers=live_servers, limits=limits)
            broker.set_cap(min(live.RAMP_CAP, trader.capital))
        return await _run(
            settings,
            trader,
            stop=stop,
            clock=clock,
            sleep=sleep,
            broker=broker,
            timing=timing,
            notice_pace=notice_pace,
        )


async def _run(
    settings: Settings,
    trader: TraderSettings,
    *,
    stop: asyncio.Event,
    clock: Clock,
    sleep: Callable[[float], Awaitable[None]],
    broker: Broker | None,
    timing: Timing | None,
    notice_pace: fill_notices.Pace | None = None,
) -> str | None:
    started = clock()
    store = TraderStore(settings.state_dir / DB_NAME)
    sessions = _Sessions(settings)
    # The review calendar (#469): its first cycle from the first trading day after the deploy.
    evaluation = review.begin(store.db, started, sessions)
    log.info(
        "trader: evaluation version %s, review cycles of %s trading days from %s",
        evaluation.version,
        evaluation.review_days,
        evaluation.first_day,
    )
    # The live mode orders only once the gate and the execution checks pass (#408): before, as paper;
    # but what is left on the live account is wound down by the live book first, nothing new bought.
    ready = live.readiness(store.db, sessions, kst_day(started))
    left = live.exposure(store.db) if trader.mode == LIVE else 0
    mode = PAPER if trader.mode == LIVE and not ready.unlocked and not left else trader.mode
    store.add_run(
        started,
        mode,
        {
            "configured": trader.mode,
            **({"live": ready.missing} if trader.mode == LIVE else {}),
            **({"winding_down": left} if mode == LIVE and not ready.unlocked else {}),
            "threshold": trader.threshold,
            "capital": trader.capital,
            "daily_loss_percent": trader.daily_loss_percent,
            "cumulative_loss_percent": trader.cumulative_loss_percent,
            "fee_rate": settings.fee_rate,
            # The risk rules (#471): a trade's loss budget, the losses in a row and their cooldown, the
            # buy orders a day.
            "risk_per_trade_percent": trader.risk_per_trade_percent,
            "loss_streak": trader.loss_streak,
            "cooldown_minutes": trader.cooldown_minutes,
            "daily_buy_orders": trader.daily_buy_orders,
            "mock_practice": trader.mock_practice,  # #427
            "practice_spacing_minutes": trader.practice_spacing_minutes,  # #598
            "cost_map": {"engine": trader.engine_url, "symbol": SYMBOL, "period": COST_MAP_PERIOD},
            "commit": settings.commit,
        },
    )
    rules = Rules.of(trader, settings.fee_rate)
    switched = False
    try:
        async with aiohttp.ClientSession() as session:
            alerts = Alerts(
                session, settings.telegram_bot_token, settings.telegram_chat_id, settings.telegram_api_url
            )
            try:
                book = Desk(
                    store,
                    rules,
                    sessions,
                    started,
                    mode,
                    alerts.notice,
                    broker=broker if mode in KIS_MODES else None,
                    clock=clock,
                    timing=timing,
                    practice=trader.mock_practice and mode == MOCK,  # #427: the settings allow mock alone
                    engine=settings.state_dir / ENGINE_DB,  # where a policy's predictor is found gone (#469)
                )
                watch = LiveWatch(trader.mode, mode, book, sessions, alerts.notice, broker=broker)
                watch.judge(started, ready, start=True)
                # The mock account's fill notices recorded (#484): beside the loops, never in their set,
                # and never read by a book. Its own task catches what it raises: it stops neither the
                # trading nor the start.
                recording = (
                    fill_notices.start(
                        broker.account.session,
                        settings.trader_kis_ws_url,
                        settings.trader_kis_hts_id,
                        broker.account.approval_key,
                        settings.state_dir,
                        clock,
                        sessions,
                        pace=notice_pace,
                    )
                    if mode == MOCK and broker is not None
                    else None
                )
                try:
                    await book.start()  # in mock and live mode the KIS account reconciled (#406)
                    switched = await _trade(
                        settings,
                        trader,
                        book,
                        session,
                        watch,
                        broker=broker,
                        stop=stop,
                        clock=clock,
                        sleep=sleep,
                        started=started,
                    )
                finally:
                    await book.close()  # the order in flight on KIS, waited for (#405)
                    await fill_notices.stop(recording)
            finally:
                await alerts.close()
    finally:
        store.close()
        log.info("trader: stopped")
    if switched:
        log.warning("trader: stopped to start again in the other mode (#408): Docker starts it again")
        return SWITCH
    return None


class LiveWatch:
    """The live mode's answer (#408, ``live.readiness``), judged at the start and at each pick for
    the trader ``configured`` (``KRX_TRADER_MODE``) running its desk in ``running``: kept in the
    champion's state, the first period stepped in live mode (``Desk.ramp``) and its cap told to the
    live ``broker``'s last check (#543), what is missing told at the start and once a day while the live
    mode waits, and ``switch`` set when the answer is not the one the trader started with."""

    def __init__(
        self,
        configured: str,
        running: str,
        desk: Desk,
        sessions: Callable[[date], Session | None],
        notify: Callable[[str], None],
        *,
        broker: Broker | None = None,
    ) -> None:
        self.configured = configured
        self.running = running
        self._desk = desk
        self._broker = broker
        self._sessions = sessions
        self._notify = notify
        self.switch = asyncio.Event()
        self._told: date | None = None  # the day what is missing was last told

    def can_switch(self) -> bool:
        """Whether the trader may stop to start again in the other mode: nothing of the champion's book
        open, and from live nothing left on the live account (``live.exposure``)."""
        if self._desk.champion.busy:
            return False
        return self.running != LIVE or live.exposure(self._desk.store.db) == 0

    def judge(self, at: datetime, ready: live.Readiness | None = None, *, start: bool = False) -> None:
        ready = ready or live.readiness(self._desk.store.db, self._sessions, kst_day(at))
        ramp = self._desk.ramp(at) if self.running == LIVE else None
        if ramp is not None and self._broker is not None:
            self._broker.set_cap(ramp.cap)  # #543: the broker's last check holds the cap the book sizes on
        self._desk.set_live(live.live_state(ready, self.configured, self.running == LIVE, ramp), at)
        if self.configured != LIVE:
            return
        wanted = LIVE if ready.unlocked else PAPER
        if wanted != self.running:
            self._desk.wind_down(at)  # nothing new bought on the mode about to end, the picks' again
            if not self.switch.is_set():
                self.switch.set()
                log.warning("trader: the live mode's answer is now %s: stopping to start again", wanted)
                if wanted == PAPER and start:
                    self._notify(
                        "자동 매매(live): 실전 조건이 빠졌지만 실전 계좌에 정리할 것(열린 매매·주문)이"
                        " 있습니다("
                        + "; ".join(ready.said())
                        + "). 새로 사지 않고 그것을 원래 규칙대로 정리한 뒤 trader를 다시 시작해 모의로"
                        " 돕니다."
                    )
                elif wanted == LIVE:
                    self._notify(
                        "자동 매매(live): 실전 관문과 실행 점검을 모두 넘었습니다. 들고 있는 것이 없으면"
                        " trader를 다시 시작해 실전 계좌에 주문합니다(첫 단계 운용 상한 "
                        f"{min(live.RAMP_CAP, self._desk.rules.capital):,}원)."
                    )
                else:
                    self._notify(
                        "자동 매매(live): 실전 조건이 빠졌습니다("
                        + "; ".join(ready.said())
                        + "). 들고 있는 것을 다 팔면 trader를 다시 시작해 실제 주문 없이 모의로 돕니다."
                    )
            return
        day = kst_day(at)
        if self.running == PAPER and self._told != day:
            self._told = day
            log.warning("trader: live mode, no real order yet: %s missing", ", ".join(ready.missing))
            self._notify(ready.notice())
        elif self.running == LIVE and start and ramp is not None:
            self._notify(
                f"자동 매매(live): 실전 계좌에 주문합니다. 운용 상한 {ramp.cap:,}원"
                + ("." if ramp.lifted_at is not None else "(첫 단계: 실전 20거래일, 왕복 매매 30건 동안).")
            )


async def _trade(
    settings: Settings,
    trader: TraderSettings,
    book: Desk,
    session: aiohttp.ClientSession,
    watch: LiveWatch,
    *,
    broker: Broker | None,
    stop: asyncio.Event,
    clock: Clock,
    sleep: Callable[[float], Awaitable[None]],
    started: datetime,
) -> bool:
    """The loops until ``stop`` is set (False), or until the live mode's answer changed and nothing of
    the champion's book is open (True). The trader ``started`` then: the engine's start is waited for
    until ``ENGINE_START_GRACE`` after it (#508). Each quote goes to ``broker`` before the books: a buy's
    price is checked against the latest (#543)."""
    reader = EngineReader(settings.state_dir / ENGINE_DB, SYMBOL)

    def on_quote(quote: BestQuote) -> None:
        if broker is not None:
            broker.quoted(SYMBOL, quote.bid, quote.ask, quote.received_at)
        book.on_quote(quote)

    def starting() -> bool:
        """Whether the engine may still be starting with the trader (#508)."""
        return clock() < started + ENGINE_START_GRACE

    log.info(
        "trader: %s trading %s on the champion's cells, the cost map from %s (threshold %g, capital %s won,"
        " loss limits %g%% a day and %g%% in all), quotes from %s",
        book.mode,
        SYMBOL,
        trader.engine_url,
        trader.threshold,
        f"{trader.capital:,}",
        trader.daily_loss_percent,
        trader.cumulative_loss_percent,
        trader.quotes_url,
    )

    async def picking() -> None:
        failing = False  # a run of failures, kept once
        warned = waited = False  # its WARNING said; the INFO of the engine's start said (#508)
        answered = False  # the engine gave a cost map since the trader started
        while True:
            selection = await pick(session, trader.engine_url)
            at = clock()
            if selection.reason == NO_COST_MAP:
                if not failing:  # kept once a run of failures
                    book.select(selection, at)
                    reader.follow(book.cells, at - LOOK_BACK)
                    watch.judge(at)
                failing = True
                if selection.unreachable and not answered and starting():
                    if not waited:
                        log.info(
                            "trader: waiting for the engine to start (%s): no cell trades until the cost map"
                            " is read, asked for every %gs",
                            selection.detail,
                            STARTING_RETRY_SECONDS,
                        )
                        waited = True
                    await sleep(STARTING_RETRY_SECONDS)
                    continue
                if not warned:  # logged once a run of failures
                    log.warning(
                        "trader: could not read the cost map (%s): no cell trades until it is read,"
                        " asked for every %gs",
                        selection.detail,
                        PICK_RETRY_SECONDS,
                    )
                    warned = True
                await sleep(PICK_RETRY_SECONDS)
                continue
            if failing:
                log.info("trader: the cost map is read again")
            failing = warned = False
            answered = True
            book.select(selection, at)
            reader.follow(book.cells, at - LOOK_BACK)
            watch.judge(at)
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

    async def switching() -> None:
        """Once the live mode's answer changed (#408): until nothing of the champion's book is open, and
        from live nothing is left on the live account."""
        await watch.switch.wait()
        while not watch.can_switch():
            await sleep(TICK_SECONDS)

    tasks = [
        asyncio.create_task(picking()),
        asyncio.create_task(follow_quotes(session, trader.quotes_url, SYMBOL, on_quote, starting=starting)),
        asyncio.create_task(predictions()),
        asyncio.create_task(ticks()),
        asyncio.create_task(controls()),
    ]
    waiting = asyncio.create_task(stop.wait())
    switch = asyncio.create_task(switching())
    try:
        done, _ = await asyncio.wait([waiting, switch, *tasks], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task not in (waiting, switch):
                task.result()  # a loop that ended raised: let it out
        return switch in done and waiting not in done
    finally:
        for task in [waiting, switch, *tasks]:
            task.cancel()
        await asyncio.gather(waiting, switch, *tasks, return_exceptions=True)
