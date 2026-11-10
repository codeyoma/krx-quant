"""The Engine API's requests counted by route (#588): the route patterns, the statuses, the p95, the
WebSocket connections, one line at each 00:00 KST and one when the API stops, and nothing in the
line that a request carried (its address, query or path values). A request slower than
``SLOW_SECONDS`` is a WARNING line of its own (#680)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from krx_quant.clock import FakeClock
from krx_quant.collector import run_collector
from krx_quant.fake_kis import FakeKis
from krx_quant.feeds.base import KST
from krx_quant.route_counts import SLOW_SECONDS, RouteCounts, install, keep_daily
from tests.collector_doubles import settings_for
from tests.replay_events import at
from tests.server_doubles import free_port, poll
from tests.waiting import until

STARTED = datetime(2026, 11, 5, 22, 0, tzinfo=KST)


def head(since: str, until: str) -> str:
    """The line's start, from ``since`` to ``until`` (KST, ``YYYY-MM-DDTHH:MM:SS``)."""
    return f"Engine API requests from {since}+09:00 to {until}+09:00: "


class Ticks:
    """A stand-in for the clock requests are timed by, in nanoseconds: it moves only when a handler
    or the test moves it."""

    def __init__(self) -> None:
        self.ns = 0

    def __call__(self) -> int:
        return self.ns

    def add(self, seconds: float) -> None:
        self.ns += round(seconds * 1_000_000_000)


