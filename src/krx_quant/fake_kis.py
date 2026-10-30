"""A local stand-in for the KIS Open API, for tests and offline runs.

It implements just enough of KIS to drive the collector: access-token and
WebSocket approval-key issuance, REST quotation endpoints answered from
canned responses, and a WebSocket endpoint that acknowledges subscriptions
(up to an optional per-session limit), pushes scripted data frames, and
sends PINGPONG heartbeats. It can also go silent on its open connections
without closing them, as a half-open TCP connection does. Everything it
receives is recorded so tests can assert on what the collector sent.

REST POSTs are answered the same way, by TR ID (``rest_posts``), and a TR
ID may get a per-second limit (``rest_limits``) or refuse its next calls for
it (``busy``). Tokens can be expired (``expire_tokens``). ``FakeAccount`` puts
a mock-trading account behind it (#403, #404): cash orders, their cancel, the
order and fill inquiry (full and partial fills), the balance, the orderable
cash, the quotes, and the encrypted real-time fill notice. Its fields, codes
and messages are those the order probe measured on KIS's mock server
(``kis_orders.PROBE_COMMENT``) but where it says they are made up. Its orders are
day orders (#406): each is of the day it was placed, and once that day is over it
neither fills nor can be cancelled.
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
from krx_quant.pricing import SELL_TAX, tick_size

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
    # REST POSTs: tr_id -> function of the JSON body returning the answer, as ``rest_responses``;
    # it raises ConnectionResetError to drop the connection before answering.
    rest_posts: dict[str, Callable[[dict[str, Any]], RestAnswer]] = field(default_factory=dict)
    # POSTs received: (monotonic arrival time, tr_id, path, JSON body).
    posts_received: list[tuple[float, str, str, dict[str, Any]]] = field(default_factory=list)
    # REST calls (GET and POST) of a TR ID answered per second at most, refused beyond with KIS's
    # EGW00201: a TR ID's own limit, simpler than KIS's for the whole app key. A TR ID not in it has
    # none.
    rest_limits: dict[str, int] = field(default_factory=dict)
    _rest_times: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque))
    # REST calls (GET and POST) of a TR ID to refuse next with EGW00201, however few came this
    # second: tr_id -> how many.
    busy: dict[str, int] = field(default_factory=dict)
    # Tokens KIS no longer takes (``expire_tokens``): a call with one is refused with EGW00123.
    expired_tokens: set[str] = field(default_factory=set)

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

    def expire_tokens(self) -> None:
        """Every token issued so far is no longer taken, as once it expires on KIS's side."""
        self.expired_tokens.update(self.tokens_issued)

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
        token = h.get("authorization", "").removeprefix("Bearer ")
        if (
            token not in self.tokens_issued
            or h.get("appkey") != self.app_key
            or h.get("appsecret") != self.app_secret
        ):
            return web.json_response(
                {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "invalid token"}, status=403
            )
        if token in self.expired_tokens:
            # The code the collector's client takes for a token KIS no longer accepts
            # (``kis._TOKEN_REJECTED``); its status and message are made up (not seen on the mock).
            expired = {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "기간이 만료된 token 입니다."}
            return web.json_response(expired, status=500)
        tr_id = h.get("tr_id", "")
        if self.busy.get(tr_id, 0) > 0:
            self.busy[tr_id] -= 1
            busy = {"rt_cd": "1", "msg_cd": BUSY_MSG_CD, "msg1": "초당 거래건수를 초과하였습니다."}
            return web.json_response(busy, status=500)
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
        try:
            answer = respond(body)
        except ConnectionResetError:
            # KIS's answer lost: the connection dropped before it (what the call did is done).
            assert request.transport is not None
            request.transport.close()
            raise
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


