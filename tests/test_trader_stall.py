"""The collector's quotes stalled (#407, #393 "호가"): more than five seconds without a quote in
continuous trading buys nothing new; the mock book keeps its open position's stop and time barrier
on KIS's REST quotes, read on the trader's key within its call limit; a paper book waits for the
stream and keeps that wait on its trade; both go back to the stream once it returns, and Telegram
hears of the stall and of the return once each.

The quotes are made up and come a second apart while they flow (005930's stream brings several a
second); a stall is a gap of more than five seconds. KIS is the fake one (``tests/test_trader_mock.py``):
its book is the last stream quote's unless an event moves it alone (``KisBook``: the stream is down,
KIS's matching goes on). The example, as in ``test_trader_paper.py``: the 10:00 barrier prediction
(up_first 0.7) read at 10:00:02.3, its target 70,250, its stop 69,850, its time barrier 10:04; the
first quote after it asks 70,100. In paper 14 shares fill there; on KIS 14 fill half a tick better,
at 70,050, and the take-profit sell rests at 70,300. Every value is made up.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant import kis, kis_orders
from krx_quant.fake_kis import SELL_SIDE, FakeAccount, FakeKis
from krx_quant.kis_orders import Servers, trader_broker
from krx_quant.predictions import DB_NAME as ENGINE_DB
from krx_quant.predictions import PredictionStore
from krx_quant.trader import MOCK, PAPER, SYMBOL
from krx_quant.trader.cells import EngineReader
from krx_quant.trader.desk import Desk
from krx_quant.trader.mock import REST_SECONDS, Timing
from krx_quant.trader.paper import STALLED, STOP_LOSS, TIME, Rules
from krx_quant.trader.quotes import BestQuote
from krx_quant.trader.selection import Selection
from krx_quant.trader.stall import STALL_AFTER, QuoteWatch
from krx_quant.trader.store import (
    DB_NAME,
    QUOTE,
    REST,
    RESTART,
    WAITED,
    TraderStore,
    read_trades,
    request_pause,
)
from tests.test_trader_mock import TIMING, kis_mock, resting_sells, settings_of
from tests.trader_doubles import (
    BARRIER,
    CALENDAR,
    FEE,
    AskPause,
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

TEN = Read("10:00:02.3", [Barrier("10:00:00")])
BOUGHT = quote("10:00:02.5", 70_000, 70_100)


def flowing(start: str, end: str, bid: float = 70_000, ask: float = 70_100) -> list[BestQuote]:
    """The stream flowing: a quote each second from ``start`` to ``end``, both included."""
    first, last = at(start), at(end)
    return [
        BestQuote(first + timedelta(seconds=n), bid, ask, 10.0, 10.0)
        for n in range(int((last - first).total_seconds()) + 1)
    ]


def stalls(state: Path) -> list[dict[str, Any]]:
    return rows(state, "stalls")


def rows(state: Path, table: str) -> list[dict[str, Any]]:
    with closing(sqlite3.connect(state / DB_NAME)) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]


# What a notice is: a stall, the quotes back (``stall.py``), or another (``limits.py``).
STALL_SAID, BACK_SAID = "호가가 5초 넘게 오지 않습니다", "호가가 다시 옵니다"


def said(notices: list[str]) -> list[str]:
    known = [STALL_SAID, BACK_SAID, "정지하고 즉시 매도 요청을 받았습니다"]
    return [next((k for k in known if k in n), n) for n in notices]


def decided(state: Path, mode: str) -> list[tuple[str, str, str | None]]:
    """(bar close, action, reason) of each decision of ``mode``'s books, in order."""
    return [
        (r["bar_close"][11:16], r["action"], r["reason"])
        for r in rows(state, "decisions")
        if r["mode"] == mode
    ]


# Paper: the desk waits for the stream.


def paper_day(state: Path, started: str = "10:00:00") -> Day:
    return Day(state, desk=True, stall_after=STALL_AFTER, started_at=at(started))