def api(ticks: Ticks) -> web.Application:
    """A few routes shaped like the Engine API's; each takes ``?ms=`` milliseconds of ``ticks``."""

    def took(request: web.Request) -> None:
        ticks.add(float(request.query.get("ms", "1")) / 1000)

    async def latest(request: web.Request) -> web.Response:
        took(request)
        if request.match_info["symbol"] == "999999":
            return web.json_response({"error": "unknown symbol"}, status=404)
        return web.json_response({"predictions": []})

    async def status(request: web.Request) -> web.Response:
        took(request)
        return web.json_response({"mode": "live"})

    async def bad(request: web.Request) -> web.Response:
        raise web.HTTPBadRequest(text="limit: a number")

    async def boom(request: web.Request) -> web.Response:
        raise RuntimeError("a bug")

    async def pause(request: web.Request) -> web.Response:
        return web.json_response({})

    async def stream(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for _ in ws:
            pass
        return ws

    app = web.Application()
    app.router.add_get("/v1/status", status)
    app.router.add_get("/v1/symbols/{symbol}/latest", latest)
    app.router.add_get("/v1/bad", bad)
    app.router.add_get("/v1/boom", boom)
    app.router.add_post("/v1/trader/pause", pause)
    app.router.add_get("/v1/stream", stream)
    return app


@asynccontextmanager
async def serving(
    ticks: Ticks, clock: FakeClock
) -> AsyncIterator[tuple[TestClient[web.Request, web.Application], RouteCounts]]:
    """``api`` with its requests counted, timed by ``ticks``; no midnight comes unless ``clock`` is
    set to one."""
    app = api(ticks)
    counts = install(app, clock, elapsed=ticks)
    async with TestClient(TestServer(app)) as client:
        yield client, counts


async def test_requests_count_by_route_pattern_and_status() -> None:
    ticks, clock = Ticks(), FakeClock(STARTED)
    async with serving(ticks, clock) as (client, counts):
        for path in ("/v1/symbols/005930/latest", "/v1/symbols/000660/latest", "/v1/symbols/999999/latest"):
            async with client.get(path) as response:
                await response.read()
        for path in ("/v1/status", "/v1/bad", "/v1/boom", "/nowhere", "/v1/symbols"):
            async with client.get(path) as response:
                await response.read()
        async with client.get("/v1/trader/pause") as response:  # the route's path, another method
            assert response.status == 405
        line = counts.line(STARTED + timedelta(hours=1))

    assert line.startswith(head("2026-11-05T22:00:00", "2026-11-05T23:00:00") + "9 in all; ")
    # The path's values go together under the route's pattern; the most called first.
    assert "; GET /v1/symbols/{symbol}/latest 3 (200 2, 404 1) p95 1ms; " in line
    assert "; GET /v1/status 1 (200 1) p95 1ms" in line
    assert "; GET /v1/bad 1 (400 1) p95 0ms" in line
    assert "; GET /v1/boom 1 (500 1) p95 0ms" in line  # aiohttp answers an exception with 500
    # No route matched: counted without the path asked.
    assert "; unmatched 3 (404 2, 405 1) p95 0ms" in line
    assert line.index("GET /v1/symbols/{symbol}/latest") < line.index("unmatched") < line.index("GET /v1/bad")
    # The routes no one called, but HEAD (aiohttp adds one to every GET).
    assert line.endswith("; not called: POST /v1/trader/pause, GET /v1/stream")


async def test_the_p95_is_the_nearest_rank_of_the_milliseconds_rounded_up() -> None:
    ticks, clock = Ticks(), FakeClock(STARTED)
    async with serving(ticks, clock) as (client, counts):
        for ms in range(1, 21):  # 1 to 20 ms: the 19th of 20 is the 95th percentile
            async with client.get(f"/v1/status?ms={ms}") as response:
                await response.read()
        for _ in range(2):
            async with client.get("/v1/symbols/005930/latest?ms=0.2") as response:  # under a millisecond
                await response.read()
        line = counts.line(STARTED)

    assert "; GET /v1/status 20 (200 20) p95 19ms; " in line
    assert "; GET /v1/symbols/{symbol}/latest 2 (200 2) p95 1ms; " in line


async def test_websocket_connections_count_when_they_end_with_their_seconds_and_those_open() -> None:
    ticks, clock = Ticks(), FakeClock(STARTED)
    async with serving(ticks, clock) as (client, counts):
        first = await client.ws_connect("/v1/stream")
        second = await client.ws_connect("/v1/stream")
        await until(lambda: "GET /v1/stream 0, WebSocket 2 open" in counts.line(STARTED))
        assert "not called" in counts.line(STARTED)
        assert "GET /v1/stream" not in counts.line(STARTED).split("not called")[1], "an open one is a call"

        ticks.add(3600.2)
        await first.close()
        await until(lambda: "GET /v1/stream 1 " in counts.line(STARTED))
        assert "; GET /v1/stream 1 (101 1), WebSocket p95 3601s max 3601s, 1 open" in counts.line(STARTED)
        # The day's line: the connection still open is counted again the day it ends.
        taken = counts.take(STARTED + timedelta(hours=2))
        assert "; GET /v1/stream 1 (101 1), WebSocket p95 3601s max 3601s, 1 open" in taken

        ticks.add(60)
        await second.close()
        await until(lambda: "GET /v1/stream 1 " in counts.line(STARTED + timedelta(hours=3)))
        line = counts.line(STARTED + timedelta(hours=3))

    assert line.startswith(head("2026-11-06T00:00:00", "2026-11-06T01:00:00"))
    assert "; GET /v1/stream 1 (101 1), WebSocket p95 3661s max 3661s" in line
    assert "open" not in line


async def test_the_line_has_no_client_address_query_or_path_value() -> None:
    ticks, clock = Ticks(), FakeClock(STARTED)
    async with serving(ticks, clock) as (client, counts):
        headers = {"X-Forwarded-For": "100.64.12.34", "User-Agent": "FinceptTerminal/9.9"}
        path = "/v1/symbols/123456/latest?before=2026-11-05T10:31:00%2B09:00&limit=777&ms=1"
        async with client.get(path, headers=headers) as response:
            await response.read()
        async with client.get("/secret-path-4242?token=abc", headers=headers) as response:
            await response.read()
        line = counts.line(STARTED)
        host = client.server.host

    assert "GET /v1/symbols/{symbol}/latest 1 (200 1)" in line
    assert "unmatched 1 (404 1)" in line
    for carried in ("123456", "777", "before", "limit", "10:31", "secret-path", "4242", "token", "abc"):
        assert carried not in line
    for address in (host, "100.64.12.34", "Fincept"):
        assert address not in line


async def test_taking_the_line_counts_again_from_zero() -> None:
    ticks, clock = Ticks(), FakeClock(STARTED)
    async with serving(ticks, clock) as (client, counts):
        async with client.get("/v1/status") as response:
            await response.read()
        midnight = datetime(2026, 11, 6, 0, 0, tzinfo=KST)
        assert "1 in all; GET /v1/status 1 (200 1)" in counts.take(midnight)
        again = counts.line(midnight + timedelta(hours=1))

    assert again.startswith(head("2026-11-06T00:00:00", "2026-11-06T01:00:00") + "0 in all; not called: ")
    assert "GET /v1/status" in again.split("not called: ")[1]


def lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "krx_quant.route_counts"]


