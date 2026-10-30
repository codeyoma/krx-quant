"""A mock start's reconcile of the KIS account with the ledger, and the restart's rules (#406, #393
"KIS 주문").

The trader is the mock book of ``tests/test_trader_mock.py`` (``MockDay``) on the fake KIS
(``fake_kis.FakeAccount``): each ``MockDay`` run is one start of the trader, the account left as it
was between them (the trader is down: KIS's orders rest, the fake's are day orders). The example is
that file's: a 10:00 barrier prediction buys 14 at the 70,100 ask (filled at 70,050), its target
70,250 (the take-profit at 70,300), its stop 69,850, its time barrier 10:04. Every value is made up.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, cast

import pytest

from krx_quant import kis_orders
from krx_quant.fake_kis import BUY_SIDE, SELL_SIDE, FakeAccount, FakeKis, FakeOrder
from krx_quant.feeds.base import KST
from krx_quant.kis_orders import Balance, Holding, OrderRow, Servers
from krx_quant.predictions import PredictionStore
from krx_quant.trader import MOCK, SYMBOL
from krx_quant.trader.cells import EngineReader
from krx_quant.trader.limits import ENTRIES_PAUSED, ORDER_FAILED, RUNNING, ask
from krx_quant.trader.mock import RECONCILING, MockTrader
from krx_quant.trader.mock_check import PROBE_JOB, SAME, check_mock_trades
from krx_quant.trader.paper import STOP_LOSS, TAKE_PROFIT, TIME
from krx_quant.trader.reconcile import Outside, candidates, compare
from krx_quant.trader.store import DB_NAME, RESUME, Placed, TraderStore
from tests.test_trader_mock import (
    BOUGHT,
    TEN,
    TIMING,
    Event,
    Fill,
    MockDay,
    kis_mock,
    open_row,
    orders_of,
    rows,
    settings_of,
)
from tests.trader_doubles import Barrier, Read, Tick, at, quote

__all__ = ["kis_mock"]  # the fixture, used here

NEXT = "2026-10-15"  # the next trading day (a Thursday)


def next_day(hms: str) -> str:
    return f"{NEXT} {hms}"


@dataclass(frozen=True)
class Resume:
    """``krx-quant trade resume`` on the server at ``hms``, read by the trader then."""

    hms: str

    @property
    def time(self) -> datetime:
        return at(self.hms)


@dataclass
class Day(MockDay):
    """``MockDay`` with the server's resume (``Resume``) among its events."""

    async def _on(
        self, event: Any, book: MockTrader, engine: PredictionStore, reader: EngineReader, store: TraderStore
    ) -> None:
        if isinstance(event, Resume):
            assert not isinstance(ask(store, RESUME, MOCK, event.time), str)
            book.on_controls(event.time)
            return
        await super()._on(event, book, engine, reader, store)


def resume(state: Path, hms: str) -> None:
    """``krx-quant trade resume`` while the trader is down: it reads it at its next start."""
    store = TraderStore(state / DB_NAME)
    try:
        assert not isinstance(ask(store, RESUME, MOCK, at(hms)), str)
    finally:
        store.close()


def reconciles(state: Path) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    return [
        (r["status"], json.loads(r["outside"]), json.loads(r["detail"])) for r in rows(state, "reconciles")
    ]


def cancels(fake: FakeKis) -> list[str]:
    """The order numbers the trader asked KIS to cancel, in order."""
    return [str(b["ORGN_ODNO"]) for _, tr, _, b in fake.posts_received if tr == kis_orders.CANCEL.mock]


def take_profit(account: FakeAccount) -> FakeOrder:
    """The take-profit sell resting on KIS."""
    [tp] = account.resting(SELL_SIDE)
    return tp


# A trade open when the trader stopped


