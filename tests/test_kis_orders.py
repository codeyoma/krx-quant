"""The trader's KIS order module (#404, ``kis_orders.Broker``): orders, fills read off the day's
inquiry (full and partial), refusals, cancels, the balance, the orderable cash and the quotes; the
trader's own token, cached and replaced when it expires; the pace and what is sent again.

KIS is the fake one (``fake_kis.FakeKis`` with a ``FakeAccount``), answering in the shapes, codes
and messages the order probe measured on the mock server (#403, ``kis_orders.PROBE_COMMENT``)
where it has them. Every value here is made up: the keys, the account, the prices.
"""

from __future__ import annotations

import json
import math
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date, datetime, time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from krx_quant import kis, kis_orders, ratelimit
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
    NoToken,
    Orderable,
    Price,
    Servers,
    Unanswered,
    Unexpected,
    trader_broker,
)
from krx_quant.ratelimit import RateLimiter
from tests.test_ratelimit import Monotonic

DAY = date(2026, 10, 30)
NOW = datetime.combine(DAY, time(10, 0), KST)
SYMBOL = "005930"
TRADER_KEY = "PStrader" + "q" * 28
TRADER_SECRET = "Sq" + "z" * 60
COLLECTOR_KEY = "PScollector" + "c" * 25
ACCOUNT = "50123456-01"
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
    fake: FakeKis, settings: Settings, now: Callable[[], datetime] = lambda: NOW
) -> AsyncIterator[Broker]:
    async with kis.rest_session() as session:
        yield trader_broker(
            session, settings, now, servers=Servers(fake.rest_url, fake.ws_url), per_second=1000, pause=0.01
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


async def test_the_trader_s_calls_are_paced_two_a_second(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[RateLimiter] = []

    def build(*args: Any, **kwargs: Any) -> RateLimiter:
        built.append(RateLimiter(*args, **kwargs))
        return built[-1]

    monkeypatch.setattr(kis_orders, "RateLimiter", build)
    settings = Settings(
        kis_app_key=COLLECTOR_KEY,
        kis_app_secret="collector-secret",
        state_dir=tmp_path,
        trader_kis_app_key=TRADER_KEY,
        trader_kis_app_secret=TRADER_SECRET,
        trader_kis_account=ACCOUNT,
    )
    async with kis.rest_session() as session:
        trader_broker(session, settings, lambda: NOW)  # the mock servers, the default pace
    [limiter] = built
    clock = Monotonic()
    monkeypatch.setattr(ratelimit, "time", SimpleNamespace(monotonic=clock))
    monkeypatch.setattr(ratelimit, "asyncio", SimpleNamespace(sleep=clock.sleep))
    # Of five sent at once the mock server answered three (#403): two a second, over a 1.1 s window.
    assert [await limiter.acquire() for _ in range(5)] == pytest.approx([0.0, 0.0, 1.1, 1.1, 2.2])


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
