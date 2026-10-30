"""The champion's paper shadow in mock mode (#405): the champion policy's paper record beside its KIS
orders, the challengers in paper, every record paper's, and the gap between the KIS trades and the
shadow's in the ledger, the Engine API and the nightly report.

KIS is the fake one (``tests/test_trader_mock.py``). The example: policy 1, the champion (θ 0.6),
kept on the night of 10-12, policy 2 a challenger kept on the night of 10-13; both run on 10-14. The
10:00 barrier prediction (up_first 0.7, target 70,250, stop 69,850) read at 10:00:02.3; the first
quote after it asks 70,100: the shadow and the challenger fill 14 there in paper, KIS fills the
mock's 14 at 70,050. The bid reaches 70,300 at 10:02: the paper books take profit at the target
(70,250); KIS fills the mock's take-profit sell at its limit (70,300) at 10:02:30. Every value is
made up.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant import kis
from krx_quant.fake_kis import FakeAccount, FakeKis
from krx_quant.kis_orders import Servers, trader_broker
from krx_quant.predictions import DB_NAME as ENGINE_DB
from krx_quant.predictions import PredictionStore
from krx_quant.trader import MOCK, PAPER, SYMBOL, promotion, report
from krx_quant.trader.cells import EngineReader
from krx_quant.trader.desk import Desk
from krx_quant.trader.gap import day_gap
from krx_quant.trader.limits import ORDER_FAILED
from krx_quant.trader.marks import read_marks
from krx_quant.trader.paper import TAKE_PROFIT, Rules
from krx_quant.trader.quotes import BestQuote
from krx_quant.trader.selection import Selection
from krx_quant.trader.store import DB_NAME, TraderStore, read_days, read_trades
from tests.test_trader_mock import TIMING, Fill, kis_mock, resting_sells, settings_of, until
from tests.trader_doubles import (
    BARRIER,
    CALENDAR,
    FEE,
    Barrier,
    Day,
    Read,
    Tick,
    at,
    keep,
    pick,
    policy_cell,
    quote,
)

__all__ = ["kis_mock"]  # the fixture, used here

DAY = date(2026, 10, 14)
TEN = Read("10:00:02.3", [Barrier("10:00:00")])
BOUGHT = quote("10:00:02.5", 70_000, 70_100)
TAKEN = quote("10:02:00", 70_300, 70_400)

Event = Read | Tick | BestQuote | Fill


def _time(event: Event) -> datetime:
    if isinstance(event, BestQuote):
        return event.received_at
    if isinstance(event, Fill):
        return at(event.hms)
    return event.time


def champion_and_challenger(state: Path) -> None:
    assert keep(state, at("2026-10-12 22:31:00"), policy_cell(BARRIER, 0.6)) == 1
    assert keep(state, at("2026-10-13 22:31:00"), policy_cell(BARRIER, 0.6)) == 2


@dataclass
class MockDesk:
    """The whole desk in mock mode on ``state`` against ``fake``'s account: each event in time order
    (the clock at it), the champion's order in flight awaited after each."""

    state: Path
    fake: FakeKis
    account: FakeAccount
    rules: Rules = field(default_factory=lambda: Rules(fee_rate=FEE))
    started_at: datetime = field(default_factory=lambda: at("09:00:00"))
    notices: list[str] = field(default_factory=list)
    status: str | None = None

    async def run(self, events: Sequence[Event]) -> None:
        engine = PredictionStore(self.state / ENGINE_DB)
        store = TraderStore(self.state / DB_NAME)
        store.add_run(self.started_at, MOCK, {})
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
                desk = Desk(
                    store,
                    self.rules,
                    CALENDAR.session,
                    self.started_at,
                    MOCK,
                    self.notices.append,
                    broker=broker,
                    clock=lambda: clock[0],
                    timing=TIMING,
                    stall_after=None,  # the made-up quotes are sparse (``trader_doubles.Day``)
                )
                desk.select(Selection((pick(BARRIER),)), self.started_at)
                reader = EngineReader(self.state / ENGINE_DB, SYMBOL)
                reader.follow(desk.cells, self.started_at - timedelta(minutes=2))
                try:
                    for event in sorted(events, key=_time):
                        clock[0] = _time(event)
                        desk.on_time(clock[0])
                        if isinstance(event, BestQuote):
                            self.account.bid, self.account.ask = round(event.bid), round(event.ask)
                            desk.on_quote(event)
                        elif isinstance(event, Read):
                            for shown in event.shown:
                                shown.write(engine)
                            read = reader.read(at("00:00:00"))
                            assert read is not None
                            windows, signals = read
                            desk.on_special(windows)
                            for signal in signals:
                                desk.on_signal(signal, event.time)
                        elif isinstance(event, Fill):
                            [resting] = resting_sells(self.account)
                            self.account.bid, self.account.improvement = resting.price, 0.0
                            self.account.fill(resting, event.quantity)
                            self.account.improvement = 0.5
                            await until(lambda: not desk.champion.busy)
                        await desk.idle()
                    desk.write(clock[0])
                    self.status = desk.status
                finally:
                    await desk.close()
        finally:
            store.close()
            engine.close()

    def trades(self, policy: int | None = None) -> list[dict[str, Any]]:
        return read_trades(self.state / DB_NAME, None, 100, policy)