# What the mock server answered (the order probe's comment on #403, ``kis_orders.PROBE_COMMENT``):
# codes and messages as it gave them.
BOUGHT = (kis_orders.BOUGHT, "모의투자 매수주문이 완료 되었습니다.")
SOLD = (kis_orders.SOLD, "모의투자 매도주문이 완료 되었습니다.")
CANCELLED = (kis_orders.CANCELLED, "모의투자 취소주문이 완료 되었습니다.")
INQUIRED = (kis_orders.INQUIRED, "모의투자 조회가 완료되었습니다.")
NO_ROWS = (kis_orders.NO_ROWS, "모의투자 조회할 내역(자료)이 없습니다.")
QUOTED = (kis_orders.QUOTED, "정상처리 되었습니다.")
OFF_TICK = ("40030000", "모의투자 주문처리가 안되었습니다(호가단위 오류)")
NOT_HELD = ("40240000", "모의투자 잔고내역이 없습니다.")
NOT_MOCK = (kis_orders.NOT_MOCK, "모의투자 TR 이 아닙니다.")
# Not measured, made up: a sell of more shares than are held when some are, and a cancel of an
# order with nothing left of it.
SHARES_REFUSED = ("FK0003", "주문가능수량을 초과 했습니다.")
NOTHING_TO_CANCEL = ("FK0004", "정정/취소할 수량이 없습니다.")
# Made up too (#406): a cancel of an order of a day gone by (a day order: KRX ended it at the close).
PAST_DAY = ("FK0005", "당일 주문만 취소할 수 있습니다.")
BUY_SIDE, SELL_SIDE = kis_orders.SIDE_BUY, kis_orders.SIDE_SELL
BRANCH = "01790"  # made up: the order's branch (KRX_FWDG_ORD_ORGNO, ord_gno_brno)
NUMBERS_FROM = 31_401  # made up: the first order number


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
    amount: float = 0.0  # what filled, in won
    cancelled: int = 0  # cancelled of it (an order), or by it (a cancel)
    original: str = ""
    day: date | None = None  # the day it was placed: its day order inquiry's (ord_dt)

    @property
    def left(self) -> int:
        return self.quantity - self.filled - self.cancelled if not self.original else 0

    @property
    def average(self) -> float:
        return self.amount / self.filled if self.filled else 0.0


