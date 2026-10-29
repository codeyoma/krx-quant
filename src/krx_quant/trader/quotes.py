"""The best quotes the trader fills on (#394): streamed by the collector, inside the compose network.

The live quotes live in the collector's memory alone (one KIS WebSocket session per app key), so
the collector streams the traded symbol's best quotes to the trader: a WebSocket of its own
(``GET /v1/quotes``) on a listener of its own (``KRX_QUOTES_HOST``, ``KRX_QUOTES_PORT``), never
on the Engine API's. Compose publishes the Engine API's port on the Tailscale address and no
other: the quote port is reachable from the containers of the compose network only
(``compose.yaml``).

A quote is the integrated best bid and ask the touch label and the barrier answer are made from
(``kinds.QuoteLog``): the integrated WebSocket book's (``H0UNASP0``, with the sizes) and the
integrated trades' (``H0UNCNT0``, without them); a REST snapshot is never one, nor a one-sided
quote. A crossed one (the best ask below the best bid, two venues) is kept, as the barrier counts
it (#187). Each message is one JSON text: ``symbol``, ``feed``, ``received_at`` (the collector's
receipt time, ISO 8601 in KST), ``bid``, ``ask``, ``bid_size`` and ``ask_size`` (null from a
trade). A client is sent each symbol's latest quote when it connects, then every quote as it
comes; one that falls behind by ``QUEUE_SIZE`` is closed and should reconnect.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import aiohttp
from aiohttp import WSCloseCode, web

from krx_quant.feeds import BOOK, TRADE
from krx_quant.feeds.base import KST

log = logging.getLogger(__name__)

PATH = "/v1/quotes"
QUEUE_SIZE = 10_000  # quotes a client may fall behind by
_FEEDS = frozenset({BOOK.tr_id, TRADE.tr_id})
_CLOSE_SECONDS = 5.0


@dataclass(frozen=True)
class BestQuote:
    """A best quote as received by the collector."""

    received_at: datetime
    bid: float
    ask: float
    bid_size: float | None = None  # from the book; None from a trade
    ask_size: float | None = None


def quote_of(feed: str, row: Mapping[str, Any]) -> BestQuote | None:
    """The best quote of a recorded event (``collector``'s ``on_event``): the integrated book's or
    trade's; None for any other feed, or a one-sided quote."""
    if feed not in _FEEDS:
        return None
    if feed == TRADE.tr_id:
        bid, ask, bid_size, ask_size = row.get("bid1"), row.get("ask1"), None, None
    else:
        bid, ask = row.get("bid_px_1"), row.get("ask_px_1")
        bid_size, ask_size = row.get("bid_qty_1"), row.get("ask_qty_1")
    if not bid or not ask or bid <= 0 or ask <= 0:
        return None
    return BestQuote(
        row["received_at"],
        float(bid),
        float(ask),
        None if bid_size is None else float(bid_size),
        None if ask_size is None else float(ask_size),
    )


def quote_json(symbol: str, feed: str, quote: BestQuote) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "feed": feed,
        "received_at": quote.received_at.astimezone(KST).isoformat(),
        "bid": quote.bid,
        "ask": quote.ask,
        "bid_size": quote.bid_size,
        "ask_size": quote.ask_size,
    }


def _message(symbol: str, feed: str, quote: BestQuote) -> str:
    return json.dumps(quote_json(symbol, feed, quote), ensure_ascii=False)


def parse_quote(text: str, symbol: str) -> BestQuote | None:
    """A message of the stream, if it is a quote of ``symbol`` the trader can fill on."""
    try:
        body = json.loads(text)
        if body.get("symbol") != symbol:
            return None
        received_at = datetime.fromisoformat(body["received_at"])
        bid, ask = float(body["bid"]), float(body["ask"])
        sizes = [None if body.get(k) is None else float(body[k]) for k in ("bid_size", "ask_size")]
    except (ValueError, TypeError, KeyError, AttributeError):
        log.warning("quotes: a message the trader cannot read: %.200s", text)
        return None
    if received_at.tzinfo is None or bid <= 0 or ask <= 0:
        return None
    return BestQuote(received_at, bid, ask, sizes[0], sizes[1])


