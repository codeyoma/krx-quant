"""An alert's delivery (#358, #489).

Said in the log: ``telegram sent: <text>`` once Telegram took it, ``telegram send failed: …`` once
it is dropped, and neither without Telegram. A Grafana rule that stands in for an alert the engine
could not send reads the first (docs/operations.md).

Sent through one queue per process, in order, the callers not waiting: a failure of the moment is
tried again for about two minutes, HTTP 429 waits its ``retry_after``, another 4xx is not tried
again; the queue holds a bounded number of messages, and ``close`` a bounded time.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from krx_quant import collector, notify
from krx_quant.collector import run_collector
from krx_quant.config import Settings
from krx_quant.fake_kis import FakeKis
from krx_quant.notify import Alerts, send_notice
from krx_quant.trader.run import run_trader
from krx_quant.trader.settings import TraderSettings
from krx_quant.trader.store import DB_NAME as TRADER_DB
from krx_quant.trader.store import request_pause
from tests.collector_doubles import FIXED_NOW, TRADE_KEY, settings_for
from tests.server_doubles import free_port, telegram
from tests.test_resilience import fast
from tests.trader_doubles import FEE, at
from tests.waiting import until

TEXT = "2026-10-22 밤 학습이 실패했습니다: exit -9 (SIGKILL)"
UNREACHABLE = "http://127.0.0.1:9"  # nothing listens there


class Bot:
    """A Bot API stand-in: each request gets the next of ``answers`` (an HTTP status; 429 with
    ``retry_after``), then 200. While ``gate`` is clear the requests wait for it."""

    def __init__(self, answers: Sequence[int] = (), retry_after: int = 7) -> None:
        self.answers = list(answers)
        self.retry_after = retry_after
        self.asked: list[str] = []  # every request's text, in the order they came
        self.took: list[str] = []  # the texts answered 200
        self.gate = asyncio.Event()
        self.gate.set()
        self.url = ""

    async def message(self, request: web.Request) -> web.Response:
        text = (await request.json())["text"]
        self.asked.append(text)
        await self.gate.wait()
        status = self.answers.pop(0) if self.answers else 200
        if status == 200:
            self.took.append(text)
            return web.json_response({"ok": True, "result": {"message_id": len(self.took)}})
        body: dict[str, object] = {"ok": False, "error_code": status, "description": "stand-in"}
        if status == 429:
            body["parameters"] = {"retry_after": self.retry_after}
        return web.json_response(body, status=status)


@asynccontextmanager
async def bot(answers: Sequence[int] = (), retry_after: int = 7) -> AsyncIterator[Bot]:
    found = Bot(answers, retry_after)
    app = web.Application()
    app.router.add_post("/bot{token}/sendMessage", found.message)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server(access_log=None)
    found.url = str(server.make_url("")).rstrip("/")
    try:
        yield found
    finally:
        found.gate.set()
        await server.close()


class Time:
    """The queue's clock and sleep: a sleep moves the clock by its seconds at once."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


def queue(session: aiohttp.ClientSession, url: str, time: Time, max_queued: int | None = None) -> Alerts:
    return Alerts(session, "123:fake", "42", url, max_queued=max_queued, clock=time.clock, sleep=time.sleep)


def said(caplog: pytest.LogCaptureFixture) -> Callable[[], list[str]]:
    return lambda: [r.getMessage() for r in caplog.records if r.name == "krx_quant.notify"]


