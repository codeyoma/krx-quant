"""The Engine API's requests counted by route (#588): one INFO line a day, never a line a request.

The collector serves the Engine API without an access log (``access_log=None``), so nothing told
which routes the screen and the user call, and which no one does. A middleware counts each request
in memory: by its route's pattern (``GET /v1/symbols/{symbol}/latest``: the path's values go
together), by status, and by its milliseconds to the answer; a WebSocket connection (the stream) by
its seconds. At each 00:00 KST, and once more when the API stops, one INFO line tells what was
counted since the line before, and the counting starts again from zero::

    Engine API requests from <since> to <until>: <N> in all; GET /v1/status 41230 (200 41229, 503 1)
    p95 3ms; …; GET /v1/stream 4 (101 4), WebSocket p95 7201s max 25200s, 1 open; not called:
    GET /v1/algorithms/{name}, POST /v1/trader/pause

- Each route as ``<method> <pattern> <requests> (<status> <requests>, …)``, the most called first.
  ``p95``: the nearest-rank 95th percentile of its requests' milliseconds to the answer, each
  rounded up to a whole millisecond (a request under one is 1). A WebSocket connection is counted
  when it ends, by its seconds (rounded up), and not in the milliseconds; one still open is
  ``open`` (it is counted on the day it ends).
- A request is counted once answered, by its status: an exception the handler raised is the
  aiohttp answer's, 500 unless it is an HTTP error. One cancelled before its answer (the client
  went, the engine stopped) is not counted.
- A request no route matches (404, or 405 for a route's path with another method) is
  ``unmatched``, without its path.
- ``not called``: the routes (but HEAD, which aiohttp adds to every GET) without a request
  answered or a connection open since the line before.
- The line holds route patterns, statuses, counts and times only: never a client's address, a
  query, a value of the path or a price.

Cheap on the event loop the collector shares with KIS and the trader's quotes (#538): a request
costs two reads of a clock and a few dictionary increments (about 0.6 µs on a laptop), and the line
is made once a day from those counts, a route's distinct milliseconds rather than its requests
(about 5 ms for 35 routes of 3,000 distinct milliseconds each).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import time as day_time

from aiohttp import hdrs, web
from aiohttp.typedefs import Handler
from aiohttp.web_urldispatcher import AbstractRoute

from krx_quant.clock import SessionClock
from krx_quant.feeds.base import KST

log = logging.getLogger(__name__)

UNMATCHED = "unmatched"  # a request no route matched
PERCENTILE = 95  # the line's p95
_NS_PER_MS = 1_000_000
_NS_PER_S = 1_000_000_000


@dataclass
class _Route:
    """One route's requests answered since the line before."""

    requests: int = 0
    statuses: Counter[int] = field(default_factory=Counter)
    ms: Counter[int] = field(default_factory=Counter)  # requests by milliseconds, rounded up
    ws_seconds: Counter[int] = field(default_factory=Counter)  # connections by seconds, rounded up


