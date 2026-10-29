"""``krx-quant trade`` (#394, #395, #396): the trader's process on a quote stream, an engine database
and the Engine API's cost map, its settings, that it never writes the engine's database, and the
pauses, resumes and re-arms it reads from its store (the screen's through the Engine API, the
server's through ``krx-quant trade resume|rearm``), with their notices on Telegram."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from krx_quant import cli
from krx_quant.api import create_app
from krx_quant.config import Settings, SettingsError
from krx_quant.feeds import BOOK
from krx_quant.predictions import DB_NAME as ENGINE_DB
from krx_quant.predictions import PredictionStore
from krx_quant.trader import CAPITAL
from krx_quant.trader.cells import open_engine_db
from krx_quant.trader.limits import ask
from krx_quant.trader.quotes import QuoteFeed
from krx_quant.trader.run import next_pick, run_trader
from krx_quant.trader.selection import NO_COST_MAP, NO_PASS
from krx_quant.trader.settings import TraderSettings
from krx_quant.trader.store import CUMULATIVE_LIMIT, TraderStore, read_trades, request_pause
from krx_quant.trader.store import DB_NAME as TRADER_DB
from tests.replay_events import at as replay_at
from tests.replay_events import run
from tests.test_live_predictions import free_port
from tests.test_notify import telegram
from tests.trader_doubles import FEE, Barrier, at, cost_map, cost_row, predictor

PASSING = cost_map([cost_row("barrier", top=predictor("breiman", 0.70, "pass"))])
FAILING = cost_map([cost_row("barrier", top=predictor("breiman", 0.60, "fail"))])


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


@dataclass
class EngineApi:
    """The Engine API's cost map, as the engine service answers inside the compose network: each
    request gets the next of ``answers`` (a body, or a status alone), the last one again after."""

    answers: list[dict[str, Any] | int]
    asked: list[dict[str, str]] = field(default_factory=list)

    async def _cost_map(self, request: web.Request) -> web.Response:
        self.asked.append(dict(request.query))
        answer = self.answers[min(len(self.asked), len(self.answers)) - 1]
        if isinstance(answer, int):
            return web.json_response({"error": "starting"}, status=answer)
        return web.json_response(answer)

    @asynccontextmanager
    async def serving(self) -> AsyncIterator[str]:
        app = web.Application()
        app.router.add_get("/v1/cost-map", self._cost_map)
        runner = web.AppRunner(app)
        await runner.setup()
        port = free_port()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            await runner.cleanup()


def rows(state: Path, table: str) -> list[dict[str, Any]]:
    if not (state / TRADER_DB).exists():
        return []
    with closing(sqlite3.connect(state / TRADER_DB)) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY id")]


async def test_the_trader_trades_on_the_stream_and_never_writes_the_engine_s_database(tmp_path: Path) -> None:
    """Input: a barrier prediction of 10:00 in the engine's database (the engine stopped since, so
    nothing else writes it), the collector's quote stream, and a cost map passing the barrier's
    breiman. Output: the trader picks it, buys on the first quote after reading its prediction and
    sells at the target; the engine's database is as it was, byte for byte."""
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
    api = EngineApi([PASSING])
    try:
        async with api.serving() as engine_url:
            trader = TraderSettings(
                quotes_url=f"ws://127.0.0.1:{port}/v1/quotes", engine_url=engine_url, poll_seconds=0.02
            )
            stop = asyncio.Event()
            task = asyncio.create_task(run_trader(settings, trader, stop=stop, now=lambda: clock[0]))
            await trade_on_the_stream(state, feed, clock, task, stop)
    finally:
        await runner.cleanup()

    [trade] = read_trades(state / TRADER_DB, None, 10)
    assert trade["entry"]["price"] == 70_100 and trade["exit"]["reason"] == "take_profit"
    assert (trade["predictor"], trade["role"]) == ("breiman", "top")
    assert api.asked == [{"symbol": "005930", "period": "20d", "special": "exclude"}]
    assert digest(state / ENGINE_DB) == before, "the trader wrote nothing there"
    wal = state / (ENGINE_DB + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0, "nor into its write-ahead log"


async def trade_on_the_stream(
    state: Path, feed: QuoteFeed, clock: list[datetime], task: asyncio.Task[None], stop: asyncio.Event
) -> None:
    """The 10:00 prediction bought at 10:00:02.5 and sold at 10:02, once the trader decided on it."""

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


async def test_without_a_cost_map_no_cell_trades_until_it_is_read(tmp_path: Path) -> None:
    """The engine is starting (503) when the trader starts: its 10:00 prediction is not read, and the
    failure is kept once. Asked again, the cost map passes the cell, and the prediction, still fresh,
    is decided on."""
    state = tmp_path / "state"
    engine = PredictionStore(state / ENGINE_DB)
    Barrier("10:00:00").write(engine)
    engine.close()
    settings = Settings(kis_app_key="", kis_app_secret="", state_dir=state, fee_rate=FEE)
    api = EngineApi([503, 503, 503, PASSING])
    async with api.serving() as engine_url:
        trader = TraderSettings(
            quotes_url=f"ws://127.0.0.1:{free_port()}/v1/quotes", engine_url=engine_url, poll_seconds=0.01
        )
        stop = asyncio.Event()
        task = asyncio.create_task(
            run_trader(
                settings,
                trader,
                stop=stop,
                now=lambda: at("10:00:02.3"),
                sleep=lambda seconds: asyncio.sleep(min(seconds, 0.02)),
            )
        )
        try:
            await until(lambda: len(rows(state, "decisions")) == 1)
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)

    assert len(api.asked) == 4
    first, then = rows(state, "selections")
    assert (first["reason"], first["picks"]) == (NO_COST_MAP, "[]")
    assert "HTTP 503" in first["detail"]
    assert then["reason"] is None and '"predictor": "breiman"' in then["picks"]
    [decision] = rows(state, "decisions")
    assert (decision["action"], decision["selection_id"]) == ("buy", then["id"])


