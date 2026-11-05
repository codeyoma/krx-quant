"""The trader's KIS order module (#404, ``kis_orders.Broker``): orders, fills read off the day's
inquiry (full and partial), refusals, cancels, the balance, the orderable cash and the quotes; the
trader's own token, cached and replaced when it expires; the pace and what is sent again; the last
check of a buy before it leaves for KIS (#543).

KIS is the fake one (``fake_kis.FakeKis`` with a ``FakeAccount``), answering in the shapes, codes
and messages the order probe measured on the mock server (#403, ``kis_orders.PROBE_COMMENT``)
where it has them. Every value here is made up: the keys, the account, the prices.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from krx_quant import kis, kis_orders, ratelimit
from krx_quant.auth import TokenManager
from krx_quant.config import DEFAULT_REST_URL, DEFAULT_WS_URL, Settings, SettingsError
from krx_quant.fake_kis import NOTHING_TO_CANCEL, FakeAccount, FakeKis
from krx_quant.feeds.base import KST
from krx_quant.kis_orders import (
    ACCEPTED,
    REFUSED,
    SIDE_BUY,
    SIDE_SELL,
    UNKNOWN,
    Answer,
    Best,
    Broker,
    BuyLimits,
    NoToken,
    Orderable,
    Price,
    Servers,
    Unanswered,
    Unexpected,
    Withheld,
    trader_broker,
    trader_live_broker,
)
from krx_quant.ratelimit import RateLimiter
from tests.test_ratelimit import Monotonic
from tests.waiting import until

DAY = date(2026, 10, 30)
NOW = datetime.combine(DAY, time(10, 0), KST)
SYMBOL = "005930"
TRADER_KEY = "PStrader" + "q" * 28
TRADER_SECRET = "Sq" + "z" * 60
COLLECTOR_KEY = "PScollector" + "c" * 25
ACCOUNT = "50123456-01"
LIVE_KEY = "PSlive" + "l" * 30
LIVE_SECRET = "Sl" + "v" * 60
LIVE_ACCOUNT = "70123456-01"
# Short waits: the fake answers at once. A wait a test relies on is a bound, not a pause.
WAIT, POLL = 1.0, 0.01


@pytest.fixture
async def trader() -> AsyncIterator[tuple[FakeKis, FakeAccount]]:
    fake = FakeKis(app_key=TRADER_KEY, app_secret=TRADER_SECRET)
    await fake.start()
    account = FakeAccount(fake, DAY, account=ACCOUNT.replace("-", "")).install()
    yield fake, account
    await fake.stop()


def settings_of(fake: FakeKis, tmp_path: Path, **changes: Any) -> Settings:
    settings = Settings(
        kis_app_key=COLLECTOR_KEY,
        kis_app_secret="collector-secret",
        state_dir=tmp_path,
        trader_kis_app_key=TRADER_KEY,
        trader_kis_app_secret=TRADER_SECRET,
        trader_kis_account=ACCOUNT,
        trader_kis_rest_url=fake.rest_url,
        trader_kis_ws_url=fake.ws_url,
        token_min_interval_seconds=0.0,
    )
    return replace(settings, **changes)


@asynccontextmanager
async def broker_of(
    fake: FakeKis,
    settings: Settings,
    now: Callable[[], datetime] = lambda: NOW,
    limits: BuyLimits | None = None,
) -> AsyncIterator[Broker]:
    async with kis.rest_session() as session:
        yield trader_broker(
            session,
            settings,
            now,
            servers=Servers(fake.rest_url, fake.ws_url),
            per_second=1000,
            pause=0.01,
            limits=limits,
        )


def posted(fake: FakeKis, tr: kis_orders.Tr) -> list[dict[str, Any]]:
    return [body for _, tr_id, _, body in fake.posts_received if tr_id == tr.mock]


def asked(fake: FakeKis, tr_id: str) -> list[dict[str, str]]:
    return [query for _, t, _, query in fake.rest_requests if t == tr_id]


# Fills, read off the day's inquiry


async def test_a_marketable_buy_fills_whole_on_the_inquiry_at_its_fill_price(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = trader
    async with broker_of(fake, settings_of(fake, tmp_path)) as broker:
        taken = await broker.take(SYMBOL, SIDE_BUY, 10, account.ask, DAY, wait=WAIT, poll=POLL)
    assert taken.sent.outcome == ACCEPTED and taken.sent.code == kis_orders.BOUGHT
    assert taken.filled == 10 and taken.left == 0 and taken.cancel is None
    # The fill's price is the inquiry's average, not the order's: half a tick under the ask here,
    # as the mock server filled a buy at the ask (#403).
    assert taken.average == account.ask - 50
    [body] = posted(fake, kis_orders.BUY)
    assert body["ORD_DVSN"] == kis_orders.LIMIT and body["EXCG_ID_DVSN_CD"] == "KRX"
    assert (body["ORD_QTY"], body["ORD_UNPR"]) == ("10", str(account.ask))
    assert account.holdings[SYMBOL] == 10
    assert fake.ws_connections == 0, "fills come from the inquiry, not the fill notice"


async def test_a_marketable_sell_fills_at_its_fill_price(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = trader
    account.holdings[SYMBOL] = 3
    async with broker_of(fake, settings_of(fake, tmp_path)) as broker:
        taken = await broker.take(SYMBOL, SIDE_SELL, 3, account.bid, DAY, wait=WAIT, poll=POLL)
    assert taken.sent.code == kis_orders.SOLD and taken.filled == 3 and taken.average == account.bid + 50
    assert SYMBOL not in {s for s, q in account.holdings.items() if q}
    assert posted(fake, kis_orders.SELL)[0]["SLL_TYPE"] == kis_orders.GENERAL_SELL


async def test_a_partial_fill_is_kept_and_what_is_left_cancelled(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = trader
    account.depth = 4  # four shares at the ask
    async with broker_of(fake, settings_of(fake, tmp_path)) as broker:
        taken = await broker.take(SYMBOL, SIDE_BUY, 10, account.ask, DAY, wait=0.2, poll=POLL)
        rows = await broker.orders(DAY, SYMBOL)
    assert taken.filled == 4 and taken.left == 0
    assert taken.cancel is not None and taken.cancel.outcome == ACCEPTED
    assert taken.cancel.code == kis_orders.CANCELLED
    assert taken.row is not None and taken.row.cancelled == 6 and taken.row.quantity == 10
    assert account.holdings[SYMBOL] == 4
    # The cancel is its own row, of the order.
    order, cancel = rows
    assert taken.sent.ref is not None and order.of_order(taken.sent.ref)
    assert cancel.cancel and kis_orders.same_number(cancel.original, order.number) and cancel.price == 0
    assert order.original == "" and not order.cancel


async def test_an_order_that_does_not_fill_is_cancelled_whole(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = trader
    account.fills = False
    async with broker_of(fake, settings_of(fake, tmp_path)) as broker:
        taken = await broker.take(SYMBOL, SIDE_BUY, 5, account.ask, DAY, wait=0.2, poll=POLL)
    assert taken.filled == 0 and taken.left == 0 and taken.average == 0
    assert taken.cancel is not None and taken.cancel.accepted
    assert taken.row is not None and taken.row.cancelled == 5
    assert not account.holdings.get(SYMBOL)


async def test_a_fill_that_races_the_cancel_counts(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    # The order fills between the last inquiry and the cancel: KIS finds nothing to cancel, and the
    # inquiry read after shows the fill.
    fake, account = trader
    account.fills, account.fills_late = False, True
    async with broker_of(fake, settings_of(fake, tmp_path)) as broker:
        taken = await broker.take(SYMBOL, SIDE_BUY, 5, account.ask, DAY, wait=0.2, poll=POLL)
    assert taken.cancel is not None and taken.cancel.outcome == REFUSED
    assert taken.cancel.code == NOTHING_TO_CANCEL[0]
    assert taken.filled == 5 and taken.left == 0 and account.holdings[SYMBOL] == 5


# Refusals and what is sent again


async def test_a_refused_order_is_not_sent_again_and_says_why(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = trader
    async with broker_of(fake, settings_of(fake, tmp_path)) as broker:
        off_tick = await broker.take(SYMBOL, SIDE_BUY, 1, account.ask + 1, DAY, wait=WAIT, poll=POLL)
        not_held = await broker.sell(SYMBOL, 1, account.bid)
    assert off_tick.sent.outcome == REFUSED and off_tick.sent.code == "40030000"
    assert off_tick.sent.reason == "the price is off the tick (호가단위 오류)"
    assert off_tick.left == 0 and off_tick.row is None and off_tick.cancel is None
    assert not_held.outcome == REFUSED and not_held.code == "40240000"
    assert not_held.reason == "no shares held to sell (잔고내역이 없습니다)"
    assert len(posted(fake, kis_orders.BUY)) == 1 and len(posted(fake, kis_orders.SELL)) == 1
    assert not asked(fake, kis_orders.FILLS.mock), "a refused order is not followed"
    assert not account.orders


async def test_a_side_neither_buy_nor_sell_sends_nothing(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = trader
    async with broker_of(fake, settings_of(fake, tmp_path)) as broker:
        with pytest.raises(ValueError, match="a side is 02"):
            await broker.take(SYMBOL, "buy", 1, account.ask, DAY, wait=WAIT, poll=POLL)
    assert not fake.posts_received


async def test_a_refusal_of_a_code_not_known_gives_kis_s_message(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = trader
    account.holdings[SYMBOL] = 1
    async with broker_of(fake, settings_of(fake, tmp_path)) as broker:
        sent = await broker.sell(SYMBOL, 2, account.bid)  # more than held (made-up code)
    assert sent.outcome == REFUSED and sent.reason == "주문가능수량을 초과 했습니다."


async def test_an_order_whose_answer_is_lost_is_not_sent_again_and_the_inquiry_shows_it(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = trader
    account.lose_answers = 1  # taken, then the connection drops before the answer
    async with broker_of(fake, settings_of(fake, tmp_path)) as broker:
        taken = await broker.take(SYMBOL, SIDE_BUY, 2, account.ask, DAY, wait=WAIT, poll=POLL)
        rows = await broker.orders(DAY, SYMBOL)
    assert taken.sent.outcome == UNKNOWN and taken.sent.answer.status is None
    assert taken.left is None, "what became of it is not known"
    assert len(posted(fake, kis_orders.BUY)) == 1, "an order sent twice is two orders"
    [row] = rows
    assert row.filled == 2 and row.left == 0 and len(account.orders) == 1


async def test_a_call_refused_for_the_per_second_limit_goes_again_after_a_pause(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = trader
    fake.busy[kis_orders.BUY.mock] = 2
    fake.busy[kis_orders.BALANCE.mock] = 3
    async with broker_of(fake, settings_of(fake, tmp_path)) as broker:
        sent = await broker.buy(SYMBOL, 1, account.ask)
        balance = await broker.balance()
    # KIS took none of a call it refused so: the order went in once.
    assert sent.accepted and len(posted(fake, kis_orders.BUY)) == 3 and len(account.orders) == 1
    assert balance.held(SYMBOL) == 1 and len(asked(fake, kis_orders.BALANCE.mock)) == 4


async def test_a_call_refused_for_the_limit_more_than_the_retries_is_refused(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = trader
    fake.busy[kis_orders.BUY.mock] = fake.busy[kis_orders.PRICE] = 10
    async with broker_of(fake, settings_of(fake, tmp_path)) as broker:
        sent = await broker.buy(SYMBOL, 1, account.ask)
        with pytest.raises(Unanswered, match="price: HTTP 500 EGW00201"):
            await broker.price(SYMBOL)
    assert sent.outcome == REFUSED and sent.code == kis_orders.BUSY
    assert len(posted(fake, kis_orders.BUY)) == 1 + kis_orders.BUSY_RETRIES and not account.orders


async def test_the_retry_rules_send_a_call_again_only_for_the_limit_and_once_for_the_token() -> None:
    answers = {
        "busy": Answer("X", 500, {"rt_cd": "1", "msg_cd": kis_orders.BUSY}),
        "token": Answer("X", 500, {"rt_cd": "1", "msg_cd": "EGW00123"}),
        "refused": Answer("X", 200, {"rt_cd": "1", "msg_cd": "40030000"}),
        "lost": Answer("X", None, None, error="ServerDisconnectedError"),
    }
    invalidated: list[bool] = []
    tokens = SimpleNamespace(invalidate=lambda: invalidated.append(True))
    broker = Broker(SimpleNamespace(), tokens, pause=0, retries=2)  # type: ignore[arg-type]

    async def calls(kind: str) -> int:
        sent: list[Answer] = []

        async def send() -> Answer:
            sent.append(answers[kind])
            return answers[kind]

        await broker._call(kind, send)
        return len(sent)

    assert await calls("busy") == 3  # once and twice again
    assert await calls("token") == 2 and invalidated == [True]  # once again, with a new token
    assert await calls("refused") == 1 and await calls("lost") == 1


# The token


async def test_the_trader_s_token_is_its_own_cached_and_reused_across_a_restart(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = trader
    settings = settings_of(fake, tmp_path)
    async with broker_of(fake, settings) as broker:
        await broker.price(SYMBOL)
        await broker.buy(SYMBOL, 1, account.lower)
        await broker.balance()
    async with broker_of(fake, settings) as again:  # a restart: a new process, the same state
        await again.orders(DAY)
    assert len(fake.tokens_issued) == 1
    auths = {query["_auth"] for _, _, _, query in fake.rest_requests}
    assert auths == {f"Bearer {fake.tokens_issued[0]}"}
    cached = json.loads((tmp_path / kis_orders.TOKEN_FILE).read_text())
    assert cached["token"] == fake.tokens_issued[0]
    assert not (tmp_path / "kis_token.json").exists(), "never the collector's token file"


async def test_a_token_past_its_expiry_is_issued_anew(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, _ = trader
    fake.token_expires = "2026-10-30 12:00:00"  # KST, as KIS writes it (#403)
    clock = [NOW]
    async with broker_of(fake, settings_of(fake, tmp_path), lambda: clock[0]) as broker:
        await broker.price(SYMBOL)
        clock[0] = datetime.combine(DAY, time(12, 30), KST)
        await broker.price(SYMBOL)
    first, second = fake.tokens_issued
    assert [query["_auth"] for query in asked(fake, kis_orders.PRICE)] == [
        f"Bearer {first}",
        f"Bearer {second}",
    ]


async def test_a_token_kis_no_longer_takes_is_replaced_and_the_order_sent_once_more(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = trader
    async with broker_of(fake, settings_of(fake, tmp_path)) as broker:
        await broker.price(SYMBOL)
        fake.expire_tokens()
        sent = await broker.buy(SYMBOL, 1, account.ask)
    assert sent.accepted and len(fake.tokens_issued) == 2
    # The first was refused for its token before KIS took it: one order.
    assert len(posted(fake, kis_orders.BUY)) == 2 and len(account.orders) == 1


async def test_without_a_token_nothing_is_sent(trader: tuple[FakeKis, FakeAccount], tmp_path: Path) -> None:
    fake, account = trader
    settings = settings_of(fake, tmp_path, trader_kis_app_secret="not-the-trader-s-secret")
    async with broker_of(fake, settings) as broker:
        with pytest.raises(NoToken, match="buy: no access token"):
            await broker.buy(SYMBOL, 1, account.ask)
    assert not fake.posts_received and not account.orders


# The pace


class Paced(RateLimiter):
    """The broker's limiter, keeping when it let each call through."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.let_through: list[float] = []

    async def acquire(self, *, last: bool = False) -> float:
        self.let_through.append(await super().acquire(last=last))
        return self.let_through[-1]


