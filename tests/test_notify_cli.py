"""The Telegram test alert used when opening the server (fake Telegram on the fake KIS seam)."""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

import pytest

from krx_quant.cli.tools import _notify
from krx_quant.fake_kis import FakeKis
from krx_quant.notify import send_notice, send_test_alert
from tests.collector_doubles import settings_for


async def test_the_test_alert_reaches_the_configured_chat(kis: FakeKis, tmp_path: Path) -> None:
    settings = replace(
        settings_for(kis, tmp_path),
        telegram_bot_token="123:fake",
        telegram_chat_id="42",
        telegram_api_url=kis.rest_url,
    )

    assert await send_test_alert(settings)

    [message] = kis.telegram_messages
    assert (message["bot_token"], message["chat_id"]) == ("123:fake", "42")
    assert message["text"].startswith("[테스트]")


async def test_without_telegram_settings_nothing_is_sent(kis: FakeKis, tmp_path: Path) -> None:
    assert not await send_test_alert(settings_for(kis, tmp_path))
    assert kis.telegram_messages == []


async def test_a_notice_from_a_script_reaches_the_chat(kis: FakeKis, tmp_path: Path) -> None:
    settings = replace(
        settings_for(kis, tmp_path),
        telegram_bot_token="123:fake",
        telegram_chat_id="42",
        telegram_api_url=kis.rest_url,
    )

    assert await send_notice(settings, "자동 업데이트: abc1234 → def5678")

    [message] = kis.telegram_messages
    assert message["text"] == "[알림] 자동 업데이트: abc1234 → def5678"


async def test_a_report_for_the_log_alone_is_logged_and_not_sent(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """#614: ``krx-quant notify --log-only``, a clean update's report: in the log (Loki keeps it), not on
    Telegram, which hears of the update from the engine's readiness line."""
    caplog.set_level(logging.INFO, logger="krx_quant.notify")
    for name, value in {
        "TELEGRAM_BOT_TOKEN": "123:fake",
        "TELEGRAM_CHAT_ID": "42",
        "TELEGRAM_API_URL": kis.rest_url,
        "KRX_STATE_DIR": str(tmp_path),
    }.items():
        monkeypatch.setenv(name, value)
    report = "자동 업데이트: abc1234 → def5678\n- Granger learns\n디스크 여유 23G → 24G / 58G"

    assert await _notify(report, log_only=True) == 0

    assert kis.telegram_messages == []
    [line] = [r for r in caplog.records if r.name == "krx_quant.notify"]
    assert (line.levelno, line.getMessage()) == (logging.INFO, f"notice, logged only: {report}")
