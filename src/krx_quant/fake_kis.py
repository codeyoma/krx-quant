"""A local stand-in for the KIS Open API, for tests and offline runs.

It implements just enough of KIS to drive the collector: access-token and
WebSocket approval-key issuance over REST, and a WebSocket endpoint that
acknowledges subscriptions, pushes scripted data frames, and sends
PINGPONG heartbeats. Everything it receives is recorded so tests can
assert on what the collector sent.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from aiohttp import WSMsgType, web

from krx_quant.kis import WS_PATH


@dataclass
class FakeKis:
    app_key: str = "test-app-key"
    app_secret: str = "test-app-secret"
    # Frames to push after a (tr_id, tr_key) subscription is acknowledged.
    script: dict[tuple[str, str], list[str]] = field(default_factory=lambda: defaultdict(list))
    subscriptions: list[dict[str, Any]] = field(default_factory=list)
    pongs: list[bytes] = field(default_factory=list)

    _runner: web.AppRunner | None = None
    _site: web.SockSite | None = None
    _sockets: list[web.WebSocketResponse] = field(default_factory=list)
    _pong_event: asyncio.Event = field(default_factory=asyncio.Event)
    _subscribed: asyncio.Condition = field(default_factory=asyncio.Condition)
    port: int = 0

    @property
    def rest_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/oauth2/tokenP", self._token)
        app.router.add_post("/oauth2/Approval", self._approval)
        app.router.add_get(WS_PATH, self._ws)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", self.port))
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

    async def round_trip(self, timeout: float = 5.0) -> None:
        """Send a PINGPONG and wait for the client's pong.

        The client handles messages in order on one connection, so once the
        pong arrives every frame pushed before it has been processed.
        """
        self._pong_event.clear()
        await self.send_pingpong()
        await asyncio.wait_for(self._pong_event.wait(), timeout)

    async def close_connections(self) -> None:
        """Drop every client connection, as KIS does on maintenance or errors."""
        for ws in list(self._sockets):
            await ws.close()

    async def send_pingpong(self) -> None:
        msg = json.dumps({"header": {"tr_id": "PINGPONG", "datetime": "20261008093000"}})
        for ws in self._sockets:
            await ws.send_str(msg)

    async def _token(self, request: web.Request) -> web.Response:
        body = await request.json()
        if body.get("appkey") != self.app_key or body.get("appsecret") != self.app_secret:
            return web.json_response({"error_description": "invalid app key"}, status=403)
        return web.json_response(
            {
                "access_token": "fake-access-token",
                "access_token_token_expired": "2099-12-31 23:59:59",
                "token_type": "Bearer",
                "expires_in": 86400,
            }
        )

    async def _approval(self, request: web.Request) -> web.Response:
        body = await request.json()
        if body.get("appkey") != self.app_key or body.get("secretkey") != self.app_secret:
            return web.json_response({"error_description": "invalid app key"}, status=403)
        return web.json_response({"approval_key": "fake-approval-key"})

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(autoping=False)
        await ws.prepare(request)
        self._sockets.append(ws)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._on_subscribe(ws, json.loads(msg.data))
                elif msg.type == WSMsgType.PING:
                    await ws.pong(msg.data)
                elif msg.type == WSMsgType.PONG:
                    self.pongs.append(msg.data)
                    self._pong_event.set()
        finally:
            self._sockets.remove(ws)
        return ws

    async def _on_subscribe(self, ws: web.WebSocketResponse, req: dict[str, Any]) -> None:
        header = req.get("header", {})
        inp = req.get("body", {}).get("input", {})
        tr_id, tr_key = inp.get("tr_id", ""), inp.get("tr_key", "")
        ok = header.get("approval_key") == "fake-approval-key"
        await ws.send_str(
            json.dumps(
                {
                    "header": {"tr_id": tr_id, "tr_key": tr_key, "encrypt": "N"},
                    "body": {
                        "rt_cd": "0" if ok else "1",
                        "msg_cd": "OPSP0000" if ok else "OPSP0011",
                        "msg1": "SUBSCRIBE SUCCESS" if ok else "invalid approval : NOT FOUND",
                        "output": {"iv": "0123456789abcdef", "key": "0123456789abcdef0123456789abcdef"},
                    },
                }
            )
        )
        async with self._subscribed:
            self.subscriptions.append(req)
            self._subscribed.notify_all()
        if ok:
            for frame in self.script.get((tr_id, tr_key), []):
                await ws.send_str(frame)
