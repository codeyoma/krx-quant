"""The live mode (#408, #393 "실전 관문", "KIS 주문"): the champion orders on the live account only past
the live gate and the execution checks, on the first period's cap until it is lifted, and the live
key stays apart from the mock one.

KIS is the fake one (``fake_kis.FakeKis`` with a ``FakeAccount`` of ``live``): the live server's TR
IDs, its codes and messages made up, as nothing of the live server was measured. The stores are
written as the trader and the nightly job write them: the mock days (the champion's KIS mock trades,
their paper shadow's, the day comparisons), the policy days and the gate's checkpoint. Every key,
account, price and P&L is made up; no test calls KIS.

The mock days of the examples: the twenty weekdays from 2026-09-16 to 2026-10-13, two round trips a
day on the first ten and one on the others (thirty). Each shadow trade buys at 70,100 and sells at
70,300 (tick 100, 10 shares, net +5,000 won); its mock twin fills half a tick better on both legs
(70,050 and 70,350), as the mock server does: a leg's adverse gap is −0.5 tick, 0 once corrected.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from krx_quant import cli, kis, kis_orders
from krx_quant.config import Settings, SettingsError
from krx_quant.fake_kis import FakeAccount, FakeKis
from krx_quant.feeds import BOOK
from krx_quant.feeds.base import KST
from krx_quant.kis_orders import Servers, trader_broker, trader_live_broker
from krx_quant.predictions import DB_NAME as ENGINE_DB
from krx_quant.predictions import PredictionStore
from krx_quant.redaction import secrets_of
from krx_quant.trader import LIVE, MOCK, PAPER, live, promotion
from krx_quant.trader import policy as policies
from krx_quant.trader.desk import Desk
from krx_quant.trader.limits import ENTRIES_PAUSED, RUNNING, ask
from krx_quant.trader.paper import PaperTrader, Rules
from krx_quant.trader.quotes import QuoteFeed
from krx_quant.trader.run import SWITCH, LiveWatch, run_trader
from krx_quant.trader.settings import TraderSettings
from krx_quant.trader.store import (
    BREACH,
    CUMULATIVE_LIMIT,
    DB_NAME,
    LIFT,
    RAMP,
    RESUME,
    SAME,
    TraderStore,
    read_trader,
    read_trades,
)
from tests.test_live_predictions import free_port
from tests.test_notify import telegram
from tests.test_trader_mock import ACCOUNT, KEY, SECRET, TIMING, until
from tests.test_trader_run import PASSING, EngineApi, book
from tests.trader_doubles import BARRIER, CALENDAR, FEE, Barrier, at, keep, policy_cell

LIVE_KEY = "PSlive" + "l" * 30
LIVE_SECRET = "Sl" + "v" * 60
LIVE_ACCOUNT = "70123456-01"
TODAY = date(2026, 10, 14)
DAYS = [d for d in (date(2026, 9, 16) + timedelta(days=i) for i in range(28)) if d.weekday() < 5][:20]
assert DAYS[-1] == date(2026, 10, 13)


@pytest.fixture
async def kis_live() -> AsyncIterator[tuple[FakeKis, FakeAccount]]:
    fake = FakeKis(app_key=LIVE_KEY, app_secret=LIVE_SECRET)
    await fake.start()
    account = FakeAccount(
        fake, TODAY, account=LIVE_ACCOUNT.replace("-", ""), live=True, improvement=0.0, cash=1_000_000
    ).install()
    yield fake, account
    await fake.stop()


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


# The stores, as the trader and the nightly job write them.

_TRADE = (
    *("mode", "symbol", "kind", "predictor", "prediction_id", "bar_close", "probability", "threshold"),
    *("anchor", "tick", "width_ticks", "target", "stop", "time_barrier", "decided_at", "entry_at"),
    *("entry_price", "quantity", "buy_fee", "exit_at", "exit_price", "exit_reason", "sell_fee", "tax"),
    *("cost", "gross", "net", "policy_id", "champion", "shadow"),
)


def trade(
    db: sqlite3.Connection,
    mode: str,
    day: date,
    n: int,
    *,
    entry: float,
    exit_: float,
    shadow: bool,
    quantity: int = 10,
    exit_at: datetime | None = None,
) -> int:
    """A trade of the champion policy 1 on ``day``, the ``n``-th of its bar closes (10:00, 10:10):
    the champion's own (``mode`` mock or live) or its paper shadow's; a KIS trade with its buy and its
    take-profit sell, each KIS's order."""
    close = datetime.combine(day, time(10, 10 * n), KST)
    entered = close + timedelta(seconds=2)
    out = exit_at or close + timedelta(minutes=2)
    row = (
        *(mode, "005930", "barrier", "breiman", day.toordinal() * 10 + n, close.isoformat(), 0.7, 0.6),
        *(70_050, 100, 2, 70_250, 69_850, (close + timedelta(minutes=4)).isoformat(), entered.isoformat()),
        *(entered.isoformat(), entry, quantity, 10.0, out.isoformat(), exit_, "take_profit", 10.0, 0.0),
        *(20.0, 5_020.0, 5_000.0, 1, 0 if shadow else 1, int(shadow)),
    )
    with db:
        cursor = db.execute(
            f"INSERT INTO trades ({', '.join(_TRADE)}) VALUES ({', '.join('?' * len(_TRADE))})", row
        )
        trade_id = int(cursor.lastrowid or 0)
        if mode != PAPER:
            for side, purpose, price, placed in (
                ("buy", "entry", entry, entered),
                ("sell", "take_profit", exit_, entered + timedelta(seconds=1)),
            ):
                kept = db.execute(
                    "INSERT INTO orders (trade_id, mode, side, purpose, price, quantity, placed_at, status,"
                    " closed_at, broker_id) VALUES (?, ?, ?, ?, ?, ?, ?, 'filled', ?, ?)",
                    (
                        trade_id,
                        mode,
                        side,
                        purpose,
                        price,
                        quantity,
                        placed.isoformat(),
                        out.isoformat(),
                        f"{trade_id}{side}",
                    ),
                )
                db.execute(  # filled whole at its limit, as KIS's inquiry shows it
                    "INSERT INTO fills (order_id, at, price, quantity) VALUES (?, ?, ?, ?)",
                    (kept.lastrowid, placed.isoformat(), price, quantity),
                )
    return trade_id