def slow(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [m for m in lines(caplog) if m.startswith("Engine API request slow: ")]


async def test_a_request_slower_than_five_seconds_is_a_warning_with_its_route_and_seconds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ticks, clock = Ticks(), FakeClock(STARTED)
    over = (SLOW_SECONDS + 1.2) * 1000
    with caplog.at_level(logging.INFO, logger="krx_quant.route_counts"):
        async with serving(ticks, clock) as (client, counts):
            for path in (
                f"/v1/symbols/005930/latest?ms={over}",
                f"/v1/symbols/999999/latest?ms={over}",
                f"/v1/status?ms={SLOW_SECONDS * 1000}",  # not longer than the limit
                "/v1/status?ms=3",
            ):
                async with client.get(path) as response:
                    await response.read()
            ws = await client.ws_connect("/v1/stream")
            ticks.add(3600)
            await ws.close()
            await until(lambda: "GET /v1/stream 1 " in counts.line(STARTED))

    assert slow(caplog) == [
        "Engine API request slow: GET /v1/symbols/{symbol}/latest 200 in 6.2s",
        "Engine API request slow: GET /v1/symbols/{symbol}/latest 404 in 6.2s",
    ], "the route's pattern, not the symbol asked; a WebSocket connection is not timed so"
    warned = [r for r in caplog.records if r.getMessage().startswith("Engine API request slow: ")]
    assert {r.levelno for r in warned} == {logging.WARNING}


async def test_a_slow_request_cancelled_before_its_answer_is_a_warning_too(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ticks, clock = Ticks(), FakeClock(STARTED)

    async def bars(request: web.Request) -> web.Response:
        ticks.add(7)
        raise asyncio.CancelledError  # as when the engine stops while it runs

    app = web.Application()
    app.router.add_get("/v1/symbols/{symbol}/bars", bars)
    install(app, clock, elapsed=ticks)
    with caplog.at_level(logging.INFO, logger="krx_quant.route_counts"):
        async with TestClient(TestServer(app)) as client:
            with pytest.raises(aiohttp.ClientError):
                async with client.get("/v1/symbols/005930/bars") as response:
                    await response.read()
            await until(lambda: bool(slow(caplog)))

    assert slow(caplog) == ["Engine API request slow: GET /v1/symbols/{symbol}/bars cancelled after 7.0s"]


class Asked(FakeClock):
    """A fake clock that keeps the times it was asked to sleep until."""

    def __init__(self, start: datetime) -> None:
        super().__init__(start)
        self.asked: list[datetime] = []

    async def sleep_until(self, at: datetime) -> None:
        self.asked.append(at)
        await super().sleep_until(at)


async def test_a_line_at_each_midnight_kst(caplog: pytest.LogCaptureFixture) -> None:
    clock = Asked(STARTED)
    counts = RouteCounts(STARTED, routes=("GET /v1/status",))
    first, second = datetime(2026, 11, 6, 0, 0, tzinfo=KST), datetime(2026, 11, 7, 0, 0, tzinfo=KST)
    with caplog.at_level(logging.INFO, logger="krx_quant.route_counts"):
        daily = asyncio.create_task(keep_daily(counts, clock))
        try:
            await until(lambda: clock.asked == [first])
            clock.set(first)
            await until(lambda: clock.asked == [first, second])
            clock.set(second + timedelta(seconds=1))  # woke late
            await until(lambda: len(clock.asked) == 3)
        finally:
            daily.cancel()

    assert clock.asked[2] == datetime(2026, 11, 8, 0, 0, tzinfo=KST)
    assert lines(caplog) == [
        head("2026-11-05T22:00:00", "2026-11-06T00:00:00") + "0 in all; not called: GET /v1/status",
        head("2026-11-06T00:00:00", "2026-11-07T00:00:01") + "0 in all; not called: GET /v1/status",
    ]


async def test_the_api_stopping_logs_what_was_counted_since_the_last_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ticks, clock = Ticks(), FakeClock(STARTED)
    with caplog.at_level(logging.INFO, logger="krx_quant.route_counts"):
        async with serving(ticks, clock) as (client, counts):
            ws = await client.ws_connect("/v1/stream")
            async with client.get("/v1/status") as response:
                await response.read()
            clock.set(STARTED + timedelta(hours=1, seconds=0.4))
            await ws.close()
            await until(lambda: "GET /v1/stream 1 " in counts.line(STARTED))

    [line] = lines(caplog)
    assert line.startswith(head("2026-11-05T22:00:00", "2026-11-05T23:00:00") + "2 in all; ")
    assert "; GET /v1/status 1 (200 1) p95 1ms" in line
    assert "; GET /v1/stream 1 (101 1), WebSocket" in line


async def test_the_collectors_engine_api_counts_its_requests(
    kis: FakeKis, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    port = free_port()
    settings = replace(settings_for(kis, tmp_path), api_port=port)
    stop = asyncio.Event()
    with caplog.at_level(logging.INFO, logger="krx_quant.route_counts"):
        task = asyncio.create_task(run_collector(settings, stop=stop, clock=FakeClock(at("09:30:10"))))
        try:
            base = f"http://127.0.0.1:{port}"
            await poll(f"{base}/v1/status", lambda b: True)
            async with aiohttp.ClientSession(base) as http, http.get("/v1/symbols/005930/latest") as response:
                assert response.status == 200
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)

    [line] = lines(caplog)
    assert line.startswith(head("2026-10-12T09:30:10", "2026-10-12T09:30:10"))
    assert "; GET /v1/symbols/{symbol}/latest 1 (200 1) p95 " in line
    assert "; GET /v1/status " in line
    assert "127.0.0.1" not in line and "005930" not in line
    assert "POST /v1/trader/pause" in line.split("not called: ")[1]