class RouteCounts:
    """The requests counted since ``since``: ``middleware`` counts them, ``take`` tells them.

    ``routes`` are the routes the line names when no one called them (``registered``);
    ``elapsed`` is the clock requests are timed by, in nanoseconds."""

    def __init__(
        self,
        since: datetime,
        *,
        routes: Iterable[str] = (),
        elapsed: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        self.since = since
        self.routes = tuple(routes)
        self._elapsed = elapsed
        self._counted: dict[str, _Route] = {}
        self._open: Counter[str] = Counter()  # WebSocket requests not ended, by route

    @web.middleware
    async def middleware(self, request: web.Request, handler: Handler) -> web.StreamResponse:
        key = _key(request.match_info.route)
        upgrade = request.headers.get(hdrs.UPGRADE, "").lower() == "websocket"
        if upgrade:
            self._open[key] += 1
        start = self._elapsed()
        try:
            response = await handler(request)
        except web.HTTPException as e:
            self._answered(key, e.status, start, websocket=False)
            raise
        except Exception:
            self._answered(key, 500, start, websocket=False)  # aiohttp answers it with 500
            raise
        finally:
            if upgrade:
                self._open[key] -= 1
        self._answered(key, response.status, start, websocket=isinstance(response, web.WebSocketResponse))
        return response

    def _answered(self, key: str, status: int, start: int, *, websocket: bool) -> None:
        took = self._elapsed() - start
        route = self._counted.get(key)
        if route is None:
            route = self._counted[key] = _Route()
        route.requests += 1
        route.statuses[status] += 1
        if websocket:
            route.ws_seconds[-(-took // _NS_PER_S)] += 1
        else:
            route.ms[-(-took // _NS_PER_MS)] += 1

    def line(self, until: datetime) -> str:
        """What was counted from ``since`` to ``until``, as the day's line tells it."""
        open_now = +self._open
        counted = sorted(self._counted.items(), key=lambda item: (-item[1].requests, item[0]))
        parts = [_told(key, route, open_now[key]) for key, route in counted]
        parts += [_told(key, _Route(), n) for key, n in sorted(open_now.items()) if key not in self._counted]
        total = sum(route.requests for route in self._counted.values())
        head = f"Engine API requests from {_at(self.since)} to {_at(until)}: {total} in all"
        unused = [r for r in self.routes if r not in self._counted and r not in open_now]
        if unused:
            parts.append("not called: " + ", ".join(unused))
        return "; ".join([head, *parts])

    def take(self, until: datetime) -> str:
        """The line to ``until``; the counting starts again from zero there (open connections stay
        open)."""
        told = self.line(until)
        self._counted = {}
        self.since = until
        return told


def registered(router: web.UrlDispatcher) -> tuple[str, ...]:
    """The routes as the line names them, but HEAD (aiohttp adds one to every GET)."""
    routes = router.routes()
    return tuple(_key(r) for r in routes if r.method != hdrs.METH_HEAD and r.resource is not None)


async def keep_daily(counts: RouteCounts, clock: SessionClock) -> None:
    """Log the line at each 00:00 KST, until cancelled."""
    while True:
        await clock.sleep_until(_next_midnight(clock.now()))
        log.info("%s", counts.take(clock.now()))


def install(
    app: web.Application, clock: SessionClock, *, elapsed: Callable[[], int] = time.perf_counter_ns
) -> RouteCounts:
    """Count ``app``'s requests: a line at each 00:00 KST of ``clock``, and one when the app stops.
    Before the app starts (its middlewares are fixed then)."""
    counts = RouteCounts(clock.now(), elapsed=elapsed)
    app.middlewares.append(counts.middleware)

    async def daily(app: web.Application) -> AsyncIterator[None]:
        counts.routes = registered(app.router)
        counts.since = clock.now()
        task = asyncio.create_task(keep_daily(counts, clock))
        yield
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        log.info("%s", counts.take(clock.now()))

    app.cleanup_ctx.append(daily)
    return counts


def _key(route: AbstractRoute) -> str:
    resource = route.resource
    return UNMATCHED if resource is None else f"{route.method} {resource.canonical}"


def _told(key: str, route: _Route, open_now: int) -> str:
    told = f"{key} {route.requests}"
    if route.statuses:
        told += " (" + ", ".join(f"{status} {n}" for status, n in sorted(route.statuses.items())) + ")"
    if route.ms:
        told += f" p95 {_quantile(route.ms)}ms"
    websocket = []
    if route.ws_seconds:
        websocket.append(f"p95 {_quantile(route.ws_seconds)}s max {max(route.ws_seconds)}s")
    if open_now:
        websocket.append(f"{open_now} open")
    if websocket:
        told += ", WebSocket " + ", ".join(websocket)
    return told


def _quantile(counts: Counter[int]) -> int:
    """The nearest-rank ``PERCENTILE`` of the values counted (at least one)."""
    rank = -(-counts.total() * PERCENTILE // 100)
    seen = 0
    for value in sorted(counts):
        seen += counts[value]
        if seen >= rank:
            return value
    return max(counts)


def _next_midnight(now: datetime) -> datetime:
    return datetime.combine(now.astimezone(KST).date() + timedelta(days=1), day_time(0), KST)


def _at(t: datetime) -> str:
    return t.astimezone(KST).isoformat(timespec="seconds")
