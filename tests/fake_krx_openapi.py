"""A stand-in for the KRX Open API's ETF daily trading service (``etf_bydd_trd``, ETF 일별매매정보,
#305).

It answers as the service page describes the service (openapi.krx.co.kr, read 2026-10-22):
``GET /svc/apis/etp/etf_bydd_trd.json?basDd=YYYYMMDD`` with the key in an ``AUTH_KEY`` header, the
day's ETFs in ``OutBlock_1``, every value a string. That is the page's description, not a
measurement (the probe assumes none of it). A wrong key gets 401, as KRX's FAQ says; its body here
is made up. A day's rows may also be an HTTP status to refuse it with.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer

KEY = "krx-openapi-key-for-tests-0123456789"
PATH = "/svc/apis/etp/etf_bydd_trd.json"

# A day's rows, or an HTTP status to refuse it with.
Rows = Callable[[date], list[dict[str, Any]] | int]


@dataclass
class FakeKrx:
    rows: Rows
    url: str = ""
    key: str = KEY  # the one key KRX accepts
    # Requests: (path, query, the AUTH_KEY header).
    requests: list[tuple[str, dict[str, str], str | None]] = field(default_factory=list)

    async def _daily(self, request: web.Request) -> web.Response:
        header = request.headers.get("AUTH_KEY")
        self.requests.append((request.path, dict(request.query), header))
        if header != self.key:
            return web.json_response({"respMsg": "Unauthorized API Call", "respCode": "401"}, status=401)
        day = datetime.strptime(request.query["basDd"], "%Y%m%d").date()
        found = self.rows(day)
        if isinstance(found, int):
            return web.json_response({"respMsg": "refused", "respCode": str(found)}, status=found)
        return web.json_response({"OutBlock_1": found})

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get(PATH, self._daily)
        return app


@asynccontextmanager
async def fake_krx(rows: Rows) -> AsyncIterator[FakeKrx]:
    fake = FakeKrx(rows)
    server = TestServer(fake.app())
    await server.start_server()
    fake.url = str(server.make_url("")).rstrip("/")
    try:
        yield fake
    finally:
        await server.close()
