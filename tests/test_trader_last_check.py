"""The broker's last check of a buy in the running trader (#543): ``krx-quant trade`` hands its broker
the trader's limits (``KRX_TRADER_CAPITAL``, the live first period's cap, 005930 alone) and the
stream's quotes, so that a buy the sizing got wrong is withheld before it leaves for KIS: kept as a
buy that could not be sent (``unsent``), a failed order told on Telegram, a WARNING.

The sizing is made wrong on purpose (``paper.size`` patched), as an agent's PR might make it. KIS is
the fake one; every key, account and price is made up, and no test calls KIS.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from krx_quant import kis, kis_orders
from krx_quant.fake_kis import FakeAccount, FakeKis
from krx_quant.feeds import BOOK
from krx_quant.kis_orders import BuyLimits, Servers, trader_live_broker
from krx_quant.predictions import DB_NAME as ENGINE_DB
from krx_quant.predictions import PredictionStore
from krx_quant.trader import LIVE, MOCK, SYMBOL, paper
from krx_quant.trader.desk import Desk
from krx_quant.trader.quotes import QuoteFeed
from krx_quant.trader.run import LiveWatch, run_trader
from krx_quant.trader.settings import TraderSettings
from krx_quant.trader.store import DB_NAME, UNSENT, TraderStore
from tests.kis_doubles import TIMING, kis_mock, live_settings, settings_of
from tests.server_doubles import free_port, telegram
from tests.test_trader_live import DAYS, decided, kis_days, kis_live, live_history, ready, run_live
from tests.trader_doubles import CALENDAR, PASSING, Barrier, EngineApi, at, book, first_champion, whole_rules
from tests.waiting import until

__all__ = ["kis_live", "kis_mock"]  # fixtures

WITHHELD = "매수 주문을 보내지 못했습니다(buy: withheld by the broker's last check (#543)"


def sized(monkeypatch: pytest.MonkeyPatch, shares: int) -> None:
    """Every buy sized ``shares``, whatever its budget, cash or capital: the sizing gone wrong."""

    def wrong(*args: Any, **kwargs: Any) -> int:
        return shares

    monkeypatch.setattr(paper, "size", wrong)


def failed_buys(state: Path) -> tuple[list[tuple[str, str, str]], list[str]]:
    """The failures kept (what, outcome, detail) and the statuses of the KIS buys kept."""
    with closing(sqlite3.connect(state / DB_NAME)) as db:
        failures = [
            (str(r[0]), str(r[1]), str(r[2]))
            for r in db.execute("SELECT what, outcome, detail FROM failures ORDER BY id")
        ]
        buys = [str(r[0]) for r in db.execute("SELECT status FROM unfilled_buys ORDER BY id")]
    return failures, buys


def warned(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "krx_quant.kis_orders" and r.levelno == logging.WARNING and "withheld" in r.getMessage()
    ]


async def run_mock(
    state: Path,
    fake: FakeKis,
    telegram_url: str,
    *,
    act: Callable[[QuoteFeed, list[datetime]], Awaitable[None]],
) -> None:
    """``krx-quant trade`` with ``KRX_TRADER_MODE=mock`` on ``state`` (the first champion, its 10:00
    prediction in the engine's database), the mock servers ``fake``'s, until ``act`` is done."""
    first_champion(state)
    engine = PredictionStore(state / ENGINE_DB)
    Barrier("10:00:00").write(engine)
    engine.close()
    feed = QuoteFeed([SYMBOL])
    port = free_port()
    runner = web.AppRunner(feed.app())
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    clock = [at("10:00:02.3")]
    settings = replace(
        settings_of(fake, state),
        telegram_bot_token="123:fake",
        telegram_chat_id="42",
        telegram_api_url=telegram_url,
    )
    try:
        async with EngineApi([PASSING]).serving() as engine_url:
            trader = TraderSettings(
                mode=MOCK,
                quotes_url=f"ws://127.0.0.1:{port}/v1/quotes",
                engine_url=engine_url,
                poll_seconds=0.02,
            )
            stop = asyncio.Event()
            task = asyncio.create_task(
                run_trader(
                    settings,
                    trader,
                    stop=stop,
                    now=lambda: clock[0],
                    servers=Servers(fake.rest_url, fake.ws_url),
                    timing=TIMING,
                )
            )
            try:
                await act(feed, clock)
            finally:
                stop.set()
                await asyncio.wait_for(task, 10)
    finally:
        await runner.cleanup()


@pytest.mark.timeout(60)  # the mock broker's pace, one call in any 1.1 s
async def test_a_mock_buy_sized_above_the_capital_is_withheld_and_kept_unsent(
    kis_mock: tuple[FakeKis, FakeAccount],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Twenty shares at 70,100 (1,402,000 won) against the 1,000,000 won capital: the mock account's
    10,000,000 won would pay them and KIS would take them, but the broker withholds the buy. It is kept
    ``unsent``, a failed order (no new entry that day), told on Telegram."""
    fake, account = kis_mock
    state = tmp_path / "state"
    sized(monkeypatch, 20)
    caplog.set_level(logging.WARNING, logger="krx_quant.kis_orders")

    async with telegram(200) as (url, sent):

        async def act(feed: QuoteFeed, clock: list[datetime]) -> None:
            await until(lambda: decided(state))
            await until(lambda: feed.clients > 0)
            account.bid, account.ask = 70_000, 70_100
            feed.on_event(BOOK.tr_id, book(at("10:00:02.5"), 70_000, 70_100))
            await until(lambda: any(WITHHELD in t for t in sent), timeout=15.0)

        await run_mock(state, fake, url, act=act)

    assert account.placed(kis_orders.SIDE_BUY) == [], "nothing reached KIS"
    assert not [tr for _, tr, _, _ in fake.posts_received if tr == kis_orders.BUY.mock]
    failures, buys = failed_buys(state)
    assert failures == [
        (
            "buy",
            "error",
            "buy: withheld by the broker's last check (#543): its amount with the 0 buy(s) of the day that"
            " may still fill is 140% of the cap",
        )
    ]
    assert buys == [UNSENT]
    [warning] = warned(caplog)
    assert "a buy of 20 share(s) of 005930 at 70100 (1,402,000 won" in warning
    assert "the cap 1,000,000 won" in warning


async def test_a_live_buy_above_the_first_period_s_cap_is_withheld(
    kis_live: tuple[FakeKis, FakeAccount],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Live, in the first period: four shares at 70,100 (280,400 won) are within the 1,000,000 won
    capital but above the period's 250,000 won cap, which the trader tells its broker: withheld."""
    fake, account = kis_live
    state = tmp_path / "state"
    ready(state)
    sized(monkeypatch, 4)
    caplog.set_level(logging.WARNING, logger="krx_quant.kis_orders")

    async with telegram(200) as (url, sent):

        async def act(feed: QuoteFeed, clock: list[datetime]) -> None:
            await until(lambda: decided(state))
            await until(lambda: feed.clients > 0)
            account.bid, account.ask = 70_000, 70_100
            feed.on_event(BOOK.tr_id, book(at("10:00:02.5"), 70_000, 70_100))
            await until(lambda: any(WITHHELD in t for t in sent))

        await run_live(state, fake, account, sent, url, act=act)

    posted = {tr for _, tr, _, _ in fake.posts_received}
    assert kis_orders.BUY.live not in posted, "nothing bought on the live account"
    failures, buys = failed_buys(state)
    assert [(what, outcome) for what, outcome, _ in failures] == [("buy", "error")]
    assert failures[0][2].endswith("is 112% of the cap")
    assert buys == [UNSENT]
    [warning] = warned(caplog)
    assert "the cap 250,000 won" in warning
    assert any(f"자동 매매({LIVE}): {WITHHELD}" in t for t in sent)


async def test_the_live_watch_tells_the_broker_the_first_period_s_cap_and_the_capital_once_lifted(
    kis_live: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """At each judgment (the start, each pick) the live broker's cap is the ramp's: 250,000 won in the
    first period; the capital once twenty live days and thirty round trips lift it."""
    fake, _ = kis_live
    live_history(tmp_path, DAYS[:10])
    store = TraderStore(tmp_path / DB_NAME)
    try:
        async with kis.rest_session() as session:
            broker = trader_live_broker(
                session,
                live_settings(tmp_path),
                lambda: at("08:30:00"),
                servers=Servers(fake.rest_url, fake.ws_url),
                limits=BuyLimits(1_000_000, frozenset({SYMBOL})),
            )
            desk = Desk(
                store,
                whole_rules(),
                CALENDAR.session,
                at("08:00:00"),
                LIVE,
                broker=broker,
                clock=lambda: at("08:30:00"),
            )
            watch = LiveWatch(LIVE, LIVE, desk, CALENDAR.session, lambda text: None, broker=broker)
            watch.judge(at("08:30:00"))
            first = broker.cap
            kis_days(tmp_path, LIVE, DAYS[10:], after=10)
            watch.judge(at("08:30:00"))
            lifted = broker.cap
            await desk.close()
    finally:
        store.close()
    assert (first, lifted) == (250_000, 1_000_000)