def test_a_stall_is_said_once_and_the_paper_book_sells_on_the_first_quote_after_it(
    tmp_path: Path,
) -> None:
    """Bought at 10:00:02.5, the quotes stop after 10:01:00: the stall is seen at 10:01:05.5 and said
    once, however long it lasts. The time barrier (10:04) passes in it with no quote to sell on; the
    first quote after it (10:05:00, bid 69,900) sells by the time barrier, and the trade keeps the
    stall and that its exit waited for it."""
    day = paper_day(tmp_path)

    [trade] = day.run(
        [
            *flowing("10:00:00", "10:00:02"),
            TEN,
            BOUGHT,
            *flowing("10:00:03", "10:01:00"),
            Tick("10:01:05"),
            Tick("10:01:05.5"),
            Tick("10:02:00"),
            Tick("10:04:01"),
            Tick("10:04:30"),
            quote("10:05:00", 69_900, 70_000),
        ]
    )

    assert trade["exit"] == {"at": "2026-10-14T10:05:00+09:00", "price": 69_900, "reason": TIME}
    assert trade["stalls"] == [
        {
            "began_at": "2026-10-14T10:01:05.500000+09:00",
            "last_quote_at": "2026-10-14T10:01:00+09:00",
            "ended_at": "2026-10-14T10:05:00+09:00",
            "effect": WAITED,
        }
    ]
    [stall] = stalls(tmp_path)
    assert (stall["mode"], stall["ended_by"], stall["rest_reads"]) == (PAPER, QUOTE, 0)
    assert day.notices == [
        "자동 매매(paper): 수집기의 호가가 5초 넘게 오지 않습니다(마지막 호가 10:01:00)."
        " 호가가 돌아올 때까지 새로 사지 않습니다."
        " 열린 포지션은 호가가 돌아오면 그 호가로 목표가·손절가·시간 규칙을 봅니다.",
        "자동 매매(paper): 호가가 다시 옵니다(10:05:00, 끊긴 지 240초). 스트림의 호가로 돌아갑니다.",
    ]


def test_in_a_stall_a_rise_is_skipped_and_a_buy_waiting_given_up_and_the_stream_back_buys(
    tmp_path: Path,
) -> None:
    """The 10:00 rise decides a buy at 10:00:02.3, but no quote comes after 10:00:01: the stall seen at
    10:00:06.5 gives the buy up (``quotes_stalled``), and the 10:01 rise read in it is skipped so. The
    stream back at 10:01:30, the 10:02 rise buys at the first quote after it."""
    day = paper_day(tmp_path)

    [trade] = day.run(
        [
            *flowing("10:00:00", "10:00:01"),
            TEN,
            Tick("10:00:06.5"),
            Read("10:01:02.3", [Barrier("10:01:00")]),
            *flowing("10:01:30", "10:02:05"),
            Read("10:02:02.3", [Barrier("10:02:00")]),
        ]
    )

    assert decided(tmp_path, PAPER) == [
        ("10:00", "buy", STALLED),
        ("10:01", "skip", STALLED),
        ("10:02", "buy", None),
    ]
    assert trade["entry"] == {"at": "2026-10-14T10:02:03+09:00", "price": 70_100, "quantity": 14}
    assert trade["stalls"] == [], "bought after the stall"
    assert len(day.notices) == 2


def test_a_pause_selling_at_once_asked_in_a_stall_sells_on_the_first_quote_after_it_in_paper(
    tmp_path: Path,
) -> None:
    day = paper_day(tmp_path)

    [trade] = day.run(
        [
            *flowing("10:00:00", "10:00:02"),
            TEN,
            BOUGHT,
            *flowing("10:00:03", "10:01:00"),
            Tick("10:01:05.5"),
            AskPause("10:01:20", sell_now=True),
            Tick("10:02:00"),
            quote("10:02:30", 69_950, 70_050),
        ]
    )

    assert trade["exit"] == {"at": "2026-10-14T10:02:30+09:00", "price": 69_950, "reason": "pause"}
    assert [s["effect"] for s in trade["stalls"]] == [WAITED]
    assert said(day.notices) == [STALL_SAID, "정지하고 즉시 매도 요청을 받았습니다", BACK_SAID]


