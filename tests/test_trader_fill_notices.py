"""The fill notice recorder in the trader's process (#484): in mock mode beside the trading loops,
never in their set, never read by a book; in paper and live it does not run.

``krx-quant trade`` runs as in ``tests/test_trader_mock.py``'s run on the fake KIS, its account
telling each order, fill and cancel on the fill notice in a made-up 23-column layout
(``fake_kis.FakeAccount.notice_columns``). Every value is made up.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from krx_quant import kis_orders
from krx_quant.config import Settings
from krx_quant.fake_kis import FakeAccount, FakeKis
from krx_quant.feeds import BOOK
from krx_quant.kis_orders import FILL_NOTICE_COLUMNS, Servers, same_number
from krx_quant.predictions import DB_NAME as ENGINE_DB
from krx_quant.predictions import PredictionStore
from krx_quant.trader import MOCK, PAPER, SYMBOL
from krx_quant.trader.fill_notices import DB_NAME as NOTICES_DB
from krx_quant.trader.fill_notices import TR_ID, Pace, read_recorded
from krx_quant.trader.paper import TAKE_PROFIT
from krx_quant.trader.quotes import QuoteFeed
from krx_quant.trader.run import run_trader
from krx_quant.trader.settings import TraderSettings
from krx_quant.trader.store import DB_NAME, read_trades
from tests.kis_doubles import TIMING, kis_mock, live_settings, resting_sells, settings_of
from tests.server_doubles import free_port, telegram
from tests.test_trader_live import decided, kis_live, ready, run_live
from tests.trader_doubles import PASSING, Barrier, EngineApi, at, book, first_champion
from tests.waiting import until

HTS_ID = "notice7hts"
# A made-up 23 of the example's 26 columns, not its first 23: the price of the order last.
LAYOUT = tuple(c for c in FILL_NOTICE_COLUMNS if c not in ("ORD_COND_PRC", "ORD_EXG_GB", "POPUP_YN"))
PACE = Pace(backoff=0.05, most=0.2, silence=5.0, heard=0.0, close=0.2, look=0.02)

__all__ = ["kis_live", "kis_mock"]  # fixtures


async def trade_on_kis(
    fake: FakeKis,
    account: FakeAccount,
    state: Path,
    settings: Settings,
    *,
    before: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """The 10:00 rise bought on KIS at the first quote after it and its take-profit filled: the trade.
    The quote comes once ``before`` holds (the recorder subscribed, or failed)."""
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
    account.clock = lambda: clock[0]
    api = EngineApi([PASSING])

    def trades() -> list[dict[str, Any]]:
        return read_trades(state / DB_NAME, None, 10) if (state / DB_NAME).exists() else []

    def decisions() -> bool:
        if not (state / DB_NAME).exists():
            return False
        with closing(sqlite3.connect(state / DB_NAME)) as db:
            return bool(db.execute("SELECT count(*) FROM decisions").fetchone()[0])

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
                    settings,
                    trader,
                    stop=stop,
                    now=lambda: clock[0],
                    servers=Servers(fake.rest_url, fake.ws_url),
                    timing=TIMING,
                    notice_pace=PACE,
                )
            )
            try:
                await until(decisions)
                await until(lambda: feed.clients > 0)
                if before is not None:
                    await until(before, timeout=15.0)
                account.bid, account.ask = 70_000, 70_100
                feed.on_event(BOOK.tr_id, book(at("10:00:02.5"), 70_000, 70_100))
                await until(lambda: len(resting_sells(account)) == 1, timeout=15.0)
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
    return trade


def the_trade(trade: dict[str, Any]) -> tuple[Any, ...]:
    """The trade as the trader made it: its mode, its entry (KIS's fill, half a tick under the ask;
    nine shares on the loss budget, #471) and its exit."""
    return (trade["mode"], trade["entry"]["price"], trade["entry"]["quantity"], trade["exit"]["reason"])


@pytest.mark.timeout(60)  # a trade on KIS at the mock broker's pace, one call in any 1.1 s
async def test_the_mock_trader_records_its_orders_fill_notices_and_trades_as_ever(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    account.notice_columns = LAYOUT
    state = tmp_path / "state"

    trade = await trade_on_kis(
        fake,
        account,
        state,
        replace(settings_of(fake, state), trader_kis_hts_id=HTS_ID),
        before=lambda: fake.subscription_count((TR_ID, HTS_ID)) > 0,
    )

    assert the_trade(trade) == (MOCK, 70_050, 9, TAKE_PROFIT)
    numbers = [o["broker_id"] for o in trade["orders"]]
    assert len(numbers) == 2 and all(numbers)
    kept = read_recorded(state)
    assert kept is not None
    told = {n for n in numbers if any(any(same_number(v, n) for v in r.fields) for r in kept.records)}
    assert told == set(numbers), "the buy's and the take-profit's notices"
    assert {len(r.fields) for r in kept.records} == {23}
    assert {r.tr_id for r in kept.records} == {TR_ID} and kept.odd == []
    assert fake.approvals_issued == ["fake-approval-key-1"]


@pytest.mark.parametrize("failing", ["the WebSocket refused", "the approval key refused", "no store"])
@pytest.mark.timeout(60)
async def test_a_recorder_that_fails_stops_neither_the_start_nor_the_trading(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path, caplog: pytest.LogCaptureFixture, failing: str
) -> None:
    fake, account = kis_mock
    state = tmp_path / "state"
    state.mkdir()
    if failing == "the WebSocket refused":
        fake.accept_ws = False
    elif failing == "the approval key refused":
        fake.approval_refusals = 1_000
    else:
        (state / NOTICES_DB).mkdir()
    caplog.set_level(logging.WARNING, logger="krx_quant.trader.fill_notices")

    def said() -> bool:
        return any(
            r.name == "krx_quant.trader.fill_notices" and r.levelno == logging.WARNING for r in caplog.records
        )

    trade = await trade_on_kis(
        fake, account, state, replace(settings_of(fake, state), trader_kis_hts_id=HTS_ID), before=said
    )

    assert the_trade(trade) == (MOCK, 70_050, 9, TAKE_PROFIT)
    assert fake.subscription_count((TR_ID, HTS_ID)) == 0


@pytest.mark.timeout(60)
async def test_without_an_hts_id_the_mock_trader_trades_as_ever(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    state = tmp_path / "state"

    trade = await trade_on_kis(fake, account, state, settings_of(fake, state))

    assert the_trade(trade) == (MOCK, 70_050, 9, TAKE_PROFIT)
    assert fake.ws_connections == 0 and not (state / NOTICES_DB).exists()


async def test_the_paper_trader_records_no_fill_notice(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, _ = kis_mock
    settings = replace(settings_of(fake, tmp_path), trader_kis_hts_id=HTS_ID)
    trader = TraderSettings(
        mode=PAPER,
        quotes_url=f"ws://127.0.0.1:{free_port()}/v1/quotes",
        engine_url=f"http://127.0.0.1:{free_port()}",
        poll_seconds=0.01,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(run_trader(settings, trader, stop=stop, now=lambda: at("10:00:00")))
    await until(lambda: (tmp_path / DB_NAME).exists())
    stop.set()
    await asyncio.wait_for(task, 10)

    assert fake.ws_connections == 0 and fake.approvals_issued == []
    assert not (tmp_path / NOTICES_DB).exists()


@pytest.mark.timeout(60)
async def test_the_live_trader_records_no_fill_notice(
    kis_live: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """Past the gate and the checks it orders on the live server; the recorder is the mock mode's."""
    fake, account = kis_live
    state = tmp_path / "state"
    ready(state)

    async def act(feed: QuoteFeed, clock: list[Any]) -> None:
        await until(lambda: decided(state))
        await until(lambda: feed.clients > 0)
        account.bid, account.ask = 70_000, 70_100
        feed.on_event(BOOK.tr_id, book(at("10:00:02.5"), 70_000, 70_100))
        await until(lambda: len(account.resting(kis_orders.SIDE_SELL)) == 1)

    async with telegram(200) as (url, sent):
        await run_live(
            state, fake, account, sent, url, act=act, settings=live_settings(state, trader_kis_hts_id=HTS_ID)
        )

    assert fake.ws_connections == 0 and fake.approvals_issued == []
    assert not (state / NOTICES_DB).exists()
