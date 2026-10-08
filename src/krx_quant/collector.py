"""Collector: subscribes to KIS real-time feeds, polls REST feeds, and records everything."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import aiohttp

from krx_quant import kis
from krx_quant.auth import TokenManager
from krx_quant.config import Settings
from krx_quant.feeds import (
    BOOK,
    ETF_FALLBACKS,
    FALLBACKS,
    FOREIGN_ESTIMATE,
    INDEX,
    KOSPI200_FUTURES,
    KRX_TRADE,
    KST,
    MEMBER,
    NXT_TRADE,
    OPTION,
    PROGRAM,
    RAW_SCHEMA,
    TRADE,
    US_TRADE,
    USD_FUTURES,
    WS_FEEDS,
    Feed,
    us_daytime_key,
)
from krx_quant.feeds.base import kst_day
from krx_quant.health import FeedHealth, GapKind
from krx_quant.masters import FrontMonths, MasterError, fetch_front_months
from krx_quant.notify import Alerts
from krx_quant.polling import Poller, PollJob
from krx_quant.ratelimit import RateLimiter
from krx_quant.recorder import ParquetRecorder

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]

_WS_ALERT = "websocket"

# WebSocket approval keys last about a day; take a new one on reconnect after this.
_APPROVAL_MAX_AGE = timedelta(hours=20)

# How often buffered partitions whose time limit has passed are flushed.
_TICK_SECONDS = 1.0


def _system_clock() -> datetime:
    return datetime.now(KST)


def subscription_plan(settings: Settings, fronts: FrontMonths | None) -> list[tuple[Feed, str]]:
    """WebSocket registrations in priority order; the session limit cuts from the end.

    Market-wide context comes first so it survives however many symbols are added,
    then each stock's trades, book and program trading, then the tier-2 feeds.
    """
    plan: list[tuple[Feed, str]] = []
    if fronts is not None:
        plan += [(KOSPI200_FUTURES, fronts.kospi200_futures), (USD_FUTURES, fronts.usd_futures)]
    plan += [(US_TRADE, us_daytime_key(t)) for t in settings.us_symbols]
    plan += [(feed, sym) for sym in settings.symbols for feed in (TRADE, BOOK, PROGRAM)]
    plan += [(MEMBER, sym) for sym in settings.symbols]
    plan += [(INDEX, code) for code in settings.index_codes]
    if fronts is not None:
        plan += [(OPTION, code) for code in fronts.kospi200_options]
    # ETFs do not trade on NXT, and the integrated feed sent nothing for them (2026-10-08).
    plan += [(KRX_TRADE, etf) for etf in settings.etf_symbols]
    if settings.venue_trades:
        plan += [(feed, sym) for feed in (KRX_TRADE, NXT_TRADE) for sym in settings.symbols]
    return plan


def rest_plan(settings: Settings) -> list[PollJob]:
    """Feeds that are only available over REST."""
    return [
        PollJob(FOREIGN_ESTIMATE, sym, settings.foreign_estimate_poll_seconds) for sym in settings.symbols
    ]


async def front_months(session: aiohttp.ClientSession, settings: Settings) -> FrontMonths | None:
    """Nearest derivative contracts from the KIS masters; configured futures codes win.

    If the masters cannot be read, configured futures codes are still used (without
    options); with no codes configured, derivative feeds are skipped (returns None).
    """
    try:
        found = await fetch_front_months(session, settings.master_url, settings.option_strikes_each_side)
    except (MasterError, aiohttp.ClientError, OSError, ValueError, TimeoutError) as e:
        if settings.kospi200_futures_code and settings.usd_futures_code:
            log.error("KIS masters unavailable (%s); using configured futures codes, no options", e)
            return FrontMonths(settings.kospi200_futures_code, settings.usd_futures_code, ())
        log.error("derivative feeds skipped: nearest contracts not found (%s)", e)
        return None
    fronts = FrontMonths(
        settings.kospi200_futures_code or found.kospi200_futures,
        settings.usd_futures_code or found.usd_futures,
        found.kospi200_options,
    )
    log.info("nearest contracts: %s", fronts)
    return fronts


async def run_collector(settings: Settings, *, stop: asyncio.Event, clock: Clock | None = None) -> None:
    """Record the configured feeds until ``stop`` is set, then flush and return."""
    now = clock or _system_clock
    recorder = ParquetRecorder(settings.data_dir, settings.flush_rows, settings.flush_seconds)
    health = FeedHealth(recorder, now)
    try:
        async with aiohttp.ClientSession() as session:
            alerts = Alerts(
                session, settings.telegram_bot_token, settings.telegram_chat_id, settings.telegram_api_url
            )
            tokens = TokenManager(
                session,
                settings.kis_rest_url,
                settings.kis_app_key,
                settings.kis_app_secret,
                settings.state_dir / "kis_token.json",
                now,
                timedelta(seconds=settings.token_refresh_margin_seconds),
                settings.token_min_interval_seconds,
            )
            await _warm_up(tokens)
            client = kis.RestClient(
                session,
                settings.kis_rest_url,
                settings.kis_app_key,
                settings.kis_app_secret,
                tokens.get,
                RateLimiter(settings.rest_max_per_second),
                on_token_rejected=tokens.invalidate,
            )
            poller = Poller(client, recorder, now, stop, on_received=health.received)
            fronts = await front_months(session, settings)
            plan = subscription_plan(settings, fronts)
            cap = len(plan) if settings.ws_max_subscriptions is None else settings.ws_max_subscriptions
            for feed, key in plan[cap:]:
                _fall_back(poller, settings, feed.tr_id, key, "beyond the configured cap")
            for job in rest_plan(settings):
                poller.add(job)
            stream = _Stream(session, settings, plan[:cap], recorder, now, stop, poller, health, alerts)
            background = [
                asyncio.create_task(_every(_TICK_SECONDS, recorder.flush_due, "periodic flush")),
                asyncio.create_task(
                    _every(settings.health_check_seconds, stream.check_stale, "health check")
                ),
            ]
            try:
                await stream.run()
            finally:
                await stream.close()
                for task in background:
                    task.cancel()
                await asyncio.gather(*background, return_exceptions=True)
                # Jobs may be queued on the rate limiter; at shutdown, drop them.
                await poller.close()
                await alerts.close()
    finally:
        health.close()
        recorder.close()


async def _every(seconds: float, action: Callable[[], None], name: str) -> None:
    """Run ``action`` every ``seconds`` until cancelled; a failure is logged, not fatal."""
    while True:
        await asyncio.sleep(seconds)
        try:
            action()
        except Exception:
            log.exception("%s failed", name)


async def _warm_up(tokens: TokenManager) -> None:
    """Get a REST token at startup so problems show early; REST polls retry on their own."""
    try:
        await tokens.get()
    except (kis.KisError, aiohttp.ClientError, OSError, ValueError, KeyError) as e:
        log.warning("REST access token not available yet: %s", e)


async def measure_subscription_limit(settings: Settings) -> int:
    """Register real-time trade feeds one by one until KIS refuses; return how many it accepted.

    KIS allows one WebSocket session per app key, so run this while the collector is stopped.
    Every accepted registration is removed again before returning.
    """
    accepted: list[str] = []
    async with aiohttp.ClientSession() as session:
        approval = await _approval_key(session, settings)
        async with session.ws_connect(settings.kis_ws_url + kis.WS_PATH) as ws:
            try:
                for code in MEASURE_CODES:
                    await ws.send_str(kis.subscribe_message(approval, TRADE.tr_id, code))
                    async with asyncio.timeout(_ACK_TIMEOUT_SECONDS):
                        reply = await _ack(ws, code)
                    if reply.ok:
                        accepted.append(code)
                    elif reply.is_limit_rejection:
                        return len(accepted)
                    else:
                        raise kis.KisError(f"registration of {code} refused: {reply.code} {reply.message}")
                raise kis.KisError(f"KIS accepted all {len(MEASURE_CODES)} registrations; no limit found")
            finally:
                with contextlib.suppress(Exception):
                    for code in accepted:
                        await ws.send_str(kis.subscribe_message(approval, TRADE.tr_id, code, subscribe=False))


_ACK_TIMEOUT_SECONDS = 10.0

# Liquid KOSPI stocks to register while measuring; more than any documented session limit.
MEASURE_CODES = (
    "005930 000660 009150 005380 035420 035720 051910 006400 207940 068270 005490 000270 105560 "
    "055550 012330 028260 066570 003550 096770 034730 017670 030200 032830 086790 015760 018260 "
    "011200 010130 009540 024110 033780 003670 316140 000810 010950 011170 090430 251270 036570 "
    "323410 377300 352820 259960 047050 042700 012450 064350 329180 010140 000100 128940 097950 "
    "271560 004020 078930 021240 161390 006800 029780 071050"
).split()


async def _ack(ws: aiohttp.ClientWebSocketResponse, tr_key: str) -> kis.SystemMessage:
    """Read until the registration reply for ``tr_key``, answering heartbeats on the way."""
    async for msg in ws:
        if msg.type != aiohttp.WSMsgType.TEXT:
            continue
        message = kis.parse_message(msg.data)
        if isinstance(message, kis.SystemMessage):
            if message.is_pingpong:
                await ws.pong(msg.data.encode())
            elif message.tr_key == tr_key:
                return message
    raise kis.KisError("websocket closed while measuring")


async def _approval_key(session: aiohttp.ClientSession, settings: Settings) -> str:
    return await kis.issue_approval_key(
        session, settings.kis_rest_url, settings.kis_app_key, settings.kis_app_secret
    )


def _fall_back(poller: Poller, settings: Settings, tr_id: str, key: str, reason: str) -> None:
    fallback = (ETF_FALLBACKS if key in settings.etf_symbols else FALLBACKS).get(tr_id)
    if fallback is None:
        log.error("%s %s not recorded (%s) and has no REST fallback", tr_id, key, reason)
        return
    log.warning(
        "%s %s %s: polling REST %s every %ss instead",
        tr_id,
        key,
        reason,
        fallback.tr_id,
        settings.fallback_poll_seconds,
    )
    poller.add(PollJob(fallback, key, settings.fallback_poll_seconds))


class _Stream:
    """The KIS WebSocket session: kept up across reconnects, every message recorded."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        settings: Settings,
        subscriptions: list[tuple[Feed, str]],
        recorder: ParquetRecorder,
        now: Clock,
        stop: asyncio.Event,
        poller: Poller,
        health: FeedHealth,
        alerts: Alerts,
    ) -> None:
        self._session = session
        self._settings = settings
        # Registered on every (re)connect; keys refused for the session limit leave the list.
        self._subscriptions = list(subscriptions)
        self._recorder = recorder
        self._now = now
        self._stop = stop
        self._poller = poller
        self._health = health
        self._alerts = alerts
        self._approval: str | None = None
        self._approval_at: datetime | None = None
        self._acknowledged = False  # KIS answered a registration on the current connection
        self._recovery: asyncio.Task[None] | None = None

    async def run(self) -> None:
        """Stay connected until ``stop``: reconnect with exponential backoff and register
        every subscription again after each reconnect. The backoff only resets once KIS
        has answered a registration, so a server that drops every new connection at once
        is not hammered.
        """
        settings = self._settings
        delay = settings.reconnect_initial_seconds
        while not self._stop.is_set():
            self._acknowledged = False
            try:
                await self._ensure_approval()
                async with self._session.ws_connect(settings.kis_ws_url + kis.WS_PATH) as ws:
                    for feed, key in list(self._subscriptions):
                        await ws.send_str(self._subscribe_message(feed.tr_id, key))
                    await self._receive(ws)
            except (kis.KisError, aiohttp.ClientError, OSError) as e:
                if self._stop.is_set():
                    return
                if self._acknowledged:
                    delay = settings.reconnect_initial_seconds
                self._connection_lost(str(e))
                log.warning("KIS WebSocket down (%s); reconnecting in %.1fs", e, delay)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), delay)
                delay = min(delay * 2, settings.reconnect_max_seconds)

    async def close(self) -> None:
        if self._recovery is not None:
            self._recovery.cancel()
            await asyncio.gather(self._recovery, return_exceptions=True)

    def check_stale(self) -> None:
        """Alert, in one message, on feeds that went quiet; announce the ones that came back."""
        newly = self._health.check_stale()
        if newly:
            self._alerts.raise_many([_stale_name(k) for k in newly], "장중 데이터가 끊긴 피드")
        still = {_stale_name(k) for k in self._health.open_gaps(GapKind.STALE)}
        back = [name for name in self._alerts.active(_STALE_PREFIX) if name not in still]
        if back:
            self._alerts.resolve_many(back, "데이터가 다시 들어오는 피드")

    async def _ensure_approval(self) -> None:
        now = self._now()
        if self._approval is None or self._approval_at is None or now - self._approval_at > _APPROVAL_MAX_AGE:
            self._approval = await kis.issue_approval_key(
                self._session,
                self._settings.kis_rest_url,
                self._settings.kis_app_key,
                self._settings.kis_app_secret,
            )
            self._approval_at = now

    def _subscribe_message(self, tr_id: str, key: str) -> str:
        assert self._approval is not None
        return kis.subscribe_message(self._approval, tr_id, key)

    def _connection_lost(self, detail: str) -> None:
        if self._recovery is not None:
            self._recovery.cancel()  # it did not stay up long enough to count as recovered
            self._recovery = None
        self._health.connection_lost(((f.tr_id, k) for f, k in self._subscriptions), detail)
        self._alerts.raise_(_WS_ALERT, f"KIS WebSocket 연결이 끊겼습니다 ({detail}). 재연결을 시도합니다.")

    def _acknowledged_on_this_connection(self) -> None:
        if self._acknowledged:
            return
        self._acknowledged = True
        if self._alerts.is_active(_WS_ALERT) and self._recovery is None:
            self._recovery = asyncio.create_task(self._announce_recovery())

    async def _announce_recovery(self) -> None:
        await asyncio.sleep(self._settings.recovery_hold_seconds)
        self._recovery = None
        self._alerts.resolve(_WS_ALERT, "KIS WebSocket이 다시 연결되어 구독을 복원했습니다.")

    async def _receive(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Read messages until the socket closes; ``stop`` closes it from a helper task.

        The read itself is never cancelled: cancelling an in-flight receive can drop a
        frame that has already arrived.
        """

        async def close_on_stop() -> None:
            await self._stop.wait()
            await ws.close()

        closer = asyncio.create_task(close_on_stop())
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle(ws, msg.data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    raise kis.KisError(f"websocket error: {ws.exception()}")
            if not self._stop.is_set():
                raise kis.KisError(f"websocket closed by KIS (code {ws.close_code})")
        finally:
            closer.cancel()
            # gather, not suppress(CancelledError): that would also swallow our own cancellation.
            await asyncio.gather(closer, return_exceptions=True)

    async def _handle(self, ws: aiohttp.ClientWebSocketResponse, text: str) -> None:
        """Record one message raw, then answer or normalize it.

        The raw copy is written before anything can fail, and a message that cannot be
        parsed or normalized is logged and skipped so one bad frame never stops recording.
        """
        at = self._now()
        day = kst_day(at)
        received_at = at.astimezone(UTC)
        raw_row = {"received_at": received_at, "message": text}
        try:
            message = kis.parse_message(text)
        except kis.KisError as e:
            self._recorder.record("raw", day, "_unparsed", "_", raw_row, RAW_SCHEMA)
            log.warning("unparseable message kept raw: %s", e)
            return

        if isinstance(message, kis.SystemMessage):
            self._recorder.record("raw", day, "_system", message.tr_key or "_", raw_row, RAW_SCHEMA)
            if message.is_pingpong:
                await ws.pong(text.encode())
                return
            self._acknowledged_on_this_connection()
            if not message.ok:
                await self._on_rejected(ws, message)
            elif message.tr_key:
                self._health.subscribed((message.tr_id, message.tr_key))
            return

        self._recorder.record("raw", day, message.tr_id, message.symbol, raw_row, RAW_SCHEMA)
        self._health.received((message.tr_id, message.symbol))
        feed = WS_FEEDS.get(message.tr_id)
        if feed is None or message.encrypted:
            log.warning(
                "not normalizing %s frame (known feed: %s, encrypted: %s)",
                message.tr_id,
                feed is not None,
                message.encrypted,
            )
            return
        try:
            rows = [
                feed.normalize(dict(zip(feed.columns, fields, strict=True)), received_at)
                for fields in message.records(len(feed.columns))
            ]
        except (kis.KisError, ValueError, KeyError, IndexError):
            log.exception("could not normalize %s frame; kept raw only", message.tr_id)
            return
        for row in rows:
            self._recorder.record("events", day, feed.tr_id, row["symbol"], row, feed.schema)

    async def _on_rejected(self, ws: aiohttp.ClientWebSocketResponse, message: kis.SystemMessage) -> None:
        key = (message.tr_id, message.tr_key or "")
        if message.is_limit_rejection and message.tr_key:
            # Moved to REST for good: not registered again on reconnects.
            self._subscriptions = [(f, k) for f, k in self._subscriptions if (f.tr_id, k) != key]
            self._health.covered_elsewhere(key)
            _fall_back(
                self._poller, self._settings, message.tr_id, message.tr_key, "refused for the session limit"
            )
        elif message.is_invalid_approval:
            log.error("KIS refused the approval key; taking a new one and reconnecting")
            self._approval = None
            await ws.close()
        else:
            log.error(
                "KIS rejected %s %s: %s %s", message.tr_id, message.tr_key, message.code, message.message
            )


_STALE_PREFIX = "stale "


def _stale_name(key: tuple[str, str]) -> str:
    return f"{_STALE_PREFIX}{key[0]} {key[1]}"
