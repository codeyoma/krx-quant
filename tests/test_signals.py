"""Telegram signal alerts: a final consensus sure enough of a move that enough of its
members agree on. Off by default.

Input: an engine on a fake clock with active stand-in algorithms, fed a morning, and a
fake Telegram. Output: the messages Telegram receives.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from krx_quant.baselines import Forecast
from krx_quant.clock import FakeClock
from krx_quant.config import Settings, SettingsError
from krx_quant.engine import PredictionEngine
from krx_quant.lifecycle import Status
from krx_quant.notify import Alerts
from krx_quant.predictions import PredictionStore
from krx_quant.replay import feed
from krx_quant.signals import SignalRule, Signals
from tests.algorithm_doubles import FixedAlgorithm
from tests.replay_events import CALENDAR, FEE, SYMBOL, at
from tests.test_replay import busy_morning

UP = Forecast(0.7, 0.2, 0.1, 2.0)
FLAT = Forecast(0.3, 0.4, 0.3, 0.0)


@pytest.fixture
async def telegram() -> AsyncIterator[tuple[Alerts, list[str]]]:
    sent: list[str] = []

    async def receive(request: web.Request) -> web.Response:
        sent.append((await request.json())["text"])
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/bottoken/sendMessage", receive)
    server = TestServer(app)
    await server.start_server()
    async with aiohttp.ClientSession() as session:
        alerts = Alerts(session, "token", "chat", str(server.make_url("")).rstrip("/"))
        yield alerts, sent
        await alerts.close()
    await server.close()


async def morning(db: Path, forecasts: list[Forecast], signals: Signals | None) -> None:
    """The members predict ``forecasts`` at every bar close from 09:01 to 09:05."""
    store = PredictionStore(db)
    algorithms = []
    for n, (forecast, feature) in enumerate(
        zip(forecasts, ("velocity", "acceleration", "relative_volume"), strict=False)
    ):
        algorithms.append(FixedAlgorithm(f"m{n}", features=(feature,), unique=(feature,), forecast=forecast))
        store.add_status(f"m{n}", at("08:00:00"), None, Status.ACTIVE, "test")
    clock = FakeClock(at("09:00:00"))
    engine = PredictionEngine(store, CALENDAR, (SYMBOL,), FEE, clock, algorithms=algorithms, signals=signals)
    await feed(engine, clock, busy_morning(end="09:05:30"), until=at("09:05:30"))
    store.close()


async def test_a_sure_consensus_that_enough_members_agree_on_is_signalled_once_per_move(
    tmp_path: Path, telegram: tuple[Alerts, list[str]]
) -> None:
    alerts, sent = telegram

    await morning(tmp_path / "engine.db", [UP, UP, FLAT], Signals(SignalRule(0.5, 2), alerts.signal))
    await alerts.close()

    # The consensus is 57% up and two of three say up. 09:02–09:04 repeat the 09:01 call
    # while it is still running; at 09:05 it has ended.
    assert sent == [
        "[신호] 005930 상승 57% · 정식 3개 중 2개 같은 방향 · 09:01 봉 마감, 09:05까지",
        "[신호] 005930 상승 57% · 정식 3개 중 2개 같은 방향 · 09:05 봉 마감, 09:09까지",
    ]


async def test_no_signal_without_enough_agreement_or_certainty(
    tmp_path: Path, telegram: tuple[Alerts, list[str]]
) -> None:
    alerts, sent = telegram

    await morning(tmp_path / "few.db", [UP, UP, FLAT], Signals(SignalRule(0.5, 3), alerts.signal))
    await morning(tmp_path / "unsure.db", [UP, UP, FLAT], Signals(SignalRule(0.6, 2), alerts.signal))
    await alerts.close()

    assert sent == []


async def test_signals_are_off_by_default(tmp_path: Path, telegram: tuple[Alerts, list[str]]) -> None:
    alerts, sent = telegram
    keys = {"KIS_APP_KEY": "key", "KIS_APP_SECRET": "secret"}

    assert Settings.from_env(keys, dotenv_path=None).signal_rule is None
    on = Settings.from_env(
        {**keys, "KRX_SIGNAL_ALERT": "on", "KRX_SIGNAL_MIN_AGREE": "3"}, dotenv_path=None
    ).signal_rule
    assert on == SignalRule(min_probability=0.6, min_agree=3)
    with pytest.raises(SettingsError, match="KRX_SIGNAL_MIN_PROBABILITY"):
        Settings.from_env(
            {**keys, "KRX_SIGNAL_ALERT": "on", "KRX_SIGNAL_MIN_PROBABILITY": "0.4"}, dotenv_path=None
        )
    await morning(tmp_path / "engine.db", [UP, UP, UP], None)
    await alerts.close()
    assert sent == []
