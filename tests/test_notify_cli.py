"""The Telegram test alert used when opening the server (fake Telegram on the fake KIS seam)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from krx_quant.fake_kis import FakeKis
from krx_quant.notify import send_test_alert
from tests.test_collector import settings_for


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
