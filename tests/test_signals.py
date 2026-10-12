"""Telegram signal alerts: a final consensus sure enough of a move that enough of its
members agree on. Off by default.

Input: an engine on a fake clock with active stand-in algorithms, fed a morning, and a
fake Telegram. Output: the messages Telegram receives and the signals the store keeps,
sent and blocked (``runs``).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable
from datetime import timedelta
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from krx_quant.baselines import Forecast
from krx_quant.clock import FakeClock
from krx_quant.config import Settings, SettingsError
from krx_quant.engine import Event, PredictionEngine
from krx_quant.lifecycle import Status
from krx_quant.notify import Alerts
from krx_quant.predictions import PredictionStore
from krx_quant.replay import replay_into
from krx_quant.signals import SignalRule, Signals
from tests.algorithm_doubles import FixedAlgorithm
from tests.replay_events import CALENDAR, FEE, SYMBOL, at, every, kospi, kospi200_futures, vi
from tests.test_replay import busy_morning

UP = Forecast(0.7, 0.2, 0.1, 2.0)
FLAT = Forecast(0.3, 0.4, 0.3, 0.0)
DOWN = Forecast(0.1, 0.2, 0.7, -2.0)


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


async def morning(
    db: Path,
    forecasts: list[Forecast],
    signals: Signals | None,
    events: Iterable[Event] | None = None,
    *,
    start: str = "09:00:00",
    until: str = "09:05:30",
    display_delay: timedelta = timedelta(0),
) -> None:
    """The members predict ``forecasts`` at every bar close from ``start`` to ``until``, fed
    ``events`` (a busy morning by default) received from ``start`` on."""
    store = PredictionStore(db)
    algorithms = []
    registered = store.statuses()
    for n, (forecast, feature) in enumerate(
        zip(forecasts, ("velocity", "acceleration", "relative_volume"), strict=False)
    ):
        algorithms.append(FixedAlgorithm(f"m{n}", features=(feature,), unique=(feature,), forecast=forecast))
        if f"m{n}" not in registered:
            store.add_status(f"m{n}", at("08:00:00"), None, Status.ACTIVE, "test")
    clock = FakeClock(at(start))
    engine = PredictionEngine(store, CALENDAR, (SYMBOL,), FEE, clock, algorithms=algorithms, signals=signals)
    fed = busy_morning(end=until) if events is None else events
    later = sorted((e for e in fed if e[1]["received_at"] >= at(start)), key=lambda e: e[1]["received_at"])
    await replay_into(engine, clock, later, until=at(until), display_delay=display_delay)
    store.close()


def kept(db: Path, kind: str) -> list[dict[str, Any]]:
    """The signals the store kept as ``kind`` (``signal_sent``, ``signal_blocked``)."""
    store = PredictionStore(db)
    try:
        return [json.loads(run.detail) for run in store.runs(kind, at("00:00:00"))]
    finally:
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
    first, second = kept(tmp_path / "engine.db", "signal_sent")
    assert first == {
        "symbol": SYMBOL,
        "direction": "up",
        "probability": pytest.approx(0.5667, abs=1e-4),
        "agree": 2,
        "members": 3,
        "bar_close": "2026-10-12T09:01:00+09:00",
        "target_at": "2026-10-12T09:05:00+09:00",
        "rule": {"min_probability": 0.5, "min_agree": 2},
        "blocked": None,
    }
    assert second["bar_close"] == "2026-10-12T09:05:00+09:00"
    assert kept(tmp_path / "engine.db", "signal_blocked") == []


async def test_a_fall_is_signalled_too(tmp_path: Path, telegram: tuple[Alerts, list[str]]) -> None:
    alerts, sent = telegram

    await morning(tmp_path / "engine.db", [DOWN, DOWN], Signals(SignalRule(0.6, 2), alerts.signal))
    await alerts.close()

    assert sent[0] == "[신호] 005930 하락 70% · 정식 2개 중 2개 같은 방향 · 09:01 봉 마감, 09:05까지"


async def test_no_signal_without_enough_agreement_or_certainty(
    tmp_path: Path, telegram: tuple[Alerts, list[str]]
) -> None:
    alerts, sent = telegram

    await morning(tmp_path / "few.db", [UP, UP, FLAT], Signals(SignalRule(0.5, 3), alerts.signal))
    await morning(tmp_path / "unsure.db", [UP, UP, FLAT], Signals(SignalRule(0.6, 2), alerts.signal))
    await alerts.close()

    assert sent == []


async def test_a_vi_learned_after_the_bar_close_blocks_its_signal(
    tmp_path: Path, telegram: tuple[Alerts, list[str]]
) -> None:
    alerts, sent = telegram
    # The VI starts a second after the 09:01 bar close and its status arrives a second
    # later, before the final consensus (displayed 3 seconds after the bar close).
    events = [*busy_morning(end="09:05:30"), vi(at("09:01:02"), "09:01:01")]

    await morning(
        tmp_path / "engine.db",
        [UP, UP, FLAT],
        Signals(SignalRule(0.5, 2), alerts.signal),
        events,
        display_delay=timedelta(seconds=3),
    )
    await alerts.close()

    # 09:02–09:04 fall inside the VI and are not predicted; at 09:05 it is over.
    assert sent == ["[신호] 005930 상승 57% · 정식 3개 중 2개 같은 방향 · 09:05 봉 마감, 09:09까지"]
    [blocked] = kept(tmp_path / "engine.db", "signal_blocked")
    assert (blocked["bar_close"], blocked["direction"], blocked["blocked"]) == (
        "2026-10-12T09:01:00+09:00",
        "up",
        "special",
    )


async def test_a_circuit_breaker_blocks_signals_and_a_sidecar_does_not(
    tmp_path: Path, telegram: tuple[Alerts, list[str]]
) -> None:
    alerts, sent = telegram
    # KOSPI 8.5% down, or KOSPI200 futures 5.5% up, from 09:06: called at 09:07 (held a minute).
    breaker = every(at("09:06:00"), at("09:12:30"), 10, lambda when: kospi(when, -8.5))
    sidecar = every(at("09:06:00"), at("09:12:30"), 10, lambda when: kospi200_futures(when, 5.5))
    rule = SignalRule(0.5, 2)

    await morning(
        tmp_path / "breaker.db",
        [UP, UP, FLAT],
        Signals(rule, alerts.signal),
        [*busy_morning(end="09:12:30"), *breaker],
        until="09:12:30",
    )
    breaker_sent = list(sent)
    sent.clear()
    await morning(
        tmp_path / "sidecar.db",
        [UP, UP, FLAT],
        Signals(rule, alerts.signal),
        [*busy_morning(end="09:12:30"), *sidecar],
        until="09:12:30",
    )
    await alerts.close()

    # The 09:05 call (to 09:09) was sent before the breaker; from 09:09 each is blocked.
    assert [m.split(" · ")[2] for m in breaker_sent] == [
        "09:01 봉 마감, 09:05까지",
        "09:05 봉 마감, 09:09까지",
    ]
    blocked = kept(tmp_path / "breaker.db", "signal_blocked")
    assert [(b["bar_close"][11:16], b["blocked"]) for b in blocked] == [
        ("09:09", "special"),
        ("09:10", "special"),
        ("09:11", "special"),
        ("09:12", "special"),
    ]
    # A sidecar halts program trading only: signals go on.
    assert [m.split(" · ")[2] for m in sent] == [
        "09:01 봉 마감, 09:05까지",
        "09:05 봉 마감, 09:09까지",
        "09:09 봉 마감, 09:13까지",
    ]
    assert kept(tmp_path / "sidecar.db", "signal_blocked") == []


async def test_a_symbol_without_a_reference_price_is_not_signalled(
    tmp_path: Path, telegram: tuple[Alerts, list[str]]
) -> None:
    alerts, sent = telegram

    # The book stops at 09:01:50: from 09:03 the last quote is over a minute old.
    await morning(
        tmp_path / "engine.db",
        [UP, UP, FLAT],
        Signals(SignalRule(0.5, 2), alerts.signal),
        busy_morning(end="09:01:50"),
    )
    await alerts.close()

    assert [m.split(" · ")[2] for m in sent] == ["09:01 봉 마감, 09:05까지"]
    [blocked] = kept(tmp_path / "engine.db", "signal_blocked")
    assert (blocked["bar_close"], blocked["blocked"]) == ("2026-10-12T09:05:00+09:00", "no_reference")


async def test_a_signal_past_its_target_time_is_not_sent(
    tmp_path: Path, telegram: tuple[Alerts, list[str]]
) -> None:
    alerts, sent = telegram

    # The engine stalls and catches up at 09:05:01: the 09:01 call's four minutes are over,
    # 09:02's are not (09:03–09:05 repeat it).
    await morning(
        tmp_path / "engine.db",
        [UP, UP, FLAT],
        Signals(SignalRule(0.5, 2), alerts.signal),
        display_delay=timedelta(minutes=4, seconds=1),
    )
    await alerts.close()

    assert [m.split(" · ")[2] for m in sent] == ["09:02 봉 마감, 09:06까지"]
    [blocked] = kept(tmp_path / "engine.db", "signal_blocked")
    assert (blocked["bar_close"], blocked["blocked"]) == ("2026-10-12T09:01:00+09:00", "expired")


async def test_a_restarted_engine_does_not_repeat_a_running_signal(
    tmp_path: Path, telegram: tuple[Alerts, list[str]]
) -> None:
    alerts, sent = telegram
    db = tmp_path / "engine.db"

    await morning(db, [UP, UP, FLAT], Signals(SignalRule(0.5, 2), alerts.signal), until="09:01:30")
    # Restarted on the same store: the 09:01 call runs to 09:05.
    await morning(db, [UP, UP, FLAT], Signals(SignalRule(0.5, 2), alerts.signal), start="09:01:30")
    await alerts.close()

    assert [m.split(" · ")[2] for m in sent] == ["09:01 봉 마감, 09:05까지", "09:05 봉 마감, 09:09까지"]


def test_signals_are_off_by_default_and_their_settings_are_checked() -> None:
    keys = {"KIS_APP_KEY": "key", "KIS_APP_SECRET": "secret"}

    assert Settings.from_env(keys, dotenv_path=None).signal_rule is None
    on = Settings.from_env(
        {**keys, "KRX_SIGNAL_ALERT": "on", "KRX_SIGNAL_MIN_AGREE": "3"}, dotenv_path=None
    ).signal_rule
    assert on == SignalRule(min_probability=0.6, min_agree=3)
    with pytest.raises(SettingsError, match="KRX_SIGNAL_MIN_PROBABILITY"):
        Settings.from_env({**keys, "KRX_SIGNAL_MIN_PROBABILITY": "0.4"}, dotenv_path=None)
    with pytest.raises(SettingsError, match="KRX_SIGNAL_ALERT"):
        Settings.from_env({**keys, "KRX_SIGNAL_ALERT": "maybe"}, dotenv_path=None)
