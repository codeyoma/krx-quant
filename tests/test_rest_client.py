"""The REST client against the fake KIS: a connection KIS drops before answering is asked again
once, at once, on a new connection; nothing is sent again after an answer (#363)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web

from krx_quant import kis as kis_module
from krx_quant.fake_kis import FakeKis
from krx_quant.feeds import VI_STATUS
from krx_quant.kis import KisError, RestClient, rest_session
from krx_quant.polling import Poller, PollJob
from krx_quant.ratelimit import RateLimiter
from krx_quant.recorder import ParquetRecorder
from tests.test_collector import FIXED_NOW

TOKEN = "the-token"
VI = VI_STATUS.tr_id
PARAMS = VI_STATUS.params("005930", date(2026, 10, 8))
NO_VI = {"rt_cd": "0", "msg_cd": "MCA00000", "msg1": "정상처리 되었습니다.", "output": []}
RETRIED = "answered on a new connection"


class Recorded(RateLimiter):
    """The limiter, with the moments it let each call through, on its own clock."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.grants: list[float] = []

    async def acquire(self) -> float:
        self.grants.append(await super().acquire())
        return self.grants[-1]


def script(*outcomes: str) -> Callable[[dict[str, str]], dict[str, Any]]:
    """KIS's VI answer, after ``outcomes`` one call each: ``drop`` (the connection closed before
    any answer), ``cut`` (closed once the answer began), ``500`` (an HTTP 500 answer)."""
    left = list(outcomes)

    def respond(params: dict[str, str]) -> dict[str, Any]:
        outcome = left.pop(0) if left else "ok"
        if outcome == "drop":
            raise ConnectionResetError("KIS dropped the connection")
        if outcome == "cut":
            raise ConnectionAbortedError("KIS dropped the connection mid-answer")
        if outcome == "500":
            body = {"rt_cd": "1", "msg_cd": "EGW00500", "msg1": "made-up server error"}
            raise web.HTTPInternalServerError(text=json.dumps(body), content_type="application/json")
        return NO_VI

    return respond


@asynccontextmanager
async def client_of(kis: FakeKis, limiter: RateLimiter | None = None) -> AsyncIterator[RestClient]:
    """A REST client for the fake KIS on a session as the collector makes it."""
    kis.tokens_issued.append(TOKEN)

    async def token() -> str:
        return TOKEN

    async with rest_session() as session:
        yield RestClient(
            session, kis.rest_url, kis.app_key, kis.app_secret, token, limiter or RateLimiter(20)
        )


def retried(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "krx_quant.kis" and RETRIED in r.getMessage()]


async def test_aiohttp_itself_sends_a_call_again_once_when_its_connection_closes_before_the_answer(
    kis: FakeKis, caplog: pytest.LogCaptureFixture
) -> None:
    # What the client's own retry builds on (aiohttp 3.14): one such send, on another connection.
    caplog.set_level(logging.INFO, logger="krx_quant.kis")
    kis.rest_responses[VI] = script("drop")
    async with client_of(kis) as client:
        body = await client.get(VI, VI_STATUS.path, PARAMS)

    assert body == NO_VI
    assert len(kis.rest_requests) == 2
    assert retried(caplog) == []


async def test_a_call_whose_pooled_connections_kis_closed_is_asked_again_at_once_on_a_new_one(
    kis: FakeKis, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="krx_quant.kis")
    kis.rest_responses[VI] = script("ok", "ok", "drop", "drop")
    async with client_of(kis) as client:
        # Two connections left in the pool; KIS closes both, as on 2026-10-23 (#363): the call and
        # aiohttp's own second send each go on one of them.
        await asyncio.gather(*(client.get(VI, VI_STATUS.path, PARAMS) for _ in range(2)))
        body = await client.get(VI, VI_STATUS.path, PARAMS)

    assert body == NO_VI
    assert len(kis.rest_requests) == 5
    *before, again = kis.rest_ports
    assert again not in before, "asked again on a new connection, not on a pooled one"
    assert retried(caplog) == [
        f"KIS REST {VI} {RETRIED} after its pooled one closed before an answer "
        "(ServerDisconnectedError: Server disconnected)"
    ]


