"""Operational alerts: one message when a problem starts, one when it clears.

Messages go to a Telegram bot when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set,
otherwise only to the log. Sending never raises into the collector.

Each message is logged before it is sent (``alert: <text>``, WARNING), and again once Telegram has
taken it (``telegram sent: <text>``, INFO), or why not (``telegram send failed: …``, ERROR). The
second says it reached Telegram: a Grafana alert rule that stands in for an alert the engine could
not send reads it (#358, docs/operations.md "Grafana 알림 규칙").
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import aiohttp

from krx_quant.config import Settings

log = logging.getLogger(__name__)


class Alerts:
    """De-duplicated alerts keyed by problem: ``raise_`` sends once until ``resolve``."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        bot_token: str | None,
        chat_id: str | None,
        api_url: str,
    ) -> None:
        self._session = session
        self._url = f"{api_url}/bot{bot_token}/sendMessage" if bot_token and chat_id else None
        self._bot_token = bot_token or ""
        self._chat_id = chat_id
        self._active: set[str] = set()
        self._sending: set[asyncio.Task[None]] = set()

    def raise_(self, key: str, text: str, *, again: bool = False) -> None:
        """Send ``text`` once until ``resolve``; ``again`` sends it while it is on too (a problem
        going on for days, alerted each day, #355)."""
        if key in self._active and not again:
            return
        self._active.add(key)
        self._send(f"[장애] {text}")

    def mark(self, key: str) -> None:
        """A problem known to be on from before a restart (#355): nothing sent now, and
        ``resolve`` sends its end."""
        self._active.add(key)

    def resolve(self, key: str, text: str) -> None:
        if key not in self._active:
            return
        self._active.discard(key)
        self._send(f"[복구] {text}")

    async def deliver(self, text: str) -> bool:
        """Send a notice that is not a problem to resolve (an algorithm to retire) and wait:
        whether it went. Not without Telegram set up (it is only logged), so it is asked for
        again once it is.
        """
        log.warning("alert: %s", text)
        if self._url is None:
            return False
        try:
            await self.send_now(f"[알림] {text}")
        except RuntimeError as e:
            log.error("telegram send failed: %s", e)
            return False
        return True

    def notice(self, text: str) -> None:
        """A notice that is not a problem to resolve (the trader's halts, pauses, resumes and
        re-arms, #396), sent and not waited for."""
        self._send(f"[알림] {text}")

    def signal(self, text: str) -> None:
        """A prediction signal (``signals.py``), sent and not waited for."""
        self._send(f"[신호] {text}")

    def raise_many(self, keys: list[str], text: str) -> None:
        """One message for every key not already alerted: ``text: key, key``."""
        new = [k for k in keys if k not in self._active]
        if new:
            self._active.update(new)
            self._send(f"[장애] {text}: {', '.join(new)}")

    def resolve_many(self, keys: list[str], text: str) -> None:
        cleared = [k for k in keys if k in self._active]
        if cleared:
            self._active.difference_update(cleared)
            self._send(f"[복구] {text}: {', '.join(cleared)}")

    def active(self, prefix: str = "") -> list[str]:
        return [k for k in self._active if k.startswith(prefix)]

    def is_active(self, key: str) -> bool:
        return key in self._active

    async def send_now(self, text: str) -> None:
        """Send ``text`` at once; raise RuntimeError if Telegram is not set up or refuses it."""
        if self._url is None:
            raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are not set")
        try:
            async with self._session.post(
                self._url,
                json={"chat_id": self._chat_id, "text": text},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(
                        f"Telegram refused it: HTTP {resp.status} {self._redact(await resp.text())}"
                    )
        except (aiohttp.ClientError, OSError, TimeoutError) as e:
            raise RuntimeError(f"Telegram not reachable: {self._redact(str(e))}") from e
        log.info("telegram sent: %s", text)  # it reached Telegram (#358)

    async def close(self, timeout: float = 5.0) -> None:
        """Wait (briefly) for messages still being sent."""
        if self._sending:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.gather(*self._sending, return_exceptions=True), timeout)

    def _send(self, text: str) -> None:
        log.warning("alert: %s", text)
        if self._url is None:
            return
        task = asyncio.create_task(self._post(text))
        self._sending.add(task)
        task.add_done_callback(self._sending.discard)

    async def _post(self, text: str) -> None:
        try:
            await self.send_now(text)
        except RuntimeError as e:
            log.error("telegram send failed: %s", e)

    def _redact(self, text: str) -> str:
        """Keep the bot token (part of the URL) out of the log."""
        return text.replace(self._bot_token, "***") if self._bot_token else text


async def send_notice(settings: Settings, text: str) -> bool:
    """Send a notice outside the collector (a training summary); whether it went."""
    async with aiohttp.ClientSession() as session:
        alerts = Alerts(
            session, settings.telegram_bot_token, settings.telegram_chat_id, settings.telegram_api_url
        )
        return await alerts.deliver(text)


async def send_test_alert(settings: Settings) -> bool:
    """Send a test message to the alert chat; False when Telegram is not configured.

    Raises RuntimeError when Telegram cannot be reached or refuses it, so a wrong token or
    chat id shows at once.
    """
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        return False
    async with aiohttp.ClientSession() as session:
        alerts = Alerts(
            session, settings.telegram_bot_token, settings.telegram_chat_id, settings.telegram_api_url
        )
        await alerts.send_now("[테스트] krx-quant 알림이 이 대화로 옵니다.")
    return True