def test_no_stall_outside_continuous_trading_nor_before_five_seconds_from_the_start(tmp_path: Path) -> None:
    """The watch begins at the trader's start or the open, whichever is later, and holds until the
    closing auction (15:20): no quote before the open, at the start, or after 15:20 makes a stall;
    nor any on a day that does not trade."""
    store = TraderStore(tmp_path / DB_NAME)
    notices: list[str] = []
    try:
        early = QuoteWatch(store, PAPER, CALENDAR.session, at("08:30:00"), notices.append)
        assert early.on_time(at("08:59:00")) is None, "before the open"
        assert early.on_time(at("09:00:05")) is None, "five seconds from the open"
        assert early.on_time(at("09:00:05.25")) is not None, "more than five"
        assert early.stall is not None
        store.end_stall(early.stall.id, at("09:00:06"), QUOTE)
        late = QuoteWatch(store, PAPER, CALENDAR.session, at("15:19:00"), notices.append)
        assert late.on_time(at("15:19:05")) is None
        assert late.on_time(at("15:20:00")) is None, "the closing auction"
        saturday = QuoteWatch(store, PAPER, CALENDAR.session, at("2026-10-17 10:00:00"), notices.append)
        assert saturday.on_time(at("2026-10-17 11:00:00")) is None
        off = QuoteWatch(store, PAPER, CALENDAR.session, at("10:00:00"), notices.append, after=None)
        assert off.on_time(at("11:00:00")) is None, "watching nothing"
    finally:
        store.close()
    assert len(notices) == 1


def test_the_book_as_it_stood_does_not_end_a_stall_and_a_restart_takes_up_the_day_s_stall(
    tmp_path: Path,
) -> None:
    """A quote received more than five seconds before the clock (the book as it stood when the stream
    came back) is not the stream back. A trader started again the same day goes on with the stall it
    stopped in, without a second notice; one of another day ends at the start."""
    store = TraderStore(tmp_path / DB_NAME)
    notices: list[str] = []
    try:
        watch = QuoteWatch(store, PAPER, CALENDAR.session, at("10:00:00"), notices.append)
        watch.on_quote(quote("10:00:01", 70_000, 70_100))
        stall = watch.on_time(at("10:00:06.25"))
        assert stall is not None and stall.last_quote_at == at("10:00:01")
        assert watch.on_time(at("10:00:30")) is None, "said once"
        watch.on_time(at("10:01:00"))
        assert watch.on_quote(quote("10:00:50", 70_000, 70_100)) is None, "ten seconds old"
        assert watch.stalled

        again = QuoteWatch(store, PAPER, CALENDAR.session, at("10:02:00"), notices.append)
        assert again.stall is not None and again.stall.id == stall.id, "taken up, not said again"
        assert again.on_quote(quote("10:01:50", 70_000, 70_100)) is None, "as it stood, before a tick"
        again.on_time(at("10:02:00.25"))
        ended = again.on_quote(quote("10:02:00.5", 70_000, 70_100))
        assert ended is not None and ended.id == stall.id and not again.stalled

        third = QuoteWatch(store, PAPER, CALENDAR.session, at("10:03:00"), notices.append)
        third.on_quote(quote("10:03:00", 70_000, 70_100))
        assert third.on_time(at("10:03:05.5")) is not None
        QuoteWatch(store, PAPER, CALENDAR.session, at("2026-10-15 09:00:00"), notices.append)
    finally:
        store.close()
    assert [(s["ended_by"], s["ended_at"]) for s in stalls(tmp_path)] == [
        (QUOTE, "2026-10-14T10:02:00.500000+09:00"),
        (RESTART, "2026-10-15T09:00:00+09:00"),
    ]
    assert said(notices) == [STALL_SAID, BACK_SAID, STALL_SAID]


