"""Local servers for the tests (#511): a free port to serve on, a JSON answer polled until it is
ready, and a Telegram Bot API stand-in.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


async def poll(url: str, ready: Callable[[Any], bool], timeout: float = 5.0) -> Any:
    """GET ``url`` until its JSON body is ``ready``."""
    async with aiohttp.ClientSession() as http, asyncio.timeout(timeout):
        while True:
            try:
                async with http.get(url) as response:
                    body = await response.json()
                    if response.status == 200 and ready(body):
                        return body
            except aiohttp.ClientConnectionError:
                pass  # not listening yet
            await asyncio.sleep(0.02)


@asynccontextmanager
async def telegram(status: int) -> AsyncIterator[tuple[str, list[str]]]:
    """A Bot API stand-in answering ``status``; the texts it was sent."""
    sent: list[str] = []

    async def message(request: web.Request) -> web.Response:
        sent.append((await request.json())["text"])
        return web.json_response({"ok": status == 200}, status=status)

    app = web.Application()
    app.router.add_post("/bot{token}/sendMessage", message)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server(access_log=None)
    try:
        yield str(server.make_url("")).rstrip("/"), sent
    finally:
        await server.close()
