"""Operational alerts: one message when a problem starts, one when it clears.

Messages go to a Telegram bot when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set,
otherwise only to the log. Sending never raises into the collector.

Each process (the engine, the trader, a command) sends through one queue of its own, in order
(#489): the callers do not wait for Telegram. A message that fails for a moment (no connection,
a timeout, HTTP 5xx) is tried again for about two minutes after it was queued (``RETRY_SECONDS``),
the messages behind it waiting; HTTP 429 waits the ``retry_after`` Telegram gives. Any other
refusal (HTTP 4xx) is not tried again. At most ``MAX_QUEUED`` messages wait: a new one past that is
dropped and counted, so that the order holds. When the process stops, ``close`` sends what is left
for ``CLOSE_SECONDS`` at most.

Each message is logged before it is queued (``alert: <text>``, WARNING; the nightly training's
summary at INFO, #580: a report, no warning), and again once Telegram has taken it (``telegram
sent: <text>``, INFO), or once it is dropped, and why (``telegram send failed: …``, ERROR). A try
that is followed by another logs ``telegram send retry in <n>s: …`` (WARNING). The ``telegram
sent`` line says it reached Telegram: a Grafana alert rule that stands in for an alert the engine
could not send reads it (#358, docs/operations.md "Grafana 알림 규칙").
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

import aiohttp

from krx_quant.config import Settings

log = logging.getLogger(__name__)

RETRY_SECONDS = 120.0  # how long after it was queued a message is still tried (#489)
RETRY_DELAYS = (1.0, 2.0, 5.0, 10.0, 20.0, 30.0)  # the waits between its tries; the last repeats
MAX_QUEUED = 100  # messages not sent yet, the one being sent included
CLOSE_SECONDS = 5.0  # at shutdown, how long what is queued may still take
SEND_SECONDS = 10.0  # one try's limit


class TelegramError(RuntimeError):
    """Telegram not reachable, or it refused a message. ``again``: a failure of the moment (no
    connection, a timeout, HTTP 5xx or 429), worth another try; ``retry_after``: the seconds
    Telegram asked to wait (HTTP 429)."""

    def __init__(self, message: str, *, again: bool, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.again = again
        self.retry_after = retry_after


@dataclass
class _Queued:
    text: str
    deadline: float  # the queue's clock: no try starts after it but the first
    went: asyncio.Future[bool] | None = None  # ``deliver``'s answer

    def done(self, went: bool) -> None:
        if self.went is not None and not self.went.done():
            self.went.set_result(went)


class Alerts:
    """De-duplicated alerts keyed by problem: ``raise_`` sends once until ``resolve``."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        bot_token: str | None,
        chat_id: str | None,
        api_url: str,
        *,
        retry_seconds: float | None = None,
        retry_delays: Sequence[float] | None = None,
        max_queued: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._session = session
        self._url = f"{api_url}/bot{bot_token}/sendMessage" if bot_token and chat_id else None
        self._bot_token = bot_token or ""
        self._chat_id = chat_id
        self._active: set[str] = set()
        # The send queue (#489), the message being sent first; ``clock`` and ``sleep`` time its
        # tries (a test's stand-ins).
        self._retry_seconds = RETRY_SECONDS if retry_seconds is None else retry_seconds
        self._retry_delays = tuple(RETRY_DELAYS if retry_delays is None else retry_delays)
        self._max_queued = MAX_QUEUED if max_queued is None else max_queued
        self._clock = clock
        self._sleep = sleep
        self._queue: deque[_Queued] = deque()
        self._worker: asyncio.Task[None] | None = None
        self._not_before = 0.0  # no try before this (HTTP 429's retry_after)
        self.overflowed = 0  # messages dropped because the queue was full

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

    async def deliver(self, text: str, *, level: int = logging.WARNING) -> bool:
        """Send a notice that is not a problem to resolve (an algorithm to retire) and wait:
        whether it went. It takes its place in the queue and its tries (#489), so the wait can
        last about two minutes while Telegram cannot be reached. Not without Telegram set up (it
        is only logged), so it is asked for again once it is. ``level`` is its ``alert:`` line's: a
        report that is no warning (the nightly training's summary, #580) is logged at INFO, so
        that its lines are not counted as warnings.
        """
        log.log(level, "alert: %s", text)
        if self._url is None:
            return False
        went: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._enqueue(f"[알림] {text}", went)
        # A caller cancelled meanwhile leaves the message queued: it is still sent.
        return await asyncio.shield(went)

    def notice(self, text: str) -> None:
        """A notice that is not a problem to resolve (the trader's halts, pauses, resumes and
        re-arms, #396), sent and not waited for."""
        self._send(f"[알림] {text}")

    def trade(self, text: str) -> None:
        """A trade the trader made (#677: the mock book's verdict trades, ``[buy]`` or ``[sell]``), sent as
        it is and not waited for; logged at INFO, being no warning."""
        log.info("alert: %s", text)
        if self._url is not None:
            self._enqueue(text)

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
        """Send ``text`` at once, one try and not through the queue; raise ``TelegramError`` (a
        RuntimeError) if Telegram is not set up, cannot be reached or refuses it."""
        if self._url is None:
            raise TelegramError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are not set", again=False)
        try:
            async with self._session.post(
                self._url,
                json={"chat_id": self._chat_id, "text": text},
                timeout=aiohttp.ClientTimeout(total=SEND_SECONDS),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise TelegramError(
                        f"Telegram refused it: HTTP {resp.status} {self._redact(body)}",
                        again=resp.status == 429 or resp.status >= 500,
                        retry_after=_retry_after(body) if resp.status == 429 else None,
                    )
        except (aiohttp.ClientError, OSError, TimeoutError) as e:
            raise TelegramError(f"Telegram not reachable: {self._redact(str(e))}", again=True) from e
        log.info("telegram sent: %s", text)  # it reached Telegram (#358)

    async def close(self, timeout: float = CLOSE_SECONDS) -> None:
        """Send what is queued, for ``timeout`` seconds at most (the process is stopping); what is
        left then is dropped, and logged. A message queued after is sent as before (a later
        ``close`` waits for it)."""
        worker = self._worker
        if worker is not None and not worker.done():
            finished, _ = await asyncio.wait({worker}, timeout=timeout)
            if not finished:
                worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker
        while self._queue:
            left = self._queue.popleft()
            log.error("telegram send failed: stopped before it was sent; dropped: %s", left.text)
            left.done(False)

    def _send(self, text: str) -> None:
        log.warning("alert: %s", text)
        if self._url is None:
            return
        self._enqueue(text)

    def _enqueue(self, text: str, went: asyncio.Future[bool] | None = None) -> None:
        """Queue ``text`` behind the messages before it, or drop it if the queue is full."""
        queued = _Queued(text, self._clock() + self._retry_seconds, went)
        if len(self._queue) >= self._max_queued:
            self.overflowed += 1
            log.error(
                "telegram send failed: %d messages wait to be sent; dropped (%d so far): %s",
                len(self._queue),
                self.overflowed,
                text,
            )
            queued.done(False)
            return
        self._queue.append(queued)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._work())

    async def _work(self) -> None:
        """Send the queue's messages one by one, in order, until it is empty."""
        while self._queue:
            head = self._queue[0]
            try:
                went = await self._try(head)
            except Exception:
                log.exception("telegram send failed; dropped: %s", head.text)
                went = False
            self._queue.popleft()
            head.done(went)

    async def _try(self, queued: _Queued) -> bool:
        """Try ``queued`` until it goes, Telegram refuses it, or its time is up: whether it went."""
        tries = 0
        while True:
            hold = self._not_before - self._clock()
            if hold > 0:
                if self._clock() + hold > queued.deadline:
                    log.error(
                        "telegram send failed: Telegram asked to wait %.0fs more (HTTP 429), past this "
                        "message's time; dropped: %s",
                        hold,
                        queued.text,
                    )
                    return False
                await self._sleep(hold)
            tries += 1
            try:
                await self.send_now(queued.text)
            except TelegramError as e:
                left = queued.deadline - self._clock()
                if e.retry_after is not None:
                    self._not_before = self._clock() + e.retry_after
                if not e.again or left <= 0 or (e.retry_after is not None and e.retry_after > left):
                    log.error("telegram send failed: %s (tries: %d); dropped: %s", e, tries, queued.text)
                    return False
                if e.retry_after is not None:
                    log.warning("telegram send retry in %.0fs: %s", e.retry_after, e)
                    continue  # waited out above
                delay = min(self._retry_delays[min(tries, len(self._retry_delays)) - 1], left)
                log.warning("telegram send retry in %.0fs: %s", delay, e)
                await self._sleep(delay)
            else:
                return True

    def _redact(self, text: str) -> str:
        """Keep the bot token (part of the URL) out of the log."""
        return text.replace(self._bot_token, "***") if self._bot_token else text


