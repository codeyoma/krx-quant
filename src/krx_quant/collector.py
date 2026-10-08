"""Collector: subscribes to KIS real-time feeds, polls REST feeds, and records everything."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime

import aiohttp

from krx_quant import kis
from krx_quant.config import Settings
from krx_quant.feeds import (
    BOOK,
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
from krx_quant.masters import FrontMonths, MasterError, fetch_front_months
from krx_quant.polling import Poller, PollJob
from krx_quant.ratelimit import RateLimiter
from krx_quant.recorder import ParquetRecorder

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]

# How often the receive loop wakes up to flush partitions whose time limit has passed.
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
    plan += [(TRADE, etf) for etf in settings.etf_symbols]
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
    try:
        async with aiohttp.ClientSession() as session:
            token = await _issue_rest_token(session, settings)
            approval = await _approval_key(session, settings)
            poller = _poller(session, settings, token, recorder, now, stop)
            fronts = await front_months(session, settings)
            plan = subscription_plan(settings, fronts)
            cap = len(plan) if settings.ws_max_subscriptions is None else settings.ws_max_subscriptions
            for feed, key in plan[cap:]:
                _fall_back(poller, settings, feed.tr_id, key, "beyond the configured cap")
            for job in rest_plan(settings):
                poller.add(job)
            try:
                async with session.ws_connect(settings.kis_ws_url + kis.WS_PATH) as ws:
                    for feed, key in plan[:cap]:
                        await ws.send_str(kis.subscribe_message(approval, feed.tr_id, key))
                    await _receive_loop(ws, recorder, now, stop, poller, settings)
            finally:
                # Jobs may be queued on the rate limiter; at shutdown, drop them.
                await poller.close()
    finally:
        recorder.close()


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


async def _issue_rest_token(session: aiohttp.ClientSession, settings: Settings) -> str | None:
    """Issue the REST access token, or return None so the stream can still run.

    KIS allows one token issuance per minute, so a refusal (e.g. on a quick
    restart) disables REST polling for this run instead of stopping recording.
    """
    try:
        return await kis.issue_access_token(
            session, settings.kis_rest_url, settings.kis_app_key, settings.kis_app_secret
        )
    except kis.KisError as e:
        log.warning("REST access token not issued: %s", e)
        return None


def _poller(
    session: aiohttp.ClientSession,
    settings: Settings,
    token: str | None,
    recorder: ParquetRecorder,
    now: Clock,
    stop: asyncio.Event,
) -> Poller:
    if token is None:
        log.error("REST polling disabled: no access token")
        return Poller(None, recorder, now, stop)
    client = kis.RestClient(
        session,
        settings.kis_rest_url,
        settings.kis_app_key,
        settings.kis_app_secret,
        token,
        RateLimiter(settings.rest_max_per_second),
    )
    return Poller(client, recorder, now, stop)


def _fall_back(poller: Poller, settings: Settings, tr_id: str, key: str, reason: str) -> None:
    fallback = FALLBACKS.get(tr_id)
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


async def _receive_loop(
    ws: aiohttp.ClientWebSocketResponse,
    recorder: ParquetRecorder,
    now: Clock,
    stop: asyncio.Event,
    poller: Poller,
    settings: Settings,
) -> None:
    """Read messages until the socket closes; ``stop`` closes it from a helper task.

    The read itself is never cancelled: cancelling an in-flight receive can drop a
    frame that has already arrived. Time-based flushing runs in its own task.
    """

    async def flush_periodically() -> None:
        while True:
            await asyncio.sleep(_TICK_SECONDS)
            try:
                recorder.flush_due()
            except Exception:
                log.exception("periodic flush failed; rows stay buffered for the next attempt")

    async def close_on_stop() -> None:
        await stop.wait()
        await ws.close()

    def on_rejected(message: kis.SystemMessage) -> None:
        if message.is_limit_rejection and message.tr_key:
            _fall_back(poller, settings, message.tr_id, message.tr_key, "refused for the session limit")
        else:
            log.error(
                "KIS rejected %s %s: %s %s", message.tr_id, message.tr_key, message.code, message.message
            )

    helpers = [asyncio.create_task(flush_periodically()), asyncio.create_task(close_on_stop())]
    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await _handle(ws, msg.data, recorder, now(), on_rejected)
            elif msg.type == aiohttp.WSMsgType.ERROR:
                raise kis.KisError(f"websocket error: {ws.exception()}")
        if not stop.is_set():
            raise kis.KisError(f"websocket closed by KIS (code {ws.close_code})")
    finally:
        for task in helpers:
            task.cancel()


async def _handle(
    ws: aiohttp.ClientWebSocketResponse,
    text: str,
    recorder: ParquetRecorder,
    at: datetime,
    on_rejected: Callable[[kis.SystemMessage], None],
) -> None:
    """Record one message raw, then answer or normalize it.

    The raw copy is written before anything can fail, and a message that cannot be
    parsed or normalized is logged and skipped so one bad frame never stops recording.
    """
    day = at.astimezone(KST).date()
    received_at = at.astimezone(UTC)
    raw_row = {"received_at": received_at, "message": text}
    try:
        message = kis.parse_message(text)
    except kis.KisError as e:
        recorder.record("raw", day, "_unparsed", "_", raw_row, RAW_SCHEMA)
        log.warning("unparseable message kept raw: %s", e)
        return

    if isinstance(message, kis.SystemMessage):
        recorder.record("raw", day, "_system", message.tr_key or "_", raw_row, RAW_SCHEMA)
        if message.is_pingpong:
            await ws.pong(text.encode())
        elif not message.ok:
            on_rejected(message)
        return

    recorder.record("raw", day, message.tr_id, message.symbol, raw_row, RAW_SCHEMA)
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
        recorder.record("events", day, feed.tr_id, row["symbol"], row, feed.schema)