def ledger_day(db: sqlite3.Connection, mode: str, day: date) -> None:
    with db:
        db.execute(
            "INSERT INTO days (mode, day, realized, trades, worst, cumulative, cumulative_worst, daily_limit,"
            " cumulative_limit, status, halt, updated_at) VALUES (?, ?, 0, 0, 0, 0, 0, 100000, 200000,"
            " 'running', NULL, ?)",
            (mode, day.isoformat(), datetime.combine(day, time(15, 30), KST).isoformat()),
        )


def kis_days(
    state: Path, mode: str, days: list[date], *, worse: float = 0.0, compared: bool = True, after: int = 0
) -> None:
    """The champion's ``mode`` (mock or live) days, the ``after``-th on: on each, its KIS trades (two
    on the first ten days, one after) with their paper shadow's, its ledger and its comparison with
    KIS (``same``).
    The KIS fills are half a tick better than the shadow's on the mock (the mock server's), as the
    shadow's on live, each ``worse`` won worse."""
    better = 50.0 if mode == MOCK else 0.0
    store = TraderStore(state / DB_NAME)
    try:
        for i, day in enumerate(days, start=after):
            for n in range(2 if i < 10 else 1):
                trade(store.db, PAPER, day, n, entry=70_100, exit_=70_300, shadow=True)
                trade(
                    store.db,
                    mode,
                    day,
                    n,
                    entry=70_100 - better + worse,
                    exit_=70_300 + better - worse,
                    shadow=False,
                )
            ledger_day(store.db, mode, day)
            if compared:
                store.add_order_check(
                    mode, day, SAME, datetime.combine(day, time(15, 31), KST), {"orders": 2}
                )
    finally:
        store.close()


def champion_days(state: Path, days: list[date]) -> None:
    """Policy 1, the champion, ran on ``days`` (its paper record's days)."""
    store = TraderStore(state / DB_NAME)
    try:
        for day in days:
            store.put_policy_day(PAPER, 1, day, True, False, datetime.combine(day, time(10), KST))
    finally:
        store.close()


def judge_gate(state: Path, now: datetime) -> list[promotion.Checkpoint]:
    with closing(policies.connect(state / DB_NAME)) as db:
        return promotion.check_gate(db, now)


def ready(state: Path, quantity: int = 10, *, policy: bool = True) -> None:
    """Policy 1 the champion since 09-15 (kept here unless ``policy`` is off), with twenty clean mock
    days and thirty round trips, its gate passed at the trader's first checkpoint (10-13): every daily
    net +10,000 or +5,000 won, with one extra adverse tick a leg (``quantity`` shares of a 100-won
    tick) +5,000 − 2,000 a trade."""
    if policy:
        keep(state, at("2026-09-15 22:31:00"), policy_cell(BARRIER, 0.6))
    kis_days(state, MOCK, DAYS)
    if quantity != 10:
        with closing(sqlite3.connect(state / DB_NAME)) as db, db:
            db.execute("UPDATE trades SET quantity = ? WHERE mode = ?", (quantity, PAPER))
    champion_days(state, DAYS)
    [checkpoint] = judge_gate(state, at("2026-10-13 22:31:00"))
    assert checkpoint.passed
    probed(state, True)


def probed(state: Path, passed: bool) -> None:
    """The live account's read by inquiry calls alone, as ``probe-live-account`` keeps it (#408)."""
    store = TraderStore(state / DB_NAME)
    try:
        store.add_live_probe(at("2026-10-13 22:40:00"), passed, {"calls": {}})
    finally:
        store.close()


def readiness(state: Path, today: date = TODAY) -> live.Readiness:
    with closing(sqlite3.connect(state / DB_NAME)) as db:
        db.row_factory = sqlite3.Row
        return live.readiness(db, CALENDAR.session, today)


# What unlocks the live mode.


def test_the_gate_and_clean_mock_days_unlock_the_live_mode(tmp_path: Path) -> None:
    ready(tmp_path)

    found = readiness(tmp_path)

    assert found.unlocked and found.missing == []
    assert found.period == tuple(DAYS)
    checks = {c.name: c for c in found.checks}
    assert (checks["mock_days"].value, checks["mock_trips"].value) == (20, 30)
    assert checks["gap"].value == pytest.approx(0.0), "half a tick better on each leg, corrected by 0.5"
    assert checks["stressed_gate"].passed