@dataclass(eq=False)  # each client is itself
class _Client:
    ws: web.WebSocketResponse
    queue: asyncio.Queue[str] = field(default_factory=lambda: asyncio.Queue(QUEUE_SIZE))


class QuoteFeed:
    """The collector's side: each event's best quote of ``symbols``, to every connected client."""

    def __init__(self, symbols: Collection[str]) -> None:
        self._symbols = frozenset(symbols)
        self._clients: set[_Client] = set()
        # Each symbol's latest quote and its feed: made into a message only for a client (the
        # collector takes thousands of quotes a second at the busiest, most with no client).
        self._latest: dict[str, tuple[str, BestQuote]] = {}
        self._closing: set[asyncio.Task[Any]] = set()

    @property
    def clients(self) -> int:
        return len(self._clients)

    def on_event(self, feed: str, row: Mapping[str, Any]) -> None:
        if feed not in _FEEDS or row.get("symbol") not in self._symbols:
            return
        quote = quote_of(feed, row)
        if quote is None:
            return
        self._latest[row["symbol"]] = (feed, quote)
        if not self._clients:
            return
        text = _message(row["symbol"], feed, quote)
        for client in list(self._clients):
            try:
                client.queue.put_nowait(text)
            except asyncio.QueueFull:
                log.warning("quote client %s fell behind; closed", client.ws)
                self._clients.discard(client)
                task = asyncio.create_task(
                    client.ws.close(code=WSCloseCode.TRY_AGAIN_LATER, message=b"too slow")
                )
                self._closing.add(task)
                task.add_done_callback(self._closing.discard)

    def app(self) -> web.Application:
        """The quote stream's own application: ``GET /v1/quotes`` and nothing else."""
        app = web.Application()
        app.router.add_get(PATH, self.handle)
        app.on_shutdown.append(self._shutdown)
        return app

    async def handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=10)
        await ws.prepare(request)
        client = _Client(ws)
        for symbol, (feed, quote) in self._latest.items():
            client.queue.put_nowait(_message(symbol, feed, quote))
        self._clients.add(client)
        sender = asyncio.create_task(self._send(client))
        try:
            async for _ in ws:  # clients send nothing; reading notices a close
                pass
        finally:
            self._clients.discard(client)
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
        return ws

    async def _send(self, client: _Client) -> None:
        try:
            while True:
                await client.ws.send_str(await client.queue.get())
        except ConnectionError:  # closed meanwhile (too slow, gone, or shutting down)
            pass

    async def _shutdown(self, app: web.Application) -> None:
        closing = [
            c.ws.close(code=WSCloseCode.GOING_AWAY, message=b"collector shutting down") for c in self._clients
        ]
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*closing, *self._closing, return_exceptions=True), _CLOSE_SECONDS
            )


async def follow_quotes(
    session: aiohttp.ClientSession,
    url: str,
    symbol: str,
    on_quote: Callable[[BestQuote], None],
    *,
    retry_seconds: float = 1.0,
    retry_max_seconds: float = 10.0,
) -> None:
    """The trader's side: ``symbol``'s quotes from the collector's stream at ``url`` to
    ``on_quote``, connecting again whenever the stream ends, until cancelled. A failure is logged
    once until the stream answers again."""
    delay = retry_seconds
    down: str | None = None  # why the stream is down, once said
    while True:
        try:
            async with session.ws_connect(url, heartbeat=10) as ws:
                log.info("quotes: following %s", url)
                down, delay = None, retry_seconds
                async for message in ws:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        quote = parse_quote(message.data, symbol)
                        if quote is not None:
                            on_quote(quote)
                    elif message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
            why = "the stream closed"
        except (aiohttp.ClientError, OSError) as e:
            why = f"{type(e).__name__}: {e}"
        if down is None:
            log.warning("quotes: %s (%s); connecting again", why, url)
        down = why
        await asyncio.sleep(delay)
        delay = min(delay * 2, retry_max_seconds)
