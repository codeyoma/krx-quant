"""``krx-quant trade`` (#394): the trader's process on a quote stream and an engine database, its
settings, and that it never writes the engine's database."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from krx_quant.config import Settings, SettingsError
from krx_quant.feeds import BOOK
from krx_quant.predictions import DB_NAME as ENGINE_DB
from krx_quant.predictions import PredictionStore
from krx_quant.trader import CAPITAL
from krx_quant.trader.cells import Cell, open_engine_db
from krx_quant.trader.quotes import QuoteFeed
from krx_quant.trader.run import run_trader
from krx_quant.trader.settings import TraderSettings
from krx_quant.trader.store import DB_NAME as TRADER_DB
from krx_quant.trader.store import read_trades
from tests.test_live_predictions import free_port
from tests.trader_doubles import BARRIER, FEE, Barrier, at


def book(received: datetime, bid: int, ask: int) -> dict[str, Any]:
    return {
        "symbol": "005930",
        "received_at": received,
        "bid_px_1": bid,
        "ask_px_1": ask,
        "bid_qty_1": 9,
        "ask_qty_1": 9,
    }


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def until(ready: Callable[[], bool], seconds: float = 5.0) -> None:
    async with asyncio.timeout(seconds):
        while not ready():
            await asyncio.sleep(0.01)


async def test_the_trader_trades_on_the_stream_and_never_writes_the_engine_s_database(tmp_path: Path) -> None:
    """Input: a barrier prediction of 10:00 in the engine's database (the engine stopped since, so
    nothing else writes it), and the collector's quote stream. Output: the trader buys on the first
    quote after reading it and sells at the target; the engine's database is as it was, byte for byte."""
    state = tmp_path / "state"
    engine = PredictionStore(state / ENGINE_DB)
    Barrier("10:00:00").write(engine)
    engine.close()
    before = digest(state / ENGINE_DB)
    feed = QuoteFeed(["005930"])
    port = free_port()
    runner = web.AppRunner(feed.app())
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    clock = [at("10:00:02.3")]
    settings = Settings(kis_app_key="", kis_app_secret="", state_dir=state, fee_rate=FEE)
    trader = TraderSettings(cell=BARRIER, quotes_url=f"ws://127.0.0.1:{port}/v1/quotes", poll_seconds=0.02)
    stop = asyncio.Event()
    task = asyncio.create_task(run_trader(settings, trader, stop=stop, now=lambda: clock[0]))

    def trades() -> list[dict[str, Any]]:
        return read_trades(state / TRADER_DB, None, 10)

    def decided() -> bool:
        if not (state / TRADER_DB).exists():
            return False
        with closing(sqlite3.connect(state / TRADER_DB)) as db:
            decisions: int = db.execute("SELECT count(*) FROM decisions").fetchone()[0]
        return decisions == 1

    try:
        await until(decided)
        await until(lambda: feed.clients > 0)
        feed.on_event(BOOK.tr_id, book(at("10:00:02.5"), 70_000, 70_100))
        await until(lambda: len(trades()) == 1)
        clock[0] = at("10:02:00")
        feed.on_event(BOOK.tr_id, book(at("10:02:00"), 70_300, 70_400))
        await until(lambda: trades()[0]["exit"] is not None)
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)
        await runner.cleanup()

    [trade] = trades()
    assert trade["entry"]["price"] == 70_100 and trade["exit"]["reason"] == "take_profit"
    assert digest(state / ENGINE_DB) == before, "the trader wrote nothing there"
    wal = state / (ENGINE_DB + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0, "nor into its write-ahead log"


def test_the_engine_s_database_is_opened_read_only(tmp_path: Path) -> None:
    PredictionStore(tmp_path / ENGINE_DB).close()
    db = open_engine_db(tmp_path / ENGINE_DB)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("DELETE FROM predictions")
    finally:
        db.close()


def test_the_trader_s_settings_and_their_defaults() -> None:
    default = TraderSettings.from_env({}, None)
    assert (default.mode, default.cell, default.threshold, default.capital) == (
        "paper",
        Cell("direction", "consensus-final"),
        0.6,
        1_000_000,
    )
    assert default.quotes_url == "ws://127.0.0.1:8081/v1/quotes"
    chosen = TraderSettings.from_env(
        {
            "KRX_TRADER_KIND": "barrier_15m",
            "KRX_TRADER_PREDICTOR": "breiman",
            "KRX_TRADER_THRESHOLD": "0.55",
            "KRX_TRADER_CAPITAL": "500000",
            "KRX_TRADER_QUOTES_URL": "ws://engine:8081/v1/quotes",
        },
        None,
    )
    assert chosen == replace(
        default,
        cell=Cell("barrier_15m", "breiman"),
        threshold=0.55,
        capital=500_000,
        quotes_url="ws://engine:8081/v1/quotes",
    )


@pytest.mark.parametrize(
    ("name", "value", "why"),
    [
        ("KRX_TRADER_CAPITAL", str(CAPITAL + 1), "never widen"),  # .env narrows the limits only (#393)
        ("KRX_TRADER_MODE", "live", "not built yet"),
        ("KRX_TRADER_MODE", "mock", "not built yet"),
        ("KRX_TRADER_MODE", "real", "must be one of"),
        ("KRX_TRADER_KIND", "barrier_60m", "must be one of"),  # not traded (#393)
        ("KRX_TRADER_KIND", "open_to_close", "must be one of"),
        ("KRX_TRADER_THRESHOLD", "1.5", "KRX_TRADER_THRESHOLD"),
        ("KRX_TRADER_CAPITAL", "0", "KRX_TRADER_CAPITAL"),
        ("KRX_TRADER_PREDICTOR", "a b", "KRX_TRADER_PREDICTOR"),
        ("KRX_TRADER_QUOTES_URL", "http://engine:8081/v1/quotes", "ws://"),
    ],
)
def test_a_trader_setting_out_of_bounds_stops_the_trader(name: str, value: str, why: str) -> None:
    with pytest.raises(SettingsError, match=why):
        TraderSettings.from_env({name: value}, None)


def test_a_trader_setting_is_never_the_collector_s(tmp_path: Path) -> None:
    """A wrong trader setting stops the trader alone: the collector does not read it."""
    env = {"KIS_APP_KEY": "k", "KIS_APP_SECRET": "s", "KRX_TRADER_CAPITAL": "99999999"}

    assert Settings.from_env(env, None).symbols == ("005930",)


async def test_the_trader_waits_for_an_engine_database_not_there_yet(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = Settings(kis_app_key="", kis_app_secret="", state_dir=tmp_path, fee_rate=FEE)
    trader = TraderSettings(
        cell=BARRIER, quotes_url=f"ws://127.0.0.1:{free_port()}/v1/quotes", poll_seconds=0.01
    )
    stop = asyncio.Event()
    task = asyncio.create_task(run_trader(settings, trader, stop=stop, now=lambda: at("10:00:00")))
    await until(lambda: "no engine database" in caplog.text)
    assert not task.done(), "it goes on, reading again"
    stop.set()
    await asyncio.wait_for(task, 5)
    assert read_trades(tmp_path / TRADER_DB, None, 10) == []
    assert not (tmp_path / ENGINE_DB).exists(), "not made by the trader"
