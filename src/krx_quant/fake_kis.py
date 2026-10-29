"""A local stand-in for the KIS Open API, for tests and offline runs.

It implements just enough of KIS to drive the collector: access-token and
WebSocket approval-key issuance, REST quotation endpoints answered from
canned responses, and a WebSocket endpoint that acknowledges subscriptions
(up to an optional per-session limit), pushes scripted data frames, and
sends PINGPONG heartbeats. It can also go silent on its open connections
without closing them, as a half-open TCP connection does. Everything it
receives is recorded so tests can assert on what the collector sent.

REST POSTs are answered the same way, by TR ID (``rest_posts``), and a TR
ID may get a per-second limit (``rest_limits``). ``FakeAccount`` puts a
mock-trading account behind it (#403): cash orders, their cancel, the order
and fill inquiry, the balance, the orderable cash, the quotes, and the
encrypted real-time fill notice. Its fields follow KIS's examples, not the
probe's evidence; its codes and messages are made up.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from aiohttp import WSMsgType, web
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from krx_quant import kis_orders
from krx_quant.feeds.stock import TRADE_COLUMNS
from krx_quant.kis import WS_PATH
from krx_quant.pricing import tick_size

# A canned REST answer: the JSON body, or the body and response headers.
RestAnswer = dict[str, Any] | tuple[dict[str, Any], dict[str, str]]


@dataclass
class FakeKis:
    app_key: str = "test-app-key"
    app_secret: str = "test-app-secret"
    # Frames to push after a (tr_id, tr_key) subscription is acknowledged.
    script: dict[tuple[str, str], list[str]] = field(default_factory=lambda: defaultdict(list))
    subscriptions: list[dict[str, Any]] = field(default_factory=list)
    pongs: list[bytes] = field(default_factory=list)
    # Registrations allowed per WebSocket session; None means unlimited.
    max_subscriptions: int | None = None
    # REST: tr_id -> function of the query params returning the JSON body to send, or the body
    # and response headers (e.g. KIS's continuation header ``tr_cont``). A request's own
    # ``tr_cont`` header reaches the function as the param ``_tr_cont``. The function raises
    # ConnectionResetError to drop the connection before answering, ConnectionAbortedError to
    # drop it once the answer has begun, or an ``aiohttp.web.HTTPException`` to answer with it.
    rest_responses: dict[str, Callable[[dict[str, str]], RestAnswer]] = field(default_factory=dict)
    # Master files served under /master/<name>, e.g. "fo_idx_code_mts.mst.zip".
    masters: dict[str, bytes] = field(default_factory=dict)
    # Access tokens: each issuance returns a new token valid until token_expires (KST,
    # "YYYY-MM-DD HH:MM:SS"). Issuing again within token_min_interval seconds is refused
    # with EGW00133, as KIS does (once per minute).
    token_expires: str = "2099-12-31 23:59:59"
    token_min_interval: float = 0.0
    tokens_issued: list[str] = field(default_factory=list)
    approvals_issued: list[str] = field(default_factory=list)
    _last_token_at: float | None = None
    # Telegram Bot API stand-in: messages sent to /bot<token>/sendMessage.
    telegram_messages: list[dict[str, Any]] = field(default_factory=list)
    # WebSocket sessions opened so far; set accept_ws=False to refuse new ones (HTTP 503).
    ws_connections: int = 0
    accept_ws: bool = True
    # Accept the WebSocket, then drop it before answering anything (a flapping server).
    drop_on_connect: bool = False
    # REST requests received: (monotonic arrival time, tr_id, path, query params plus the
    # authorization header under "_auth").
    rest_requests: list[tuple[float, str, str, dict[str, str]]] = field(default_factory=list)
    # The client's port of each REST request's connection, in the order of ``rest_requests``: a
    # new port is a new connection.
    rest_ports: list[int] = field(default_factory=list)
    # REST POSTs: tr_id -> function of the JSON body returning the answer, as ``rest_responses``.
    rest_posts: dict[str, Callable[[dict[str, Any]], RestAnswer]] = field(default_factory=dict)
    # POSTs received: (monotonic arrival time, tr_id, path, JSON body).
    posts_received: list[tuple[float, str, str, dict[str, Any]]] = field(default_factory=list)
    # REST calls (GET and POST) of a TR ID answered per second at most, refused beyond with KIS's
    # EGW00201: a TR ID's own limit, simpler than KIS's for the whole app key. A TR ID not in it has
    # none.
    rest_limits: dict[str, int] = field(default_factory=dict)
    _rest_times: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque))

    _demo_count: int = 0
    _runner: web.AppRunner | None = None
    _site: web.SockSite | None = None
    _sockets: list[web.WebSocketResponse] = field(default_factory=list)
    _transports: dict[web.WebSocketResponse, asyncio.Transport] = field(default_factory=dict)
    # Connections gone silent (go_silent): neither written to nor read from, until stop.
    _silent: list[asyncio.Transport] = field(default_factory=list)
    _pong_event: asyncio.Event = field(default_factory=asyncio.Event)
    _subscribed: asyncio.Condition = field(default_factory=asyncio.Condition)
    port: int = 0
    # Listen address; 0.0.0.0 lets other containers reach it.
    host: str = "127.0.0.1"

    @property
    def rest_url(self) -> str:
        return f"http://{self._reachable_host}:{self.port}"

    @property
    def ws_url(self) -> str:
        return f"ws://{self._reachable_host}:{self.port}"

    @property
    def _reachable_host(self) -> str:
        return "127.0.0.1" if self.host == "0.0.0.0" else self.host

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/oauth2/tokenP", self._token)
        app.router.add_post("/oauth2/Approval", self._approval)
        app.router.add_get(WS_PATH, self._ws)
        app.router.add_get("/uapi/{tail:.*}", self._rest)
        app.router.add_post("/uapi/{tail:.*}", self._rest_post)
        app.router.add_get("/master/{name}", self._master)
        app.router.add_post("/bot{token}/sendMessage", self._telegram)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        self.port = sock.getsockname()[1]
        self._site = web.SockSite(self._runner, sock)
        await self._site.start()

    async def stop(self) -> None:
        for transport in self._silent:
            transport.abort()  # its handler would wait for ever on a connection it no longer reads
        for ws in list(self._sockets):
            await ws.close()
        if self._runner is not None:
            await self._runner.cleanup()

    def subscribed_keys(self) -> set[tuple[str, str]]:
        return {(s["body"]["input"]["tr_id"], s["body"]["input"]["tr_key"]) for s in self.subscriptions}

    async def wait_for_subscriptions(self, keys: set[tuple[str, str]], timeout: float = 5.0) -> None:
        """Wait until every (tr_id, tr_key) in ``keys`` has been subscribed."""
        async with self._subscribed:
            await asyncio.wait_for(self._subscribed.wait_for(lambda: keys <= self.subscribed_keys()), timeout)

    def subscription_count(self, key: tuple[str, str]) -> int:
        return sum(
            1
            for s in self.subscriptions
            if (s["body"]["input"]["tr_id"], s["body"]["input"]["tr_key"]) == key
        )

    async def wait_for_subscription_count(
        self, key: tuple[str, str], count: int, timeout: float = 10.0
    ) -> None:
        """Wait until ``key`` has been registered ``count`` times (e.g. again after a reconnect)."""
        async with self._subscribed:
            await asyncio.wait_for(
                self._subscribed.wait_for(lambda: self.subscription_count(key) >= count), timeout
            )

    async def round_trip(self, timeout: float = 5.0) -> None:
        """Send a PINGPONG and wait for the client's pong.

        The client handles messages in order on one connection, so once the
        pong arrives every frame pushed before it has been processed.
        """
        self._pong_event.clear()
        await self.send_pingpong()
        await asyncio.wait_for(self._pong_event.wait(), timeout)

    async def push(self, frame: str) -> None:
        """Send a data frame to every connected client now."""
        for ws in self._sockets:
            await ws.send_str(frame)

    async def close_connections(self) -> None:
        """Drop every client connection, as KIS does on maintenance or errors."""
        for ws in list(self._sockets):
            await ws.close()

    def go_silent(self) -> None:
        """Stop talking on every open connection without closing it, as a connection that
        died half-open does (#308): nothing more is sent on it (no data, no PINGPONG), and
        nothing the client sends is read, a close included, so no close reply comes back.
        New connections are served as usual.
        """
        for ws in list(self._sockets):
            self._sockets.remove(ws)
            transport = self._transports.pop(ws)
            transport.pause_reading()
            self._silent.append(transport)

    async def send_demo_trades(self, at: datetime) -> None:
        """One synthetic integrated trade for every subscribed ``H0UNCNT0`` key (offline runs)."""
        self._demo_count += 1
        for tr_id, key in sorted(self.subscribed_keys()):
            if tr_id != "H0UNCNT0":
                continue
            values = {
                "MKSC_SHRN_ISCD": key,
                "STCK_CNTG_HOUR": at.strftime("%H%M%S"),
                "STCK_PRPR": str(80000 + 100 * (self._demo_count % 5)),
                "CNTG_VOL": "1",
                "ACML_VOL": str(self._demo_count),
                "CNTG_CLS_CODE": "1",
                "BSOP_DATE": at.strftime("%Y%m%d"),
            }
            await self.push(f"0|H0UNCNT0|001|{'^'.join(values.get(c, '0') for c in TRADE_COLUMNS)}")

    async def send_pingpong(self) -> None:
        msg = json.dumps({"header": {"tr_id": "PINGPONG", "datetime": "20261008093000"}})
        for ws in self._sockets:
            await ws.send_str(msg)

    async def _token(self, request: web.Request) -> web.Response:
        body = await request.json()
        if body.get("appkey") != self.app_key or body.get("appsecret") != self.app_secret:
            return web.json_response({"error_description": "invalid app key"}, status=403)
        now = time.monotonic()
        if self._last_token_at is not None and now - self._last_token_at < self.token_min_interval:
            return web.json_response(
                {
                    "error_code": "EGW00133",
                    "error_description": "접근토큰 발급 잠시 후 다시 시도하세요(1분당 1회)",
                },
                status=403,
            )
        self._last_token_at = now
        token = f"fake-access-token-{len(self.tokens_issued) + 1}"
        self.tokens_issued.append(token)
        return web.json_response(
            {
                "access_token": token,
                "access_token_token_expired": self.token_expires,
                "token_type": "Bearer",
                "expires_in": 86400,
            }
        )

    async def _approval(self, request: web.Request) -> web.Response:
        body = await request.json()
        if body.get("appkey") != self.app_key or body.get("secretkey") != self.app_secret:
            return web.json_response({"error_description": "invalid app key"}, status=403)
        key = f"fake-approval-key-{len(self.approvals_issued) + 1}"
        self.approvals_issued.append(key)
        return web.json_response({"approval_key": key})

    @property
    def master_url(self) -> str:
        return f"{self.rest_url}/master"

    async def _telegram(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.telegram_messages.append({"bot_token": request.match_info["token"], **body})
        return web.json_response({"ok": True, "result": {"message_id": len(self.telegram_messages)}})

    async def _master(self, request: web.Request) -> web.Response:
        body = self.masters.get(request.match_info["name"])
        if body is None:
            return web.Response(status=404)
        return web.Response(body=body, content_type="application/zip")

    def _refused(self, request: web.Request) -> web.Response | None:
        """KIS's refusal of a REST call: a token or key it does not know, or one call too many
        this second; None if it is answered."""
        h = request.headers
        if (
            h.get("authorization", "").removeprefix("Bearer ") not in self.tokens_issued
            or h.get("appkey") != self.app_key
            or h.get("appsecret") != self.app_secret
        ):
            return web.json_response(
                {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "invalid token"}, status=403
            )
        tr_id = h.get("tr_id", "")
        if tr_id in self.rest_limits:
            now, times = time.monotonic(), self._rest_times[tr_id]
            while times and now - times[0] >= 1.0:
                times.popleft()
            if len(times) >= self.rest_limits[tr_id]:
                busy = {"rt_cd": "1", "msg_cd": BUSY_MSG_CD, "msg1": "초당 거래건수를 초과하였습니다."}
                return web.json_response(busy, status=500)
            times.append(now)
        return None

    async def _rest_post(self, request: web.Request) -> web.StreamResponse:
        tr_id = request.headers.get("tr_id", "")
        body = await request.json()
        self.posts_received.append((time.monotonic(), tr_id, request.path, body))
        refused = self._refused(request)
        if refused is not None:
            return refused
        respond = self.rest_posts.get(tr_id)
        if respond is None:
            return web.json_response({"rt_cd": "1", "msg_cd": "OPSQ0002", "msg1": f"unknown tr_id {tr_id}"})
        answer = respond(body)
        answer_body, headers = answer if isinstance(answer, tuple) else (answer, {})
        return web.json_response(answer_body, headers=headers)

    async def _rest(self, request: web.Request) -> web.StreamResponse:
        h = request.headers
        tr_id = h.get("tr_id", "")
        query = {**request.query, "_auth": h.get("authorization", "")}
        self.rest_requests.append((time.monotonic(), tr_id, request.path, query))
        peer = request.transport.get_extra_info("peername") if request.transport is not None else None
        self.rest_ports.append(peer[1] if peer else 0)
        refused = self._refused(request)
        if refused is not None:
            return refused
        respond = self.rest_responses.get(tr_id)
        if respond is None:
            return web.json_response({"rt_cd": "1", "msg_cd": "OPSQ0002", "msg1": f"unknown tr_id {tr_id}"})
        params = dict(request.query)
        if h.get("tr_cont"):
            params["_tr_cont"] = h["tr_cont"]
        try:
            answer = respond(params)
        except ConnectionResetError:
            # Simulate KIS dropping the connection mid-request, before any answer.
            assert request.transport is not None
            request.transport.close()
            raise
        except ConnectionAbortedError:
            # Simulate KIS dropping the connection once its answer has begun: the status line and
            # headers sent, the body cut short.
            cut = web.StreamResponse(headers={"Content-Type": "application/json", "Content-Length": "100"})
            await cut.prepare(request)
            await cut.write(b'{"rt_cd": "0", "output"')
            assert request.transport is not None
            request.transport.close()
            return cut
        body, headers = answer if isinstance(answer, tuple) else (answer, {})
        return web.json_response(body, headers=headers)

    async def _ws(self, request: web.Request) -> web.StreamResponse:
        if not self.accept_ws:
            return web.Response(status=503, text="maintenance")
        self.ws_connections += 1
        ws = web.WebSocketResponse(autoping=False)
        await ws.prepare(request)
        if self.drop_on_connect:
            await ws.close()
            return ws
        self._sockets.append(ws)
        assert request.transport is not None
        self._transports[ws] = request.transport
        active: set[tuple[str, str]] = set()
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._on_subscribe(ws, json.loads(msg.data), active)
                elif msg.type == WSMsgType.PING:
                    await ws.pong(msg.data)
                elif msg.type == WSMsgType.PONG:
                    self.pongs.append(msg.data)
                    self._pong_event.set()
        finally:
            if ws in self._sockets:  # a silent one left the list already
                self._sockets.remove(ws)
            self._transports.pop(ws, None)
        return ws

    async def _on_subscribe(
        self, ws: web.WebSocketResponse, req: dict[str, Any], active: set[tuple[str, str]]
    ) -> None:
        header = req.get("header", {})
        inp = req.get("body", {}).get("input", {})
        key = (inp.get("tr_id", ""), inp.get("tr_key", ""))
        if header.get("tr_type") == "2":
            active.discard(key)
            await ws.send_str(_ack(key, "0", "OPSP0000", "UNSUBSCRIBE SUCCESS"))
            return
        if header.get("approval_key") not in self.approvals_issued:
            ok, reply = False, _ack(key, "1", "OPSP0011", "invalid approval : NOT FOUND")
        elif self.max_subscriptions is not None and len(active) >= self.max_subscriptions:
            ok, reply = False, _ack(key, "1", LIMIT_MSG_CD, LIMIT_MSG)
        else:
            ok, reply = True, _ack(key, "0", "OPSP0000", "SUBSCRIBE SUCCESS")
            active.add(key)
        await ws.send_str(reply)
        async with self._subscribed:
            self.subscriptions.append(req)
            self._subscribed.notify_all()
        if ok:
            for frame in self.script.get(key, []):
                await ws.send_str(frame)


# What KIS answers when a session exceeds its registration limit (observed live 2026-10-08).
LIMIT_MSG_CD = "OPSP0008"
LIMIT_MSG = "MAX SUBSCRIBE OVER"
# What KIS answers to a call beyond its per-second limit (``kis._BUSY``).
BUSY_MSG_CD = "EGW00201"
# The key and IV every subscription's answer gives, which encrypt the fill notice's frames.
NOTICE_KEY = "0123456789abcdef0123456789abcdef"
NOTICE_IV = "0123456789abcdef"


def _ack(key: tuple[str, str], rt_cd: str, msg_cd: str, msg1: str) -> str:
    return json.dumps(
        {
            "header": {"tr_id": key[0], "tr_key": key[1], "encrypt": "N"},
            "body": {
                "rt_cd": rt_cd,
                "msg_cd": msg_cd,
                "msg1": msg1,
                "output": {"iv": NOTICE_IV, "key": NOTICE_KEY},
            },
        }
    )


def encrypt(key: str, iv: str, text: str) -> str:
    """``text`` as KIS sends an encrypted frame's fields: base64 of AES-256-CBC, PKCS#7 padded
    (``kis_orders.decrypt`` reads it back)."""
    padder = padding.PKCS7(128).padder()
    padded = padder.update(text.encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key.encode()), modes.CBC(iv.encode())).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode()


# The mock account's made-up refusals (KIS's own codes are what the probe records, #403).
TICK_REFUSED = ("FK0001", "주문단가가 호가단위에 맞지 않습니다.")
CASH_REFUSED = ("FK0002", "주문가능금액을 초과 했습니다.")
SHARES_REFUSED = ("FK0003", "주문가능수량을 초과 했습니다.")
NOTHING_TO_CANCEL = ("FK0004", "정정/취소할 수량이 없습니다.")
UNSUPPORTED = ("FK0005", "모의투자에서는 해당업무가 제공되지 않습니다.")
BUY_SIDE, SELL_SIDE = "02", "01"  # SLL_BUY_DVSN_CD


@dataclass
class FakeOrder:
    """An order on ``FakeAccount``: an order, or a cancel (``original`` names the order)."""

    number: str
    side: str  # BUY_SIDE or SELL_SIDE
    symbol: str
    quantity: int
    price: int
    at: str  # HHMMSS
    filled: int = 0
    fill_price: int = 0
    cancelled: int = 0  # cancelled of it (an order), or by it (a cancel)
    original: str = ""

    @property
    def left(self) -> int:
        return self.quantity - self.filled - self.cancelled if not self.original else 0


@dataclass
class FakeAccount:
    """A mock-trading account behind ``kis`` (#403), registered on it by ``install``.

    A limit buy at the ask or above fills at the ask at once, a limit sell at the bid or below at
    the bid, unless ``fills`` is off; any other rests until cancelled. A price off the tick, a buy
    beyond the cash or a sell beyond the shares held is refused. Each order, fill, cancel and
    refusal is told on the fill notice (encrypted, to every open WebSocket) when ``notices``.
    The cancellable-orders inquiry is refused as unsupported, as KIS's example suggests of the mock.
    """

    kis: FakeKis
    today: date
    cash: int = 10_000_000
    holdings: dict[str, int] = field(default_factory=dict)
    price: int = 61_000
    ask: int = 61_100
    bid: int = 61_000
    upper: int = 79_300
    lower: int = 42_700
    fills: bool = True
    # A marketable order fills only once its cancel comes (``fills`` off), which then finds nothing
    # to cancel: a fill that races the cancel.
    fills_late: bool = False
    notices: bool = True
    hts_id: str = "testhts01"
    account: str = "1234567801"
    orders: list[FakeOrder] = field(default_factory=list)
    _pushes: set[asyncio.Task[None]] = field(default_factory=set)

    def install(self) -> FakeAccount:
        for tr in (kis_orders.BUY, kis_orders.SELL):
            self.kis.rest_posts[tr.mock] = self._order
        self.kis.rest_posts[kis_orders.CANCEL.mock] = self._cancel
        self.kis.rest_responses[kis_orders.FILLS.mock] = self._fills
        self.kis.rest_responses[kis_orders.BALANCE.mock] = self._balance
        self.kis.rest_responses[kis_orders.ORDERABLE.mock] = self._orderable
        self.kis.rest_responses[kis_orders.CANCELABLE.mock] = lambda _: _refusal(UNSUPPORTED)
        self.kis.rest_responses[kis_orders.PRICE] = self._price
        self.kis.rest_responses[kis_orders.BOOK] = self._book
        return self

    def placed(self, side: str) -> list[FakeOrder]:
        return [o for o in self.orders if o.side == side and not o.original]

    def _order(self, body: dict[str, Any]) -> RestAnswer:
        side = SELL_SIDE if "SLL_TYPE" in body else BUY_SIDE
        symbol, quantity, price = str(body["PDNO"]), int(body["ORD_QTY"]), int(body["ORD_UNPR"])
        if price % tick_size(price):
            return self._refuse(TICK_REFUSED, side, symbol, quantity, price)
        if side == BUY_SIDE and quantity * price > self.cash - self._held_cash():
            return self._refuse(CASH_REFUSED, side, symbol, quantity, price)
        if side == SELL_SIDE and quantity > self.holdings.get(symbol, 0) - self._held_shares(symbol):
            return self._refuse(SHARES_REFUSED, side, symbol, quantity, price)
        order = FakeOrder(self._number(), side, symbol, quantity, price, self._now())
        self.orders.append(order)
        self._tell(order, "1", "0", "1", quantity, price)
        if self.fills and self._marketable(order):
            self._fill(order)
        return _accepted(order, "주문 전송 완료 되었습니다.")

    def _marketable(self, order: FakeOrder) -> bool:
        return order.price >= self.ask if order.side == BUY_SIDE else order.price <= self.bid

    def _fill(self, order: FakeOrder) -> None:
        """Fill what is left of ``order`` at the ask (a buy) or the bid (a sell)."""
        quantity = order.left
        order.filled += quantity
        order.fill_price = self.ask if order.side == BUY_SIDE else self.bid
        amount = quantity * order.fill_price
        self.cash += -amount if order.side == BUY_SIDE else amount
        change = quantity if order.side == BUY_SIDE else -quantity
        self.holdings[order.symbol] = self.holdings.get(order.symbol, 0) + change
        self._tell(order, "2", "0", "2", quantity, order.fill_price)

    def _cancel(self, body: dict[str, Any]) -> RestAnswer:
        original = next((o for o in self.orders if o.number == str(body["ORGN_ODNO"])), None)
        if original is not None and self.fills_late and original.left > 0 and self._marketable(original):
            self._fill(original)
        if original is None or original.left <= 0:
            return _refusal(NOTHING_TO_CANCEL)
        cancel = FakeOrder(
            self._number(),
            original.side,
            original.symbol,
            original.left,
            0,
            self._now(),
            cancelled=original.left,
            original=original.number,
        )
        original.cancelled += original.left
        self.orders.append(cancel)
        self._tell(cancel, "1", "2", "2", cancel.quantity, original.price)
        return _accepted(cancel, "정정/취소 주문 전송 완료 되었습니다.")

    def _refuse(
        self, refusal: tuple[str, str], side: str, symbol: str, quantity: int, price: int
    ) -> RestAnswer:
        refused = FakeOrder("", side, symbol, quantity, price, self._now())
        self._tell(refused, "1", "0", "1", quantity, price, refused=True)
        return _refusal(refusal)

    def _fills(self, params: dict[str, str]) -> RestAnswer:
        rows = [
            self._row(o)
            for o in self.orders
            if (not params.get("ODNO") or o.number == params["ODNO"])
            and (not params.get("PDNO") or o.symbol == params["PDNO"])
        ]
        totals = {
            "tot_ord_qty": str(sum(int(r["ord_qty"]) for r in rows)),
            "tot_ccld_qty": str(sum(int(r["tot_ccld_qty"]) for r in rows)),
            "tot_ccld_amt": str(sum(int(r["tot_ccld_amt"]) for r in rows)),
            "pchs_avg_pric": "0.0000",
            "prsm_tlex_smtl": "0",
        }
        return {
            "rt_cd": "0",
            "msg_cd": "FK0000",
            "msg1": "조회가 완료되었습니다.",
            "output1": rows,
            "output2": totals,
        }

    def _row(self, o: FakeOrder) -> dict[str, str]:
        return {
            "ord_dt": self.today.strftime("%Y%m%d"),
            "ord_gno_brno": "01790",
            "odno": o.number,
            "orgn_odno": o.original,
            "ord_dvsn_name": "지정가",
            "sll_buy_dvsn_cd": o.side,
            "sll_buy_dvsn_cd_name": "현금매수" if o.side == BUY_SIDE else "현금매도",
            "pdno": o.symbol,
            "prdt_name": "삼성전자",
            "ord_qty": str(o.quantity),
            "ord_unpr": str(o.price),
            "ord_tmd": o.at,
            "tot_ccld_qty": str(o.filled),
            "avg_prvs": str(o.fill_price),
            "cncl_yn": "Y" if o.original else "N",
            "tot_ccld_amt": str(o.filled * o.fill_price),
            "ord_dvsn_cd": "00",
            "cnc_cfrm_qty": str(o.cancelled if o.original else 0),
            "rmn_qty": str(o.left),
            "rjct_qty": "0",
            "excg_id_dvsn_cd": "KRX",
            # What KIS's example lists beside them, made up here: a phone number and an IP address.
            "ctac_tlno": "01012345678",
            "inqr_ip_addr": "10.11.12.13",
        }

    def _balance(self, params: dict[str, str]) -> RestAnswer:
        rows = [
            {
                "pdno": symbol,
                "prdt_name": "삼성전자",
                "hldg_qty": str(quantity),
                "ord_psbl_qty": str(quantity - self._held_shares(symbol)),
                "pchs_avg_pric": f"{self.ask}.0000",
                "pchs_amt": str(quantity * self.ask),
                "prpr": str(self.price),
                "evlu_amt": str(quantity * self.price),
                "evlu_pfls_amt": str(quantity * (self.price - self.ask)),
            }
            for symbol, quantity in self.holdings.items()
            if quantity
        ]
        totals = {
            "dnca_tot_amt": str(self.cash),
            "nxdy_excc_amt": str(self.cash),
            "prvs_rcdl_excc_amt": str(self.cash),
            "tot_evlu_amt": str(self.cash + sum(int(r["evlu_amt"]) for r in rows)),
        }
        return {
            "rt_cd": "0",
            "msg_cd": "FK0000",
            "msg1": "조회가 완료되었습니다.",
            "output1": rows,
            "output2": [totals],
        }

    def _orderable(self, params: dict[str, str]) -> RestAnswer:
        price = int(params["ORD_UNPR"]) if params.get("ORD_UNPR") else self.upper
        free = self.cash - self._held_cash()
        output = {
            "ord_psbl_cash": str(free),
            "nrcvb_buy_amt": str(free),
            "nrcvb_buy_qty": str(free // price),
            "max_buy_amt": str(free),
            "max_buy_qty": str(free // price),
            "psbl_qty_calc_unpr": str(price),
        }
        return {"rt_cd": "0", "msg_cd": "FK0000", "msg1": "매수가능 조회가 완료되었습니다.", "output": output}

    def _price(self, params: dict[str, str]) -> RestAnswer:
        output = {
            "stck_prpr": str(self.price),
            "stck_mxpr": str(self.upper),
            "stck_llam": str(self.lower),
            "aspr_unit": str(tick_size(self.price)),
        }
        return {"rt_cd": "0", "msg_cd": "FK0000", "msg1": "정상처리 되었습니다.", "output": output}

    def _book(self, params: dict[str, str]) -> RestAnswer:
        book = {"aspr_acpt_hour": "100000", "askp1": str(self.ask), "bidp1": str(self.bid)}
        return {
            "rt_cd": "0",
            "msg_cd": "FK0000",
            "msg1": "정상처리 되었습니다.",
            "output1": book,
            "output2": {},
        }

    def _held_cash(self) -> int:
        return sum(o.left * o.price for o in self.orders if o.side == BUY_SIDE)

    def _held_shares(self, symbol: str) -> int:
        return sum(o.left for o in self.orders if o.side == SELL_SIDE and o.symbol == symbol)

    def _number(self) -> str:
        return f"{len(self.orders) + 1:010d}"

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%H%M%S")

    def _tell(
        self,
        order: FakeOrder,
        filled: str,
        correction: str,
        accepted: str,
        quantity: int,
        price: int,
        *,
        refused: bool = False,
    ) -> None:
        """Push the fill notice of ``order``: ``filled`` (CNTG_YN) 1 an order, 2 a fill;
        ``correction`` (RCTF_CLS) 0, or 2 a cancel; ``accepted`` (ACPT_YN)."""
        if not self.notices:
            return
        values = {
            "CUST_ID": self.hts_id,
            "ACNT_NO": self.account,
            "ODER_NO": order.number,
            "OODER_NO": order.original,
            "SELN_BYOV_CLS": order.side,
            "RCTF_CLS": correction,
            "ODER_KIND": "00",
            "ODER_COND": "0",
            "STCK_SHRN_ISCD": order.symbol,
            "CNTG_QTY": str(quantity),
            "CNTG_UNPR": str(price) if filled == "2" else "0",
            "STCK_CNTG_HOUR": self._now(),
            "RFUS_YN": "1" if refused else "0",
            "CNTG_YN": filled,
            "ACPT_YN": accepted,
            "BRNC_NO": "01790",
            "ODER_QTY": str(order.quantity),
            "ACNT_NAME": "홍길동",
            "ORD_COND_PRC": "0",
            "ORD_EXG_GB": "1",
            "POPUP_YN": "N",
            "FILLER": "",
            "CRDT_CLS": "10",
            "CRDT_LOAN_DATE": "",
            "CNTG_ISNM40": "삼성전자",
            "ODER_PRC": str(order.price),
        }
        fields = "^".join(values[c] for c in kis_orders.FILL_NOTICE_COLUMNS)
        frame = f"1|{kis_orders.FILL_NOTICE.mock}|001|{encrypt(NOTICE_KEY, NOTICE_IV, fields)}"
        task = asyncio.get_running_loop().create_task(self.kis.push(frame))
        self._pushes.add(task)
        task.add_done_callback(self._pushes.discard)


_DONE = {"rt_cd": "0", "msg_cd": "FK0000", "msg1": "조회가 완료되었습니다."}


def _accepted(order: FakeOrder, message: str) -> RestAnswer:
    output = {"KRX_FWDG_ORD_ORGNO": "01790", "ODNO": order.number, "ORD_TMD": order.at}
    return {"rt_cd": "0", "msg_cd": "FK0000", "msg1": message, "output": output}


def _refusal(refusal: tuple[str, str]) -> RestAnswer:
    return {"rt_cd": "1", "msg_cd": refusal[0], "msg1": refusal[1]}