async def test_a_trade_past_its_time_barrier_is_sold_at_the_first_quote_after_the_restart(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """The trader stops with the trade open, its take-profit resting on KIS, and starts again at
    10:05, past the 10:04 time barrier: it sells at once, at the first quote's best bid (the
    take-profit taken off first), the exit's reason ``time``."""
    fake, account = kis_mock
    [held] = await MockDay(tmp_path, fake, account).run([TEN, BOUGHT])
    assert held["exit"] is None

    [trade] = await MockDay(tmp_path, fake, account, started_at=at("10:05:00")).run(
        [quote("10:05:01", 70_000, 70_100)]
    )

    assert trade["exit"]["reason"] == TIME and account.holdings[SYMBOL] == 0
    assert orders_of(trade) == [
        ("buy", "entry", 70_100, 14, "filled", 14),
        ("sell", "take_profit", 70_300, 14, "cancelled", 0),
        ("sell", "time", 70_000, 14, "filled", 14),
    ]
    assert rows(tmp_path, "trades")[0]["selling"] == TIME, "the sale begun at the start"
    assert [r[0] for r in reconciles(tmp_path)] == ["clean", "clean"]


async def test_a_trade_left_open_overnight_is_sold_the_next_day_without_cancelling_yesterday_s_orders(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """The trader is down from 10:01 to the next morning. Its take-profit was a day order: KIS ended
    it at the close, so the start closes it in the ledger without a cancel (KIS refuses one of a day
    gone by), and the trade is sold at the next day's first quote."""
    fake, account = kis_mock
    await MockDay(tmp_path, fake, account).run([TEN, BOUGHT])

    later = MockDay(tmp_path, fake, account, started_at=at(next_day("09:00:00")))
    [trade] = await later.run([quote(next_day("09:00:30"), 70_000, 70_100)])

    assert trade["exit"]["reason"] == TIME and account.holdings[SYMBOL] == 0
    assert [o[1:5] for o in orders_of(trade)] == [
        ("entry", 70_100, 14, "filled"),
        ("take_profit", 70_300, 14, "cancelled"),  # over with its day
        ("time", 70_000, 14, "filled"),
    ]
    assert cancels(fake) == [], "nothing cancelled: yesterday's order is over, today's sold whole"
    assert later.status == RUNNING and later.notices == []


async def test_a_trade_with_time_left_keeps_its_take_profit_and_its_stop_goes_on(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """Started again at 10:01 with the take-profit resting on KIS: the same order is followed (none
    placed again), and the stop sells when the bid reaches it."""
    fake, account = kis_mock
    await MockDay(tmp_path, fake, account).run([TEN, BOUGHT])

    [trade] = await MockDay(tmp_path, fake, account, started_at=at("10:01:00")).run(
        [Tick("10:01:00.5"), quote("10:01:30", 69_800, 69_900)]
    )

    assert trade["exit"]["reason"] == STOP_LOSS and account.holdings[SYMBOL] == 0
    assert [o[1:5] for o in orders_of(trade)] == [
        ("entry", 70_100, 14, "filled"),
        ("take_profit", 70_300, 14, "cancelled"),
        ("stop_loss", 69_800, 14, "filled"),
    ]
    assert len(account.placed(SELL_SIDE)) == 2, "the take-profit and the stop's sell"


async def test_a_take_profit_that_vanished_from_kis_is_placed_again_for_the_shares_left(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """While the trader was down, 4 of the take-profit's 14 filled and the rest was cancelled on KIS
    (not by the trader). Started again with time left, it keeps the 4 fills, places the take-profit
    again for the 10 left, and exits ``take_profit`` once those fill."""
    fake, account = kis_mock
    await MockDay(tmp_path, fake, account).run([TEN, BOUGHT])
    gone = take_profit(account)
    account.bid, account.improvement = gone.price, 0.0
    account.fill(gone, 4)
    account.bid, account.improvement = 70_000, 0.5  # the bid back below the target
    account._cancel({"ORGN_ODNO": gone.number})

    restarted = MockDay(tmp_path, fake, account, started_at=at("10:01:00"))
    [trade] = await restarted.run([Tick("10:01:00.5"), Fill("10:02:00")])

    assert trade["exit"]["reason"] == TAKE_PROFIT and account.holdings[SYMBOL] == 0
    assert orders_of(trade) == [
        ("buy", "entry", 70_100, 14, "filled", 14),
        ("sell", "take_profit", 70_300, 14, "cancelled", 4),
        ("sell", "take_profit", 70_300, 10, "filled", 10),
    ]
    assert [r[0] for r in reconciles(tmp_path)] == ["clean", "clean"]


async def test_restarts_never_send_an_order_twice(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """Started again and again: the resting take-profit is followed, never placed again; once KIS
    cancelled it, it is placed again once, and the restarts after follow that one."""
    fake, account = kis_mock
    await MockDay(tmp_path, fake, account).run([TEN, BOUGHT])
    for hms in ("10:00:30", "10:00:40"):
        await MockDay(tmp_path, fake, account, started_at=at(hms)).run([Tick(f"{hms}.5")])
    assert (len(account.placed(BUY_SIDE)), len(account.placed(SELL_SIDE))) == (1, 1)

    account._cancel({"ORGN_ODNO": take_profit(account).number})
    for hms in ("10:01:00", "10:01:10", "10:01:20"):
        await MockDay(tmp_path, fake, account, started_at=at(hms)).run([Tick(f"{hms}.5")])

    assert (len(account.placed(BUY_SIDE)), len(account.placed(SELL_SIDE))) == (1, 2)
    [trade] = rows(tmp_path, "trades")
    statuses = [r["status"] for r in rows(tmp_path, "orders")]
    assert statuses == ["filled", "cancelled", "open"] and trade["exit_at"] is None
    assert len(rows(tmp_path, "decisions")) == 1, "the prediction decided once"


# Outside the ledger


async def test_holdings_not_in_the_ledger_are_left_alone_and_pause_entries_until_resumed(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """The account holds 3 shares the ledger does not (the order probe's, say). The trader touches
    none of them, pauses new entries and says so; started again it says nothing more. The server's
    resume accepts the 3 as the account's: the trader buys again, and later starts find the account
    as the ledger has it."""
    fake, account = kis_mock
    account.holdings[SYMBOL], account.paid[SYMBOL] = 3, 3 * 60_000.0
    day = MockDay(tmp_path, fake, account)

    assert await day.run([TEN, BOUGHT]) == []

    assert day.status == ENTRIES_PAUSED and account.orders == [], "nothing sold, nothing bought"
    [notice] = day.notices
    assert "KIS 계좌가 장부와 다릅니다: 장부에 없는 보유 005930 3주" in notice
    assert PROBE_JOB in notice and "krx-quant trade resume" in notice
    assert [d["reason"] for d in day.decisions()] == [ENTRIES_PAUSED]
    [(status, outside, detail)] = reconciles(tmp_path)
    assert (status, outside["holdings"], detail["new"]["holdings"]) == ("held", {SYMBOL: 3}, {SYMBOL: 3})
    [pause] = rows(tmp_path, "controls")
    assert (pause["action"], pause["source"], pause["sell_now"]) == ("pause", "reconcile", 0)
    assert day.state_now is not None and day.state_now["pause"]["source"] == "reconcile"

    again = MockDay(tmp_path, fake, account, started_at=at("10:01:00"))
    await again.run([Tick("10:01:00.5")])
    assert again.status == ENTRIES_PAUSED and again.notices == [], "told once"

    resumed = Day(tmp_path, fake, account, started_at=at("10:02:00"))
    events = [
        Resume("10:02:30"),
        Read("10:05:02.3", [Barrier("10:05:00")]),
        quote("10:05:02.5", 70_000, 70_100),
    ]
    trades = await resumed.run(cast(Sequence[Event], events))  # ``Day`` reads its ``Resume`` too
    assert resumed.status == RUNNING and len(trades) == 1
    assert account.holdings[SYMBOL] == 3 + 14, "the 3 left alone"
    assert rows(tmp_path, "reconciles")[-1]["resumed_at"] is not None

    restarted = MockDay(tmp_path, fake, account, started_at=at("10:06:00"))
    await restarted.run([Tick("10:06:00.5")])
    assert restarted.status == RUNNING and restarted.notices == []
    assert reconciles(tmp_path)[-1][:2] == ("clean", {"holdings": {SYMBOL: 3}, "orders": []})


async def test_more_shares_than_accepted_pause_again(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    account.holdings["000660"] = 2  # another symbol: the trader trades 005930 alone
    first = MockDay(tmp_path, fake, account)
    await first.run([Tick("09:00:00.5")])
    assert "장부에 없는 보유 000660 2주" in first.notices[0] and PROBE_JOB not in first.notices[0]
    resume(tmp_path, "09:01:00")

    account.holdings["000660"] = 5
    later = MockDay(tmp_path, fake, account, started_at=at("09:02:00"))
    await later.run([Tick("09:02:00.5")])

    assert later.status == ENTRIES_PAUSED
    assert "정지를 풀었습니다" in later.notices[0], "the resume read at the start"
    assert "장부에 없는 보유 000660 3주" in later.notices[-1], "the 3 beyond the 2 accepted"


async def test_an_open_order_not_in_the_ledger_is_left_alone_and_pauses_entries(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """An order resting on KIS that the ledger does not hold: never cancelled, new entries paused."""
    fake, account = kis_mock
    account._order({"PDNO": SYMBOL, "ORD_QTY": "2", "ORD_UNPR": "60000"})  # placed on KIS alone, resting
    [other] = account.orders
    day = MockDay(tmp_path, fake, account)

    assert await day.run([TEN, BOUGHT]) == []

    assert day.status == ENTRIES_PAUSED and cancels(fake) == [] and other.left == 2
    [notice] = day.notices
    assert "장부에 없는 미체결 주문 1건" in notice and PROBE_JOB not in notice
    assert len(account.orders) == 1, "nothing placed"


async def test_an_open_order_the_order_probe_placed_is_not_the_trader_s_nor_outside_the_ledger(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    account._order({"PDNO": SYMBOL, "ORD_QTY": "1", "ORD_UNPR": "60000"})
    account.orders[-1].at = "093010"
    probe = {
        "day": "2026-10-14",
        "outcome": "failed",
        "started_at": "2026-10-14T09:30:00+09:00",
        "seconds": 60.0,
    }
    (tmp_path / "scheduled.json").write_text(json.dumps({"jobs": {PROBE_JOB: probe}}))
    day = MockDay(tmp_path, fake, account)

    [trade] = await day.run([TEN, BOUGHT])

    assert day.status == RUNNING and trade["entry"]["quantity"] == 14
    [(status, outside, detail)] = reconciles(tmp_path)
    assert (status, outside["orders"], detail["probe"]) == ("clean", [], 1)


async def test_shares_of_the_ledger_missing_from_the_account_pause_entries(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """The ledger's trade holds 14 the account no longer does (sold outside the trader): it is not
    told apart from a ledger gone wrong, so new entries pause, and no take-profit is placed again."""
    fake, account = kis_mock
    await MockDay(tmp_path, fake, account).run([TEN, BOUGHT])
    account._cancel({"ORGN_ODNO": take_profit(account).number})
    account.holdings[SYMBOL] = 0

    later = MockDay(tmp_path, fake, account, started_at=at("10:01:00"))
    await later.run([Tick("10:01:00.5")])

    assert later.status == ENTRIES_PAUSED
    [notice] = later.notices
    assert "장부의 포지션 가운데 14주가 계좌에 없습니다" in notice
    assert len(account.placed(SELL_SIDE)) == 1, "no take-profit placed again"


# A buy whose answer was lost


def inquiries_fail_after_the_buy(fake: FakeKis) -> list[bool]:
    """From the trader's buy on, KIS answers no order inquiry (made up), until the test sets the flag
    back."""
    read, buy = fake.rest_responses[kis_orders.FILLS.mock], fake.rest_posts[kis_orders.BUY.mock]
    failing = [False]

    def inquiry(params: dict[str, str]) -> Any:
        if failing[0]:
            return {"rt_cd": "1", "msg_cd": "FK0010", "msg1": "made up: 조회하지 못했습니다"}
        return read(params)

    def bought(body: dict[str, Any]) -> Any:
        failing[0] = True
        return buy(body)

    fake.rest_responses[kis_orders.FILLS.mock] = inquiry
    fake.rest_posts[kis_orders.BUY.mock] = bought
    return failing


async def test_a_buy_whose_answer_was_lost_and_not_found_then_is_adopted_at_the_next_start(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """The buy's answer is lost and KIS answers no inquiry while the trader looks for it: it stays
    sent. The next start finds it and opens its trade, its take-profit resting."""
    fake, account = kis_mock
    account.lose_answers = 1
    failing = inquiries_fail_after_the_buy(fake)
    assert await MockDay(tmp_path, fake, account).run([TEN, BOUGHT]) == []
    [kept] = rows(tmp_path, "unfilled_buys")
    assert (kept["status"], kept["broker_id"]) == ("sent", "")
    failing[0] = False

    later = MockDay(tmp_path, fake, account, started_at=at("10:01:00"))
    [trade] = await later.run([Tick("10:01:00.5")])

    assert trade["entry"]["quantity"] == 14 and trade["target"] == 70_250
    assert [o[1:5] for o in orders_of(trade)] == [
        ("entry", 70_100, 14, "filled"),
        ("take_profit", 70_300, 14, "open"),
    ]
    assert [r["status"] for r in rows(tmp_path, "unfilled_buys")] == ["filled"]
    assert [d["trade_id"] for d in rows(tmp_path, "decisions")] == [trade["id"]]
    assert len(account.placed(BUY_SIDE)) == 1 and later.status == ORDER_FAILED, "the day's failure holds"
    assert [r[0] for r in reconciles(tmp_path)] == ["clean", "clean"]


async def test_a_buy_whose_answer_was_lost_and_that_kis_never_took_is_not_on_kis(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock

    def lost(body: dict[str, Any]) -> Any:
        raise ConnectionResetError("lost before KIS took it")

    fake.rest_posts[kis_orders.BUY.mock] = lost
    assert await MockDay(tmp_path, fake, account).run([TEN, BOUGHT]) == []
    assert [r["status"] for r in rows(tmp_path, "unfilled_buys")] == ["sent"], "not on KIS yet"

    later = MockDay(tmp_path, fake, account, started_at=at("10:01:00"))
    assert await later.run([Tick("10:01:00.5")]) == []

    assert [r["status"] for r in rows(tmp_path, "unfilled_buys")] == ["absent"]
    assert later.notices == [] and account.orders == []


async def test_a_buy_whose_answer_was_lost_that_two_kis_orders_could_be_pauses_entries(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """Another buy of the same limit and quantity went in on KIS at the same time: the trader cannot
    tell which is its own, adopts neither, and pauses new entries until the server has looked."""
    fake, account = kis_mock
    buy = fake.rest_posts[kis_orders.BUY.mock]

    def twice(body: dict[str, Any]) -> Any:
        buy(dict(body))  # someone else's, alike
        account.lose_answers = 1
        return buy(body)

    fake.rest_posts[kis_orders.BUY.mock] = twice
    day = MockDay(tmp_path, fake, account)

    assert await day.run([TEN, BOUGHT]) == []

    assert day.status == ENTRIES_PAUSED and len(account.placed(BUY_SIDE)) == 2
    assert "응답이 없던 매수 1건을 KIS의 주문 하나로 가리지 못했습니다" in day.notices[-1]
    assert [r["status"] for r in rows(tmp_path, "unfilled_buys")] == ["sent"]
    status, _, detail = reconciles(tmp_path)[-1]
    assert (status, detail["doubtful"]) == ("held", 1)


# The day an order was placed on


async def test_a_buy_whose_fills_are_read_the_next_day_stays_an_order_of_its_own_day(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """KIS takes the buy and fills it, but the trader stops before it reads the fills (KIS answers
    none of its inquiries), and starts again the next trading day. The fills open the trade then, but
    the buy stays an order of the day it was placed: each day's check against KIS's inquiry finds
    the ledger's orders as KIS has them."""
    fake, account = kis_mock
    failing = inquiries_fail_after_the_buy(fake)
    held = open_row(tmp_path, "SELECT 1 FROM unfilled_buys WHERE status = 'open'")
    long = replace(TIMING, wait=30.0)
    assert await MockDay(tmp_path, fake, account, timing=long, stop_when=held).run([TEN, BOUGHT]) == []
    failing[0] = False

    later = MockDay(tmp_path, fake, account, started_at=at(next_day("09:00:00")))
    [trade] = await later.run([quote(next_day("09:00:30"), 70_000, 70_100)])

    assert trade["exit"]["reason"] == TIME and account.holdings[SYMBOL] == 0
    entry, sold = trade["orders"]
    assert entry["placed_at"] == "2026-10-14T10:00:02.500000+09:00", "when it was sent"
    assert entry["fills"][0]["at"].startswith(NEXT), "its fills read the next day"
    assert sold["placed_at"].startswith(NEXT)
    servers = Servers(fake.rest_url, fake.ws_url)
    evening = datetime(2026, 10, 15, 23, 0, tzinfo=KST)
    for day in (date(2026, 10, 14), date(2026, 10, 15)):
        assert await check_mock_trades(settings_of(fake, tmp_path), evening, day, servers=servers) == SAME, (
            day
        )


async def test_a_buy_left_open_on_kis_overnight_is_over_with_its_day(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """The buy rests unfilled and KIS refuses every cancel of it; the trader stops. The next day the
    start reads it on its own day's inquiry: over with its day, nothing filled, no cancel sent. The
    trader buys again that day."""
    fake, account = kis_mock
    account.fills = False
    refused = {"rt_cd": "1", "msg_cd": "FK0009", "msg1": "made up: 취소를 받지 못했습니다"}
    fake.rest_posts[kis_orders.CANCEL.mock] = lambda body: refused
    await MockDay(tmp_path, fake, account, timing=replace(TIMING, retry=3600.0)).run([TEN, BOUGHT])
    asked = len(cancels(fake))
    account.fills = True

    later = MockDay(tmp_path, fake, account, started_at=at(next_day("09:00:00")))
    [trade] = await later.run(
        [
            Read(next_day("10:00:02.3"), [Barrier(next_day("10:00:00"))]),
            quote(next_day("10:00:02.5"), 70_000, 70_100),
        ]
    )

    assert len(cancels(fake)) == asked, "no cancel of yesterday's order"
    assert [r["status"] for r in rows(tmp_path, "unfilled_buys")] == ["cancelled", "filled"]
    first, second = rows(tmp_path, "decisions")
    assert (first["reason"], second["trade_id"]) == ("unfilled", trade["id"])
    assert later.status == RUNNING and trade["entry"]["quantity"] == 14


# The start itself


async def test_nothing_is_bought_or_sold_until_the_account_could_be_read(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """KIS answers the balance only from the third ask (the start's, then one a minute at most): the
    trader says it could not read the account, skips the prediction read while it reads it again
    (``reconciling``), and says so once it has reconciled."""
    fake, account = kis_mock
    balance = fake.rest_responses[kis_orders.BALANCE.mock]
    asked = [0]

    def later(params: dict[str, str]) -> Any:
        asked[0] += 1
        if asked[0] < 3:
            return {"rt_cd": "1", "msg_cd": "FK0011", "msg1": "made up: 잔고를 읽지 못했습니다"}
        return balance(params)

    fake.rest_responses[kis_orders.BALANCE.mock] = later
    day = MockDay(tmp_path, fake, account, timing=replace(TIMING, retry=60.0))
    events: Sequence[Event] = [Tick("09:30:00"), TEN, BOUGHT]

    assert await day.run(events) == []

    assert [d["reason"] for d in day.decisions()] == [RECONCILING]
    unreadable, done = day.notices
    assert (
        "시작할 때 KIS 계좌를 읽지 못했습니다" in unreadable and "KIS 계좌를 읽어 장부와 맞췄습니다" in done
    )
    assert day.status == RUNNING and day.failures() == [] and account.orders == []


# The comparison


def balance(held: dict[str, int] | None = None) -> Balance:
    holdings = {s: Holding(s, q, q, 70_000.0) for s, q in (held or {}).items()}
    return Balance(holdings, 10_000_000, 10_000_000, 10_000_000, 0)


def row(
    number: str, left: int = 0, *, side: str = kis_orders.SIDE_BUY, at: str = "100002", filled: int = 14
) -> OrderRow:
    return OrderRow(number, "", side, SYMBOL, 14, 70_100, filled, left, 0, 70_050.0, False, at)


def test_the_shares_held_are_compared_within_what_the_ledger_s_open_orders_may_still_fill() -> None:
    nothing = Outside()
    # 14 held, the take-profit's 14 open: anything from 0 to 14 is the ledger's.
    for shares in (0, 14):
        assert compare(balance({SYMBOL: shares}), [], [], (0, 14), nothing, None).clean
    found = compare(balance({SYMBOL: 17}), [], [], (0, 14), nothing, None)
    assert found.new.holdings == {SYMBOL: 3} and not found.clean
    short = compare(balance({SYMBOL: 10}), [], [], (14, 14), nothing, None)
    assert short.missing == 4 and not short.clean
    assert compare(balance({SYMBOL: 3}), [], [], (0, 0), Outside({SYMBOL: 3}), None).clean, "accepted"


def test_the_open_orders_outside_the_ledger_are_the_day_s_with_something_left() -> None:
    today = [
        row("0000031401"),
        row("0000031402", left=3),
        row("0000031403", left=2, side=kis_orders.SIDE_SELL),
    ]
    found = compare(balance(), today, ["31402"], (0, 0), Outside(), None)
    assert found.new.orders == ("0000031403",), "the filled one is over, the followed one the trader's"
    probe = (time(10, 0), time(10, 1))
    assert compare(balance(), today, ["31402"], (0, 0), Outside(), probe).clean
    accepted = Outside({}, ("31403",))
    assert compare(balance(), today, ["31402"], (0, 0), accepted, None).clean


def test_a_lost_buy_is_a_kis_buy_of_its_limit_and_quantity_placed_when_it_was_sent() -> None:
    sent = Placed(70_100, 14, at=at("10:00:02.5"))
    day = date(2026, 10, 14)
    ok = row("0000031401")
    assert candidates([ok], sent, day, [], None) == [ok]
    assert candidates([ok], sent, day, ["31401"], None) == [], "the ledger's own"
    assert candidates([replace(ok, price=70_000)], sent, day, [], None) == []
    assert candidates([replace(ok, quantity=13)], sent, day, [], None) == []
    assert candidates([replace(ok, side=kis_orders.SIDE_SELL)], sent, day, [], None) == []
    assert candidates([replace(ok, at="095000")], sent, day, [], None) == [], "long before"
    assert candidates([replace(ok, at="100400")], sent, day, [], None) == [], "long after"
    assert candidates([ok], sent, day, [], (time(10, 0), time(10, 1))) == [], "the probe's"
    assert candidates([replace(ok, cancel=True)], sent, day, [], None) == []


@pytest.mark.parametrize(
    "held", [{"holdings": {}, "orders": []}, {"holdings": {SYMBOL: 1}, "orders": ["31401"]}]
)
def test_what_is_outside_the_ledger_is_kept_as_json(held: dict[str, Any]) -> None:
    assert Outside.of(held).as_json() == held