def test_the_mock_period_is_the_latest_days_that_hold_twenty_days_and_thirty_round_trips(
    tmp_path: Path,
) -> None:
    """Five more mock days before: the period counted is the twenty latest (they hold thirty round
    trips), so a failed order on an older day does not count."""
    older = [d for d in (date(2026, 9, 7) + timedelta(days=i) for i in range(7)) if d.weekday() < 5]
    ready(tmp_path)
    kis_days(tmp_path, MOCK, older)
    store = TraderStore(tmp_path / DB_NAME)
    try:
        store.add_failure(
            datetime.combine(older[0], time(10), KST), MOCK, "buy", "refused", "made up", None, None
        )
    finally:
        store.close()

    found = readiness(tmp_path)

    assert found.period == tuple(DAYS) and found.unlocked


def _uncompared(db: sqlite3.Connection) -> None:
    db.execute("DELETE FROM order_checks WHERE day = ?", (DAYS[4].isoformat(),))


def _different(db: sqlite3.Connection) -> None:
    db.execute("UPDATE order_checks SET verdict = 'different' WHERE day = ?", (DAYS[4].isoformat(),))


def _failed(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO failures (at, mode, day, what, outcome, detail)"
        " VALUES (?, ?, ?, 'sell', 'unknown', 'x')",
        (datetime.combine(DAYS[5], time(11), KST).isoformat(), MOCK, DAYS[5].isoformat()),
    )


def _held(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO reconciles (at, mode, status, outside, detail, control_id)"
        " VALUES (?, ?, 'held', '{}', '{}', 1)",
        (datetime.combine(DAYS[6], time(8), KST).isoformat(), MOCK),
    )


def _open_order(db: sqlite3.Connection) -> None:
    db.execute(
        "UPDATE orders SET status = 'open' WHERE id = (SELECT min(id) FROM orders WHERE mode = ?)", (MOCK,)
    )


def _late(db: sqlite3.Connection) -> None:
    db.execute(
        "UPDATE trades SET exit_at = ? WHERE id = (SELECT max(id) FROM trades WHERE mode = ?)",
        (datetime.combine(DAYS[-1], time(15, 25), KST).isoformat(), MOCK),
    )


def _worse(db: sqlite3.Connection) -> None:
    """Every mock leg two ticks worse than the shadow's after the correction: a buy at 70,250."""
    db.execute(
        "UPDATE trades SET entry_price = entry_price + 250, exit_price = exit_price - 250 WHERE mode = ?",
        (MOCK,),
    )


def _unprobed(db: sqlite3.Connection) -> None:
    db.execute("DELETE FROM live_probes")


def _probe_failed(db: sqlite3.Connection) -> None:
    db.execute("UPDATE live_probes SET passed = 0")


def _short(db: sqlite3.Connection) -> None:
    db.execute("DELETE FROM days WHERE mode = ? AND day = ?", (MOCK, DAYS[0].isoformat()))


@pytest.mark.parametrize(
    ("change", "missing"),
    [
        (_uncompared, "order_checks"),  # a day not compared is no pass
        (_different, "order_checks"),
        (_failed, "order_failed"),
        (_held, "reconcile"),
        (_open_order, "unresolved"),
        (_late, "flat"),
        (_worse, "gap"),
        (_short, "mock_days"),
        (_unprobed, "live_probe"),  # the live account not read yet by inquiry calls alone
        (_probe_failed, "live_probe"),
    ],
)
def test_each_execution_check_keeps_the_live_mode_locked(
    tmp_path: Path, change: Callable[[sqlite3.Connection], None], missing: str
) -> None:
    ready(tmp_path)
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db, db:
        change(db)

    found = readiness(tmp_path)

    assert not found.unlocked and missing in found.missing
    assert found.gate.passed, "the gate alone does not unlock it"


def test_the_gate_charged_one_more_adverse_tick_a_leg_must_pass_too(tmp_path: Path) -> None:
    """Thirty shares a trade: one extra tick a leg is 2 × 100 × 30 = 6,000 won a round trip, more than
    its +5,000. The gate passes, its stressed bound does not."""
    ready(tmp_path, quantity=30)

    found = readiness(tmp_path)

    assert found.gate.passed and not found.unlocked
    assert found.missing == ["stressed_gate"]


def test_before_the_first_checkpoint_the_gate_is_missing_with_the_next_one_named(tmp_path: Path) -> None:
    keep(tmp_path, at("2026-09-15 22:31:00"), policy_cell(BARRIER, 0.6))
    champion_days(tmp_path, DAYS[:12])

    found = readiness(tmp_path)

    assert found.missing[0] == "gate"
    assert found.gate_said() == (
        "실전 관문: 챔피언 정책 1의 챔피언으로 돈 모의 기록 12일, 다음 판정은 모의 이력 20거래일째"
        "(지금 12일, α 1.520%), 지금 하한 +0원"
    )
    assert "KIS 모의 거래일 0/20일" in found.notice()


# The trader in live mode.