def _retry_after(body: str) -> float | None:
    """The seconds an HTTP 429 asks to wait (the Bot API's ``parameters.retry_after``)."""
    try:
        seconds = float(json.loads(body)["parameters"]["retry_after"])
    except (ValueError, KeyError, TypeError):
        return None
    return seconds if seconds >= 0 else None


async def send_notice(settings: Settings, text: str, *, level: int = logging.WARNING) -> bool:
    """Send a notice outside the collector (a training summary, the auto-update's); whether it
    went. The process waits for its tries (#489), about two minutes at most. ``level``: its
    ``alert:`` line's (``Alerts.deliver``)."""
    async with aiohttp.ClientSession() as session:
        alerts = Alerts(
            session, settings.telegram_bot_token, settings.telegram_chat_id, settings.telegram_api_url
        )
        try:
            return await alerts.deliver(text, level=level)
        finally:
            await alerts.close()


def log_notice(text: str) -> None:
    """A notice for the log alone (``krx-quant notify --log-only``, #614): the auto-update's report of a
    clean update, which the engine's readiness line tells at 08:40. INFO, a report: never ``alert:``
    nor ``telegram sent:``, which say a message was meant for Telegram or reached it."""
    log.info("notice, logged only: %s", text)


async def send_test_alert(settings: Settings) -> bool:
    """Send a test message to the alert chat; False when Telegram is not configured.

    Raises RuntimeError when Telegram cannot be reached or refuses it, so a wrong token or
    chat id shows at once: one try, not through the queue.
    """
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        return False
    async with aiohttp.ClientSession() as session:
        alerts = Alerts(
            session, settings.telegram_bot_token, settings.telegram_chat_id, settings.telegram_api_url
        )
        await alerts.send_now("[테스트] krx-quant 알림이 이 대화로 옵니다.")
    return True