async def test_the_cells_are_picked_again_every_day_at_08_30(tmp_path: Path) -> None:
    """Started at 08:00 on a cost map passing none; at 08:30 it passes the barrier's breiman. The
    clock moves as the trader's loops wait."""
    clock = [at("08:00:00")]

    async def sleep(seconds: float) -> None:
        clock[0] += timedelta(seconds=seconds)
        await asyncio.sleep(0)

    settings = Settings(kis_app_key="", kis_app_secret="", state_dir=tmp_path, fee_rate=FEE)
    api = EngineApi([FAILING, PASSING])
    async with api.serving() as engine_url:
        trader = TraderSettings(
            quotes_url=f"ws://127.0.0.1:{free_port()}/v1/quotes", engine_url=engine_url, poll_seconds=0.5
        )
        stop = asyncio.Event()
        task = asyncio.create_task(run_trader(settings, trader, stop=stop, now=lambda: clock[0], sleep=sleep))
        try:
            await until(lambda: len(rows(tmp_path, "selections")) == 2, seconds=20)
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)

    first, again = rows(tmp_path, "selections")
    assert first["at"] < "2026-10-14T08:30:00+09:00" and first["reason"] == NO_PASS
    assert again["at"] >= "2026-10-14T08:30:00+09:00" and again["reason"] is None


def test_the_next_pick_is_the_first_08_30_after_now() -> None:
    assert next_pick(at("08:29:59")) == at("08:30:00")
    assert next_pick(at("08:30:00")) == at("08:30:00") + timedelta(days=1)
    assert next_pick(at("15:00:00")) == at("08:30:00") + timedelta(days=1)


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
    assert (default.mode, default.threshold, default.capital, default.ignored) == (
        "paper",
        0.6,
        1_000_000,
        (),
    )
    assert default.quotes_url == "ws://127.0.0.1:8081/v1/quotes"
    assert default.engine_url == "http://127.0.0.1:8080"
    chosen = TraderSettings.from_env(
        {
            "KRX_TRADER_THRESHOLD": "0.55",
            "KRX_TRADER_CAPITAL": "500000",
            "KRX_TRADER_QUOTES_URL": "ws://engine:8081/v1/quotes",
            "KRX_TRADER_ENGINE_URL": "http://engine:8080/",
        },
        None,
    )
    assert chosen == replace(
        default,
        threshold=0.55,
        capital=500_000,
        quotes_url="ws://engine:8081/v1/quotes",
        engine_url="http://engine:8080",
    )