def failed(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The ERROR lines: the messages dropped."""
    return [
        r.getMessage() for r in caplog.records if r.name == "krx_quant.notify" and r.levelno == logging.ERROR
    ]


def retries(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [m for m in said(caplog)() if m.startswith("telegram send retry in ")]


async def test_an_alert_telegram_took_is_logged_as_sent_after_it_is(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, "krx_quant.notify")
    async with telegram(200) as (url, sent), aiohttp.ClientSession() as session:
        alerts = Alerts(session, "123:fake", "42", url)
        alerts.raise_("training", TEXT)
        await until(lambda: f"telegram sent: [장애] {TEXT}" in said(caplog)())
        await alerts.close()
    assert sent == [f"[장애] {TEXT}"]
    assert said(caplog)() == [f"alert: [장애] {TEXT}", f"telegram sent: [장애] {TEXT}"]


async def test_an_alert_telegram_refused_is_not_tried_again_nor_logged_as_sent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 4xx other than 429 (a wrong chat, a message too long) would be refused again: dropped at
    once, and the messages behind it go."""
    caplog.set_level(logging.INFO, "krx_quant.notify")
    time = Time()
    async with bot([400]) as telegram_, aiohttp.ClientSession() as session:
        alerts = queue(session, telegram_.url, time)
        alerts.raise_("training", TEXT)
        alerts.notice("다음 알림")
        await until(lambda: "[알림] 다음 알림" in telegram_.took)
        await alerts.close()
    assert telegram_.asked == [f"[장애] {TEXT}", "[알림] 다음 알림"], "tried once"
    assert time.slept == [] and retries(caplog) == []
    [dropped] = failed(caplog)
    assert dropped.startswith("telegram send failed: Telegram refused it: HTTP 400 ")
    assert dropped.endswith(f"(tries: 1); dropped: [장애] {TEXT}")
    assert not [m for m in said(caplog)() if m.startswith(f"telegram sent: [장애] {TEXT}")]


async def test_without_telegram_an_alert_is_only_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, "krx_quant.notify")
    async with aiohttp.ClientSession() as session:
        alerts = Alerts(session, None, None, UNREACHABLE)
        alerts.raise_("training", TEXT)
        await alerts.close()
    assert said(caplog)() == [f"alert: [장애] {TEXT}"]


