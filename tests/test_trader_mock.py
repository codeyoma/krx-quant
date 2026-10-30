"""The mock mode (#405): the champion's book ordering on KIS's mock server, by paper's decisions,
limits and ledger, its trades written from KIS's fills.

KIS is the fake one (``fake_kis.FakeKis`` with a ``FakeAccount``), answering in the shapes, codes and
messages the order probe measured on the mock server (#403, ``kis_orders.PROBE_COMMENT``): a
marketable order fills at once, half a tick better than the quote it crosses, and a resting one
when the test fills it (``Fill``). The trader's quotes are the collector's stream; each also sets
KIS's book unless it says otherwise (``Quote``). Every value is made up.

The example used throughout, as in ``test_trader_paper.py``: a barrier prediction of the 10:00 bar
close read at 10:00:02.3 (the rise, up_first 0.7), its reference 70,050 (tick 100) and width 2: the
target 70,250, the stop 69,850, the time barrier 10:04. The first quote after it asks 70,100: KIS
takes a limit buy of 14 shares there (1,000,000 ÷ 70,100) and fills it at 70,050. The take-profit
sell rests at 70,300, the first tick at or above the target.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from krx_quant import kis, kis_orders
from krx_quant.config import DEFAULT_REST_URL, DEFAULT_WS_URL, Settings, SettingsError
from krx_quant.fake_kis import BUY_SIDE, OFF_TICK, SELL_SIDE, FakeAccount, FakeKis, FakeOrder
from krx_quant.feeds import BOOK
from krx_quant.kis_orders import Servers, trader_broker
from krx_quant.predictions import DB_NAME as ENGINE_DB
from krx_quant.predictions import PredictionStore
from krx_quant.trader import MOCK, PAPER, SYMBOL, promotion, report
from krx_quant.trader.cells import EngineReader
from krx_quant.trader.desk import Desk
from krx_quant.trader.limits import ORDER_FAILED, RUNNING
from krx_quant.trader.mock import UNFILLED, MockTrader, Timing, tick_up
from krx_quant.trader.paper import STOP_LOSS, TAKE_PROFIT, TIME, PaperTrader, Rules
from krx_quant.trader.quotes import BestQuote, QuoteFeed
from krx_quant.trader.run import run_trader
from krx_quant.trader.selection import Selection
from krx_quant.trader.settings import TraderSettings
from krx_quant.trader.store import DB_NAME, TraderStore, read_trader, read_trades
from tests.test_live_predictions import free_port
from tests.test_trader_run import PASSING, EngineApi, book
from tests.trader_doubles import BARRIER, CALENDAR, FEE, Barrier, Read, Tick, at, pick, quote

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


Event = Read | Tick | BestQuote | Quote | Fill | Depth


def _time(event: Event) -> datetime:
    if isinstance(event, BestQuote):
        return event.received_at
    if isinstance(event, Quote):
        return event.quote.received_at
    if isinstance(event, Fill | Depth):
        return at(event.hms)
    return event.time


async def until(ready: Callable[[], bool], seconds: float = 5.0) -> None:
    async with asyncio.timeout(seconds):
        while not ready():
            await asyncio.sleep(0.005)


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
    rules: Rules = field(default_factory=lambda: Rules(fee_rate=FEE))
    started_at: datetime = field(default_factory=lambda: at("09:00:00"))
    notices: list[str] = field(default_factory=list)
    status: str | None = None  # the book's status after the last event
    state_now: dict[str, Any] | None = None  # GET /v1/trader after the last event

    async def run(self, events: Sequence[Event]) -> list[dict[str, Any]]:
        engine = PredictionStore(self.state / ENGINE_DB)
        store = TraderStore(self.state / DB_NAME)
        store.add_run(self.started_at, MOCK, {})  # as the trader keeps its start
        clock = [self.started_at]
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
                book = MockTrader(
                    store,
                    self.rules,
                    CALENDAR.session,
                    self.started_at,
                    broker,
                    lambda: clock[0],
                    self.notices.append,
                    TIMING,
                )
                book.select(Selection((pick(BARRIER),)), self.started_at)
                reader = EngineReader(self.state / ENGINE_DB, SYMBOL)
                reader.follow(book.cells, self.started_at - timedelta(minutes=2))
                try:
                    for event in sorted(events, key=_time):
                        clock[0] = _time(event)
                        book.on_time(clock[0])
                        await self._on(event, book, engine, reader, store)
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
        book: MockTrader,
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
    return [o for o in account.placed(SELL_SIDE) if o.left > 0]


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


# Entry and take-profit


async def test_a_rise_buys_at_the_ask_on_kis_and_its_take_profit_rests_until_it_fills(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """14 bought at 70,050 (KIS's fill, half a tick under the ask) and sold at 70,300: 3,500 won
    gross; the buy's commission 980,700 × 0.015% = 147.105, the sale's 984,200 × 0.015% = 147.63,
    the tax 984,200 × 0.20% = 1,968.4: 1,236.865 won net."""
    fake, account = kis_mock
    day = MockDay(tmp_path, fake, account)

    [trade] = await day.run([TEN, BOUGHT, quote("10:01:00", 70_200, 70_300), Fill("10:02:00")])

    assert (trade["mode"], trade["champion"]) == (MOCK, True)
    assert trade["entry"] == {"at": "2026-10-14T10:00:02.500000+09:00", "price": 70_050, "quantity": 14}
    assert trade["exit"] == {"at": "2026-10-14T10:02:00+09:00", "price": 70_300, "reason": TAKE_PROFIT}
    assert orders_of(trade) == [
        ("buy", "entry", 70_100, 14, "filled", 14),
        ("sell", "take_profit", 70_300, 14, "filled", 14),
    ]
    assert (trade["gross"], trade["net"]) == pytest.approx((3_500, 1_236.865))
    # Each order is kept with KIS's.
    numbers = [o["broker_id"] for o in trade["orders"]]
    assert [o.number for o in account.placed(BUY_SIDE) + account.placed(SELL_SIDE)] == numbers
    assert account.holdings[SYMBOL] == 0
    # The size was read off the orderable cash at the ask, before the buy.
    asked = [q for _, tr, _, q in fake.rest_requests if tr == kis_orders.ORDERABLE.mock]
    assert [q["ORD_UNPR"] for q in asked] == ["70100"]
    assert day.status == RUNNING and day.notices == []


async def test_the_take_profit_is_the_first_tick_at_or_above_the_target() -> None:
    assert tick_up(70_250) == 70_300  # between ticks of 100
    assert tick_up(70_300) == 70_300  # on one
    assert tick_up(19_995) == 20_000  # past a band's edge, onto the coarser tick above
    assert tick_up(199_950.5) == 200_000


async def test_a_partial_fill_of_the_take_profit_is_kept_and_the_stop_sells_the_rest(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    [trade] = await MockDay(tmp_path, fake, account).run(
        [TEN, BOUGHT, Fill("10:01:00", 5), quote("10:01:30", 69_800, 69_900)]
    )

    assert orders_of(trade) == [
        ("buy", "entry", 70_100, 14, "filled", 14),
        ("sell", "take_profit", 70_300, 14, "cancelled", 5),  # 5 at the target, the rest taken off
        ("sell", "stop_loss", 69_800, 9, "filled", 9),  # filled at 69,850
    ]
    assert trade["exit"]["reason"] == STOP_LOSS
    assert trade["exit"]["price"] == pytest.approx((5 * 70_300 + 9 * 69_850) / 14)


# Stop, time


async def test_a_bid_at_or_below_the_stop_cancels_the_take_profit_and_sells_at_that_bid(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """Sold at 69,800 and filled at 69,850: (69,850 − 70,050) × 14 = −2,800 gross, less 147.105,
    146.685 (977,900 × 0.015%) and 1,955.8."""
    fake, account = kis_mock
    [trade] = await MockDay(tmp_path, fake, account).run(
        [TEN, BOUGHT, quote("10:01:00", 69_900, 70_000), quote("10:01:30", 69_800, 69_900)]
    )

    assert trade["exit"] == {"at": "2026-10-14T10:01:30+09:00", "price": 69_850, "reason": STOP_LOSS}
    assert orders_of(trade) == [
        ("buy", "entry", 70_100, 14, "filled", 14),
        ("sell", "take_profit", 70_300, 14, "cancelled", 0),
        ("sell", "stop_loss", 69_800, 14, "filled", 14),
    ]
    assert trade["net"] == pytest.approx(-2_800 - 147.105 - 146.685 - 1_955.8)
    cancels = [o for o in account.orders if o.original]
    assert len(cancels) == 1 and cancels[0].quantity == 14
    assert account.holdings[SYMBOL] == 0


async def test_the_time_barrier_sells_at_the_bid(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    [trade] = await MockDay(tmp_path, fake, account).run(
        [TEN, BOUGHT, quote("10:03:59.5", 70_000, 70_100), quote("10:04:00.2", 70_100, 70_200)]
    )

    assert trade["exit"]["reason"] == TIME
    assert orders_of(trade)[1:] == [
        ("sell", "take_profit", 70_300, 14, "cancelled", 0),
        ("sell", "time", 70_100, 14, "filled", 14),  # the bid of the latest quote, filled at 70,150
    ]
    assert trade["exit"]["price"] == 70_150


async def test_a_sale_partly_filled_sells_what_is_left_again_at_the_bid(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    [trade] = await MockDay(tmp_path, fake, account).run(
        [TEN, BOUGHT, Depth("10:01:00", 5), quote("10:01:30", 69_800, 69_900)]
    )

    assert orders_of(trade)[2:] == [
        ("sell", "stop_loss", 69_800, 14, "cancelled", 5),
        ("sell", "stop_loss", 69_800, 9, "cancelled", 5),
        ("sell", "stop_loss", 69_800, 4, "filled", 4),
    ]
    assert trade["exit"]["price"] == 69_850 and account.holdings[SYMBOL] == 0


async def test_a_sale_whose_cancel_did_not_take_is_followed_and_never_sold_twice(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """The stop's sell fills 5 of 14 and KIS refuses its cancel: the order stays open on KIS. It
    fills the other 9 before the sale is tried again, so the next try takes it off and finds every
    share sold: no second sell (KIS would have sold shares the trader no longer holds)."""
    fake, account = kis_mock
    cancel = fake.rest_posts[kis_orders.CANCEL.mock]
    asked: list[str] = []

    def refuse_once(body: dict[str, Any]) -> Any:  # the stop's sell: refused, then filled meanwhile
        [order] = [o for o in account.orders if kis_orders.same_number(o.number, body["ORGN_ODNO"])]
        if order.side == SELL_SIDE and order.price == 69_800:
            asked.append(order.number)
            if len(asked) == 1:
                return {"rt_cd": "1", "msg_cd": "FK0009", "msg1": "made up: 취소를 받지 못했습니다"}
            account.depth = None
            account.fill(order)  # the rest filled during the wait
        return cancel(body)

    fake.rest_posts[kis_orders.CANCEL.mock] = refuse_once
    day = MockDay(tmp_path, fake, account)

    [trade] = await day.run([TEN, BOUGHT, Depth("10:01:00", 5), quote("10:01:30", 69_800, 69_900)])

    assert orders_of(trade)[2:] == [("sell", "stop_loss", 69_800, 14, "filled", 14)]  # 5, then 9
    assert trade["exit"]["reason"] == STOP_LOSS and trade["exit"]["price"] == 69_850
    assert len(asked) == 2, "taken off before anything more was sold"
    assert account.holdings[SYMBOL] == 0 and len(account.placed(SELL_SIDE)) == 2  # the take-profit, the stop
    assert [(f["what"], f["outcome"]) for f in day.failures()] == [("cancel", "refused")]


async def test_a_partly_filled_take_profit_is_valued_at_its_fills_and_the_rest_at_the_bid(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """13 of the 14 sold at 70,300, the last share at the bid of 69,900: 983,800 won in all against
    980,700 paid, less 147.105 for the buy and 983,800 × 0.215% = 2,115.17 for the sales: 837.725 won.
    Valued as 14 shares at that bid it would be −4,351, past a 4,000-won daily limit."""
    fake, account = kis_mock
    day = MockDay(tmp_path, fake, account, rules=Rules(fee_rate=FEE, daily_loss_percent=0.4))

    [trade] = await day.run([TEN, BOUGHT, Fill("10:01:00", 13), quote("10:01:30", 69_900, 70_000)])

    assert day.status == RUNNING and rows(tmp_path, "halts") == []
    assert trade["exit"] is None
    assert day.state_now is not None
    assert day.state_now["position"]["unrealized"] == pytest.approx(837.725)
    assert day.state_now["today"]["net"] == pytest.approx(837.725)


# Partial fills and the cancel race


async def test_a_partial_buy_fill_is_a_position_of_that_many_shares(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    account.depth = 4  # four shares at the ask

    [trade] = await MockDay(tmp_path, fake, account).run([TEN, BOUGHT])

    assert trade["entry"]["quantity"] == 4 and trade["exit"] is None
    assert orders_of(trade) == [
        ("buy", "entry", 70_100, 14, "cancelled", 4),  # the rest cancelled
        ("sell", "take_profit", 70_300, 4, "open", 0),
    ]
    [resting] = resting_sells(account)
    assert (resting.quantity, resting.price) == (4, 70_300)


async def test_a_buy_that_fills_before_its_cancel_lands_is_a_position(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    account.fills, account.fills_late = False, True  # it fills only as its cancel comes

    [trade] = await MockDay(tmp_path, fake, account).run([TEN, BOUGHT])

    assert trade["entry"]["quantity"] == 14
    assert orders_of(trade)[0] == ("buy", "entry", 70_100, 14, "filled", 14)
    assert [o.quantity for o in account.orders if o.original] == [], "KIS found nothing to cancel"


async def test_a_take_profit_that_fills_before_its_cancel_lands_exits_at_the_target(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """The stream's bid falls to the stop while KIS's book has met the take-profit already: its
    cancel finds nothing left, and nothing is sold at the bid."""
    fake, account = kis_mock
    account.fills_late = True
    account.improvement = 0.0
    day = MockDay(tmp_path, fake, account)

    [trade] = await day.run([TEN, BOUGHT, Quote(quote("10:01:30", 69_800, 69_900), 70_300, 70_400)])

    assert trade["exit"]["reason"] == TAKE_PROFIT and trade["exit"]["price"] == 70_300
    assert [o["purpose"] for o in trade["orders"]] == ["entry", "take_profit"]
    assert day.failures() == []


async def test_a_buy_nothing_of_which_fills_buys_nothing(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    account.fills = False  # the ask moved away: nothing at the limit
    day = MockDay(tmp_path, fake, account)

    assert await day.run([TEN, BOUGHT]) == []
    [decision] = day.decisions()
    assert (decision["action"], decision["reason"], decision["trade_id"]) == ("buy", UNFILLED, None)
    assert day.status == RUNNING and day.notices == [], "not a failure"
    assert not account.holdings.get(SYMBOL)
    # The order is kept all the same, with KIS's: the ledger holds every order KIS has.
    [kept] = rows(tmp_path, "unfilled_buys")
    [order] = account.placed(BUY_SIDE)
    assert (kept["decision_id"], kept["quantity"], kept["price"], kept["status"]) == (
        decision["id"],
        14,
        70_100,
        "cancelled",
    )
    assert kept["broker_id"] == order.number


# Failures


async def test_a_refused_buy_alerts_and_stops_new_entries_for_the_rest_of_the_day(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    day = MockDay(tmp_path, fake, account)

    trades = await day.run(
        [
            TEN,
            quote("10:00:02.5", 70_000, 70_150),  # off the tick: KIS refuses the buy
            Read("10:08:02.3", [Barrier("10:08:00")]),
            quote("10:08:02.5", 70_000, 70_100),
        ]
    )

    assert trades == []
    assert [(d["action"], d["reason"]) for d in day.decisions()] == [
        ("buy", ORDER_FAILED),
        ("skip", ORDER_FAILED),  # no new entry that day
    ]
    [failure] = day.failures()
    assert (failure["what"], failure["outcome"], failure["code"]) == ("buy", "refused", OFF_TICK[0])
    assert day.status == ORDER_FAILED
    assert day.state_now is not None and day.state_now["status"] == ORDER_FAILED
    [notice] = day.notices
    assert "자동 매매(mock): 매수 주문을 KIS가 거부했습니다" in notice and "호가단위" in notice
    assert "오늘은 새로 사지 않고" in notice


async def test_the_night_s_report_shows_the_failed_order_and_calls_an_agent(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    await MockDay(tmp_path, fake, account).run([TEN, quote("10:00:02.5", 70_000, 70_150)])

    made = report.read(tmp_path / DB_NAME, date(2026, 10, 14), CALENDAR)
    text = report.render(made)

    assert made.mode == MOCK, "the mode the trader ran in that day"
    assert "자동 매매 리포트 (#402) · 2026-10-14(수) · KIS 모의(mock)" in text
    assert "KIS 주문 실패 1건(첫 실패 10:00:02)" in text and report.calls_agent(text)
    assert (
        "- KIS 주문 실패: 10:00:02 매수 · KIS가 거부(40030000: the price is off the tick (호가단위 오류))"
        in text
    )


async def test_a_failed_order_stops_entries_for_that_day_alone(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    first = MockDay(tmp_path, fake, account)
    await first.run([TEN, quote("10:00:02.5", 70_000, 70_150)])
    assert first.status == ORDER_FAILED

    later = MockDay(tmp_path, fake, account, started_at=at("2026-10-15 09:00:00"))
    [trade] = await later.run(
        [
            Read("2026-10-15 10:00:02.3", [Barrier("2026-10-15 10:00:00")]),
            quote("2026-10-15 10:00:02.5", 70_000, 70_100),
        ]
    )
    assert trade["entry"]["quantity"] == 14 and later.status == RUNNING


async def test_a_buy_without_an_answer_alerts_that_its_outcome_is_not_known(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    account.lose_answers = 1  # KIS takes the buy; its answer is lost on the way
    day = MockDay(tmp_path, fake, account)

    assert await day.run([TEN, BOUGHT]) == []
    [failure] = day.failures()
    assert (failure["what"], failure["outcome"]) == ("buy", "unknown")
    assert day.status == ORDER_FAILED
    [notice] = day.notices
    assert "응답이 없어 결과를 모릅니다" in notice and "KIS 계좌에서 확인" in notice
    assert len(account.placed(BUY_SIDE)) == 1, "not sent again: an order sent twice is two orders"


async def test_a_refused_take_profit_alerts_and_the_stop_still_sells(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    taken = fake.rest_posts[kis_orders.SELL.mock]

    def order(body: dict[str, Any]) -> Any:  # KIS refuses the take-profit sell (a code it refuses with)
        if int(body["ORD_UNPR"]) == 70_300:
            return {"rt_cd": "1", "msg_cd": OFF_TICK[0], "msg1": OFF_TICK[1]}
        return taken(body)

    fake.rest_posts[kis_orders.SELL.mock] = order
    day = MockDay(tmp_path, fake, account)

    [trade] = await day.run([TEN, BOUGHT, quote("10:01:30", 69_800, 69_900)])

    assert [f["what"] for f in day.failures()] == ["take_profit"]
    assert orders_of(trade)[1:] == [("sell", "stop_loss", 69_800, 14, "filled", 14)]
    assert trade["exit"]["reason"] == STOP_LOSS


# The limits hold as in paper


async def test_a_loss_limit_reached_sells_on_kis_at_the_bid(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """A daily limit of 0.5% (5,000 won) and a stop four ticks down (69,650): at the bid of 69,700
    the open position nets (69,700 − 70,050) × 14 = −4,900 less its costs, past the limit."""
    fake, account = kis_mock
    rules = Rules(fee_rate=FEE, daily_loss_percent=0.5)
    day = MockDay(tmp_path, fake, account, rules=rules)

    [trade] = await day.run(
        [
            Read("10:00:02.3", [Barrier("10:00:00", w=4)]),
            BOUGHT,
            quote("10:01:00", 69_700, 69_800),
            Read("10:08:02.3", [Barrier("10:08:00", w=4)]),
        ]
    )

    assert trade["exit"]["reason"] == "daily_limit"
    assert orders_of(trade)[2:] == [("sell", "daily_limit", 69_700, 14, "filled", 14)]
    assert day.status == "daily_limit"
    assert day.decisions()[-1]["reason"] == "daily_limit"


# The mode, its servers, its records


def test_the_mock_mode_is_a_setting_and_live_is_not_yet() -> None:
    assert TraderSettings.from_env({"KRX_TRADER_MODE": "mock"}, None).mode == MOCK
    with pytest.raises(SettingsError, match="not built yet"):
        TraderSettings.from_env({"KRX_TRADER_MODE": "live"}, None)


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"trader_kis_rest_url": DEFAULT_REST_URL, "trader_kis_ws_url": DEFAULT_WS_URL}, "not KIS's mock"),
        ({"trader_kis_app_key": None}, "are needed"),
        ({"trader_kis_app_key": "PScollector" + "c" * 25}, "the collector's"),
    ],
)
async def test_the_trader_does_not_start_in_mock_mode_unless_it_orders_on_the_mock_servers(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path, change: dict[str, Any], why: str
) -> None:
    """The live servers, no key, the collector's key: the trader stops before it keeps a run or sends
    a call."""
    fake, _ = kis_mock
    settings = replace(settings_of(fake, tmp_path), **change)
    trader = TraderSettings(
        mode=MOCK, quotes_url="ws://127.0.0.1:1/v1/quotes", engine_url="http://127.0.0.1:1"
    )

    with pytest.raises(SettingsError, match=why):
        await run_trader(settings, trader, stop=asyncio.Event(), servers=kis_orders.MOCK)

    assert not (tmp_path / DB_NAME).exists(), "no run kept"
    assert fake.rest_requests == [] and fake.posts_received == [] and fake.tokens_issued == []


async def test_the_challengers_stay_in_paper_beside_the_mock_champion(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, _ = kis_mock
    store = TraderStore(tmp_path / DB_NAME)
    try:
        async with kis.rest_session() as session:
            broker = trader_broker(
                session,
                settings_of(fake, tmp_path),
                lambda: at("09:00:00"),
                servers=Servers(fake.rest_url, fake.ws_url),
            )
            desk = Desk(
                store,
                Rules(fee_rate=FEE),
                CALENDAR.session,
                at("09:00:00"),
                MOCK,
                broker=broker,
                clock=lambda: at("09:00:00"),
            )
            assert isinstance(desk.champion, MockTrader)
            challenger = desk._book(7)
            assert type(challenger) is PaperTrader and challenger.challenger == 7
            await desk.close()
        with pytest.raises(ValueError, match="broker"):
            Desk(store, Rules(fee_rate=FEE), CALENDAR.session, at("09:00:00"), MOCK)
    finally:
        store.close()


def test_the_records_are_of_the_mode_the_trader_last_started_in(tmp_path: Path) -> None:
    store = TraderStore(tmp_path / DB_NAME)
    try:
        store.add_run(at("09:00:00"), PAPER, {})
        store.add_run(at("2026-10-15 08:00:00"), MOCK, {})
    finally:
        store.close()
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db:
        assert promotion.mode_at(db) == MOCK
        assert promotion.mode_at(db, at("23:00:00")) == PAPER, "the mode of the day's close"
    with closing(sqlite3.connect(tmp_path / "none.sqlite3")) as db:
        assert promotion.mode_at(db) == PAPER
    # A live trader (#408) is gated on a simulated record: paper's for now.
    assert [promotion.records_of(m) for m in (PAPER, MOCK, "live")] == [PAPER, MOCK, PAPER]


# A restart, and the trader's process


async def test_a_restarted_mock_book_follows_the_take_profit_resting_on_kis(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    [open_trade] = await MockDay(tmp_path, fake, account).run([TEN, BOUGHT])
    assert open_trade["exit"] is None

    restarted = MockDay(tmp_path, fake, account, started_at=at("10:01:00"))
    [trade] = await restarted.run([Tick("10:01:00.5"), Fill("10:02:00")])

    assert trade["id"] == open_trade["id"]
    assert trade["exit"] == {"at": "2026-10-14T10:02:00+09:00", "price": 70_300, "reason": TAKE_PROFIT}
    assert len(account.placed(SELL_SIDE)) == 1, "the same resting sell, not another"


async def test_the_trader_in_mock_mode_orders_on_kis_from_its_stream(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """``krx-quant trade`` with ``KRX_TRADER_MODE=mock``: the 10:00 prediction bought on KIS at the
    first quote after it, its take-profit sell resting there until KIS fills it; the run kept as
    mock's."""
    fake, account = kis_mock
    state = tmp_path / "state"
    engine = PredictionStore(state / ENGINE_DB)
    Barrier("10:00:00").write(engine)
    engine.close()
    feed = QuoteFeed([SYMBOL])
    port = free_port()
    runner = web.AppRunner(feed.app())
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    clock = [at("10:00:02.3")]
    api = EngineApi([PASSING])
    servers = Servers(fake.rest_url, fake.ws_url)

    def trades() -> list[dict[str, Any]]:
        return read_trades(state / DB_NAME, None, 10) if (state / DB_NAME).exists() else []

    try:
        async with api.serving() as engine_url:
            trader = TraderSettings(
                mode=MOCK,
                quotes_url=f"ws://127.0.0.1:{port}/v1/quotes",
                engine_url=engine_url,
                poll_seconds=0.02,
            )
            stop = asyncio.Event()
            task = asyncio.create_task(
                run_trader(
                    settings_of(fake, state),
                    trader,
                    stop=stop,
                    now=lambda: clock[0],
                    servers=servers,
                    timing=TIMING,
                )
            )
            try:
                await until(lambda: bool(rows(state, "decisions")) if (state / DB_NAME).exists() else False)
                await until(lambda: feed.clients > 0)
                account.bid, account.ask = 70_000, 70_100
                feed.on_event(BOOK.tr_id, book(at("10:00:02.5"), 70_000, 70_100))
                await until(lambda: len(resting_sells(account)) == 1)
                clock[0] = at("10:02:00")
                account.bid, account.improvement = 70_300, 0.0
                account.fill(resting_sells(account)[0])
                await until(lambda: bool(trades()) and trades()[0]["exit"] is not None)
            finally:
                stop.set()
                await asyncio.wait_for(task, 10)
    finally:
        await runner.cleanup()

    [trade] = trades()
    assert (trade["mode"], trade["entry"]["price"], trade["exit"]["reason"]) == (MOCK, 70_050, TAKE_PROFIT)
    assert all(o["broker_id"] for o in trade["orders"])
    assert [r["mode"] for r in rows(state, "runs")] == [MOCK]