# Mock: the champion keeps its stop and time barrier on KIS's REST quotes.


@dataclass(frozen=True)
class KisBook:
    """From ``hms`` on, KIS's book is ``bid``/``ask`` while the stream brings nothing; the trader's
    clock moves to it."""

    hms: str
    bid: int
    ask: int


@dataclass(frozen=True)
class PauseNow:
    """The screen's pause that sells at once, asked at ``hms`` and read by the trader then."""

    hms: str


Event = Read | Tick | BestQuote | KisBook | PauseNow


def _time(event: Event) -> datetime:
    if isinstance(event, BestQuote):
        return event.received_at
    if isinstance(event, KisBook | PauseNow):
        return at(event.hms)
    return event.time


@dataclass
class StallDesk:
    """The whole desk in mock mode on ``state`` against ``fake``'s account, its quotes watched: each
    event in time order (the clock at it), the champion's order and REST read in flight awaited after
    each. The champion policy (1) is kept, so that its paper shadow's trades are its policy's."""

    state: Path
    fake: FakeKis
    account: FakeAccount
    rules: Rules = field(default_factory=lambda: Rules(fee_rate=FEE))
    started_at: datetime = field(default_factory=lambda: at("10:00:00"))
    notices: list[str] = field(default_factory=list)
    timing: Timing = TIMING

    async def run(self, events: Sequence[Event]) -> None:
        keep(self.state, at("2026-10-13 22:31:00"), policy_cell(BARRIER, 0.6))
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
                    timing=self.timing,
                    stall_after=STALL_AFTER,
                )
                desk.select(Selection((pick(BARRIER),)), self.started_at)
                reader = EngineReader(self.state / ENGINE_DB, SYMBOL)
                reader.follow(desk.cells, self.started_at - timedelta(minutes=2))
                try:
                    for event in sorted(events, key=_time):
                        if isinstance(event, KisBook):  # before the clock: a read it begins sees it
                            self.account.bid, self.account.ask = event.bid, event.ask
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
                        elif isinstance(event, PauseNow):
                            request_pause(self.state / DB_NAME, True, clock[0])
                            desk.on_controls(clock[0])
                        await desk.idle()
                    desk.write(clock[0])
                finally:
                    await desk.close()
        finally:
            store.close()
            engine.close()

    def trades(self, policy: int | None = None) -> list[dict[str, Any]]:
        return read_trades(self.state / DB_NAME, None, 100, policy)

    def shadow(self) -> dict[str, Any]:
        """The paper shadow's first trade."""
        return [t for t in self.trades(policy=1) if t["shadow"]][-1]

    def rest_reads(self) -> int:
        return sum(1 for _, tr, _, _ in self.fake.rest_requests if tr == kis_orders.BOOK)