async def test_a_message_that_failed_for_a_moment_is_sent_again_and_those_behind_it_wait(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """2026-11-03 14:02: the trader's quotes-stalled notice was lost to a minute without the
    network, while the next one went (#489)."""
    caplog.set_level(logging.INFO, "krx_quant.notify")
    time = Time()
    async with bot([503, 502]) as telegram_, aiohttp.ClientSession() as session:
        alerts = queue(session, telegram_.url, time)
        alerts.notice("호가 멈춤")
        alerts.notice("호가 돌아옴")
        alerts.notice("거래 재개")
        await until(lambda: "[알림] 거래 재개" in telegram_.took)
        await alerts.close()
    first, second, third = "[알림] 호가 멈춤", "[알림] 호가 돌아옴", "[알림] 거래 재개"
    assert telegram_.asked == [first, first, first, second, third]
    assert telegram_.took == [first, second, third]
    assert time.slept == [1.0, 2.0], "RETRY_DELAYS"
    assert [m for m in said(caplog)() if m.startswith("telegram sent: ")] == [
        f"telegram sent: {first}",
        f"telegram sent: {second}",
        f"telegram sent: {third}",
    ]
    assert len(retries(caplog)) == 2 and failed(caplog) == []


async def test_a_message_telegram_cannot_be_reached_for_is_tried_for_two_minutes_then_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tried until two minutes after it was queued, the last try at the end; a message queued with
    it, its time up too, is tried once."""
    caplog.set_level(logging.INFO, "krx_quant.notify")
    time = Time()
    async with aiohttp.ClientSession() as session:
        alerts = queue(session, UNREACHABLE, time)
        alerts.notice("첫째")
        alerts.notice("둘째")
        await until(lambda: any(m.endswith("dropped: [알림] 둘째") for m in failed(caplog)))
        await alerts.close()
    assert time.slept == [1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 30.0, 22.0]
    assert sum(time.slept) == notify.RETRY_SECONDS
    first, second = failed(caplog)
    assert first.startswith("telegram send failed: Telegram not reachable: ")
    assert first.endswith("(tries: 9); dropped: [알림] 첫째")
    assert second.endswith("(tries: 1); dropped: [알림] 둘째")
    assert len(retries(caplog)) == 8
    assert not [m for m in said(caplog)() if m.startswith("telegram sent: ")]


async def test_http_429_waits_the_retry_after_telegram_gives(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, "krx_quant.notify")
    time = Time()
    async with bot([429], retry_after=7) as telegram_, aiohttp.ClientSession() as session:
        alerts = queue(session, telegram_.url, time)
        alerts.notice("첫째")
        alerts.notice("둘째")
        await until(lambda: "[알림] 둘째" in telegram_.took)
        await alerts.close()
    assert telegram_.asked == ["[알림] 첫째", "[알림] 첫째", "[알림] 둘째"]
    assert time.slept == [7.0]
    assert retries(caplog)[0].startswith("telegram send retry in 7s: Telegram refused it: HTTP 429 ")
    assert failed(caplog) == []


async def test_a_429_asking_past_the_messages_time_drops_them_without_asking_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Telegram asked for five minutes: the messages whose time ends before are dropped, not sent
    into the wait; one queued after it goes."""
    caplog.set_level(logging.INFO, "krx_quant.notify")
    time = Time()
    async with bot([429], retry_after=300) as telegram_, aiohttp.ClientSession() as session:
        alerts = queue(session, telegram_.url, time)
        alerts.notice("첫째")
        alerts.notice("둘째")
        await until(lambda: any(m.endswith("dropped: [알림] 둘째") for m in failed(caplog)))
        time.now += 300
        alerts.notice("셋째")
        await until(lambda: "[알림] 셋째" in telegram_.took)
        await alerts.close()
    assert telegram_.asked == ["[알림] 첫째", "[알림] 셋째"]
    assert time.slept == []
    first, second = failed(caplog)
    assert first.endswith("(tries: 1); dropped: [알림] 첫째") and "HTTP 429" in first
    assert second.startswith("telegram send failed: Telegram asked to wait 300s more (HTTP 429)")


async def test_the_callers_do_not_wait_for_telegram(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, "krx_quant.notify")
    async with bot() as telegram_, aiohttp.ClientSession() as session:
        telegram_.gate.clear()  # Telegram holds its answers
        alerts = Alerts(session, "123:fake", "42", telegram_.url)
        alerts.raise_("feed", "피드가 끊겼습니다")
        alerts.notice("호가 멈춤")
        # Returned at once: the loop goes on while the first request waits for its answer.
        await until(lambda: "[장애] 피드가 끊겼습니다" in telegram_.asked)
        assert telegram_.took == []
        telegram_.gate.set()
        await until(lambda: "[알림] 호가 멈춤" in telegram_.took)
        await alerts.close()
    assert telegram_.took == ["[장애] 피드가 끊겼습니다", "[알림] 호가 멈춤"]


async def test_deliver_waits_for_the_tries_and_says_whether_it_went() -> None:
    time = Time()
    async with bot([503]) as telegram_, aiohttp.ClientSession() as session:
        alerts = queue(session, telegram_.url, time)
        alerts.notice("앞선 알림")
        assert await alerts.deliver("은퇴 후보")
        assert telegram_.took == ["[알림] 앞선 알림", "[알림] 은퇴 후보"], "in its place in the queue"
        telegram_.answers = [400]
        assert not await alerts.deliver("거절될 알림")
        await alerts.close()
    ahead = "[알림] 앞선 알림"
    assert telegram_.asked == [ahead, ahead, "[알림] 은퇴 후보", "[알림] 거절될 알림"]


async def test_the_queue_holds_a_bounded_number_and_drops_the_newest(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, "krx_quant.notify")
    async with bot() as telegram_, aiohttp.ClientSession() as session:
        telegram_.gate.clear()
        alerts = queue(session, telegram_.url, Time(), max_queued=3)
        for n in range(5):
            alerts.notice(f"알림 {n}")
        assert alerts.overflowed == 2
        telegram_.gate.set()
        await until(lambda: "[알림] 알림 2" in telegram_.took)
        alerts.notice("그 뒤의 알림")  # room again
        await until(lambda: "[알림] 그 뒤의 알림" in telegram_.took)
        await alerts.close()
    assert telegram_.took == ["[알림] 알림 0", "[알림] 알림 1", "[알림] 알림 2", "[알림] 그 뒤의 알림"]
    assert failed(caplog) == [
        "telegram send failed: 3 messages wait to be sent; dropped (1 so far): [알림] 알림 3",
        "telegram send failed: 3 messages wait to be sent; dropped (2 so far): [알림] 알림 4",
    ]


async def test_close_sends_what_is_left_for_its_time_limit_at_most(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, "krx_quant.notify")
    async with bot() as telegram_, aiohttp.ClientSession() as session:
        telegram_.gate.clear()  # Telegram never answers before the limit
        alerts = Alerts(session, "123:fake", "42", telegram_.url)
        alerts.notice("첫째")
        alerts.notice("둘째")
        told = asyncio.create_task(alerts.deliver("셋째"))
        await until(lambda: "[알림] 첫째" in telegram_.asked)
        await asyncio.wait_for(alerts.close(timeout=0.05), 5)
        assert await asyncio.wait_for(told, 5) is False
        assert telegram_.took == []
        telegram_.gate.set()
        alerts.notice("닫은 뒤")  # sent as before: the next close waits for it
        await alerts.close()
    # The first try's request may still be answered once the client gave up on it.
    assert [t for t in telegram_.asked if t != "[알림] 첫째"] == ["[알림] 닫은 뒤"]
    assert telegram_.took[-1] == "[알림] 닫은 뒤"
    assert failed(caplog) == [
        "telegram send failed: stopped before it was sent; dropped: [알림] 첫째",
        "telegram send failed: stopped before it was sent; dropped: [알림] 둘째",
        "telegram send failed: stopped before it was sent; dropped: [알림] 셋째",
    ]


async def test_a_notice_from_a_short_lived_process_waits_for_its_tries_at_most(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``krx-quant notify`` (the auto-update) and the nightly training's summary send and exit:
    they wait for the message's tries, not longer."""
    monkeypatch.setattr(notify, "RETRY_SECONDS", 0.2)
    monkeypatch.setattr(notify, "RETRY_DELAYS", (0.05,))
    settings = Settings(
        kis_app_key="",
        kis_app_secret="",
        telegram_bot_token="123:fake",
        telegram_chat_id="42",
        telegram_api_url=UNREACHABLE,
    )
    assert await asyncio.wait_for(send_notice(settings, "자동 업데이트"), 5) is False
    async with bot([503]) as telegram_:
        assert await send_notice(replace(settings, telegram_api_url=telegram_.url), "자동 업데이트")
    assert telegram_.took == ["[알림] 자동 업데이트"]


# The engine and the trader send the same way.


async def test_the_engine_s_alerts_go_through_the_queue(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(notify, "RETRY_DELAYS", (0.01,))
    monkeypatch.setattr(collector, "WS_ALERT_AFTER_SECONDS", 0.2)  # a drop is alerted once this old (#488)
    kis.script[TRADE_KEY] = []
    async with bot([503]) as telegram_:
        settings = replace(
            fast(settings_for(kis, tmp_path)),
            telegram_bot_token="123:fake",
            telegram_chat_id="42",
            telegram_api_url=telegram_.url,
            recovery_hold_seconds=0.05,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(run_collector(settings, stop=stop, clock=lambda: FIXED_NOW))
        try:
            await kis.wait_for_subscription_count(TRADE_KEY, 1)
            kis.accept_ws = False
            await kis.close_connections()
            await until(lambda: bool(websocket(telegram_.took, "[장애]")))
            kis.accept_ws = True
            await until(lambda: bool(websocket(telegram_.took, "[복구]")))
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)
    [down], [back] = websocket(telegram_.took, "[장애]"), websocket(telegram_.took, "[복구]")
    assert telegram_.took.index(down) < telegram_.took.index(back)
    assert telegram_.asked[0] == telegram_.asked[1], "refused once (HTTP 503), sent again"
    assert telegram_.took == list(dict.fromkeys(telegram_.asked)), "each once, in order"


def websocket(texts: list[str], kind: str) -> list[str]:
    """The KIS WebSocket alerts of ``kind`` (``[장애]``, ``[복구]``) among ``texts``."""
    return [t for t in texts if t.startswith(kind) and "WebSocket" in t]


async def test_the_trader_s_notices_go_through_the_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(notify, "RETRY_DELAYS", (0.01,))
    state = tmp_path / "state"
    async with bot([503]) as telegram_:
        settings = Settings(
            kis_app_key="",
            kis_app_secret="",
            state_dir=state,
            fee_rate=FEE,
            telegram_bot_token="123:fake",
            telegram_chat_id="42",
            telegram_api_url=telegram_.url,
        )
        trader = TraderSettings(
            quotes_url=f"ws://127.0.0.1:{free_port()}/v1/quotes",
            engine_url=f"http://127.0.0.1:{free_port()}",
            poll_seconds=0.01,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(run_trader(settings, trader, stop=stop, now=lambda: at("10:00:00")))
        try:
            await until(lambda: (state / TRADER_DB).exists())
            request_pause(state / TRADER_DB, False, at("10:00:01"))
            await until(lambda: any("새 진입 정지" in text for text in telegram_.took))
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)
    assert telegram_.asked[0] == telegram_.asked[1], "refused once (HTTP 503), sent again"
    assert telegram_.took == list(dict.fromkeys(telegram_.asked)), "each once, in order"
