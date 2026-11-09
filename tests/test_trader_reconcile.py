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
import sqlite3
from collections.abc import Callable, Sequence
from contextlib import closing
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
from krx_quant.trader import LIVE, MOCK, SYMBOL, live
from krx_quant.trader.book import STOP_LOSS, TAKE_PROFIT, TIME
from krx_quant.trader.cells import EngineReader
from krx_quant.trader.limits import ENTRIES_PAUSED, ORDER_FAILED, RUNNING, ask
from krx_quant.trader.mock import OUTSIDE_HELD, RECONCILING, MockTrader
from krx_quant.trader.mock_check import PROBE_JOB, SAME, check_mock_trades
from krx_quant.trader.reconcile import Outside, candidates, compare
from krx_quant.trader.store import CLEAN, DB_NAME, RESUME, Placed, TraderStore, read_trades
from tests.api_schema import check
from tests.kis_doubles import (
    BOUGHT,
    TEN,
    TIMING,
    Depth,
    Event,
    Fill,
    MockDay,
    kis_mock,
    orders_of,
    rows,
    settings_of,
)
from tests.test_trader_mock import open_row
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


async def test_a_take_profit_filled_while_the_trader_was_down_exits_take_profit_though_a_stop_came_first(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """The take-profit filled whole while the trader was down. Started again, it cannot read KIS's
    inquiry at first, and a quote at the stop comes meanwhile (the stop's sale begins, nothing sold).
    Once the account is read, the take-profit's fills hold every share: the trade exits
    ``take_profit``, and nothing more is sold."""
    fake, account = kis_mock
    await MockDay(tmp_path, fake, account).run([TEN, BOUGHT])
    tp = take_profit(account)
    account.bid, account.improvement = tp.price, 0.0
    account.fill(tp)
    read = fake.rest_responses[kis_orders.FILLS.mock]
    asked = [0]

    def later(params: dict[str, str]) -> Any:
        asked[0] += 1
        if asked[0] < 2:
            return {"rt_cd": "1", "msg_cd": "FK0010", "msg1": "made up: 조회하지 못했습니다"}
        return read(params)

    fake.rest_responses[kis_orders.FILLS.mock] = later
    restarted = MockDay(
        tmp_path, fake, account, started_at=at("10:01:00"), timing=replace(TIMING, retry=60.0)
    )
    [trade] = await restarted.run([quote("10:01:10", 69_800, 69_900), Tick("10:02:10")])

    assert trade["exit"]["reason"] == TAKE_PROFIT and account.holdings[SYMBOL] == 0
    assert [o[1:] for o in orders_of(trade)][1:] == [("take_profit", 70_300, 14, "filled", 14)]
    assert len(account.placed(SELL_SIDE)) == 1, "nothing sold at the stop"


# A sell whose answer was lost (#460)

# A look again comes a minute later: the clock is the test's, so a later event reaches it.
LATER = replace(TIMING, retry=60.0)


@dataclass(frozen=True)
class Do:
    """At ``hms``, ``act`` on KIS's side (made up), before the trader's clock reaches the next event."""

    hms: str
    act: Callable[[], None]

    @property
    def time(self) -> datetime:
        return at(self.hms)


@dataclass
class Acting(Day):
    """``Day`` with KIS's side acting among its events (``Do``)."""

    async def _on(
        self, event: Any, book: MockTrader, engine: PredictionStore, reader: EngineReader, store: TraderStore
    ) -> None:
        if isinstance(event, Do):
            event.act()
            return
        await super()._on(event, book, engine, reader, store)


@dataclass
class Lost:
    """The trader's sell at ``price`` has no answer: KIS takes it first (``taken``), or the connection
    drops before it does. With ``alike``, another sell of the same limit and quantity goes in on KIS
    at the same moment (someone else's). With ``unread``, KIS answers no order inquiry from then on
    until ``answer``; with ``hidden``, KIS's inquiry shows no sell at ``price`` (it lags behind the
    order it took, #466). Every value is made up."""

    fake: FakeKis
    account: FakeAccount
    price: int
    taken: bool = True
    alike: bool = False
    unread: bool = False
    hidden: bool = False
    failing: bool = False  # KIS answers no order inquiry now
    sent: bool = False  # the sell sent

    def __post_init__(self) -> None:
        read, sell = (
            self.fake.rest_responses[kis_orders.FILLS.mock],
            self.fake.rest_posts[kis_orders.SELL.mock],
        )

        def inquiry(params: dict[str, str]) -> Any:
            if self.failing:
                return {"rt_cd": "1", "msg_cd": "FK0010", "msg1": "made up: 조회하지 못했습니다"}
            answer = read(params)
            if not self.hidden:
                return answer
            body, headers = answer if isinstance(answer, tuple) else (answer, {})
            shown = [
                r
                for r in body["output1"]
                if not (r["sll_buy_dvsn_cd"] == SELL_SIDE and int(r["ord_unpr"]) == self.price)
            ]
            return {**body, "output1": shown}, headers

        def selling(body: dict[str, Any]) -> Any:
            if int(body["ORD_UNPR"]) != self.price or self.sent:
                return sell(body)
            self.sent = True
            self.failing = self.unread
            if self.alike:
                sell(dict(body))
            if not self.taken:
                raise ConnectionResetError("lost before KIS took it")
            self.account.lose_answers = 1
            return sell(body)

        self.fake.rest_responses[kis_orders.FILLS.mock] = inquiry
        self.fake.rest_posts[kis_orders.SELL.mock] = selling

    def answer(self) -> None:
        self.failing = False


def sells(fake: FakeKis) -> list[int]:
    """The limit of each sell the trader sent KIS, in order (refused or not, its answer had or not)."""
    return [int(b["ORD_UNPR"]) for _, tr, _, b in fake.posts_received if tr == kis_orders.SELL.mock]


def accept(state: Path, holdings: dict[str, int]) -> None:
    """``holdings`` outside the ledger accepted as the account's, as a resume or a clean start keeps
    them (``store.accepted_outside``)."""
    store = TraderStore(state / DB_NAME)
    try:
        store.add_reconcile(at("08:00:00"), MOCK, CLEAN, {"holdings": holdings, "orders": []}, {})
    finally:
        store.close()


async def test_a_time_sell_whose_answer_was_lost_is_never_sent_again_and_is_found_on_kis(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """The time barrier's sell goes in on KIS and fills, but its answer is lost and KIS answers no
    inquiry for a while. The trader sends no other sell: a minute later it looks again, finds the one
    KIS order it can be, keeps its fills, and the trade exits ``time``."""
    fake, account = kis_mock
    lost = Lost(fake, account, 70_000, unread=True)
    day = Acting(tmp_path, fake, account, timing=LATER)
    events = [
        TEN,
        BOUGHT,
        quote("10:04:00.2", 70_000, 70_100),
        Tick("10:05:01"),  # looked up again: KIS answers no inquiry yet
        Do("10:05:30", lost.answer),
        Tick("10:06:02"),
    ]

    [trade] = await day.run(cast(Sequence[Event], events))

    assert sells(fake) == [70_300, 70_000], "the take-profit and the lost sell: none sent again"
    assert trade["exit"]["reason"] == TIME and account.holdings[SYMBOL] == 0
    assert orders_of(trade) == [
        ("buy", "entry", 70_100, 14, "filled", 14),
        ("sell", "take_profit", 70_300, 14, "cancelled", 0),
        ("sell", "time", 70_000, 14, "filled", 14),
    ]
    assert trade["orders"][2]["broker_id"] == account.placed(SELL_SIDE)[1].number
    lost_notice, found = day.notices
    assert "매도 주문(14주)에 응답이 없어 KIS에 들어갔는지 모릅니다" in lost_notice
    assert "다시 보내지 않고" in lost_notice and "KIS 계좌에서 확인하세요" in lost_notice
    assert (
        "응답이 없던 매도 주문을 KIS 주문 체결 조회에서 찾아 장부에 넣었습니다(14주 가운데 14주 체결)"
        in found
    )
    assert [(f["what"], f["outcome"]) for f in day.failures()] == [("sell", "unknown")]
    assert day.status == ORDER_FAILED, "no new entry that day"


@pytest.mark.parametrize("seen", ["none", "two"])
async def test_a_lost_sell_kis_shows_none_or_two_of_stays_sent_and_a_restart_sends_none(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path, seen: str
) -> None:
    """The stop's sell has no answer. KIS never took it (``none``), or took it with another of the
    same limit and quantity (``two``: the account held 14 more, accepted outside the ledger). The
    trader cannot tell, so the sell stays as sent: looked up again and again, no other sell sent,
    the stop and the time barrier selling nothing; a restart neither. (``none`` is concluded absent
    once a second look finds none 30 s after the first one past two minutes, #464, below: here the
    last look is that first.)"""
    fake, account = kis_mock
    if seen == "two":
        account.holdings[SYMBOL], account.paid[SYMBOL] = 14, 14 * 60_000.0
        accept(tmp_path, {SYMBOL: 14})
    Lost(fake, account, 69_800, taken=seen == "two", alike=seen == "two")
    day = MockDay(tmp_path, fake, account, timing=LATER)
    await day.run([TEN, BOUGHT, quote("10:01:30", 69_800, 69_900), Tick("10:02:31")])

    later = MockDay(tmp_path, fake, account, started_at=at("10:03:00"), timing=LATER)
    await later.run(
        [Tick("10:03:00.5"), quote("10:03:30", 69_700, 69_800), quote("10:05:00", 69_700, 69_800)]
    )

    assert sells(fake) == [70_300, 69_800], "never sent again"
    [trade] = read_trades(tmp_path / DB_NAME, None, 10)
    assert trade["exit"] is None and orders_of(trade) == [
        ("buy", "entry", 70_100, 14, "filled", 14),
        ("sell", "take_profit", 70_300, 14, "cancelled", 0),
        ("sell", "stop_loss", 69_800, 14, "sent", 0),
    ]
    assert trade["orders"][2]["broker_id"] is None
    check("trader_trade", trade)
    assert account.holdings[SYMBOL] == (14 if seen == "none" else 0)
    assert later.status == ORDER_FAILED and later.notices == [], "told once, by the first run"
    assert [r[0] for r in reconciles(tmp_path)][-1] == "clean", "its shares may be sold: not missing"
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db:
        assert live.exposure(db, MOCK) == 2, "the trade and its sell sent"


async def test_with_a_share_outside_the_ledger_accepted_a_lost_sell_that_filled_sells_it_not(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """The account holds a share outside the ledger, accepted; the trader buys one more. The stop's
    sell fills on KIS, its answer lost. A second sell would sell the account's share (KIS sells what
    the account holds): none is sent, and the one found later ends the trade."""
    fake, account = kis_mock
    account.cash = 100_000  # the orderable cash buys one share
    account.holdings[SYMBOL], account.paid[SYMBOL] = 1, 60_000.0
    accept(tmp_path, {SYMBOL: 1})
    lost = Lost(fake, account, 69_800, unread=True)
    day = Acting(tmp_path, fake, account, timing=LATER)
    events = [
        TEN,
        BOUGHT,
        quote("10:01:30", 69_800, 69_900),
        Tick("10:02:31"),
        Do("10:02:40", lost.answer),
        Tick("10:03:32"),
    ]

    [trade] = await day.run(cast(Sequence[Event], events))

    assert trade["entry"]["quantity"] == 1 and sells(fake) == [70_300, 69_800]
    assert account.holdings[SYMBOL] == 1, "the account's own share left alone"
    assert trade["exit"]["reason"] == STOP_LOSS
    assert orders_of(trade)[2:] == [("sell", "stop_loss", 69_800, 1, "filled", 1)]


@pytest.mark.parametrize("ends", ["filled", "next_day"])
async def test_a_lost_sell_found_with_shares_left_is_not_cancelled_and_waits_until_it_is_over(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path, ends: str
) -> None:
    """The stop's sell fills 5 of 14 on KIS, the rest resting, its answer lost. Found on the inquiry
    with shares left, it is not cancelled (a cancel names KIS's branch, which the inquiry's
    ``ord_gno_brno`` is not measured to be, #403) and no other sell goes: it stays as sent. Once it is
    over on KIS, it is the trade's: its 9 filled there (``filled``), or its day ended with 5 filled
    and the next day sells only the 9 left (``next_day``)."""
    fake, account = kis_mock
    Lost(fake, account, 69_800)
    day = Acting(tmp_path, fake, account, timing=LATER)
    events: list[Any] = [
        TEN,
        BOUGHT,
        Depth("10:01:00", 5),
        quote("10:01:30", 69_800, 69_900),
        Tick("10:02:31"),
    ]
    if ends == "filled":
        events += [Do("10:02:40", lambda: account.fill(account.placed(SELL_SIDE)[1])), Tick("10:03:32")]
    await day.run(cast(Sequence[Event], events))
    lost = account.placed(SELL_SIDE)[1]
    if ends == "next_day":
        assert lost.left == 9 and rows(tmp_path, "trades")[0]["exit_at"] is None
        account.depth = None
        later = MockDay(tmp_path, fake, account, started_at=at(next_day("09:00:00")), timing=LATER)
        await later.run([quote(next_day("09:00:30"), 69_900, 70_000)])

    [trade] = read_trades(tmp_path / DB_NAME, None, 10)
    assert trade["exit"]["reason"] == STOP_LOSS and account.holdings[SYMBOL] == 0
    assert cancels(fake) == [account.placed(SELL_SIDE)[0].number], "the take-profit's alone"
    stop = [o[2:] for o in orders_of(trade) if o[1] == "stop_loss"]
    if ends == "filled":
        assert stop == [(69_800, 14, "filled", 14)] and sells(fake) == [70_300, 69_800]
    else:
        assert stop == [(69_800, 14, "cancelled", 5), (69_900, 9, "filled", 9)], "over with its day"
        assert sells(fake) == [70_300, 69_800, 69_900]
    assert trade["orders"][2]["broker_id"] == lost.number


async def test_a_take_profit_whose_answer_was_lost_is_never_placed_again_and_the_stop_sells_nothing(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """The take-profit goes in on KIS, resting at the target, but its answer is lost. It rests on
    KIS, so it is not settled (and not cancelled), and the bid reaching the stop sells nothing: the
    trader says so. Once it fills on KIS, it is found, and the trade exits ``take_profit``."""
    fake, account = kis_mock
    Lost(fake, account, 70_300)
    day = Acting(tmp_path, fake, account, timing=LATER)

    def filled() -> None:  # the bid back up at the target: KIS fills it there
        account.bid, account.improvement = 70_300, 0.0
        account.fill(account.placed(SELL_SIDE)[0])

    events = [TEN, BOUGHT, quote("10:01:30", 69_800, 69_900), Do("10:01:40", filled), Tick("10:02:31")]

    [trade] = await day.run(cast(Sequence[Event], events))

    assert sells(fake) == [70_300] and cancels(fake) == [], "nothing sold at the stop, nothing cancelled"
    assert trade["exit"]["reason"] == TAKE_PROFIT and account.holdings[SYMBOL] == 0
    assert orders_of(trade)[1:] == [("sell", "take_profit", 70_300, 14, "filled", 14)]
    lost_notice, due, found = day.notices
    assert "익절 매도 주문(14주)에 응답이 없어" in lost_notice
    assert "팔 때가 됐지만(stop_loss)" in due and "KIS 계좌에서 확인하세요" in due
    assert (
        "응답이 없던 익절 매도 주문을 KIS 주문 체결 조회에서 찾아 장부에 넣었습니다(14주 가운데 14주 체결)"
        in found
    )
    assert [(f["what"], f["outcome"]) for f in day.failures()] == [("take_profit", "unknown")]


# A lost sell that never reached KIS did not go in (#464)

ABSENT_SAID = (
    "자동 매매({mode}): 응답이 없던 매도 주문(005930 14주)은 KIS에 들어가지 않은 것으로 정했습니다({why},"
    " 계좌에 이 매매의 005930 14주가 있습니다). 이 매매는 이 뒤에 받은 호가부터 원래 규칙(목표가, 손절가,"
    " 시간)대로 다시 팝니다."
)
MOCK_WHY = "보낸 지 2분이 지난 뒤 30초 이상 사이를 둔 두 번의 주문 체결 조회에 없고"
DAY_OVER_WHY = "그날이 지난 뒤 읽은 그날의 주문 체결 조회에 없고"
# A look again a second later: each event a second or more after the last looks again (the clock is
# the test's; with no wait at all a look would begin the next at once, on the same clock).
EACH_SECOND = replace(TIMING, retry=1.0)


def stop_sells(state: Path) -> list[tuple[float, str, str | None]]:
    """(limit, status, placed_at) of each stop sell kept, in order."""
    return [
        (o["price"], o["status"], o["placed_at"]) for o in rows(state, "orders") if o["purpose"] == STOP_LOSS
    ]


def absent_notices(notices: list[str]) -> list[str]:
    return [n for n in notices if "들어가지 않은 것으로" in n]


async def test_a_mock_sell_that_never_reached_kis_is_absent_after_two_empty_looks_and_sold_again(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """The stop's sell (10:01:30) is lost before KIS takes it: no inquiry will ever show it. On the
    mock account a look finding none from two minutes after it was sent (10:03:30) begins a run, and
    one finding none 30 s after that (10:04:00), the account holding the trade's 14, ends it: the
    sell did not go in. Its row stays, ``absent``; it is told; and the sale goes on at the next
    quote's bid (10:04:10), not at the bid it was lost at. Before then it stays as sent."""
    fake, account = kis_mock
    Lost(fake, account, 69_800, taken=False)
    seen: list[list[tuple[float, str, str | None]]] = []
    day = Acting(tmp_path, fake, account, timing=EACH_SECOND)
    events = [
        TEN,
        BOUGHT,
        quote("10:01:30", 69_800, 69_900),
        Tick("10:03:29"),  # under two minutes: no look counts
        Tick("10:03:30"),  # the run's first
        Tick("10:03:59"),  # 29 s after it
        Do("10:03:59.1", lambda: seen.append(stop_sells(tmp_path))),
        Tick("10:04:00"),  # 30 s after it: absent
        Do("10:04:05", lambda: seen.append(stop_sells(tmp_path))),
        quote("10:04:10", 69_700, 69_800),
    ]

    [trade] = await day.run(cast(Sequence[Event], events))

    assert seen == [
        [(69_800, "sent", "2026-10-14T10:01:30+09:00")],
        [(69_800, "absent", "2026-10-14T10:01:30+09:00")],
    ], "no sell at the old bid once it is absent: the next quote's"
    assert sells(fake) == [70_300, 69_800, 69_700]
    assert trade["exit"]["reason"] == STOP_LOSS and account.holdings[SYMBOL] == 0
    assert orders_of(trade) == [
        ("buy", "entry", 70_100, 14, "filled", 14),
        ("sell", "take_profit", 70_300, 14, "cancelled", 0),
        ("sell", "stop_loss", 69_800, 14, "absent", 0),
        ("sell", "stop_loss", 69_700, 14, "filled", 14),
    ]
    absent = trade["orders"][2]
    assert (absent["broker_id"], absent["closed_at"]) == (None, "2026-10-14T10:04:00+09:00")
    check("trader_trade", trade)
    assert absent_notices(day.notices) == [ABSENT_SAID.format(mode=MOCK, why=MOCK_WHY)]
    assert [(f["what"], f["outcome"]) for f in day.failures()] == [("sell", "unknown")]
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db:
        assert live.exposure(db, MOCK) == 0, "nothing left on KIS"


async def test_a_mock_take_profit_that_never_reached_kis_is_absent_and_rests_again_with_time_left(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """The take-profit (10:00:02.5) is lost before KIS takes it. Absent at 10:03:02.5, with the time
    barrier (10:04) still ahead: by the trade's rules its take-profit rests again for the 14, and
    the trade exits there."""
    fake, account = kis_mock
    Lost(fake, account, 70_300, taken=False)
    day = Acting(tmp_path, fake, account, timing=EACH_SECOND)

    [trade] = await day.run([TEN, BOUGHT, Tick("10:02:32.5"), Tick("10:03:02.5"), Fill("10:03:30")])

    assert sells(fake) == [70_300, 70_300]
    assert orders_of(trade)[1:] == [
        ("sell", "take_profit", 70_300, 14, "absent", 0),
        ("sell", "take_profit", 70_300, 14, "filled", 14),
    ]
    assert trade["exit"]["reason"] == TAKE_PROFIT and account.holdings[SYMBOL] == 0
    [said] = absent_notices(day.notices)
    assert said.startswith("자동 매매(mock): 응답이 없던 익절 매도 주문(005930 14주)은 KIS에 들어가지 않은")


@pytest.mark.parametrize("through", ["running", "restarted"])
async def test_a_live_sell_that_never_reached_kis_is_absent_once_its_day_is_over_and_sold_the_next_session(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path, through: str
) -> None:
    """The same lost stop sell on the live account stays as sent all its day, however many looks find
    none: the window of the candidates is no bound on how late KIS's inquiry may show an order. Once
    the day is over, the first look (the trader running through the night, or started again at
    06:00) finds none on that day's inquiry, the account holding the 14: absent. The trade is sold at
    the next session's first quote, not at the night's look on the day before's last bid."""
    fake, account = kis_mock
    Lost(fake, account, 69_800, taken=False)
    seen: list[list[tuple[float, str, str | None]]] = []

    def look() -> None:
        seen.append(stop_sells(tmp_path))

    events: list[Any] = [
        TEN,
        BOUGHT,
        quote("10:01:30", 69_800, 69_900),
        Tick("10:03:30"),
        Tick("10:04:00"),
        Tick("10:30:00"),
        Tick("15:40:00"),
        Do("15:40:01", look),
    ]
    rest = [Tick(next_day("08:00:00")), quote(next_day("09:00:30"), 69_900, 70_000)]
    if through == "running":
        events += [Tick(next_day("00:00:30")), Do(next_day("00:00:31"), look), *rest]
    first = Acting(tmp_path, fake, account, timing=EACH_SECOND, mode=LIVE)
    await first.run(cast(Sequence[Event], events))
    notices = first.notices
    if through == "restarted":
        later = Acting(
            tmp_path, fake, account, started_at=at(next_day("06:00:00")), timing=EACH_SECOND, mode=LIVE
        )
        await later.run(cast(Sequence[Event], [Do(next_day("06:00:01"), look), *rest]))
        notices += later.notices

    sent, absent = "2026-10-14T10:01:30+09:00", "2026-10-15T09:00:30+09:00"
    assert seen == [[(69_800, "sent", sent)], [(69_800, "absent", sent)]], "nothing sold in the night"
    assert stop_sells(tmp_path) == [(69_800, "absent", sent), (69_900, "filled", absent)]
    [trade] = read_trades(tmp_path / DB_NAME, None, 10)
    assert trade["exit"]["reason"] == STOP_LOSS and account.holdings[SYMBOL] == 0
    assert sells(fake) == [70_300, 69_800, 69_900]
    assert absent_notices(notices) == [ABSENT_SAID.format(mode=LIVE, why=DAY_OVER_WHY)]
    assert [(f["what"], f["outcome"], f["day"]) for f in rows(tmp_path, "failures")] == [
        ("sell", "unknown", "2026-10-14")
    ], "no sell refused in the night: the next day's entries go on"


@pytest.mark.parametrize("case", ["unread", "short", "two"])
async def test_a_lost_sell_is_not_absent_while_kis_is_not_read_the_shares_are_short_or_two_could_be_it(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path, case: str
) -> None:
    """The stop's sell has no answer, and the mock's looks go on past its two minutes and into the
    next day. KIS answers no inquiry from then on (``unread``); or the account holds 13 of the trade's
    14 (``short``: one sold outside the trader); or KIS took it with another of the same limit and
    quantity (``two``: the account held 14 more, accepted). None is concluded absent: the sell stays
    as sent, no other sell goes, nothing more is told."""
    fake, account = kis_mock
    if case == "two":
        account.holdings[SYMBOL], account.paid[SYMBOL] = 14, 14 * 60_000.0
        accept(tmp_path, {SYMBOL: 14})
    Lost(fake, account, 69_800, taken=case == "two", alike=case == "two", unread=case == "unread")

    def sold_one() -> None:
        account.holdings[SYMBOL] -= 1

    events: list[Any] = [TEN, BOUGHT, quote("10:01:30", 69_800, 69_900)]
    if case == "short":
        events.append(Do("10:02:00", sold_one))
    events += [
        Tick("10:03:30"),
        Tick("10:04:00"),
        Tick("10:05:00"),
        Tick(next_day("00:00:30")),
        Tick(next_day("00:01:00")),
    ]
    day = Acting(tmp_path, fake, account, timing=EACH_SECOND)

    [trade] = await day.run(cast(Sequence[Event], events))

    assert trade["exit"] is None
    assert orders_of(trade)[2:] == [("sell", "stop_loss", 69_800, 14, "sent", 0)]
    assert sells(fake) == [70_300, 69_800], "never sent again"
    assert absent_notices(day.notices) == []
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db:
        assert live.exposure(db, MOCK) == 2, "the trade and its sell sent"


async def test_with_a_share_outside_the_ledger_accepted_a_filled_lost_sell_the_inquiry_lags_is_not_absent(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """The account holds a share outside the ledger, accepted; the trader buys one more. The stop's
    sell fills on KIS, its answer lost, and KIS's inquiry shows it on none of the looks (it lags). Two
    looks 30 s apart past two minutes find none, and the balance holds one share: the one accepted
    outside the ledger, not the trade's (#466). The sell is not concluded absent, and no second sell
    goes (KIS would sell the account's share)."""
    fake, account = kis_mock
    account.cash = 100_000  # the orderable cash buys one share
    account.holdings[SYMBOL], account.paid[SYMBOL] = 1, 60_000.0
    accept(tmp_path, {SYMBOL: 1})
    Lost(fake, account, 69_800, hidden=True)
    day = MockDay(tmp_path, fake, account, timing=EACH_SECOND)
    events: list[Event] = [
        TEN,
        BOUGHT,
        quote("10:01:30", 69_800, 69_900),
        Tick("10:03:30"),  # the run's first
        Tick("10:04:00"),  # 30 s after it: none again, but the share held is the accepted one
        quote("10:04:10", 69_700, 69_800),
        Tick("10:05:00"),
    ]

    [trade] = await day.run(events)

    assert trade["entry"]["quantity"] == 1 and sells(fake) == [70_300, 69_800], "no second sell"
    assert account.holdings[SYMBOL] == 1, "the account's own share left alone"
    assert trade["exit"] is None and orders_of(trade)[2:] == [("sell", "stop_loss", 69_800, 1, "sent", 0)]
    assert absent_notices(day.notices) == []


async def test_an_absent_sell_stays_in_the_ledger_and_a_restart_neither_looks_for_it_nor_sends_it(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The mock's lost stop sell is absent at 10:04:00, and the trader stops before the next quote.
    Started again, the sell is no longer one whose answer was lost: not looked up, not told; its row
    stays as it was, and the sale goes on at the first quote after the start."""
    fake, account = kis_mock
    Lost(fake, account, 69_800, taken=False)
    first = MockDay(tmp_path, fake, account, timing=EACH_SECOND)
    await first.run([TEN, BOUGHT, quote("10:01:30", 69_800, 69_900), Tick("10:03:30"), Tick("10:04:00")])
    [absent] = [o for o in rows(tmp_path, "orders") if o["status"] == "absent"]

    caplog.clear()
    later = MockDay(tmp_path, fake, account, started_at=at("10:05:00"), timing=EACH_SECOND)
    [trade] = await later.run([Tick("10:05:00.5"), quote("10:05:10", 69_700, 69_800)])

    assert [o for o in rows(tmp_path, "orders") if o["id"] == absent["id"]] == [absent], "kept as it was"
    assert orders_of(trade)[2:] == [
        ("sell", "stop_loss", 69_800, 14, "absent", 0),
        ("sell", "stop_loss", 69_700, 14, "filled", 14),
    ]
    assert trade["exit"]["reason"] == STOP_LOSS and sells(fake) == [70_300, 69_800, 69_700]
    assert later.notices == [] and not [r for r in caplog.records if "answer was" in r.getMessage()]
    assert [r[0] for r in reconciles(tmp_path)][-1] == "clean"


@pytest.mark.parametrize("restarted", ["evening", "morning"])
async def test_a_restart_in_the_night_after_an_absent_sell_sells_nothing_until_krx_takes_orders(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path, restarted: str
) -> None:
    """The mock's lost stop sell is absent at 10:04:00, and the trader stops before the next quote.
    It starts again that evening (20:00) or the next morning (06:00): the stream's quotes of the
    night and of 08:00 sell nothing, KRX taking no order then (#466; a refused sell would stop that
    day's entries); the first quote from 08:30, of the opening auction, sells the 14."""
    fake, account = kis_mock
    Lost(fake, account, 69_800, taken=False)
    first = MockDay(tmp_path, fake, account, timing=EACH_SECOND)
    await first.run([TEN, BOUGHT, quote("10:01:30", 69_800, 69_900), Tick("10:03:30"), Tick("10:04:00")])
    assert [s[1] for s in stop_sells(tmp_path)] == ["absent"]

    start = "20:00:00" if restarted == "evening" else next_day("06:00:00")
    events: list[Any] = [quote(next_day("08:00:00"), 69_700, 69_800), Tick(next_day("08:29:59"))]
    if restarted == "evening":
        events = [quote("20:00:30", 69_700, 69_800), Tick("23:00:00"), *events]
    later = MockDay(tmp_path, fake, account, started_at=at(start), timing=EACH_SECOND)
    [trade] = await later.run([*events, quote(next_day("08:30:30"), 69_600, 69_700)])

    sent = "2026-10-14T10:01:30+09:00"
    assert stop_sells(tmp_path) == [
        (69_800, "absent", sent),
        (69_600, "filled", "2026-10-15T08:30:30+09:00"),
    ], "nothing sold before 08:30"
    assert trade["exit"]["reason"] == STOP_LOSS and account.holdings[SYMBOL] == 0
    assert [(f["what"], f["outcome"], f["day"]) for f in rows(tmp_path, "failures")] == [
        ("sell", "unknown", "2026-10-14")
    ], "no sell refused in the night: the next day's entries go on"


# The live account's own 005930 (#460)


@pytest.mark.parametrize("symbol", [SYMBOL, "000660"])
async def test_live_buys_nothing_while_the_account_holds_005930_outside_the_ledger_resumed_or_not(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path, symbol: str
) -> None:
    """The live account holds 3 shares the ledger does not. The start pauses new entries as in mock;
    the resume accepts them. Of 005930, a sell of the trader's could sell them: the live book still
    buys nothing (the mock book does, ``test_holdings_not_in_the_ledger_are_left_alone…``). Of
    another symbol, it buys."""
    fake, account = kis_mock
    account.holdings[symbol], account.paid[symbol] = 3, 3 * 60_000.0
    first = MockDay(tmp_path, fake, account, mode=LIVE)
    await first.run([Tick("09:00:00.5")])
    assert first.status == ENTRIES_PAUSED
    resume(tmp_path, "09:01:00")

    later = MockDay(tmp_path, fake, account, started_at=at("09:02:00"), mode=LIVE)
    trades = await later.run([TEN, BOUGHT])

    assert later.status == RUNNING
    if symbol == SYMBOL:
        assert "재개해도 새로 사지 않습니다(#460)" in first.notices[-1]
        assert trades == [] and account.placed(BUY_SIDE) == []
        assert [d["reason"] for d in later.decisions()] == [OUTSIDE_HELD]
        [held] = [n for n in later.notices if "장부 밖의 005930" in n]
        assert "005930 3주가 있어 새로 사지 않습니다" in held and "trade resume으로는 풀리지 않고" in held
    else:
        assert "#460" not in first.notices[-1]
        assert [t["mode"] for t in trades] == [LIVE] and account.holdings[SYMBOL] == 14


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
    sold = replace(ok, side=kis_orders.SIDE_SELL)  # a lost sell's (#460)
    assert candidates([ok, sold], sent, day, [], None, kis_orders.SIDE_SELL) == [sold]


@pytest.mark.parametrize(
    "held", [{"holdings": {}, "orders": []}, {"holdings": {SYMBOL: 1}, "orders": ["31401"]}]
)
def test_what_is_outside_the_ledger_is_kept_as_json(held: dict[str, Any]) -> None:
    assert Outside.of(held).as_json() == held