async def run_live(
    state: Path,
    fake: FakeKis,
    account: FakeAccount,
    sent: list[str],
    telegram_url: str,
    *,
    act: Callable[[QuoteFeed, list[datetime]], Awaitable[None]],
    start: str = "10:00:02.3",
    settings: Settings | None = None,
    fast: bool = False,
    ends: bool = False,
    predict: bool = True,
) -> str | None:
    """``krx-quant trade`` with ``KRX_TRADER_MODE=live`` on ``state`` (its 10:00 prediction in the
    engine's database), the live servers ``fake``'s, until ``act`` is done (and with ``ends`` until
    the trader stops by itself, within ten seconds); what it returned. ``fast`` makes the loops' waits
    short, so that the clock set ahead is read at once."""
    engine = PredictionStore(state / ENGINE_DB)
    if predict:
        Barrier("10:00:00").write(engine)
    engine.close()
    feed = QuoteFeed(["005930"])
    port = free_port()
    runner = web.AppRunner(feed.app())
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    clock = [at(start)]
    account.clock = lambda: clock[0]
    api = EngineApi([PASSING])
    chosen = settings or live_settings(state)
    chosen = replace(
        chosen, telegram_bot_token="123:fake", telegram_chat_id="42", telegram_api_url=telegram_url
    )

    async def quick(seconds: float) -> None:
        await asyncio.sleep(min(seconds, 0.01))

    try:
        async with api.serving() as engine_url:
            trader = TraderSettings(
                mode=LIVE,
                quotes_url=f"ws://127.0.0.1:{port}/v1/quotes",
                engine_url=engine_url,
                poll_seconds=0.02,
            )
            stop = asyncio.Event()
            task = asyncio.create_task(
                run_trader(
                    chosen,
                    trader,
                    stop=stop,
                    now=lambda: clock[0],
                    sleep=quick if fast else asyncio.sleep,
                    live_servers=Servers(fake.rest_url, fake.ws_url),
                    timing=TIMING,
                )
            )
            try:
                await act(feed, clock)
                if ends:
                    await asyncio.wait_for(asyncio.shield(task), 10)
            finally:
                stop.set()
                returned = await asyncio.wait_for(task, 10)
    finally:
        await runner.cleanup()
    return returned


def runs(state: Path) -> list[dict[str, Any]]:
    with closing(sqlite3.connect(state / DB_NAME)) as db:
        db.row_factory = sqlite3.Row
        return [
            dict(r) | {"detail": json.loads(r["detail"])}
            for r in db.execute("SELECT * FROM runs ORDER BY id")
        ]


def decided(state: Path) -> bool:
    if not (state / DB_NAME).exists():
        return False
    with closing(sqlite3.connect(state / DB_NAME)) as db:
        return bool(db.execute("SELECT count(*) FROM decisions").fetchone()[0])


