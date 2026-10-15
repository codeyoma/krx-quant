"""A local stand-in for the KIS Open API, for tests and offline runs.

It implements just enough of KIS to drive the collector: access-token and
WebSocket approval-key issuance, REST quotation endpoints answered from
canned responses, and a WebSocket endpoint that acknowledges subscriptions
(up to an optional per-session limit), pushes scripted data frames, and
sends PINGPONG heartbeats. Everything it receives is recorded so tests can
assert on what the collector sent.
"""

from __future__ import annotations

import asyncio
import json
import socket
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from aiohttp import WSMsgType, web

from krx_quant.feeds.stock import TRADE_COLUMNS
from krx_quant.kis import WS_PATH

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
    # ``tr_cont`` header reaches the function as the param ``_tr_cont``.
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

    _demo_count: int = 0
    _runner: web.AppRunner | None = None
    _site: web.SockSite | None = None
    _sockets: list[web.WebSocketResponse] = field(default_factory=list)
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

    async def _rest(self, request: web.Request) -> web.Response:
        h = request.headers
        tr_id = h.get("tr_id", "")
        query = {**request.query, "_auth": h.get("authorization", "")}
        self.rest_requests.append((time.monotonic(), tr_id, request.path, query))
        if (
            h.get("authorization", "").removeprefix("Bearer ") not in self.tokens_issued
            or h.get("appkey") != self.app_key
            or h.get("appsecret") != self.app_secret
        ):
            return web.json_response(
                {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "invalid token"}, status=403
            )
        respond = self.rest_responses.get(tr_id)
        if respond is None:
            return web.json_response({"rt_cd": "1", "msg_cd": "OPSQ0002", "msg1": f"unknown tr_id {tr_id}"})
        params = dict(request.query)
        if h.get("tr_cont"):
            params["_tr_cont"] = h["tr_cont"]
        try:
            answer = respond(params)
        except ConnectionResetError:
            # Simulate KIS dropping the connection mid-request.
            assert request.transport is not None
            request.transport.close()
            raise
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
            self._sockets.remove(ws)
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


def _ack(key: tuple[str, str], rt_cd: str, msg_cd: str, msg1: str) -> str:
    return json.dumps(
        {
            "header": {"tr_id": key[0], "tr_key": key[1], "encrypt": "N"},
            "body": {
                "rt_cd": rt_cd,
                "msg_cd": msg_cd,
                "msg1": msg1,
                "output": {"iv": "0123456789abcdef", "key": "0123456789abcdef0123456789abcdef"},
            },
        }
    )