def paced(monkeypatch: pytest.MonkeyPatch) -> list[Paced]:
    """The limiters the brokers made from here on, on a clock that moves only when they wait."""
    built: list[Paced] = []

    def build(*args: Any, **kwargs: Any) -> Paced:
        built.append(Paced(*args, **kwargs))
        return built[-1]

    monkeypatch.setattr(kis_orders, "RateLimiter", build)
    clock = Monotonic()
    monkeypatch.setattr(ratelimit, "time", SimpleNamespace(monotonic=clock))
    monkeypatch.setattr(ratelimit, "asyncio", SimpleNamespace(sleep=clock.sleep))
    return built


@pytest.mark.parametrize(
    ("make", "let_through"),
    [
        # Of five sent at once the mock server refused two; of thirty sent a second apart, none
        # (#403). Two in a window were refused on every mock trade of 2026-11-02 (#461): one.
        (trader_broker, [0.0, 1.1, 2.2, 3.3, 4.4]),
        # The live server's limit was not measured: the live broker keeps two in a window.
        (trader_live_broker, [0.0, 0.0, 1.1, 1.1, 2.2]),
    ],
)
async def test_each_mode_s_broker_paces_its_calls_over_a_1_1_second_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make: Callable[..., Broker], let_through: list[float]
) -> None:
    limiters = paced(monkeypatch)
    settings = Settings(
        kis_app_key=COLLECTOR_KEY,
        kis_app_secret="collector-secret",
        state_dir=tmp_path,
        trader_kis_app_key=TRADER_KEY,
        trader_kis_app_secret=TRADER_SECRET,
        trader_kis_account=ACCOUNT,
        trader_live_kis_app_key=LIVE_KEY,
        trader_live_kis_app_secret=LIVE_SECRET,
        trader_live_kis_account=LIVE_ACCOUNT,
    )
    async with kis.rest_session() as session:
        make(session, settings, lambda: NOW)  # KIS's servers of the mode, the default pace
    [limiter] = limiters
    assert [await limiter.acquire() for _ in range(5)] == pytest.approx(let_through)


