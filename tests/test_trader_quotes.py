"""The collector's quote stream to the trader (#394): the traded symbol's best quotes, on a
listener of their own that compose does not publish (``trader/quotes.py``)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from krx_quant.api import create_app
from krx_quant.clock import FakeClock
from krx_quant.collector import run_collector
from krx_quant.config import Settings, SettingsError
from krx_quant.fake_kis import FakeKis
from krx_quant.feeds import BOOK, KRX_TRADE, TRADE
from krx_quant.trader.quotes import BestQuote, QuoteFeed, follow_quotes, parse_quote, quote_json, quote_of
from tests.kis_frames import book_frame
from tests.replay_events import at, run
from tests.test_collector import settings_for
from tests.test_live_predictions import free_port, poll

RECEIVED = datetime(2026, 10, 14, 1, 0, 2, 500_000, tzinfo=UTC)  # 10:00:02.5 KST


def book(
    symbol: str = "005930", bid: int = 70_000, ask: int = 70_100, received: datetime = RECEIVED
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "received_at": received,
        "bid_px_1": bid,
        "ask_px_1": ask,
        "bid_qty_1": 30,
        "ask_qty_1": 12,
    }


def test_a_quote_is_the_integrated_book_s_or_trade_s_best_bid_and_ask() -> None:
    assert quote_of(BOOK.tr_id, book()) == BestQuote(RECEIVED, 70_000, 70_100, 30, 12)
    trade = {"symbol": "005930", "received_at": RECEIVED, "bid1": 70_000, "ask1": 70_100, "price": 70_100}
    assert quote_of(TRADE.tr_id, trade) == BestQuote(RECEIVED, 70_000, 70_100, None, None), "no sizes"
    assert quote_of(BOOK.tr_id, book(bid=0)) is None, "one-sided"
    assert quote_of(KRX_TRADE.tr_id, trade) is None, "the KRX-only trades are not the integrated book"
    crossed = quote_of(BOOK.tr_id, book(bid=70_200, ask=70_100))
    assert crossed is not None and crossed.ask < crossed.bid, "kept, as the barrier counts it (#187)"


def test_a_message_is_read_back_into_its_quote() -> None:
    sent = quote_of(BOOK.tr_id, book())
    assert sent is not None
    text = json.dumps(quote_json("005930", BOOK.tr_id, sent))

    assert json.loads(text) == {
        "symbol": "005930",
        "feed": "H0UNASP0",
        "received_at": "2026-10-14T10:00:02.500000+09:00",
        "bid": 70_000,
        "ask": 70_100,
        "bid_size": 30,
        "ask_size": 12,
    }
    assert parse_quote(text, "005930") == sent
    assert parse_quote(text, "000660") is None, "another symbol's"
    assert parse_quote("not json", "005930") is None


async def test_a_client_gets_the_latest_quote_then_each_as_it_comes_of_the_traded_symbol_only() -> None:
    feed = QuoteFeed(["005930"])
    feed.on_event(BOOK.tr_id, book(bid=69_900, ask=70_000))
    got: list[BestQuote] = []
    async with TestClient(TestServer(feed.app())) as client:
        url = str(client.make_url("/v1/quotes")).replace("http", "ws", 1)
        task = asyncio.create_task(follow_quotes(client.session, url, "005930", got.append))
        try:
            async with asyncio.timeout(5):
                while not got:
                    await asyncio.sleep(0.01)
                feed.on_event(BOOK.tr_id, book(symbol="000660"))  # not traded: not streamed
                feed.on_event(BOOK.tr_id, book(received=RECEIVED.replace(second=3)))
                while len(got) < 2:
                    await asyncio.sleep(0.01)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert [(q.bid, q.ask) for q in got] == [(69_900, 70_000), (70_000, 70_100)]


async def test_the_trader_connects_again_once_the_stream_is_back(caplog: pytest.LogCaptureFixture) -> None:
    port = free_port()
    got: list[BestQuote] = []
    feed = QuoteFeed(["005930"])
    feed.on_event(BOOK.tr_id, book())
    async with aiohttp.ClientSession() as session:
        task = asyncio.create_task(
            follow_quotes(
                session, f"ws://127.0.0.1:{port}/v1/quotes", "005930", got.append, retry_seconds=0.05
            )
        )
        async with asyncio.timeout(5):  # nothing listens yet: it says so, and tries again
            while "connecting again" not in caplog.text:
                await asyncio.sleep(0.01)
        runner = web.AppRunner(feed.app())
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        try:
            async with asyncio.timeout(5):
                while not got:
                    await asyncio.sleep(0.01)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await runner.cleanup()

    assert got == [quote_of(BOOK.tr_id, book())]


async def test_the_collector_streams_the_quotes_on_their_own_port_not_on_the_engine_api_s(
    kis: FakeKis, tmp_path: Path
) -> None:
    api_port, quotes_port = free_port(), free_port()
    settings = replace(settings_for(kis, tmp_path), api_port=api_port, quotes_port=quotes_port)
    kis.script[("H0UNASP0", "005930")] = [
        book_frame(
            {
                "MKSC_SHRN_ISCD": "005930",
                "BSOP_HOUR": "093010",
                "BIDP1": "81200",
                "ASKP1": "81300",
                "BIDP_RSQN1": "40",
                "ASKP_RSQN1": "25",
            }
        )
    ]
    stop = asyncio.Event()
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=FakeClock(at("09:30:10"))))
    got: list[BestQuote] = []
    try:
        await kis.wait_for_subscriptions(set(kis.script))
        await kis.round_trip()
        await poll(f"http://127.0.0.1:{api_port}/v1/status", lambda b: True)
        async with aiohttp.ClientSession() as session:
            follower = asyncio.create_task(
                follow_quotes(session, f"ws://127.0.0.1:{quotes_port}/v1/quotes", "005930", got.append)
            )
            try:
                async with asyncio.timeout(5):
                    while not got:
                        await asyncio.sleep(0.01)
            finally:
                follower.cancel()
                await asyncio.gather(follower, return_exceptions=True)
            async with session.get(f"http://127.0.0.1:{api_port}/v1/quotes") as published:
                on_the_api = published.status
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)

    assert got == [BestQuote(at("09:30:10"), 81_200, 81_300, 40, 25)]
    assert on_the_api == 404, "the Engine API's port, the one compose publishes, has no quotes"


async def test_the_engine_api_serves_no_quotes(tmp_path: Path) -> None:
    engine = await run(tmp_path / "engine.db", [], until=at("09:00:00"))

    async with TestClient(TestServer(create_app(engine))) as client:
        response = await client.get("/v1/quotes")

    assert response.status == 404


def test_the_quote_port_is_its_own_and_may_be_off() -> None:
    env = {"KIS_APP_KEY": "k", "KIS_APP_SECRET": "s"}

    default = Settings.from_env(env, None)
    assert (default.quotes_host, default.quotes_port, default.api_port) == ("127.0.0.1", 8081, 8080)
    assert Settings.from_env({**env, "KRX_QUOTES_PORT": "off"}, None).quotes_port is None
    moved = Settings.from_env({**env, "KRX_API_PORT": "8081"}, None)
    assert (moved.api_port, moved.quotes_port) == (8081, 8082), "the port after the API's unless set"
    assert Settings.from_env({**env, "KRX_API_PORT": "65535"}, None).quotes_port == 65534
    with pytest.raises(SettingsError, match="KRX_QUOTES_PORT"):
        Settings.from_env({**env, "KRX_QUOTES_PORT": "8080"}, None)
    with pytest.raises(SettingsError, match="KRX_QUOTES_PORT"):
        Settings.from_env({**env, "KRX_API_PORT": "9000", "KRX_QUOTES_PORT": "9000"}, None)