async def test_the_cell_settings_of_394_are_no_longer_read(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """#395: the cost map picks the cells. A server .env still naming one is not refused: the trader
    starts and says the settings are not read."""
    found = TraderSettings.from_env(
        {"KRX_TRADER_KIND": "barrier_15m", "KRX_TRADER_PREDICTOR": "breiman"}, None
    )
    assert found.ignored == ("KRX_TRADER_KIND", "KRX_TRADER_PREDICTOR")
    settings = Settings(kis_app_key="", kis_app_secret="", state_dir=tmp_path, fee_rate=FEE)
    trader = replace(
        found,
        quotes_url=f"ws://127.0.0.1:{free_port()}/v1/quotes",
        engine_url=f"http://127.0.0.1:{free_port()}",
        poll_seconds=0.01,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(run_trader(settings, trader, stop=stop, now=lambda: at("10:00:00")))
    await until(lambda: "KRX_TRADER_PREDICTOR is no longer read" in caplog.text)
    stop.set()
    await asyncio.wait_for(task, 5)
    assert "KRX_TRADER_KIND is no longer read" in caplog.text


@pytest.mark.parametrize(
    ("name", "value", "why"),
    [
        ("KRX_TRADER_CAPITAL", str(CAPITAL + 1), "never widen"),  # .env narrows the limits only (#393)
        ("KRX_TRADER_DAILY_LOSS_PERCENT", "10.5", "never widen"),  # #396
        ("KRX_TRADER_CUMULATIVE_LOSS_PERCENT", "25", "never widen"),
        ("KRX_TRADER_DAILY_LOSS_PERCENT", "0", "above 0"),
        ("KRX_TRADER_CUMULATIVE_LOSS_PERCENT", "-1", "KRX_TRADER_CUMULATIVE_LOSS_PERCENT"),
        ("KRX_TRADER_DAILY_LOSS_PERCENT", "ten", "a number"),
        ("KRX_TRADER_MODE", "live", "not built yet"),
        ("KRX_TRADER_MODE", "mock", "not built yet"),
        ("KRX_TRADER_MODE", "real", "must be one of"),
        ("KRX_TRADER_THRESHOLD", "1.5", "KRX_TRADER_THRESHOLD"),
        ("KRX_TRADER_CAPITAL", "0", "KRX_TRADER_CAPITAL"),
        ("KRX_TRADER_QUOTES_URL", "http://engine:8081/v1/quotes", "ws://"),
        ("KRX_TRADER_ENGINE_URL", "ws://engine:8080", "http://host:port"),
        ("KRX_TRADER_ENGINE_URL", "http://engine:8080/v1", "without a path"),
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
        quotes_url=f"ws://127.0.0.1:{free_port()}/v1/quotes",
        engine_url=f"http://127.0.0.1:{free_port()}",
        poll_seconds=0.01,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(run_trader(settings, trader, stop=stop, now=lambda: at("10:00:00")))
    await until(lambda: "no engine database" in caplog.text)
    assert not task.done(), "it goes on, reading again"
    stop.set()
    await asyncio.wait_for(task, 5)
    assert read_trades(tmp_path / TRADER_DB, None, 10) == []
    assert not (tmp_path / ENGINE_DB).exists(), "not made by the trader"


def test_the_loss_limits_default_to_the_code_s_and_env_narrows_them() -> None:
    """#393, #396: 10% a day and 20% in all of the capital; ``.env`` may narrow them, never widen them."""
    default = TraderSettings.from_env({}, None)
    assert (default.daily_loss_percent, default.cumulative_loss_percent) == (10.0, 20.0)
    narrowed = TraderSettings.from_env(
        {"KRX_TRADER_DAILY_LOSS_PERCENT": "5", "KRX_TRADER_CUMULATIVE_LOSS_PERCENT": "12.5"}, None
    )
    assert (narrowed.daily_loss_percent, narrowed.cumulative_loss_percent) == (5.0, 12.5)
    same = TraderSettings.from_env(
        {"KRX_TRADER_DAILY_LOSS_PERCENT": "10", "KRX_TRADER_CUMULATIVE_LOSS_PERCENT": "20"}, None
    )
    assert same == default


async def test_the_screen_s_pause_reaches_the_running_trader_and_telegram(tmp_path: Path) -> None:
    """The screen pauses the trader through the Engine API, which writes the pause into the trader's
    store; the running trader reads it (within ``CONTROL_SECONDS``), and says so on Telegram.
    ``krx-quant trade resume`` on the server lifts it the same way."""
    state = tmp_path / "state"
    async with telegram(200) as (url, sent):
        settings = Settings(
            kis_app_key="",
            kis_app_secret="",
            state_dir=state,
            fee_rate=FEE,
            telegram_bot_token="123:fake",
            telegram_chat_id="42",
            telegram_api_url=url,
        )
        trader = TraderSettings(
            quotes_url=f"ws://127.0.0.1:{free_port()}/v1/quotes",
            engine_url=f"http://127.0.0.1:{free_port()}",
            poll_seconds=0.01,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(run_trader(settings, trader, stop=stop, now=lambda: at("10:00:00")))
        engine = await run(tmp_path / "replay.db", [], until=replay_at("09:00:00"))
        try:
            async with TestClient(TestServer(create_app(engine, state_dir=state))) as client:
                await until(lambda: (state / TRADER_DB).exists())
                response = await client.post("/v1/trader/pause", json={"sell_now": False})
                assert response.status == 202
                await until(lambda: status(state) == "entries_paused")
                await until(lambda: any("새 진입 정지" in text for text in sent))
                server = TraderStore(state / TRADER_DB)
                try:
                    resume = ask(server, "resume", "paper", at("10:00:01"))
                finally:
                    server.close()
                assert not isinstance(resume, str)
                await until(lambda: status(state) == "running")
                await until(lambda: any("정지를 풀었습니다" in text for text in sent))
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)
    controls = rows(state, "controls")
    assert [(c["action"], c["source"]) for c in controls] == [("pause", "api"), ("resume", "cli")]
    assert all(c["applied_at"] is not None for c in controls)
    assert all(text.startswith("[알림] 자동 매매(paper): ") for text in sent)


def status(state: Path) -> str | None:
    snapshot = rows(state, "snapshot")
    return None if not snapshot else json.loads(snapshot[0]["body"])["status"]


def test_trade_resume_and_rearm_on_the_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``krx-quant trade resume|rearm`` writes the control for the running trader to read; with
    nothing to resume or re-arm it writes nothing and exits 1. With no trader running it says the
    trader reads it when it runs."""
    monkeypatch.chdir(tmp_path)  # no .env
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "CONTROL_SECONDS", 0.01)
    path = tmp_path / "state" / TRADER_DB

    assert cli.main(["trade", "resume"]) == 1
    assert "not paused: nothing to resume" in capsys.readouterr().out
    assert cli.main(["trade", "rearm"]) == 1
    assert "has not reached its cumulative loss limit" in capsys.readouterr().out

    request_pause(path, True, at("10:00:00"))
    assert cli.main(["trade", "resume"]) == 0
    out = capsys.readouterr().out
    assert "resume asked for (control 2)" in out and "has not read it yet" in out

    store = TraderStore(path)
    try:
        store.add_halt(at("10:00:00"), "paper", CUMULATIVE_LIMIT, -200_100, -200_100, 0, 200_000)
    finally:
        store.close()
    assert cli.main(["trade", "rearm"]) == 0
    assert "rearm asked for (control 3)" in capsys.readouterr().out
    assert cli.main(["trade", "rearm"]) == 1, "one waiting already"
    assert "waiting for the trader to read it already" in capsys.readouterr().out
    assert [(c["action"], c["mode"]) for c in rows(tmp_path / "state", "controls")] == [
        ("pause", None),
        ("resume", None),
        ("rearm", "paper"),
    ]