def by_book(trades: list[dict[str, Any]]) -> dict[tuple[str, bool, bool], dict[str, Any]]:
    """Each trade by (mode, champion, shadow)."""
    found = {(t["mode"], t["champion"], t["shadow"]): t for t in trades}
    assert len(found) == len(trades)
    return found


async def test_in_mock_mode_the_champion_keeps_a_paper_shadow_beside_its_kis_trades(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    champion_and_challenger(tmp_path)
    desk = MockDesk(tmp_path, fake, account)

    await desk.run([TEN, BOUGHT, quote("10:01:00", 70_200, 70_300), TAKEN, Fill("10:02:30")])

    # The trader's trades (the trades table, the chart, the limits): the KIS mock one alone.
    [mine] = desk.trades()
    assert (mine["mode"], mine["champion"], mine["shadow"], mine["policy"]) == (MOCK, True, False, 1)
    assert (mine["entry"]["price"], mine["exit"]["price"]) == (70_050, 70_300)
    marks = read_marks(tmp_path / DB_NAME, SYMBOL, None, 10)
    assert {(m["mode"], m["side"]) for m in marks} == {(MOCK, "buy"), (MOCK, "sell")}
    # The champion policy's trades: its KIS one and its paper shadow's, filled in paper.
    books = by_book(desk.trades(policy=1))
    shadow = books[PAPER, False, True]
    assert (shadow["entry"]["price"], shadow["entry"]["quantity"]) == (70_100, 14)
    assert shadow["exit"] == {"at": "2026-10-14T10:02:00+09:00", "price": 70_250, "reason": TAKE_PROFIT}
    assert set(books) == {(MOCK, True, False), (PAPER, False, True)}
    # The challenger in paper, labelled paper.
    [theirs] = desk.trades(policy=2)
    assert (theirs["mode"], theirs["champion"], theirs["shadow"]) == (PAPER, False, False)
    assert theirs["entry"]["price"] == 70_100
    # Each policy-day once, in paper; the champion's not stopped (the shadow is held by nothing).
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db:
        days = db.execute(
            "SELECT mode, policy_id, day, champion, stopped FROM policy_days ORDER BY 2"
        ).fetchall()
    assert days == [(PAPER, 1, "2026-10-14", 1, 0), (PAPER, 2, "2026-10-14", 0, 0)]
    # The records: paper's, the shadow's for the champion, never the KIS fills.
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db:
        kept = promotion.records(db)
    [champion_day] = kept[1].days
    assert champion_day.day == DAY and champion_day.net == pytest.approx(shadow["net"])
    assert champion_day.net != pytest.approx(mine["net"])
    [challenger_day] = kept[2].days
    assert challenger_day.day == DAY and challenger_day.net == pytest.approx(theirs["net"])


async def test_the_gap_between_the_kis_trades_and_the_shadow_is_in_the_ledger_and_the_report(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """KIS bought half a tick under the shadow (70,050 against 70,100) and sold half a tick over it
    (70,300 against the target 70,250): one tick a share better, 14 × 100 = 1,400 won gross; the buy's
    commission on 700 won less (0.105 won saved), the sale's commission and tax on 700 won more
    (700 × 0.215% = 1.505): 1,398.6 won."""
    fake, account = kis_mock
    champion_and_challenger(tmp_path)
    await MockDesk(tmp_path, fake, account).run([TEN, BOUGHT, TAKEN, Fill("10:02:30")])
    [mine] = read_trades(tmp_path / DB_NAME, None, 10)
    shadow = by_book(read_trades(tmp_path / DB_NAME, None, 10, 1))[PAPER, False, True]

    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db:
        db.row_factory = sqlite3.Row
        gap = day_gap(db, DAY)
    assert gap is not None
    assert gap["mock"] == {"trades": 1, "net": pytest.approx(mine["net"])}
    assert gap["shadow"] == {"trades": 1, "net": pytest.approx(shadow["net"])}
    assert gap["difference"] == pytest.approx(mine["net"] - shadow["net"])
    [pair] = gap["pairs"]
    assert (pair["kind"], pair["predictor"], pair["bar_close"]) == (
        "barrier",
        "breiman",
        "2026-10-14T10:00:00+09:00",
    )
    assert (pair["entry_ticks"], pair["exit_ticks"]) == (pytest.approx(-0.5), pytest.approx(0.5))
    assert pair["net_difference"] == pytest.approx(1_398.6) == mine["net"] - shadow["net"]
    assert pair["mock"] == {"quantity": 14, "exit_reason": TAKE_PROFIT, "net": pytest.approx(mine["net"])}

    [day] = [d for d in read_days(tmp_path / DB_NAME, DAY, DAY) if d["mode"] == MOCK]
    assert day["gap"] == gap

    text = report.render(report.read(tmp_path / DB_NAME, DAY, CALENDAR))
    assert "KIS 모의와 그림자 장부 (#405)" in text
    assert "- KIS 모의 매매 1건" in text and "그림자(모의 체결) 1건" in text
    assert "- 10:00 barrier breiman: 진입 -0.50틱 · 청산 +0.50틱 · 14주 ↔ 14주" in text
    assert "70,050" not in text and "70050" not in text, "no price"


async def test_the_shadow_trades_apart_from_what_kis_does(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """KIS refuses the buy (an ask off the tick): the mock book stops new entries for the day, while
    the shadow buys in paper, sells, and buys the next rise too. No KIS failure reaches it."""
    fake, account = kis_mock
    champion_and_challenger(tmp_path)
    desk = MockDesk(tmp_path, fake, account)

    await desk.run(
        [
            TEN,
            quote("10:00:02.5", 70_000, 70_150),
            quote("10:02:00", 70_300, 70_400),
            Read("10:08:02.3", [Barrier("10:08:00")]),
            quote("10:08:02.5", 70_000, 70_100),
        ]
    )

    assert desk.status == ORDER_FAILED and desk.trades() == []
    shadows = [t for t in desk.trades(policy=1) if t["shadow"]]
    assert [(t["entry"]["price"], t["entry"]["quantity"]) for t in reversed(shadows)] == [
        (70_150, 14),
        (70_100, 14),
    ]
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db:
        db.row_factory = sqlite3.Row
        gap = day_gap(db, DAY)
    assert gap is not None and gap["mock"]["trades"] == 0 and gap["shadow"]["trades"] == 2
    assert [p["mock"] for p in gap["pairs"]] == [None, None]


async def test_the_shadow_and_the_challengers_size_on_the_paper_account_not_on_kis(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """A paper loss of the champion's earlier paper book (from before mock mode) narrows the paper
    account the shadow and the challenger buy on; the KIS trade sizes on its own account."""
    fake, account = kis_mock
    champion_and_challenger(tmp_path)
    store = TraderStore(tmp_path / DB_NAME)
    try:
        with store._db:  # an exited paper trade of the champion's book, a loss of 300,000 won
            store._db.execute(
                "INSERT INTO trades (mode, symbol, kind, predictor, prediction_id, bar_close, probability,"
                " threshold, anchor, tick, width_ticks, target, stop, time_barrier, decided_at, entry_at,"
                " entry_price, quantity, buy_fee, exit_at, exit_price, exit_reason, net, champion)"
                " VALUES ('paper', '005930', 'barrier', 'breiman', 0, ?, 0.7, 0.6, 1, 1, 1, 2, 0, ?, ?, ?,"
                " 1, 1, 0, ?, 1, 'stop_loss', -300000, 1)",
                (*[at("2026-10-13 10:00:00").isoformat()] * 4, at("2026-10-13 10:04:00").isoformat()),
            )
        assert store.paper_account() == -300_000
    finally:
        store.close()
    desk = MockDesk(tmp_path, fake, account)

    await desk.run([TEN, BOUGHT])

    [mine] = [t for t in desk.trades() if t["mode"] == MOCK]  # beside the paper book's history
    assert mine["entry"]["quantity"] == 14, "the mock's account is its own (KIS's cash, its mock trades)"
    paper = [
        t for t in desk.trades(policy=1) + desk.trades(policy=2) if t["mode"] == PAPER and t["exit"] is None
    ]
    assert sorted(t["entry"]["quantity"] for t in paper) == [9, 9]  # 700,000 ÷ 70,100


async def test_a_shadow_trade_left_open_at_a_switch_back_to_paper_is_sold_apart_from_the_guard(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """A trader started again in paper sells the shadow's open trade by its barriers in a shadow book
    of its own: it stays the shadow's (its policy's paper record), and the paper book's limits and
    cash never count it, before or after a restart."""
    fake, account = kis_mock
    champion_and_challenger(tmp_path)
    await MockDesk(tmp_path, fake, account).run([TEN, BOUGHT])

    paper = Day(tmp_path, started_at=at("10:01:00"), desk=True)
    paper.run([TAKEN])

    [shadow] = [t for t in read_trades(tmp_path / DB_NAME, None, 10, 1) if t["shadow"]]
    assert shadow["exit"] == {"at": "2026-10-14T10:02:00+09:00", "price": 70_250, "reason": TAKE_PROFIT}
    assert (shadow["mode"], shadow["champion"]) == (PAPER, False)
    assert paper.state_now("10:02:01")["today"]["realized"] == 0.0, "the guard did not count it"
    store = TraderStore(tmp_path / DB_NAME)
    try:
        assert store.realized(PAPER, None) == (0.0, 0), "nor does the store, as the guard reloads it"
        assert store.paper_account() == pytest.approx(shadow["net"]), "the paper account holds it"
    finally:
        store.close()
