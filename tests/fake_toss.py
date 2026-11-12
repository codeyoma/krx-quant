"""A stand-in for the Toss Securities Open API's token and stock information (#250) endpoints (its
daily trading-trend ones, #235, left with the daily supply's Toss part, #694).

It keeps Toss's rules: one valid token per client (issuing revokes the one before, 401
``token-revoked``), the IP allowlist (403 ``access_denied`` from the token endpoint, 403
``ip-not-allowed`` from the others), 429 with ``Retry-After`` when busy. Stock information lists
the asked symbols it has a name for.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from aiohttp import web
from aiohttp.test_utils import TestServer

CLIENT_ID = "toss-client-id-for-tests"
CLIENT_SECRET = "toss-client-secret-for-tests"


@dataclass
class FakeToss:
    url: str = ""
    ip_allowed: bool = True  # the token endpoint and the API both refuse when False
    api_ip_allowed: bool = True  # the API alone refuses when False
    expires_in: int = 86400
    valid: str | None = None  # the one token Toss accepts
    revoked: set[str] = field(default_factory=set)
    issued: list[str] = field(default_factory=list)
    # API requests: (monotonic time, path, query, the bearer token)
    requests: list[tuple[float, str, dict[str, str], str]] = field(default_factory=list)
    busy: int = 0  # the next this many API calls get 429
    names: dict[str, str] = field(default_factory=dict)  # stock information: symbol -> name

    def accept(self, token: str) -> None:
        """Make ``token`` the valid one, as if issued (the one before is revoked)."""
        if self.valid is not None:
            self.revoked.add(self.valid)
        self.valid = token

    def revoke(self) -> str:
        """Another process of the client issues a token: the current one stops working."""
        token = f"toss-token-elsewhere-{len(self.issued) + len(self.revoked)}"
        self.accept(token)
        return token

    async def _token(self, request: web.Request) -> web.Response:
        form = await request.post()
        if not self.ip_allowed:
            return web.json_response(
                {"error": "access_denied", "error_description": "IP address not allowed"}, status=403
            )
        if form.get("grant_type") != "client_credentials":
            return web.json_response({"error": "unsupported_grant_type"}, status=400)
        if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_SECRET:
            return web.json_response(
                {
                    "error": "invalid_client",
                    "error_description": "Client authentication failed: client_secret",
                },
                status=401,
            )
        token = f"toss-token-{len(self.issued) + 1}"
        self.issued.append(token)
        self.accept(token)
        return web.json_response(
            {"access_token": token, "token_type": "Bearer", "expires_in": self.expires_in}
        )

    def _refused(self, request: web.Request) -> web.Response | None:
        """Toss's answer to a call it refuses: the address, the token, or too many calls."""
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        self.requests.append((time.monotonic(), request.path, dict(request.query), token))
        if not (self.ip_allowed and self.api_ip_allowed):
            return _error(403, "ip-not-allowed", "허용되지 않은 IP 주소입니다.")
        if token in self.revoked:
            return _error(
                401, "token-revoked", "새로 발급된 토큰으로 대체되어 더 이상 유효하지 않은 토큰입니다."
            )
        if token != self.valid:
            return _error(401, "invalid-token", "유효하지 않은 토큰입니다.")
        if self.busy:
            self.busy -= 1
            response = _error(429, "rate-limit-exceeded", "")
            response.headers["Retry-After"] = "0"
            return response
        return None

    async def _stocks(self, request: web.Request) -> web.Response:
        if (refused := self._refused(request)) is not None:
            return refused
        asked = request.query.get("symbols", "").split(",")
        found = [
            {"symbol": s, "name": self.names[s], "market": "KOSPI", "status": "ACTIVE"}
            for s in asked
            if s in self.names
        ]
        return web.json_response({"result": found})

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/oauth2/token", self._token)
        app.router.add_get("/api/v1/stocks", self._stocks)
        return app


def _error(status: int, code: str, message: str) -> web.Response:
    body = {"error": {"requestId": "01HXYZTEST", "code": code, "message": message}}
    return web.json_response(body, status=status)


@asynccontextmanager
async def fake_toss() -> AsyncIterator[FakeToss]:
    fake = FakeToss()
    server = TestServer(fake.app())
    await server.start_server()
    fake.url = str(server.make_url("")).rstrip("/")
    try:
        yield fake
    finally:
        await server.close()