async def test_live_before_the_gate_orders_nothing_and_says_what_is_missing(
    kis_live: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """The champion has no checkpoint yet: the trader runs as paper (its run kept as paper, configured
    live), buys the 10:00 rise on paper, calls no KIS server, and says on Telegram what is missing."""
    fake, account = kis_live
    state = tmp_path / "state"
    keep(state, at("2026-10-13 22:31:00"), policy_cell(BARRIER, 0.6))

    async def act(feed: QuoteFeed, clock: list[datetime]) -> None:
        await until(lambda: decided(state))
        await until(lambda: feed.clients > 0)
        feed.on_event(BOOK.tr_id, book(at("10:00:02.5"), 70_000, 70_100))
        await until(lambda: len(read_trades(state / DB_NAME, None, 10)) == 1)

    async with telegram(200) as (url, sent):
        returned = await run_live(state, fake, account, sent, url, act=act)
        await until(lambda: any("자동 매매(live): 실전 모드" in t for t in sent))

    [paper] = read_trades(state / DB_NAME, None, 10)
    assert (returned, paper["mode"], paper["entry"]["quantity"]) == (None, PAPER, 14), (
        "a paper trade of the whole capital: no live order, no cap"
    )
    assert fake.rest_requests == [] and fake.posts_received == [] and fake.tokens_issued == []
    [run] = runs(state)
    assert (run["mode"], run["detail"]["configured"]) == (PAPER, LIVE)
    assert run["detail"]["live"][0] == "gate"
    [notice] = [t for t in sent if "자동 매매(live): 실전 모드" in t]
    assert "실전 관문: 챔피언 정책 1의 챔피언으로 돈 모의 기록 0일, 다음 판정은 모의 이력 20거래일째" in (
        notice
    )
    assert "KIS 모의 거래일 0/20일" in notice and "KIS 모의 왕복 매매 0/30건" in notice
    state_now = read_trader(state / DB_NAME, at("10:00:03"))
    assert state_now["mode"] == PAPER
    assert state_now["live"]["configured"] and not state_now["live"]["ordering"]
    assert state_now["live"]["missing"][:3] == ["gate", "mock_days", "mock_trips"]


async def test_live_past_the_gate_and_the_checks_orders_on_the_live_server_within_the_first_cap(
    kis_live: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """Past the gate and the checks: the 10:00 rise buys on the live server, with the live server's TR
    IDs, 3 shares (⌊250,000 ÷ 70,100⌋: the first period's cap), its take-profit resting there; its
    paper shadow buys 14. An account worth 2,000,000 won, above the 1,000,000 cap, is told."""
    fake, account = kis_live
    account.cash = 2_000_000
    state = tmp_path / "state"
    ready(state)

    async def act(feed: QuoteFeed, clock: list[datetime]) -> None:
        await until(lambda: decided(state))
        await until(lambda: feed.clients > 0)
        account.bid, account.ask = 70_000, 70_100
        feed.on_event(BOOK.tr_id, book(at("10:00:02.5"), 70_000, 70_100))
        await until(lambda: len(account.resting(kis_orders.SIDE_SELL)) == 1)

    async with telegram(200) as (url, sent):
        returned = await run_live(state, fake, account, sent, url, act=act)
        await until(lambda: any("실전 계좌의 평가금" in t for t in sent))

    assert returned is None
    [bought] = [t for t in read_trades(state / DB_NAME, None, 100) if t["mode"] == LIVE]
    assert (bought["mode"], bought["entry"]["quantity"], bought["entry"]["price"]) == (LIVE, 3, 70_100)
    posted = {tr_id for _, tr_id, _, _ in fake.posts_received}
    assert posted == {kis_orders.BUY.live, kis_orders.SELL.live}, "the live server's TR IDs alone"
    assert not posted & {kis_orders.BUY.mock, kis_orders.SELL.mock}
    with closing(sqlite3.connect(state / DB_NAME)) as db:
        shadow = db.execute(
            "SELECT mode, quantity FROM trades WHERE shadow = 1 AND entry_at > '2026-10-14'"
        ).fetchall()
    assert shadow == [(PAPER, 14)], "its paper shadow, sized on the capital"
    assert runs(state)[-1]["mode"] == LIVE
    assert any("자동 매매(live): 실전 계좌에 주문합니다. 운용 상한 250,000원" in t for t in sent)
    assert any("실전 계좌의 평가금(2,000,000원)이 운용 상한(1,000,000원)보다 큽니다" in t for t in sent)
    live_state = read_trader(state / DB_NAME, at("10:00:03"))["live"]
    assert live_state["ordering"] and live_state["ramp"]["cap"] == 250_000


async def test_live_never_buys_more_than_the_orderable_cash(
    kis_live: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """150,000 won orderable: 2 shares (⌊150,000 ÷ (70,100 × 1.00015)⌋), below the cap's 3."""
    fake, account = kis_live
    account.cash = 150_000
    state = tmp_path / "state"
    ready(state)

    async def act(feed: QuoteFeed, clock: list[datetime]) -> None:
        await until(lambda: decided(state))
        await until(lambda: feed.clients > 0)
        account.bid, account.ask = 70_000, 70_100
        feed.on_event(BOOK.tr_id, book(at("10:00:02.5"), 70_000, 70_100))
        await until(lambda: len(account.resting(kis_orders.SIDE_SELL)) == 1)

    async with telegram(200) as (url, sent):
        await run_live(state, fake, account, sent, url, act=act)

    [bought] = [t for t in read_trades(state / DB_NAME, None, 100) if t["mode"] == LIVE]
    assert (bought["mode"], bought["entry"]["quantity"]) == (LIVE, 2)
    [buy] = account.placed(kis_orders.SIDE_BUY)
    assert buy.quantity == 2


async def test_the_live_mode_s_answer_changing_at_a_pick_stops_the_trader_to_start_again(
    kis_live: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """Started at 08:00 without the gate, it runs as paper; the gate and the checks pass before the
    08:30 pick: at that pick, holding nothing, it says so and stops (``SWITCH``), for Docker to start
    it again in live mode."""
    fake, account = kis_live
    state = tmp_path / "state"

    async def act(feed: QuoteFeed, clock: list[datetime]) -> None:
        await until(lambda: len(runs(state)) == 1)
        await until(lambda: any("자동 매매(live): 실전 모드" in t for t in sent))
        ready(state, policy=False)
        clock[0] = at("08:30:00.5")

    keep(state, at("2026-09-15 22:31:00"), policy_cell(BARRIER, 0.6))
    async with telegram(200) as (url, sent):
        returned = await run_live(
            state, fake, account, sent, url, act=act, start="08:00:00", fast=True, ends=True
        )
        await until(lambda: any("실전 관문과 실행 점검을 모두 넘었습니다" in t for t in sent))

    assert returned == SWITCH
    assert [r["mode"] for r in runs(state)] == [PAPER]
    assert fake.posts_received == [], "no live order before the start again"


async def test_a_start_with_live_exposure_winds_it_down_on_the_live_account_before_paper(
    kis_live: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """Codex on #425: a live trade still open when the trader starts again after a change of the
    champion (policy 2, no checkpoint passed). The trader does not fall back to paper at once: it runs
    the live book to wind the trade down (its take-profit resting on KIS followed, nothing new bought),
    and once KIS fills it and nothing is left, it stops to start again as paper (``SWITCH``)."""
    fake, account = kis_live
    state = tmp_path / "state"
    ready(state)

    async def buy(feed: QuoteFeed, clock: list[datetime]) -> None:
        await until(lambda: decided(state))
        await until(lambda: feed.clients > 0)
        account.bid, account.ask = 70_000, 70_100
        feed.on_event(BOOK.tr_id, book(at("10:00:02.5"), 70_000, 70_100))
        await until(lambda: len(account.resting(kis_orders.SIDE_SELL)) == 1)

    async def wind_down(feed: QuoteFeed, clock: list[datetime]) -> None:
        await until(lambda: len(runs(state)) == 2)
        [resting] = account.resting(kis_orders.SIDE_SELL)
        account.bid = resting.price
        account.fill(resting)

    async with telegram(200) as (url, sent):
        assert await run_live(state, fake, account, sent, url, act=buy) is None
        keep(state, at("10:00:30"), policy_cell(BARRIER, 0.65))
        with closing(policies.connect(state / DB_NAME)) as db:
            assert policies.crown(db, at("10:00:40"), 2, "test", {}, replacing=1)
        returned = await run_live(
            state, fake, account, sent, url, act=wind_down, start="10:01:00", ends=True, predict=False
        )
        await until(lambda: any("정리할 것(열린 매매·주문)이 있습니다" in t for t in sent))

    assert returned == SWITCH
    first, second = runs(state)
    assert (first["mode"], second["mode"]) == (LIVE, LIVE)
    assert second["detail"]["winding_down"] == 2, "the open trade and its take-profit"
    assert second["detail"]["live"][0] == "gate"
    [trade] = [t for t in read_trades(state / DB_NAME, None, 100) if t["mode"] == LIVE]
    assert trade["exit"]["reason"] == "take_profit"
    assert len(account.placed(kis_orders.SIDE_BUY)) == 1, "nothing new bought while winding down"
    with closing(sqlite3.connect(state / DB_NAME)) as db:
        assert live.exposure(db) == 0


def test_a_live_trader_whose_champion_no_longer_passes_switches_back_at_the_pick(tmp_path: Path) -> None:
    store = TraderStore(tmp_path / DB_NAME)
    notices: list[str] = []
    try:
        desk = Desk(store, Rules(fee_rate=FEE), CALENDAR.session, at("08:30:00"), PAPER, notices.append)
        watch = LiveWatch(LIVE, LIVE, desk, CALENDAR.session, notices.append)
        watch.judge(at("08:30:00"))
    finally:
        store.close()

    assert watch.switch.is_set()
    assert notices[-1].startswith("자동 매매(live): 실전 조건이 빠졌습니다(실전 관문: 챔피언이 아직 없다")


# The first live period.


def live_history(state: Path, days: list[date], *, worse: float = 0.0, since: datetime | None = None) -> None:
    """The live mode first started at ``since`` (the first day's 08:00 by default), then ``days`` of
    the champion's live trades (two a day on the first ten, one after) with their paper shadow's."""
    store = TraderStore(state / DB_NAME)
    try:
        store.add_run(since or datetime.combine(days[0], time(8), KST), LIVE, {})
    finally:
        store.close()
    kis_days(state, LIVE, days, worse=worse)


def stepped(state: Path, now: datetime, notices: list[str]) -> live.Ramp:
    store = TraderStore(state / DB_NAME)
    try:
        return live.step(store, CALENDAR.session, now, 1_000_000, notices.append)
    finally:
        store.close()


def ramp_rows(state: Path) -> list[tuple[str, int | None]]:
    with closing(sqlite3.connect(state / DB_NAME)) as db:
        return [(r[0], r[1]) for r in db.execute("SELECT event, control_id FROM ramp ORDER BY id")]


def test_the_first_period_holds_the_cap_until_twenty_days_and_thirty_round_trips_then_lifts_it(
    tmp_path: Path,
) -> None:
    notices: list[str] = []
    live_history(tmp_path, DAYS[:19])

    held = stepped(tmp_path, at("08:30:00"), notices)
    kis_days(tmp_path, LIVE, DAYS[19:], after=19)
    lifted = stepped(tmp_path, at("08:30:00"), notices)
    after = stepped(tmp_path, at("2026-10-15 08:30:00"), notices)

    assert (held.cap, held.outcome, len(held.days), held.trips) == (250_000, None, 19, 29)
    assert (lifted.cap, lifted.lifted_at) == (1_000_000, at("08:30:00"))
    assert (after.cap, after.lifted_at) == (1_000_000, at("08:30:00")), "for good"
    assert ramp_rows(tmp_path) == [(LIFT, None)]
    assert notices == [
        "자동 매매(live): 실전 첫 단계(20거래일, 왕복 매매 30건)를 점검대로 마쳤습니다. 운용 상한을 "
        "250,000원에서 1,000,000원으로 올립니다."
    ]


def test_a_live_gap_beyond_the_bounds_at_the_period_s_end_breaches_and_pauses_new_entries(
    tmp_path: Path,
) -> None:
    """Each live leg 3 ticks worse than its shadow's (no correction on live): a breach, a pause of new
    entries from the ramp, the cap kept."""
    notices: list[str] = []
    live_history(tmp_path, DAYS, worse=300.0)

    found = stepped(tmp_path, at("08:30:00"), notices)

    assert (found.cap, found.breached_at) == (250_000, at("08:30:00"))
    [(event, control)] = ramp_rows(tmp_path)
    assert event == BREACH and control is not None
    store = TraderStore(tmp_path / DB_NAME)
    try:
        [kept] = store.controls()
        assert (kept.action, kept.source, kept.sell_now) == ("pause", RAMP, False)
    finally:
        store.close()
    assert notices[0].startswith(
        "자동 매매(live): 실전 첫 단계의 점검을 벗어나 새 진입을 멈췄습니다: 다리 60개"
    )
    assert "krx-quant trade resume" in notices[0]
    assert stepped(tmp_path, at("2026-10-15 08:30:00"), notices).outcome is None, "once, until reviewed"


def test_a_failed_order_in_the_first_period_breaches_at_the_next_pick_and_a_resume_begins_it_again(
    tmp_path: Path,
) -> None:
    """A failed sell on the third live day: the next pick breaches (before the period is long enough),
    the champion's book stops buying; the server's resume lifts the pause and the period counts again
    from then, the failure before it."""
    notices: list[str] = []
    live_history(tmp_path, DAYS[:5])
    store = TraderStore(tmp_path / DB_NAME)
    try:
        store.add_failure(
            datetime.combine(DAYS[2], time(11), KST), LIVE, "sell", "unknown", "made up", None, None
        )
        book = PaperTrader(store, Rules(fee_rate=FEE), CALENDAR.session, at("08:29:00"), LIVE, notices.append)
        found = live.step(store, CALENDAR.session, at("08:30:00"), 1_000_000, notices.append)
        book.on_controls(at("08:30:00"))
        paused = book.status
        assert not isinstance(ask(store, RESUME, LIVE, at("08:40:00")), str)
        book.on_controls(at("08:40:00"))
        resumed = book.status
        again = live.ramp(store.db, CALENDAR.session, at("2026-10-15 08:30:00"), 1_000_000)
    finally:
        store.close()

    assert found.breached_at == at("08:30:00")
    assert any("주문 실패가 있던 날 1일" in n for n in notices)
    assert (paused, resumed) == (ENTRIES_PAUSED, RUNNING)
    assert (again.since, again.breached_at, again.outcome, again.cap) == (at("08:40:00"), None, None, 250_000)
    assert again.days == (), "the period from the resume: its day not compared with KIS yet does not count"
    assert "확인을 기다리는 날 1일" in next(c.said for c in again.checks if c.name == "live_days")


def test_a_breach_resumed_while_the_trader_runs_as_paper_begins_a_new_period(tmp_path: Path) -> None:
    """The ramp's pause holds every mode: resumed by a trader running as paper (its champion changed),
    the breach is over all the same, and the next live period counts from the resume."""
    notices: list[str] = []
    live_history(tmp_path, DAYS[:5])
    store = TraderStore(tmp_path / DB_NAME)
    try:
        failed_at = datetime.combine(DAYS[2], time(11), KST)
        store.add_failure(failed_at, LIVE, "sell", "unknown", "made up", None, None)
        assert live.step(store, CALENDAR.session, at("08:30:00"), 1_000_000, notices.append).breached_at
        rules = Rules(fee_rate=FEE)
        book = PaperTrader(store, rules, CALENDAR.session, at("08:31:00"), PAPER, notices.append)
        assert book.status == ENTRIES_PAUSED, "the ramp's pause holds the paper book too"
        assert not isinstance(ask(store, RESUME, PAPER, at("08:40:00")), str)
        book.on_controls(at("08:40:00"))
        again = live.ramp(store.db, CALENDAR.session, at("2026-10-15 08:30:00"), 1_000_000)
    finally:
        store.close()

    assert book.status == RUNNING
    assert (again.since, again.breached_at) == (at("08:40:00"), None)


def test_a_live_day_counts_once_compared_and_its_fills_sound(tmp_path: Path) -> None:
    """A live day not compared with KIS yet waits (no count, no breach); a sale filled six ticks over
    its limit is not sound: a breach of ``live_fills``."""
    notices: list[str] = []
    live_history(tmp_path, DAYS[:3])
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db, db:
        db.execute("DELETE FROM order_checks WHERE day = ?", (DAYS[2].isoformat(),))

    waiting = stepped(tmp_path, at("08:30:00"), notices)
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db, db:
        db.execute(
            "UPDATE fills SET price = price + 600 WHERE id = (SELECT min(f.id) FROM fills f"
            " JOIN orders o ON o.id = f.order_id WHERE o.mode = ? AND o.side = 'sell')",
            (LIVE,),
        )
    breached = stepped(tmp_path, at("08:30:00"), notices)

    assert (waiting.outcome, waiting.breached_at, len(waiting.days)) == (None, None, 2)
    assert "확인을 기다리는 날 1일" in next(c.said for c in waiting.checks if c.name == "live_days")
    assert breached.breached_at == at("08:30:00")
    fills = next(c for c in breached.checks if c.name == "live_fills")
    assert not fills.passed and "체결가가 지정가와 +6.0틱 떨어졌다" in fills.said
    assert any("실전 체결 확인" in n for n in notices)


async def test_the_live_desk_sizes_on_the_cap_until_the_period_is_lifted(
    kis_live: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """Ten live days: the champion's KIS book sizes on 250,000 won, its paper shadow beside it; twenty
    days and thirty round trips later the pick lifts the cap, and it sizes on the capital."""
    fake, _ = kis_live
    notices: list[str] = []
    live_history(tmp_path, DAYS[:10])
    store = TraderStore(tmp_path / DB_NAME)
    try:
        async with kis.rest_session() as session:
            broker = trader_live_broker(
                session,
                live_settings(tmp_path),
                lambda: at("08:30:00"),
                servers=Servers(fake.rest_url, fake.ws_url),
            )
            desk = Desk(
                store,
                Rules(fee_rate=FEE),
                CALENDAR.session,
                at("08:00:00"),
                LIVE,
                notices.append,
                broker=broker,
                clock=lambda: at("08:30:00"),
            )
            first = desk.ramp(at("08:30:00"))
            capped = desk.champion.cap
            kis_days(tmp_path, LIVE, DAYS[10:], after=10)
            lifted = desk.ramp(at("08:30:00"))
            uncapped = desk.champion.cap
            shadow = desk.shadow
            await desk.close()
    finally:
        store.close()

    assert shadow is not None and (shadow.mode, shadow.shadow) == (PAPER, True)
    assert first is not None and (first.cap, capped) == (250_000, 250_000)
    assert lifted is not None and (lifted.cap, uncapped) == (1_000_000, None)
    assert fake.posts_received == [] and fake.rest_requests == [], "judging the ramp calls nothing"


# The live key, apart from the mock one.


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"trader_live_kis_app_key": None}, "are needed for the live mode"),
        ({"trader_live_kis_account": None}, "are needed for the live mode"),
        ({"trader_live_kis_app_key": "PScollector" + "c" * 25}, "the collector's"),
        ({"trader_live_kis_app_key": KEY}, "is the mock one"),
        ({"trader_live_kis_account": ACCOUNT}, "is the mock one"),
        ({"trader_live_kis_account": "7012345"}, "KRX_TRADER_LIVE_KIS_ACCOUNT"),
    ],
)
async def test_the_live_broker_takes_the_live_account_s_own_key_alone(
    kis_live: tuple[FakeKis, FakeAccount], tmp_path: Path, change: dict[str, Any], why: str
) -> None:
    fake, _ = kis_live
    trader = TraderSettings(
        mode=LIVE, quotes_url="ws://127.0.0.1:1/v1/quotes", engine_url="http://127.0.0.1:1"
    )

    with pytest.raises(SettingsError, match=why):
        await run_trader(
            live_settings(tmp_path, **change),
            trader,
            stop=asyncio.Event(),
            live_servers=Servers(fake.rest_url, fake.ws_url),
        )

    assert not (tmp_path / DB_NAME).exists(), "no run kept"
    assert fake.rest_requests == [] and fake.posts_received == [] and fake.tokens_issued == []


@pytest.mark.parametrize(
    "change", [{"trader_kis_app_key": LIVE_KEY}, {"trader_kis_account": LIVE_ACCOUNT.replace("-", "")}]
)
async def test_the_mock_broker_never_takes_the_live_key(tmp_path: Path, change: dict[str, Any]) -> None:
    settings = replace(live_settings(tmp_path), **change)
    async with kis.rest_session() as session:
        with pytest.raises(SettingsError, match="is the live one"):
            trader_broker(session, settings, lambda: at("09:00:00"), servers=kis_orders.MOCK)


def test_the_live_key_and_account_are_masked_in_what_the_server_posts(tmp_path: Path) -> None:
    masked = secrets_of(live_settings(tmp_path))
    assert {LIVE_KEY, LIVE_SECRET, LIVE_ACCOUNT, "7012345601", "70123456"} <= set(masked)


def test_the_live_mode_s_tr_ids_are_the_live_server_s() -> None:
    """The live broker's account sends the live TR IDs (``Tr.live``), the mock one the mock's."""
    assert kis_orders.LIVE.rest != kis_orders.MOCK.rest
    for tr in (kis_orders.BUY, kis_orders.SELL, kis_orders.CANCEL, kis_orders.FILLS, kis_orders.BALANCE):
        assert tr.of(False) == tr.live != tr.mock == tr.of(True)


def test_a_live_trader_running_as_paper_re_arms_its_paper_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``KRX_TRADER_MODE=live`` before the gate: the trader runs as paper, so ``krx-quant trade rearm``
    re-arms the paper cumulative limit, the mode it last started in."""
    monkeypatch.chdir(tmp_path)  # no .env
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KRX_TRADER_MODE", LIVE)
    monkeypatch.setattr(cli, "CONTROL_SECONDS", 0.01)
    store = TraderStore(tmp_path / "state" / DB_NAME)
    try:
        store.add_run(at("09:00:00"), PAPER, {"configured": LIVE})
        store.add_halt(at("10:00:00"), PAPER, CUMULATIVE_LIMIT, -200_100, -200_100, 0, 200_000)
    finally:
        store.close()

    assert cli.main(["trade", "rearm"]) == 0
    assert "rearm asked for" in capsys.readouterr().out
    with closing(sqlite3.connect(tmp_path / "state" / DB_NAME)) as db:
        assert db.execute("SELECT action, mode FROM controls").fetchall() == [("rearm", PAPER)]
