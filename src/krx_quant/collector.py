"""Collector: subscribes to KIS real-time feeds and records everything it receives."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime

import aiohttp

from krx_quant import kis
from krx_quant.config import Settings
from krx_quant.feeds import FEEDS_BY_TR_ID, KST, RAW_SCHEMA, STREAM_FEEDS
from krx_quant.recorder import ParquetRecorder

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]

# How often the receive loop wakes up to flush partitions whose time limit has passed.
_TICK_SECONDS = 1.0


def _system_clock() -> datetime:
    return datetime.now(KST)


async def run_collector(settings: Settings, *, stop: asyncio.Event, clock: Clock | None = None) -> None:
    """Record the configured feeds until ``stop`` is set, then flush and return."""
    now = clock or _system_clock
    recorder = ParquetRecorder(settings.data_dir, settings.flush_rows, settings.flush_seconds)
    try:
        async with aiohttp.ClientSession() as session:
            await _check_rest_token(session, settings)
            approval = await kis.issue_approval_key(
                session, settings.kis_rest_url, settings.kis_app_key, settings.kis_app_secret
            )
            async with session.ws_connect(settings.kis_ws_url + kis.WS_PATH) as ws:
                for symbol in settings.symbols:
                    for feed in STREAM_FEEDS:
                        await ws.send_str(kis.subscribe_message(approval, feed.tr_id, symbol))
                await _receive_loop(ws, recorder, now, stop)
    finally:
        recorder.close()


async def _check_rest_token(session: aiohttp.ClientSession, settings: Settings) -> None:
    """Issue the REST access token so a bad key or account problem shows up at startup.

    The stream itself only needs the approval key, and KIS allows one token issuance
    per minute, so a refusal here (e.g. on a quick restart) is logged, not fatal.
    REST feeds will reuse the token once they exist.
    """
    try:
        await kis.issue_access_token(
            session, settings.kis_rest_url, settings.kis_app_key, settings.kis_app_secret
        )
    except kis.KisError as e:
        log.warning("REST access token not issued: %s", e)


async def _receive_loop(
    ws: aiohttp.ClientWebSocketResponse, recorder: ParquetRecorder, now: Clock, stop: asyncio.Event
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

    helpers = [asyncio.create_task(flush_periodically()), asyncio.create_task(close_on_stop())]
    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await _handle(ws, msg.data, recorder, now())
            elif msg.type == aiohttp.WSMsgType.ERROR:
                raise kis.KisError(f"websocket error: {ws.exception()}")
        if not stop.is_set():
            raise kis.KisError(f"websocket closed by KIS (code {ws.close_code})")
    finally:
        for task in helpers:
            task.cancel()


async def _handle(
    ws: aiohttp.ClientWebSocketResponse, text: str, recorder: ParquetRecorder, at: datetime
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
            log.error("KIS rejected %s %s: %s", message.tr_id, message.tr_key, message.message)
        return

    recorder.record("raw", day, message.tr_id, message.symbol, raw_row, RAW_SCHEMA)
    feed = FEEDS_BY_TR_ID.get(message.tr_id)
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
