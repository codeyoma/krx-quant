"""The trader's books on the fake KIS for the tests (#405, #460, #511): the fake account the mock and
live books trade on (``kis_mock``), their settings, and the mock book's driver (``MockDay``) with the
events it plays on KIS (``Quote``, ``Fill``, ``Depth``, ``Resting_``, ``PauseNow``). Every value is
made up.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant import kis
from krx_quant.config import Settings
from krx_quant.fake_kis import SELL_SIDE, FakeAccount, FakeKis, FakeOrder
from krx_quant.kis_orders import Servers, trader_broker
from krx_quant.market_calendar import MarketCalendar
from krx_quant.predictions import DB_NAME as ENGINE_DB
from krx_quant.predictions import PredictionStore
from krx_quant.trader import MOCK, SYMBOL
from krx_quant.trader.book import Rules
from krx_quant.trader.cells import EngineReader
from krx_quant.trader.mock import Timing, kis_book
from krx_quant.trader.paper import PaperTrader
from krx_quant.trader.quotes import BestQuote
from krx_quant.trader.selection import Selection
from krx_quant.trader.store import (
    DB_NAME,
    TraderStore,
    read_trader,
    read_trades,
    request_pause,
)
from tests.trader_doubles import (
    BARRIER,
    CALENDAR,
    FEE,
    Barrier,
    Read,
    Tick,
    at,
    pick,
    quote,
    whole_rules,
)
from tests.waiting import until

KEY = "PStrader" + "m" * 28
SECRET = "Sm" + "k" * 60
ACCOUNT = "50123456-01"
# Short waits: the fake answers at once. A wait a test relies on is a bound, not a pause.
TIMING = Timing(wait=0.2, poll=0.01, follow=0.01, retry=0.0, rounds=5)

TEN = Read("10:00:02.3", [Barrier("10:00:00")])
BOUGHT = quote("10:00:02.5", 70_000, 70_100)


@dataclass(frozen=True)
class Quote:
    """A quote of the collector's stream while KIS's book is ``bid``/``ask`` (the stream lags KIS,
    or KIS's matching runs ahead of it)."""

    quote: BestQuote
    bid: int
    ask: int


@dataclass(frozen=True)
class Fill:
    """At ``hms``, KIS fills ``quantity`` (all if None) of the take-profit sell resting on it, at its
    limit; the test waits for the trader to have read it."""

    hms: str
    quantity: int | None = None


@dataclass(frozen=True)
class Depth:
    """From ``hms`` on, KIS's book holds ``shares`` at a price: a marketable order fills that many at
    once, the rest rests (a partial fill)."""

    hms: str
    shares: int | None


@dataclass(frozen=True)
class Resting_:
    """From ``hms`` on, KIS's book takes nothing at once: an order rests, and fills as its cancel
    comes (``FakeAccount.fills_late``) when ``late``, else not at all."""

    hms: str
    late: bool = True


@dataclass(frozen=True)
class PauseNow:
    """The screen's pause that sells at once (#396), asked at ``hms`` and read by the trader then."""

    hms: str


Event = Read | Tick | BestQuote | Quote | Fill | Depth | Resting_ | PauseNow


def _time(event: Event) -> datetime:
    if isinstance(event, BestQuote):
        return event.received_at
    if isinstance(event, Quote):
        return event.quote.received_at
    if isinstance(event, Fill | Depth | Resting_ | PauseNow):
        return at(event.hms)
    return event.time


@pytest.fixture
async def kis_mock() -> AsyncIterator[tuple[FakeKis, FakeAccount]]:
    fake = FakeKis(app_key=KEY, app_secret=SECRET)
    await fake.start()
    account = FakeAccount(fake, date(2026, 10, 14), account=ACCOUNT.replace("-", "")).install()
    yield fake, account
    await fake.stop()


def settings_of(fake: FakeKis, state: Path) -> Settings:
    return Settings(
        kis_app_key="PScollector" + "c" * 25,
        kis_app_secret="collector-secret",
        state_dir=state,
        fee_rate=FEE,
        trader_kis_app_key=KEY,
        trader_kis_app_secret=SECRET,
        trader_kis_account=ACCOUNT,
        trader_kis_rest_url=fake.rest_url,
        trader_kis_ws_url=fake.ws_url,
        token_min_interval_seconds=0.0,
    )


@dataclass
class MockDay:
    """A trading day of the mock book on ``state`` against ``fake``'s account: each event in time
    order (the clock at it), the book's order in flight awaited after each."""

    state: Path
    fake: FakeKis
    account: FakeAccount
    rules: Rules = field(default_factory=whole_rules)
    started_at: datetime = field(default_factory=lambda: at("09:00:00"))
    notices: list[str] = field(default_factory=list)
    status: str | None = None  # the book's status after the last event
    state_now: dict[str, Any] | None = None  # GET /v1/trader after the last event
    timing: Timing = TIMING
    # The trader stopped after the last event once this holds, its order in flight cancelled (a stop
    # that does not wait for it): None waits for the order as ever.
    stop_when: Callable[[], bool] | None = None
    mode: str = MOCK  # the book's: ``live`` on the same fake account (#460), KIS's answers alike
    calendar: MarketCalendar = CALENDAR  # the trader's market calendar (#466: a holiday in it)

    async def run(self, events: Sequence[Event]) -> list[dict[str, Any]]:
        engine = PredictionStore(self.state / ENGINE_DB)
        store = TraderStore(self.state / DB_NAME)
        store.add_run(self.started_at, self.mode, {})  # as the trader keeps its start
        clock = [self.started_at]
        self.account.clock = lambda: clock[0]  # KIS's time and day are the test's
        try:
            async with kis.rest_session() as session:
                broker = trader_broker(
                    session,
                    settings_of(self.fake, self.state),
                    lambda: clock[0],
                    servers=Servers(self.fake.rest_url, self.fake.ws_url),
                    per_second=1000,
                    pause=0.01,
                )
                book = kis_book(
                    store,
                    self.rules,
                    self.calendar.session,
                    self.started_at,
                    broker,
                    lambda: clock[0],
                    self.notices.append,
                    self.timing,
                    self.mode,
                )
                await book.start()  # the account reconciled with the ledger first (#406)
                book.select(Selection((pick(BARRIER),)), self.started_at)
                reader = EngineReader(self.state / ENGINE_DB, SYMBOL)
                reader.follow(book.cells, self.started_at - timedelta(minutes=2))
                try:
                    ordered = sorted(events, key=_time)
                    for n, event in enumerate(ordered, 1):
                        clock[0] = _time(event)
                        book.on_time(clock[0])
                        await self._on(event, book, engine, reader, store)
                        if n == len(ordered) and self.stop_when is not None:
                            await until(self.stop_when)
                            await book.close(timeout=0.0)  # the order in flight cancelled mid-poll
                            break
                        await book.idle()
                    book.write(clock[0])
                    self.status = book.status
                    self.state_now = read_trader(self.state / DB_NAME, clock[0])
                finally:
                    await book.close()
        finally:
            store.close()
            engine.close()
        return read_trades(self.state / DB_NAME, None, 100)

    async def _on(
        self,
        event: Event,
        book: PaperTrader,
        engine: PredictionStore,
        reader: EngineReader,
        store: TraderStore,
    ) -> None:
        if isinstance(event, Quote):
            self.account.bid, self.account.ask = event.bid, event.ask
            book.on_quote(event.quote)
        elif isinstance(event, BestQuote):
            self.account.bid, self.account.ask = round(event.bid), round(event.ask)
            book.on_quote(event)
        elif isinstance(event, Read):
            for shown in event.shown:
                shown.write(engine)
            read = reader.read(at("00:00:00"))
            assert read is not None
            windows, signals = read
            book.on_special(windows)
            for signal in signals:
                book.on_signal(signal, event.time)
        elif isinstance(event, Depth):
            self.account.depth = event.shares
        elif isinstance(event, Resting_):
            self.account.fills, self.account.fills_late = False, event.late
        elif isinstance(event, PauseNow):
            request_pause(self.state / DB_NAME, True, at(event.hms))
            book.on_controls(at(event.hms))
        elif isinstance(event, Fill):
            [resting] = resting_sells(self.account)
            want = resting.filled + (resting.left if event.quantity is None else event.quantity)
            improvement = self.account.improvement
            self.account.bid, self.account.improvement = resting.price, 0.0  # at its limit
            self.account.fill(resting, event.quantity)
            self.account.improvement = improvement
            trade = store.open_trade(MOCK)
            assert trade is not None
            if want >= resting.quantity:
                await until(lambda: not book.busy)  # the trader read it, and the trade exited
            else:
                await until(lambda: (r := store.resting(trade.id)) is not None and r.filled == want)

    def decisions(self) -> list[dict[str, Any]]:
        return rows(self.state, "decisions")

    def failures(self) -> list[dict[str, Any]]:
        return rows(self.state, "failures")


def resting_sells(account: FakeAccount) -> list[FakeOrder]:
    return account.resting(SELL_SIDE)


def rows(state: Path, table: str) -> list[dict[str, Any]]:
    with closing(sqlite3.connect(state / DB_NAME)) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY id")]


def orders_of(trade: dict[str, Any]) -> list[tuple[str, str, float, int, str, int]]:
    """(side, purpose, limit, quantity, status, filled) of each of the trade's orders."""
    return [
        (
            o["side"],
            o["purpose"],
            o["price"],
            o["quantity"],
            o["status"],
            sum(f["quantity"] for f in o["fills"]),
        )
        for o in trade["orders"]
    ]


LIVE_KEY = "PSlive" + "l" * 30
LIVE_SECRET = "Sl" + "v" * 60
LIVE_ACCOUNT = "70123456-01"


def live_settings(state: Path, **changes: Any) -> Settings:
    settings = Settings(
        kis_app_key="PScollector" + "c" * 25,
        kis_app_secret="collector-secret",
        state_dir=state,
        fee_rate=FEE,
        trader_kis_app_key=KEY,
        trader_kis_app_secret=SECRET,
        trader_kis_account=ACCOUNT,
        trader_live_kis_app_key=LIVE_KEY,
        trader_live_kis_app_secret=LIVE_SECRET,
        trader_live_kis_account=LIVE_ACCOUNT,
        token_min_interval_seconds=0.0,
    )
    return replace(settings, **changes)