async def test_a_call_whose_new_connection_closes_too_fails_without_another_send(
    kis: FakeKis, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="krx_quant.kis")
    kis.rest_responses[VI] = script("drop", "drop", "drop")
    async with client_of(kis) as client:
        with pytest.raises(aiohttp.ServerDisconnectedError):
            await client.get(VI, VI_STATUS.path, PARAMS)

    assert len(kis.rest_requests) == 3, "the call, aiohttp's second send, the client's one retry"
    assert retried(caplog) == []


async def test_a_poll_failed_that_way_is_warned_and_asked_again_on_the_next_interval(
    kis: FakeKis, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    kis.rest_responses[VI] = script("drop", "drop", "drop")
    interval = 0.2
    stop = asyncio.Event()
    recorder = ParquetRecorder(tmp_path, flush_rows=1, flush_seconds=3600)
    async with client_of(kis) as client:
        poller = Poller(client, recorder, lambda: FIXED_NOW, stop, on_received=lambda _: stop.set())
        poller.add(PollJob(VI_STATUS, "005930", interval))
        await asyncio.wait_for(stop.wait(), 5)
        await poller.close()
    recorder.close()

    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warned == [f"poll {VI} 005930 failed: Server disconnected"]
    *_, retry, next_interval = (t for t, _, _, _ in kis.rest_requests)
    assert len(kis.rest_requests) == 4
    assert next_interval - retry >= interval, "the next ask waited for the next interval"
    assert retried(caplog) == [], "the next interval's ask was answered at once"


async def test_the_call_asked_again_waits_for_the_limiter(kis: FakeKis) -> None:
    limiter = Recorded(1, period=0.3, margin=0)
    kis.rest_responses[VI] = script("drop", "drop")
    async with client_of(kis, limiter) as client:
        await client.get(VI, VI_STATUS.path, PARAMS)

    first, again = limiter.grants
    assert again - first >= 0.3
    # aiohttp's own second send does not go through the limiter; the client's retry does.
    sent, aiohttps, retry = (t for t, _, _, _ in kis.rest_requests)
    assert first <= sent <= aiohttps < again <= retry


async def test_nothing_is_sent_again_after_an_http_500(
    kis: FakeKis, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="krx_quant.kis")
    kis.rest_responses[VI] = script("500")
    async with client_of(kis) as client:
        with pytest.raises(KisError, match="HTTP 500"):
            await client.get(VI, VI_STATUS.path, PARAMS)

    assert len(kis.rest_requests) == 1
    assert retried(caplog) == []


async def test_nothing_is_sent_again_once_the_answer_has_begun(
    kis: FakeKis, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="krx_quant.kis")
    kis.rest_responses[VI] = script("cut")
    async with client_of(kis) as client:
        with pytest.raises(aiohttp.ClientPayloadError):
            await client.get(VI, VI_STATUS.path, PARAMS)

    assert len(kis.rest_requests) == 1
    assert retried(caplog) == []


async def test_a_rest_session_reuses_a_connection_only_while_kis_keeps_it(
    kis: FakeKis, monkeypatch: pytest.MonkeyPatch
) -> None:
    kis.rest_responses[VI] = script()
    async with client_of(kis) as client:
        for _ in range(2):
            await client.get(VI, VI_STATUS.path, PARAMS)
    reused, at_once = kis.rest_ports
    assert at_once == reused, "a connection just used is used again"

    monkeypatch.setattr(kis_module, "KEEPALIVE_SECONDS", 0.5)
    async with client_of(kis) as client:
        await client.get(VI, VI_STATUS.path, PARAMS)
        await asyncio.sleep(1.0)  # the connection's idle time, the input under test
        await client.get(VI, VI_STATUS.path, PARAMS)
    _, _, before_idle, after_idle = kis.rest_ports
    assert after_idle != before_idle, "a connection idle past the keep-alive is not used again"