async def test_in_a_stall_the_mock_book_sells_at_its_stop_on_kis_s_rest_quote_and_the_shadow_waits(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """The stream stops after 10:00:30 while KIS's bid falls to 69,800, under the stop. The stall seen
    at 10:00:35.5, the champion reads KIS's best quotes on its key and sells there: the take-profit
    sell cancelled, 14 sold at that bid (filled at 69,850, half a tick over it). Its paper shadow has
    no such quote and waits: the first stream quote after the stall (10:01:30, bid 69,700) stops it
    out. The 10:01 rise read in the stall buys nothing; the stream back, the 10:02 rise buys on KIS at
    the first quote after it."""
    fake, account = kis_mock
    desk = StallDesk(tmp_path, fake, account)

    await desk.run(
        [
            *flowing("10:00:00", "10:00:02"),
            TEN,
            BOUGHT,
            *flowing("10:00:03", "10:00:30"),
            KisBook("10:00:33", 69_800, 69_900),
            Tick("10:00:35.5"),
            Read("10:01:02.3", [Barrier("10:01:00")]),
            Tick("10:01:10"),
            quote("10:01:30", 69_700, 69_800),
            *flowing("10:01:31", "10:02:05"),
            Read("10:02:02.3", [Barrier("10:02:00")]),
        ]
    )

    later, first = desk.trades()
    assert first["exit"] == {"at": "2026-10-14T10:00:35.500000+09:00", "price": 69_850, "reason": STOP_LOSS}
    assert [(o["purpose"], o["price"], o["status"]) for o in first["orders"]] == [
        ("entry", 70_100, "filled"),
        ("take_profit", 70_300, "cancelled"),
        ("stop_loss", 69_800, "filled"),
    ]
    stall = {
        "began_at": "2026-10-14T10:00:35.500000+09:00",
        "last_quote_at": "2026-10-14T10:00:30+09:00",
        "ended_at": "2026-10-14T10:01:30+09:00",
    }
    assert first["stalls"] == [stall | {"effect": REST}]
    assert (later["mode"], later["entry"]["at"], later["exit"]) == (
        MOCK,
        "2026-10-14T10:02:03+09:00",
        None,
    )
    assert later["stalls"] == []
    # The paper shadow had no REST quote: it waited for the stream.
    shadow = desk.shadow()
    assert shadow["exit"] == {"at": "2026-10-14T10:01:30+09:00", "price": 69_700, "reason": STOP_LOSS}
    assert shadow["stalls"] == [stall | {"effect": WAITED}]
    # No new entry in the stall, by either book; the stream back, both bought.
    assert decided(tmp_path, MOCK) == [
        ("10:00", "buy", None),
        ("10:01", "skip", STALLED),
        ("10:02", "buy", None),
    ]
    assert decided(tmp_path, PAPER) == [
        ("10:00", "buy", None),
        ("10:01", "skip", STALLED),
        ("10:02", "buy", None),
    ]
    # One REST read: at the stall, with the position open; none once it was sold, nor after.
    assert desk.rest_reads() == 1
    [kept] = stalls(tmp_path)
    assert (kept["mode"], kept["ended_by"], kept["rest_reads"]) == (MOCK, QUOTE, 1)
    assert len(desk.notices) == 2
    assert "trader의 키로 KIS REST 호가를 읽어 손절·시간 규칙대로 지킵니다" in desk.notices[0]
    assert desk.notices[1] == (
        "자동 매매(mock): 호가가 다시 옵니다(10:01:30, 끊긴 지 60초). 스트림의 호가로 돌아갑니다."
    )


async def test_in_a_stall_the_mock_book_sells_at_its_time_barrier_on_kis_s_rest_quote(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """The stream stops after 10:03:52. KIS's best quotes are read at the stall (10:03:57.5) and two
    seconds later (10:03:59.5), not in between; the time barrier passed (10:04:01, its second of
    grace), the champion sells at the last of them, the last quote before the barrier (bid 70,000,
    filled at 70,050). The shadow sells by its time barrier on the first stream quote after the stall."""
    fake, account = kis_mock
    desk = StallDesk(tmp_path, fake, account)

    await desk.run(
        [
            *flowing("10:00:00", "10:00:02"),
            TEN,
            BOUGHT,
            *flowing("10:00:03", "10:03:52"),
            Tick("10:03:57.5"),
            Tick("10:03:58.5"),
            Tick("10:03:59.5"),
            Tick("10:04:01"),
            quote("10:04:30", 69_900, 70_000),
        ]
    )

    [trade] = desk.trades()
    assert trade["exit"] == {"at": "2026-10-14T10:04:01+09:00", "price": 70_050, "reason": TIME}
    assert [s["effect"] for s in trade["stalls"]] == [REST]
    assert desk.rest_reads() == 2
    shadow = desk.shadow()
    assert shadow["exit"] == {"at": "2026-10-14T10:04:30+09:00", "price": 69_900, "reason": TIME}
    assert [s["effect"] for s in shadow["stalls"]] == [WAITED]


async def test_a_pause_selling_at_once_asked_in_a_stall_sells_on_kis_s_next_rest_quote(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """The pause (10:01:06) sells on the first quote received after it, as ever: in the stall, KIS's
    REST quote of 10:01:07.5, two seconds after the stall's first read (bid 70,000, filled at
    70,050)."""
    fake, account = kis_mock
    desk = StallDesk(tmp_path, fake, account)

    await desk.run(
        [
            *flowing("10:00:00", "10:00:02"),
            TEN,
            BOUGHT,
            *flowing("10:00:03", "10:01:00"),
            Tick("10:01:05.5"),
            PauseNow("10:01:06"),
            Tick("10:01:07"),
            Tick("10:01:07.5"),
        ]
    )

    [trade] = desk.trades()
    assert trade["exit"] == {"at": "2026-10-14T10:01:07.500000+09:00", "price": 70_050, "reason": "pause"}
    assert [s["effect"] for s in trade["stalls"]] == [REST]
    assert resting_sells(account) == [] and account.holdings[SYMBOL] == 0
    assert desk.rest_reads() == 2
    assert said(desk.notices) == [STALL_SAID, "정지하고 즉시 매도 요청을 받았습니다"]


# The call limit


def test_the_rest_reads_leave_the_order_calls_room_within_the_trader_s_call_limit() -> None:
    """Every call on the trader's key goes through one limiter: ``PER_SECOND`` in any window of 1.1 s
    (#403: of five sent at once the mock server answered three). A read every ``REST_SECONDS`` takes
    at most one slot of any window, leaving the other to the orders; with a sale's inquiry every
    ``POLL`` seconds beside it, the calls a second stay under the limiter's pace."""
    window = 1.0 + 0.1  # RateLimiter's period and its margin, as ``trader_broker`` makes it
    assert Timing().rest == REST_SECONDS >= window, "at most one read in any window"
    assert kis_orders.PER_SECOND - 1 >= 1, "a slot of each window for the order calls"
    resting = 1 / REST_SECONDS + 1 / Timing().follow  # the take-profit's row read as it rests
    selling = 1 / REST_SECONDS + 1 / kis_orders.POLL  # a sale's inquiry, the take-profit's reading stopped
    assert max(resting, selling) == pytest.approx(1.5)
    assert max(resting, selling) < kis_orders.PER_SECOND / window


async def test_kis_s_rest_quotes_are_read_every_two_seconds_of_a_stall_with_a_position_open_and_no_more(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """The clock ticks every quarter of a second (as the trader's does) through 20 seconds of stall
    with the position open, then the stream comes back: KIS's book is read at the stall and every two
    seconds after, never two in flight, and not once more after the return."""
    fake, account = kis_mock
    desk = StallDesk(tmp_path, fake, account)
    assert desk.timing.rest == REST_SECONDS
    stalled = [Tick(f"10:00:{30 + n / 4:06.3f}") for n in range(1, 81)]  # 10:00:30.25 to 10:00:50

    await desk.run(
        [
            *flowing("10:00:00", "10:00:02"),
            TEN,
            BOUGHT,
            *flowing("10:00:03", "10:00:30"),
            *stalled,
            *flowing("10:00:51", "10:00:55"),
        ]
    )

    # The stall seen at 10:00:35.25; read then, at 37.25, …, 49.25: eight reads in 15 seconds.
    [kept] = stalls(tmp_path)
    assert kept["began_at"] == "2026-10-14T10:00:35.250000+09:00"
    assert desk.rest_reads() == kept["rest_reads"] == 8
    [trade] = desk.trades()
    assert trade["exit"] is None and len(account.placed(SELL_SIDE)) == 1, "the take-profit rests as ever"