async def test_the_mock_broker_sends_no_two_calls_within_1_1_seconds(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A buy and the inquiry of its fill right after it, as every mock trade of 2026-11-02 sent them
    (#461): the inquiry waits out the buy's window."""
    fake, account = trader
    limiters = paced(monkeypatch)
    async with kis.rest_session() as session:
        broker = trader_broker(
            session, settings_of(fake, tmp_path), lambda: NOW, servers=Servers(fake.rest_url, fake.ws_url)
        )
        sent = await broker.buy(SYMBOL, 1, account.ask)
        [row] = await broker.orders(DAY)
    [limiter] = limiters
    assert sent.accepted and sent.ref is not None and row.number == sent.ref.number
    assert limiter.let_through == pytest.approx([0.0, 1.1])


async def test_a_call_sent_again_after_a_refusal_for_the_limit_waits_for_the_limiter_too(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inquiry refused once for the limit goes again after the pause (a hundredth of a second
    here) and through the limiter, as does the call after it: none of them within 1.1 s of another."""
    fake, account = trader
    fake.busy[kis_orders.FILLS.mock] = 1
    limiters = paced(monkeypatch)
    async with kis.rest_session() as session:
        broker = trader_broker(
            session,
            settings_of(fake, tmp_path),
            lambda: NOW,
            servers=Servers(fake.rest_url, fake.ws_url),
            pause=0.01,
        )
        sent = await broker.buy(SYMBOL, 1, account.ask)
        [row] = await broker.orders(DAY)
        balance = await broker.balance()
    [limiter] = limiters
    assert sent.accepted and sent.ref is not None and row.number == sent.ref.number
    assert balance.held(SYMBOL) == 1
    assert len(asked(fake, kis_orders.FILLS.mock)) == 2, "the inquiry, refused once and sent again"
    assert limiter.let_through == pytest.approx([0.0, 1.1, 2.2, 3.3])


# The trader's own key, on the mock server only


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        ({"trader_kis_app_key": None}, "are needed"),
        ({"trader_kis_account": None}, "are needed"),
        ({"trader_kis_app_key": COLLECTOR_KEY}, "the collector's KIS_APP_KEY"),
        (
            {"trader_kis_rest_url": DEFAULT_REST_URL, "trader_kis_ws_url": DEFAULT_WS_URL},
            "not KIS's mock servers",
        ),
        ({"trader_kis_account": "1234-5678"}, "KRX_TRADER_KIS_ACCOUNT"),
    ],
)
async def test_the_broker_is_the_trader_s_own_on_the_mock_server_only(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path, changes: dict[str, Any], why: str
) -> None:
    fake, _ = trader
    with pytest.raises(SettingsError, match=why):
        async with broker_of(fake, settings_of(fake, tmp_path, **changes)):
            pass
    assert not (fake.tokens_issued or fake.rest_requests or fake.posts_received)


# The inquiries


async def test_a_cancel_of_a_resting_order_shows_on_the_inquiry(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = trader
    async with broker_of(fake, settings_of(fake, tmp_path)) as broker:
        sent = await broker.buy(SYMBOL, 3, account.lower)  # rests: far under the ask
        assert sent.ref is not None
        [open_order] = await broker.open_orders(DAY, SYMBOL)
        cancel = await broker.cancel(sent.ref)
        rows = await broker.orders(DAY, SYMBOL)
        left = await broker.open_orders(DAY, SYMBOL)
    assert open_order.of_order(sent.ref) and open_order.left == 3 and open_order.price == account.lower
    assert cancel.outcome == ACCEPTED and cancel.code == kis_orders.CANCELLED and cancel.ref is not None
    order, its_cancel = rows
    assert (order.left, order.filled, order.cancelled, order.cancel) == (0, 0, 3, False)
    assert its_cancel.cancel and its_cancel.of_order(cancel.ref) is False
    assert kis_orders.same_number(its_cancel.number, cancel.ref.number)
    assert kis_orders.same_number(its_cancel.original, sent.ref.number)
    assert left == []
    [body] = posted(fake, kis_orders.CANCEL)
    assert body["ORGN_ODNO"] == sent.ref.number and body["QTY_ALL_ORD_YN"] == "Y"


async def test_the_inquiry_is_read_every_page(trader: tuple[FakeKis, FakeAccount], tmp_path: Path) -> None:
    fake, account = trader
    account.page_rows = 2
    async with broker_of(fake, settings_of(fake, tmp_path)) as broker:
        for _ in range(5):
            await broker.buy(SYMBOL, 1, account.lower)
        rows = await broker.orders(DAY, SYMBOL)
    assert [row.number for row in rows] == [o.number for o in account.orders]
    pages = asked(fake, kis_orders.FILLS.mock)
    assert [p["CTX_AREA_NK100"] for p in pages] == ["", "2", "4"]


async def test_an_empty_day_has_no_orders(trader: tuple[FakeKis, FakeAccount], tmp_path: Path) -> None:
    fake, _ = trader
    async with broker_of(fake, settings_of(fake, tmp_path)) as broker:
        assert await broker.orders(DAY) == [] and await broker.open_orders(DAY) == []


async def test_the_balance_orderable_cash_and_quotes_are_read_by_their_measured_fields(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = trader
    account.cash = 1_000_000
    async with broker_of(fake, settings_of(fake, tmp_path)) as broker:
        before = await broker.orderable(SYMBOL, account.ask)
        await broker.take(SYMBOL, SIDE_BUY, 10, account.ask, DAY, wait=WAIT, poll=POLL)
        await broker.sell(SYMBOL, 4, account.upper)  # rests, holding four of the ten
        balance = await broker.balance()
        after = await broker.orderable(SYMBOL, account.ask)
        price = await broker.price(SYMBOL)
        best = await broker.best(SYMBOL)
    # 1,000,000 ÷ (61,100 × 1.00014) = 16.36: sixteen shares.
    assert before == Orderable(cash=1_000_000, quantity=16, amount=16 * 61_100)
    held = balance.holdings[SYMBOL]
    assert (held.quantity, held.sellable, held.average) == (10, 6, 61_050.0)
    # The deposit stays the day's start; the deposit two days on paid the fill and its commission.
    paid = 10 * 61_050 * 1.00014
    assert balance.deposit == 1_000_000 and balance.settled == round(1_000_000 - paid)
    assert balance.costs == round(10 * 61_050 * 0.00014)
    assert after.cash == round(1_000_000 - paid) and after.quantity == 6
    assert price == Price(price=61_000, upper=79_300, lower=42_700, tick=100)
    assert best == Best(ask=61_100, bid=61_000, ask_size=1_500, bid_size=2_500, at="100000")


def test_the_most_shares_to_buy_keep_to_the_cash_with_the_commission_and_to_kis_s_count() -> None:
    # The mock server accepted a buy beyond the shares the cash buys (#403): the trader stops there.
    assert Orderable(cash=1_000_000, quantity=99, amount=0).most(61_100, 0.00014) == 16
    assert Orderable(cash=1_000_000, quantity=15, amount=0).most(61_100, 0.00014) == 15
    assert Orderable(cash=61_100, quantity=1, amount=0).most(61_100, 0.00014) == 0  # the fee
    assert Orderable(cash=1_000_000, quantity=5, amount=0).most(0, 0.00014) == 0
    assert math.floor(1_000_000 / (61_100 * 1.00014)) == 16


async def test_an_answer_short_of_a_measured_field_is_unexpected(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = trader
    account.holdings[SYMBOL] = 2
    measured = fake.rest_responses[kis_orders.BALANCE.mock]

    def without_quantity(params: dict[str, str]) -> Any:
        answer = measured(params)
        assert isinstance(answer, tuple)
        body, headers = answer
        for row in body["output1"]:
            del row["hldg_qty"]
        return body, headers

    fake.rest_responses[kis_orders.BALANCE.mock] = without_quantity
    fake.rest_responses[kis_orders.PRICE] = lambda _: {"rt_cd": "1", "msg_cd": "FK0009", "msg1": "made up"}
    async with broker_of(fake, settings_of(fake, tmp_path)) as broker:
        with pytest.raises(Unexpected, match="no hldg_qty"):
            await broker.balance()
        with pytest.raises(Unanswered, match="price: HTTP 200 FK0009 made up"):
            await broker.price(SYMBOL)


async def test_a_read_that_fails_while_an_order_is_followed_is_read_again(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = trader
    fake.busy[kis_orders.FILLS.mock] = 1 + kis_orders.BUSY_RETRIES  # the first read fails whole
    async with broker_of(fake, settings_of(fake, tmp_path)) as broker:
        taken = await broker.take(SYMBOL, SIDE_BUY, 2, account.ask, DAY, wait=WAIT, poll=POLL)
    assert taken.filled == 2 and taken.cancel is None


async def test_a_cancel_that_cannot_be_sent_leaves_the_order_open_for_the_caller(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#405: once KIS took the order, ``take`` raises nothing: a cancel with no token to send it with
    ends it with what filled and what is left open, for the trader to follow."""
    fake, account = trader
    account.depth = 4

    async def no_token(self: Broker, order: kis_orders.OrderRef) -> kis_orders.Sent:
        raise NoToken("cancel: no access token (made up), nothing sent")

    monkeypatch.setattr(Broker, "cancel", no_token)
    async with broker_of(fake, settings_of(fake, tmp_path)) as broker:
        taken = await broker.take(SYMBOL, SIDE_BUY, 10, account.ask, DAY, wait=0.2, poll=POLL)

    assert taken.sent.accepted and taken.cancel is None and taken.unsettled
    assert (taken.filled, taken.left) == (4, 6)
    assert [o.left for o in account.orders] == [6], "open on KIS still"


async def test_fills_that_cannot_be_read_leave_the_order_unsettled_for_the_caller(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """#405: no read of the order's row answers: ``take`` raises nothing, and the order KIS took is
    unsettled (its end not known), named for the caller to follow."""
    fake, account = trader
    fake.rest_responses[kis_orders.FILLS.mock] = lambda params: {
        "rt_cd": "1",
        "msg_cd": "FK0010",
        "msg1": "x",
    }
    async with broker_of(fake, settings_of(fake, tmp_path)) as broker:
        taken = await broker.take(SYMBOL, SIDE_BUY, 10, account.ask, DAY, wait=0.2, poll=POLL)

    assert taken.sent.accepted and taken.row is None and taken.left is None and taken.unsettled
    assert taken.sent.ref is not None and kis_orders.same_number(
        taken.sent.ref.number, account.orders[0].number
    )


# The last check of a buy (#543)

# The trader's limits as ``trader/run.py`` injects them: the capital, its one symbol.
LIMITS = BuyLimits(1_000_000, frozenset({SYMBOL}))


def quote_of(broker: Broker, account: FakeAccount, at: datetime = NOW, symbol: str = SYMBOL) -> None:
    """The collector's latest best quote, KIS's book as it is, as the trader tells the broker."""
    broker.quoted(symbol, account.bid, account.ask, at)


async def test_a_buy_within_the_limits_leaves_for_kis(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """Sixteen shares at the ask, 977,600 won, within the 1,000,000 cap: sent and filled."""
    fake, account = trader
    async with broker_of(fake, settings_of(fake, tmp_path), limits=LIMITS) as broker:
        quote_of(broker, account)
        taken = await broker.take(SYMBOL, SIDE_BUY, 16, account.ask, DAY, wait=WAIT, poll=POLL)
    assert taken.sent.outcome == ACCEPTED and taken.filled == 16
    assert len(posted(fake, kis_orders.BUY)) == 1


async def test_a_buy_above_the_cap_is_withheld_with_a_warning_and_nothing_is_sent(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Seventeen shares at the ask, 1,038,700 won, above the cap: withheld, before any call. The
    error's text, which the failures and the night's report keep, holds no price."""
    fake, account = trader
    caplog.set_level(logging.WARNING, logger="krx_quant.kis_orders")
    async with broker_of(fake, settings_of(fake, tmp_path), limits=LIMITS) as broker:
        quote_of(broker, account)
        with pytest.raises(Withheld) as withheld:
            await broker.take(SYMBOL, SIDE_BUY, 17, account.ask, DAY, wait=WAIT, poll=POLL)
    assert str(withheld.value) == (
        "buy: withheld by the broker's last check (#543): its amount with the 0 buy(s) of the day that"
        " may still fill is 104% of the cap"
    )
    assert str(account.ask) not in str(withheld.value)
    assert fake.posts_received == [] and fake.rest_requests == [], "nothing sent, nothing read"
    [warning] = [r for r in caplog.records if r.name == "krx_quant.kis_orders"]
    assert warning.levelno == logging.WARNING
    assert "withheld a buy of 17 share(s) of 005930 at 61100 (1,038,700 won" in warning.getMessage()


async def test_the_buys_that_may_still_fill_count_toward_the_cap_until_a_read_shows_them_over(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """Ten shares resting on KIS (611,000 won) and seven more (427,700) are above the cap together:
    the seven are withheld. Once the ten are cancelled and a read shows nothing left of them, the
    seven go."""
    fake, account = trader
    account.fills = False  # a buy rests
    async with broker_of(fake, settings_of(fake, tmp_path), limits=LIMITS) as broker:
        quote_of(broker, account)
        first = await broker.buy(SYMBOL, 10, account.ask)
        assert first.accepted and first.ref is not None
        with pytest.raises(Withheld, match="the 1 buy\\(s\\) of the day that may still fill is 104%"):
            await broker.buy(SYMBOL, 7, account.ask)
        await broker.cancel(first.ref)
        with pytest.raises(Withheld):  # cancelled on KIS, but no read has shown it yet
            await broker.buy(SYMBOL, 7, account.ask)
        assert await broker.order(DAY, SYMBOL, first.ref) is not None
        second = await broker.buy(SYMBOL, 7, account.ask)
    assert second.accepted
    assert [o.quantity for o in account.placed(kis_orders.SIDE_BUY)] == [10, 7]


async def test_a_buy_left_open_by_a_cancel_that_could_not_be_sent_counts(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Four of ten filled and the cancel could not be sent: the six left (366,600 won) still count,
    and eleven more (672,100) are withheld; ten (611,000) go."""
    fake, account = trader
    account.depth = 4

    async def no_token(self: Broker, order: kis_orders.OrderRef) -> kis_orders.Sent:
        raise NoToken("cancel: no access token (made up), nothing sent")

    monkeypatch.setattr(Broker, "cancel", no_token)
    async with broker_of(fake, settings_of(fake, tmp_path), limits=LIMITS) as broker:
        quote_of(broker, account)
        taken = await broker.take(SYMBOL, SIDE_BUY, 10, account.ask, DAY, wait=0.2, poll=POLL)
        assert taken.unsettled and taken.left == 6
        with pytest.raises(Withheld, match="104% of the cap"):
            await broker.buy(SYMBOL, 11, account.ask)
        sent = await broker.buy(SYMBOL, 10, account.ask)
    assert sent.accepted


async def test_after_a_restart_the_buys_the_day_s_inquiry_shows_count(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """A buy resting on KIS from before a restart (or the account's own): the new broker learns it from
    the day's inquiry, which the start's reconcile reads before any buy (#406), and counts it."""
    fake, account = trader
    account.fills = False
    async with broker_of(fake, settings_of(fake, tmp_path)) as before:
        assert (await before.buy(SYMBOL, 10, account.ask)).accepted
    async with broker_of(fake, settings_of(fake, tmp_path), limits=LIMITS) as broker:
        quote_of(broker, account)
        [row] = await broker.open_orders(DAY)
        with pytest.raises(Withheld, match="the 1 buy\\(s\\) of the day that may still fill"):
            await broker.buy(SYMBOL, 7, account.ask)
    assert row.left == 10 and len(account.placed(kis_orders.SIDE_BUY)) == 1


async def test_a_buy_whose_answer_is_lost_counts_whole_until_its_day_is_over(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """Whether a buy without an answer went in, no answer tells: it counts whole for its day (the
    trader buys nothing more that day after it, #405). A KRX limit order is a day order: the next day
    it counts no more."""
    fake, account = trader
    account.fills, account.lose_answers = False, 1
    clock = [NOW]
    async with broker_of(fake, settings_of(fake, tmp_path), lambda: clock[0], LIMITS) as broker:
        quote_of(broker, account)
        lost = await broker.buy(SYMBOL, 10, account.ask)
        assert lost.outcome == UNKNOWN
        with pytest.raises(Withheld, match="104% of the cap"):
            await broker.buy(SYMBOL, 7, account.ask)
        clock[0] = NOW + timedelta(days=1)
        quote_of(broker, account, clock[0])
        next_day = await broker.buy(SYMBOL, 7, account.ask)
    assert next_day.accepted


async def test_two_buys_at_once_count_each_other(trader: tuple[FakeKis, FakeAccount], tmp_path: Path) -> None:
    """Nine shares twice at once (549,900 won each): the second is checked while the first is in flight,
    counts it, and is withheld."""
    fake, account = trader
    async with broker_of(fake, settings_of(fake, tmp_path), limits=LIMITS) as broker:
        quote_of(broker, account)
        first, second = await asyncio.gather(
            broker.buy(SYMBOL, 9, account.ask), broker.buy(SYMBOL, 9, account.ask), return_exceptions=True
        )
    assert isinstance(first, kis_orders.Sent) and first.accepted
    assert isinstance(second, Withheld)
    assert len(posted(fake, kis_orders.BUY)) == 1


def gate_tokens(monkeypatch: pytest.MonkeyPatch, held: set[int]) -> tuple[asyncio.Event, list[int]]:
    """The trader's token held back on its calls numbered in ``held`` (from 1), until the gate opens:
    a token renewed, KIS's once-a-minute issuance waited out. The calls asked so far."""
    gate, asked = asyncio.Event(), list[int]()
    real = TokenManager.get

    async def gated(self: TokenManager) -> str:
        asked.append(len(asked) + 1)
        if asked[-1] in held:
            await gate.wait()
        return await real(self)

    monkeypatch.setattr(TokenManager, "get", gated)
    return gate, asked


WHY_AGAIN = {
    "the quote ages": "is 11s old",
    "the quote moves": "below the latest best bid",
    "the cap narrows": "122% of the cap",
}


@pytest.mark.parametrize("meanwhile", list(WHY_AGAIN))
async def test_a_buy_is_checked_again_after_waiting_for_its_token(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, meanwhile: str
) -> None:
    """The buy passed its first check, then waited for its token: checked again right before it is
    sent, it is withheld once the quote is eleven seconds old, has moved 5% away, or the cap is the
    first period's. Nothing is sent."""
    fake, account = trader
    gate, asked = gate_tokens(monkeypatch, {1})
    clock = [NOW]
    async with broker_of(fake, settings_of(fake, tmp_path), lambda: clock[0], LIMITS) as broker:
        quote_of(broker, account)
        buying = asyncio.create_task(broker.buy(SYMBOL, 5, account.ask))
        await until(lambda: asked == [1])
        if meanwhile == "the quote ages":
            clock[0] = NOW + timedelta(seconds=11)
        elif meanwhile == "the quote moves":
            broker.quoted(SYMBOL, 64_100, 64_200, NOW + timedelta(seconds=1))
        else:
            broker.set_cap(250_000)
        gate.set()
        with pytest.raises(Withheld, match=WHY_AGAIN[meanwhile]):
            await buying
    assert account.placed(kis_orders.SIDE_BUY) == [] and posted(fake, kis_orders.BUY) == []


async def test_a_buy_sent_again_after_a_refusal_for_the_limit_is_checked_again(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KIS's gateway refused the buy for its per-second limit (``BUSY``, not taken); before it goes
    again the quote moved 5% away: the second send is withheld."""
    fake, account = trader
    fake.busy[kis_orders.BUY.mock] = 1
    gate, asked = gate_tokens(monkeypatch, {2})
    async with broker_of(fake, settings_of(fake, tmp_path), limits=LIMITS) as broker:
        quote_of(broker, account)
        buying = asyncio.create_task(broker.buy(SYMBOL, 5, account.ask))
        await until(lambda: asked == [1, 2])
        broker.quoted(SYMBOL, 57_800, 57_900, NOW + timedelta(seconds=1))
        gate.set()
        with pytest.raises(Withheld, match="above the latest best ask"):
            await buying
    assert account.placed(kis_orders.SIDE_BUY) == []
    assert len(posted(fake, kis_orders.BUY)) == 1, "the refused send alone"


async def test_a_buy_of_a_symbol_the_trader_does_not_buy_is_withheld(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = trader
    async with broker_of(fake, settings_of(fake, tmp_path), limits=LIMITS) as broker:
        quote_of(broker, account, symbol="000660")
        with pytest.raises(Withheld, match="000660 is not a symbol the trader buys \\(005930\\)"):
            await broker.buy("000660", 1, account.ask)
    assert fake.posts_received == []


@pytest.mark.parametrize(
    ("price", "quoted_ago", "why"),
    [
        (64_200, 0.0, "its price is 5.1% above the latest best ask \\(more than 3%\\)"),
        (57_900, 0.0, "its price is 5.1% below the latest best bid \\(more than 3%\\)"),
        (61_100, 11.0, "the latest best quote of 005930 is 11s old \\(more than 10s\\)"),
        (61_100, None, "no best quote of 005930 to check its price against"),
    ],
)
async def test_a_buy_far_from_the_latest_quote_or_without_a_recent_one_is_withheld(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path, price: int, quoted_ago: float | None, why: str
) -> None:
    """Against the latest best quote of 61,000/61,100: more than 3% above the ask or below the bid, or
    no quote within ten seconds."""
    fake, account = trader
    async with broker_of(fake, settings_of(fake, tmp_path), limits=LIMITS) as broker:
        if quoted_ago is not None:
            quote_of(broker, account, NOW - timedelta(seconds=quoted_ago))
        with pytest.raises(Withheld, match=why):
            await broker.buy(SYMBOL, 1, price)
    assert fake.posts_received == []


async def test_a_buy_within_the_band_goes_and_kis_s_own_book_is_a_quote_too(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """3% above the ask at most goes (62,900 against 61,100: 2.9%); the broker's own REST read of KIS's
    book (``best``, in a stall) is a quote to check against, as the stream's is."""
    fake, account = trader
    async with broker_of(fake, settings_of(fake, tmp_path), limits=LIMITS) as broker:
        await broker.best(SYMBOL)
        near = await broker.buy(SYMBOL, 1, 62_900)
        ten_seconds = NOW - timedelta(seconds=10)
        broker.quoted(SYMBOL, 0, account.ask, NOW)  # a side empty: not a quote, the REST read stays
        broker.quoted(SYMBOL, account.bid, account.ask, ten_seconds)  # older than the one kept
        at_the_ask = await broker.buy(SYMBOL, 1, account.ask)
    assert near.accepted and at_the_ask.accepted


async def test_the_live_first_period_s_cap_narrows_the_cap_never_above_the_settings(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """The first period's 250,000 won (#408, told by ``run.LiveWatch``): five shares (305,500) are
    withheld, four (244,400) go. A cap above the settings' capital is the capital."""
    fake, account = trader
    async with broker_of(fake, settings_of(fake, tmp_path), limits=LIMITS) as broker:
        quote_of(broker, account)
        broker.set_cap(250_000)
        with pytest.raises(Withheld, match="122% of the cap"):
            await broker.buy(SYMBOL, 5, account.ask)
        four = await broker.buy(SYMBOL, 4, account.ask)
        broker.set_cap(5_000_000)
        capped = broker.cap
    assert four.accepted and capped == 1_000_000


async def test_no_sell_nor_cancel_is_ever_checked(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """A stop in a fall is never held up: with no quote at all and a cap of one won, a sell of
    6,100,000 won at the day's lower limit, a sell of another symbol, a marketable sell (``take``) and
    a cancel all leave for KIS."""
    fake, account = trader
    account.holdings.update({SYMBOL: 101, "000660": 5})
    tiny = BuyLimits(1, frozenset({"999999"}))
    async with broker_of(fake, settings_of(fake, tmp_path), limits=tiny) as broker:
        far = await broker.sell(SYMBOL, 100, account.lower)
        other = await broker.sell("000660", 5, account.lower)
        taken = await broker.take(SYMBOL, SIDE_SELL, 1, account.bid, DAY, wait=WAIT, poll=POLL)
        account.holdings[SYMBOL] = 1
        account.fills = False
        resting = await broker.sell(SYMBOL, 1, account.upper)
        assert resting.ref is not None
        cancel = await broker.cancel(resting.ref)
    assert far.accepted and other.accepted and taken.filled == 1 and cancel.accepted
    assert len(posted(fake, kis_orders.SELL)) == 4 and len(posted(fake, kis_orders.CANCEL)) == 1


async def test_without_limits_a_broker_checks_nothing(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """The read-only brokers (the day's check, the notice layout, the live probe) carry no limits."""
    fake, account = trader
    async with broker_of(fake, settings_of(fake, tmp_path)) as broker:
        sent = await broker.buy("000660", 1_000, account.ask)
        broker.set_cap(1)
    assert sent.accepted and broker.cap is None and broker.limits is None
