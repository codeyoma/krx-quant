"""An alert's delivery said in the log (#358): ``telegram sent: <text>`` once Telegram took it,
``telegram send failed: …`` when it did not, and neither without Telegram. A Grafana rule that
stands in for an alert the engine could not send reads the first (docs/operations.md)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from krx_quant.notify import Alerts
from tests.test_subscription_limit import until

TEXT = "2026-10-22 밤 학습이 실패했습니다: exit -9 (SIGKILL)"


@asynccontextmanager
async def telegram(status: int) -> AsyncIterator[tuple[str, list[str]]]:
    """A Bot API stand-in answering ``status``; the texts it was sent."""
    sent: list[str] = []

    async def message(request: web.Request) -> web.Response:
        sent.append((await request.json())["text"])
        return web.json_response({"ok": status == 200}, status=status)

    app = web.Application()
    app.router.add_post("/bot{token}/sendMessage", message)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server(access_log=None)
    try:
        yield str(server.make_url("")).rstrip("/"), sent
    finally:
        await server.close()


def said(caplog: pytest.LogCaptureFixture) -> Callable[[], list[str]]:
    return lambda: [r.getMessage() for r in caplog.records if r.name == "krx_quant.notify"]


async def test_an_alert_telegram_took_is_logged_as_sent_after_it_is(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, "krx_quant.notify")
    async with telegram(200) as (url, sent), aiohttp.ClientSession() as session:
        alerts = Alerts(session, "123:fake", "42", url)
        alerts.raise_("training", TEXT)
        await until(lambda: len(said(caplog)()) == 2)
        await alerts.close()
    assert sent == [f"[장애] {TEXT}"]
    assert said(caplog)() == [f"alert: [장애] {TEXT}", f"telegram sent: [장애] {TEXT}"]


async def test_an_alert_telegram_refused_is_not_logged_as_sent(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, "krx_quant.notify")
    async with telegram(500) as (url, sent), aiohttp.ClientSession() as session:
        alerts = Alerts(session, "123:fake", "42", url)
        alerts.raise_("training", TEXT)
        await until(lambda: any(m.startswith("telegram send failed: ") for m in said(caplog)()))
        await alerts.close()
    assert sent == [f"[장애] {TEXT}"]
    assert not [m for m in said(caplog)() if m.startswith("telegram sent:")]
    assert [r.levelname for r in caplog.records if r.getMessage().startswith("telegram send failed")] == [
        "ERROR"
    ]


async def test_without_telegram_an_alert_is_only_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, "krx_quant.notify")
    async with aiohttp.ClientSession() as session:
        alerts = Alerts(session, None, None, "http://127.0.0.1:9")
        alerts.raise_("training", TEXT)
        await alerts.close()
    assert said(caplog)() == [f"alert: [장애] {TEXT}"]
