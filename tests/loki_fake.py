"""A fake Grafana Cloud Loki (#358): takes pushes at ``loki.PUSH`` on the test's event loop.

The shipper's thread posts to it, so a test that runs a command (``cli.main``, which blocks) runs
it in a worker thread.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer

from krx_quant import loki

USER = "123456"
TOKEN = "glc_" + "Z" * 40  # a made-up Grafana Cloud token, put together here


class FakeLoki:
    """Each push's body and auth, and its answer: ``answers`` in turn, 204 once they run out."""

    def __init__(self) -> None:
        self.url = ""
        self.bodies: list[bytes] = []
        self.auth: list[str | None] = []
        self.answers: deque[int] = deque()
        self.taken: list[dict[str, Any]] = []

    async def push(self, request: web.Request) -> web.Response:
        body = await request.read()
        self.bodies.append(body)
        self.auth.append(request.headers.get("Authorization"))
        status = self.answers.popleft() if self.answers else 204
        if status == 204:
            self.taken.append(json.loads(body))
        return web.Response(status=status)

    def lines(self) -> list[tuple[dict[str, str], str]]:
        """Each line taken, with its stream's labels, in time order."""
        found = [
            (int(ns), stream["stream"], line)
            for push in self.taken
            for stream in push["streams"]
            for ns, line in stream["values"]
        ]
        return [(labels, line) for _, labels, line in sorted(found, key=lambda f: f[0])]

    def batches(self) -> list[int]:
        return [sum(len(s["values"]) for s in push["streams"]) for push in self.taken]

    def values(self) -> dict[str, str]:
        """The raw settings that point at it."""
        return {loki.URL: self.url, loki.USER: USER, loki.TOKEN: TOKEN}


@asynccontextmanager
async def serving() -> AsyncIterator[FakeLoki]:
    found = FakeLoki()
    app = web.Application()
    app.router.add_post(loki.PUSH, found.push)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server(access_log=None)  # its lines would go to Loki too
    found.url = str(server.make_url("")).rstrip("/")
    try:
        yield found
    finally:
        await server.close()