@dataclass
class FakeAccount:
    """A mock-trading account behind ``kis`` (#403, #404), registered on it by ``install``.

    A limit buy at the ask or above fills at once, a limit sell at the bid or below, unless
    ``fills`` is off; any other rests until cancelled. As on the mock server, a fill is half a tick
    better than the quote it crosses (``improvement``: the probe's one-share buy at the ask filled
    half a tick under it, its sell at the bid half a tick over it). ``depth`` caps the shares a
    marketable order fills at once (a partial fill: the rest rests). A price off the tick is
    refused, and a sell of shares not held; a buy beyond the cash is not, as the mock accepted
    one. Each order, fill, cancel and refusal is told on the fill notice (encrypted, to every open
    WebSocket) when ``notices``: in KIS's example's 26 fields, or the first ``notice_fields`` of
    them (the mock's records had 23). The cancellable-orders inquiry is not a mock TR (HTTP 500).

    Its time is ``clock``'s (KST; the wall clock without one), its day ``clock``'s date (``today``
    without one). An order is a day order (#406): the inquiry of a day shows that day's orders
    alone, and an order of a day gone by neither fills nor can be cancelled (``PAST_DAY``, made
    up). Its row still shows what was left of it when its day ended: how KIS shows an expired order
    was not measured, so the fake shows the worst case for the trader, which never reads an order
    of a day gone by as open.

    The deposit (``dnca_tot_amt``) stays the day's start (``cash``); the fills and their costs (the
    commission ``fee_rate`` a side, the sell tax) move the deposit two days on
    (``prvs_rcdl_excc_amt``), as on the mock server.
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
    depth: int | None = None
    improvement: float = 0.5  # ticks
    fee_rate: float = 0.00014  # the mock's commission: thdt_tlex_amt rose by 0.00014 of a buy's amount
    notices: bool = True
    notice_fields: int | None = None
    hts_id: str = "testhts01"
    account: str = "1234567801"
    page_rows: int = 15  # rows a page of the inquiry (KIS's example's for the mock; not measured)
    # The next orders whose answer is lost: taken, then the connection drops before the answer.
    lose_answers: int = 0
    clock: Callable[[], datetime] | None = None  # the time (KST); the wall clock without one
    orders: list[FakeOrder] = field(default_factory=list)
    flows: float = 0.0  # the day's fills and costs, in won: what moved the deposit two days on
    bought: float = 0.0
    sold: float = 0.0
    costs: float = 0.0
    paid: dict[str, float] = field(default_factory=dict)  # by symbol: what the shares held cost
    _pushes: set[asyncio.Task[None]] = field(default_factory=set)

    def install(self) -> FakeAccount:
        for tr in (kis_orders.BUY, kis_orders.SELL):
            self.kis.rest_posts[tr.mock] = self._order
        self.kis.rest_posts[kis_orders.CANCEL.mock] = self._cancel
        self.kis.rest_responses[kis_orders.FILLS.mock] = self._fills
        self.kis.rest_responses[kis_orders.BALANCE.mock] = self._balance
        self.kis.rest_responses[kis_orders.ORDERABLE.mock] = self._orderable
        self.kis.rest_responses[kis_orders.CANCELABLE.mock] = _not_mock
        self.kis.rest_responses[kis_orders.PRICE] = self._price
        self.kis.rest_responses[kis_orders.BOOK] = self._book
        return self

    def placed(self, side: str) -> list[FakeOrder]:
        return [o for o in self.orders if o.side == side and not o.original]

    def resting(self, side: str) -> list[FakeOrder]:
        """The day's orders of ``side`` with something left to fill (an order of a day gone by is
        over, whatever its row shows)."""
        return [o for o in self.placed(side) if self._live(o)]

    @property
    def day(self) -> date:
        """The account's day: ``clock``'s date, else ``today``."""
        return self.clock().date() if self.clock is not None else self.today

    def _live(self, order: FakeOrder) -> bool:
        return order.left > 0 and not self._past(order)

    def _past(self, order: FakeOrder) -> bool:
        """Whether ``order``'s day is over: a day order of a day gone by."""
        return order.day is not None and order.day != self.day

    @property
    def settled(self) -> float:
        """The deposit two days on (``prvs_rcdl_excc_amt``)."""
        return self.cash + self.flows

    def fill(self, order: FakeOrder, quantity: int | None = None) -> None:
        """Fill ``quantity`` of what is left of ``order`` (all of it: None), half a tick better than
        the ask (a buy) or the bid (a sell)."""
        quantity = order.left if quantity is None else min(order.left, quantity)
        if quantity <= 0 or self._past(order):  # a day order's day is over: it fills no more
            return
        quote = self.ask if order.side == BUY_SIDE else self.bid
        better = self.improvement * tick_size(quote)
        price = quote - better if order.side == BUY_SIDE else quote + better
        amount = quantity * price
        order.filled += quantity
        order.amount += amount
        fee = amount * self.fee_rate
        if order.side == BUY_SIDE:
            self.flows -= amount + fee
            self.bought += amount
            self.costs += fee
            self.paid[order.symbol] = self.paid.get(order.symbol, 0.0) + amount
        else:
            tax = amount * SELL_TAX
            held = self.holdings.get(order.symbol, 0)
            self.paid[order.symbol] = (
                self.paid.get(order.symbol, 0.0) * (held - quantity) / held if held else 0.0
            )
            self.flows += amount - fee - tax
            self.sold += amount
            self.costs += fee + tax
        change = quantity if order.side == BUY_SIDE else -quantity
        self.holdings[order.symbol] = self.holdings.get(order.symbol, 0) + change
        self._tell(order, "2", "0", "2", quantity, round(price))

    def _order(self, body: dict[str, Any]) -> RestAnswer:
        side = SELL_SIDE if "SLL_TYPE" in body else BUY_SIDE
        symbol, quantity, price = str(body["PDNO"]), int(body["ORD_QTY"]), int(body["ORD_UNPR"])
        if price % tick_size(price):
            return self._refuse(OFF_TICK, side, symbol, quantity, price)
        if side == SELL_SIDE and not self.holdings.get(symbol, 0):
            return self._refuse(NOT_HELD, side, symbol, quantity, price)
        if side == SELL_SIDE and quantity > self.holdings[symbol] - self._held_shares(symbol):
            return self._refuse(SHARES_REFUSED, side, symbol, quantity, price)
        order = FakeOrder(self._number(), side, symbol, quantity, price, self._now(), day=self.day)
        self.orders.append(order)
        self._tell(order, "1", "0", "1", quantity, price)
        if self.fills and self._marketable(order):
            self.fill(order, self.depth)
        answer = _accepted(order, BOUGHT if side == BUY_SIDE else SOLD)
        if self.lose_answers > 0:
            self.lose_answers -= 1
            raise ConnectionResetError("the answer is lost")
        return answer

    def _marketable(self, order: FakeOrder) -> bool:
        return order.price >= self.ask if order.side == BUY_SIDE else order.price <= self.bid

    def _cancel(self, body: dict[str, Any]) -> RestAnswer:
        number = str(body["ORGN_ODNO"])
        original = next(
            (o for o in self.orders if not o.original and kis_orders.same_number(o.number, number)), None
        )
        if original is not None and original.left > 0 and self._past(original):
            return _refusal(PAST_DAY)
        if original is not None and self.fills_late and original.left > 0 and self._marketable(original):
            self.fill(original)
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
            day=self.day,
        )
        original.cancelled += original.left
        self.orders.append(cancel)
        self._tell(cancel, "1", "2", "2", cancel.quantity, original.price)
        return _accepted(cancel, CANCELLED)

    def _refuse(
        self, refusal: tuple[str, str], side: str, symbol: str, quantity: int, price: int
    ) -> RestAnswer:
        refused = FakeOrder("", side, symbol, quantity, price, self._now())
        self._tell(refused, "1", "0", "1", quantity, price, refused=True)
        return _refusal(refusal)

    def _fills(self, params: dict[str, str]) -> RestAnswer:
        """The orders of the day asked (``INQR_STRT_DT``), oldest first, ``page_rows`` a page (the
        next asked by the offset in ``CTX_AREA_NK100``, ``tr_cont`` M while there are more)."""
        asked = params.get("INQR_STRT_DT")
        day = datetime.strptime(asked, "%Y%m%d").date() if asked else None
        rows = [
            self._row(o)
            for o in self.orders
            if (not params.get("ODNO") or kis_orders.same_number(o.number, params["ODNO"]))
            and (not params.get("PDNO") or o.symbol == params["PDNO"])
            and (day is None or (o.day or self.today) == day)
        ]
        start = int(params.get("CTX_AREA_NK100") or 0)
        page = rows[start : start + self.page_rows]
        more = start + self.page_rows < len(rows)
        totals = {
            "tot_ord_qty": str(sum(int(r["ord_qty"]) for r in rows)),
            "tot_ccld_qty": str(sum(int(r["tot_ccld_qty"]) for r in rows)),
            "tot_ccld_amt": str(sum(int(r["tot_ccld_amt"]) for r in rows)),
            "pchs_avg_pric": "0.0000",
            "prsm_tlex_smtl": "0",
        }
        code, message = INQUIRED if rows else NO_ROWS
        body = {
            "ctx_area_fk100": "",
            "ctx_area_nk100": str(start + self.page_rows) if more else "",
            "output1": page,
            "output2": totals,
            "rt_cd": "0",
            "msg_cd": code,
            "msg1": message,
        }
        return body, {"tr_cont": "M" if more else "E"}

    def _row(self, o: FakeOrder) -> dict[str, str]:
        side = "매수" if o.side == BUY_SIDE else "매도"
        return {
            "ord_dt": (o.day or self.today).strftime("%Y%m%d"),
            "ord_gno_brno": BRANCH,
            "odno": o.number,
            "orgn_odno": o.original or "0",
            "ord_dvsn_name": "보통가",
            "sll_buy_dvsn_cd": o.side,
            "sll_buy_dvsn_cd_name": side + ("취소" if o.original else ""),
            "pdno": o.symbol,
            "prdt_name": "삼성전자",
            "ord_qty": str(o.quantity),
            "ord_unpr": str(o.price),
            "ord_tmd": o.at,
            "tot_ccld_qty": str(o.filled),
            "avg_prvs": _price_text(o.average),
            "cncl_yn": "Y" if o.original else "N",
            "tot_ccld_amt": str(round(o.amount)),
            "ord_dvsn_cd": "00",
            "cncl_cfrm_qty": str(o.cancelled if o.original else 0),
            "rmn_qty": str(o.left),
            "rjct_qty": "0",
            "ccld_cndt_name": "",
            "infm_tmd": o.at,
            "prdt_type_cd": "",
            "excg_dvsn_cd": "",
            "excg_id_dvsn_cd": "KRX",
            "stpm_cndt_pric": "0",
            "stpm_efct_occr_dtmd": "",
            "loan_dt": "",
            "ord_orgno": "",
            "ordr_empno": "",
            "rsvn_ord_end_dt": "",
            "cpbc_ordp_infm_mthd_dvsn_cd": "",
            "cpbc_ordp_mtrl_dvsn_cd": "",
            "cpbc_ordp_ord_rcit_dvsn_cd": "",
            # Empty on the mock server; made up here, a phone number and an IP address, for the tests
            # to see the probe keep them out.
            "ctac_tlno": "01012345678",
            "inqr_ip_addr": "10.11.12.13",
        }

    def _balance(self, params: dict[str, str]) -> RestAnswer:
        rows = []
        for symbol, quantity in self.holdings.items():
            if not quantity:
                continue  # the mock server drops a symbol sold out
            paid = self.paid.get(symbol, 0.0)
            value = quantity * self.price
            rows.append(
                {
                    "pdno": symbol,
                    "prdt_name": "삼성전자",
                    "trad_dvsn_name": "현금",
                    "bfdy_buy_qty": "0",
                    "bfdy_sll_qty": "0",
                    "thdt_buyqty": str(sum(o.filled for o in self.placed(BUY_SIDE) if o.symbol == symbol)),
                    "thdt_sll_qty": str(sum(o.filled for o in self.placed(SELL_SIDE) if o.symbol == symbol)),
                    "hldg_qty": str(quantity),
                    "ord_psbl_qty": str(quantity - self._held_shares(symbol)),
                    "pchs_avg_pric": f"{paid / quantity:.4f}",
                    "pchs_amt": str(round(paid)),
                    "prpr": str(self.price),
                    "evlu_amt": str(value),
                    "evlu_pfls_amt": str(round(value - paid)),
                    "evlu_pfls_rt": "0.00",
                    "fltt_rt": "0.00000000",
                    "bfdy_cprs_icdc": "0",
                }
            )
        held = sum(int(r["evlu_amt"]) for r in rows)
        paid_all = sum(int(r["pchs_amt"]) for r in rows)
        settled = round(self.settled)
        totals = {
            "dnca_tot_amt": str(self.cash),
            "nxdy_excc_amt": str(self.cash),
            "prvs_rcdl_excc_amt": str(settled),
            "cma_evlu_amt": "0",
            "bfdy_buy_amt": "0",
            "thdt_buy_amt": str(round(self.bought)),
            "nxdy_auto_rdpt_amt": "0",
            "bfdy_sll_amt": "0",
            "thdt_sll_amt": str(round(self.sold)),
            "d2_auto_rdpt_amt": "0",
            "bfdy_tlex_amt": "0",
            "thdt_tlex_amt": str(round(self.costs)),
            "tot_loan_amt": "0",
            "scts_evlu_amt": str(held),
            "tot_evlu_amt": str(settled + held),
            "nass_amt": str(settled + held),
            "fncg_gld_auto_rdpt_yn": "",
            "pchs_amt_smtl_amt": str(paid_all),
            "evlu_amt_smtl_amt": str(held),
            "evlu_pfls_smtl_amt": str(held - paid_all),
            "tot_stln_slng_chgs": "0",
            "bfdy_tot_asst_evlu_amt": str(self.cash),
            "asst_icdc_amt": str(settled + held - self.cash),
            "asst_icdc_erng_rt": "0.00000000",
        }
        code, message = INQUIRED
        body = {
            "ctx_area_fk100": "",
            "ctx_area_nk100": "",
            "output1": rows,
            "output2": [totals],
            "rt_cd": "0",
            "msg_cd": code,
            "msg1": message,
        }
        return body, {"tr_cont": "D" if rows else "E"}

    def _orderable(self, params: dict[str, str]) -> RestAnswer:
        price = int(params["ORD_UNPR"]) if params.get("ORD_UNPR") else self.upper
        free = max(0, round(self.settled - self._held_cash()))
        shares = int(free // (price * (1 + self.fee_rate)))
        output = {
            "ord_psbl_cash": str(free),
            "ord_psbl_sbst": "0",
            "ruse_psbl_amt": "0",
            "fund_rpch_chgs": "0",
            "psbl_qty_calc_unpr": str(price),
            "nrcvb_buy_amt": str(round(shares * price)),
            "nrcvb_buy_qty": str(shares),
            "max_buy_amt": str(round(shares * price)),
            "max_buy_qty": str(shares),
            "cma_evlu_amt": "0",
            "ovrs_re_use_amt_wcrc": "0",
            "ord_psbl_frcr_amt_wcrc": "0",
        }
        code, message = INQUIRED
        return {"output": output, "rt_cd": "0", "msg_cd": code, "msg1": message}, {"tr_cont": "E"}

    def _price(self, params: dict[str, str]) -> RestAnswer:
        output = {
            "stck_shrn_iscd": params.get("FID_INPUT_ISCD", ""),
            "stck_prpr": str(self.price),
            "stck_mxpr": str(self.upper),
            "stck_llam": str(self.lower),
            "stck_sdpr": str(self.price),
            "aspr_unit": str(tick_size(self.price)),
            "vi_cls_code": "N",
        }
        code, message = QUOTED
        return {"output": output, "rt_cd": "0", "msg_cd": code, "msg1": message}

    def _book(self, params: dict[str, str]) -> RestAnswer:
        book = {
            "aspr_acpt_hour": "100000",
            "askp1": str(self.ask),
            "bidp1": str(self.bid),
            "askp_rsqn1": "1500",
            "bidp_rsqn1": "2500",
        }
        code, message = QUOTED
        return {"output1": book, "output2": {}, "rt_cd": "0", "msg_cd": code, "msg1": message}

    def _held_cash(self) -> int:
        return sum(o.left * o.price for o in self.orders if o.side == BUY_SIDE and self._live(o))

    def _held_shares(self, symbol: str) -> int:
        return sum(
            o.left for o in self.orders if o.side == SELL_SIDE and o.symbol == symbol and self._live(o)
        )

    def _number(self) -> str:
        """A new order number, made up: ten digits, leading zeros (ODNO and odno are int-looking)."""
        return f"{NUMBERS_FROM + len(self.orders):010d}"

    def _now(self) -> str:
        return (self.clock() if self.clock is not None else datetime.now()).strftime("%H%M%S")

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
            "BRNC_NO": BRANCH,
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
        columns = kis_orders.FILL_NOTICE_COLUMNS[: self.notice_fields]
        fields = "^".join(values[c] for c in columns)
        frame = f"1|{kis_orders.FILL_NOTICE.mock}|001|{encrypt(NOTICE_KEY, NOTICE_IV, fields)}"
        task = asyncio.get_running_loop().create_task(self.kis.push(frame))
        self._pushes.add(task)
        task.add_done_callback(self._pushes.discard)


def _accepted(order: FakeOrder, said: tuple[str, str]) -> RestAnswer:
    output = {"KRX_FWDG_ORD_ORGNO": BRANCH, "ODNO": order.number, "ORD_TMD": order.at, "SOR_ODNO": ""}
    return {"rt_cd": "0", "msg_cd": said[0], "msg1": said[1], "output": output}


def _refusal(refusal: tuple[str, str]) -> RestAnswer:
    return {"rt_cd": "1", "msg_cd": refusal[0], "msg1": refusal[1]}


def _not_mock(params: dict[str, str]) -> RestAnswer:
    body = {"rt_cd": "1", "msg_cd": NOT_MOCK[0], "msg1": NOT_MOCK[1]}
    raise web.HTTPInternalServerError(
        text=json.dumps(body, ensure_ascii=False), content_type="application/json"
    )


def _price_text(value: float) -> str:
    """A price as KIS writes it: whole, or with its decimals."""
    return str(int(value)) if float(value).is_integer() else f"{value:.4f}"
