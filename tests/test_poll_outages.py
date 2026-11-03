"""REST polls' outages (#441): each call counted by feed and key, failures grouped into outages,
an endpoint alerted once when one goes on and once when it is over; the polling is unchanged.

Input: KIS's answers from the fake KIS server (the refusals the server saw on 2026-10-31,
``tests/kis_rest.py``), or made-up errors. Output: ``PollOutages.snapshot()``, ``/v1/status``'s
``kis.polls`` and the doctor's summary of it, the requests and token issuances the fake KIS saw,
and the alerts sent to its Telegram stand-in.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import aiohttp
import pytest

from krx_quant.auth import tokens_and_client
from krx_quant.clock import FakeClock
from krx_quant.collector import run_collector
from krx_quant.doctor import Status, report
from krx_quant.fake_kis import FakeKis
from krx_quant.feeds import FOREIGN_ESTIMATE, VI_STATUS
from krx_quant.feeds.base import KST
from krx_quant.kis import KisError, RestClient
from krx_quant.notify import Alerts
from krx_quant.poll_outages import ALERT_PREFIX, PollOutages, PollState, error_kind
from krx_quant.polling import Poller, PollJob
from krx_quant.recorder import ParquetRecorder
from tests.kis_rest import query_error, requests_for, session_full
from tests.test_collector import settings_for
from tests.test_live_predictions import free_port, poll
from tests.waiting import until

FOREIGN = FOREIGN_ESTIMATE.tr_id
KEY = (FOREIGN, "005930")
START = datetime(2026, 10, 8, 9, 20, tzinfo=KST)
EVERY = timedelta(seconds=301)  # the investor estimates' 300 s interval, and the call
OK = {"rt_cd": "0", "msg_cd": "MCA00000", "msg1": "정상처리 되었습니다.", "output2": []}
ALERT = ALERT_PREFIX + FOREIGN


def answers(clock: FakeClock, outcomes: str, done: asyncio.Event) -> Callable[[dict[str, str]], Any]:
    """One answer a call, in order: ``o`` OK, ``S`` OPSQ1002, ``E`` EGW00316. Each call first moves
    ``clock`` on by ``EVERY``, one poll interval; ``done`` is set at the last."""
    left = list(outcomes)

    def respond(params: dict[str, str]) -> dict[str, Any]:
        clock.set(clock.now() + EVERY)
        outcome = left.pop(0)
        if not left:
            done.set()  # the poll job stops after this answer
        if outcome == "S":
            return session_full()
        if outcome == "E":
            query_error()
        return OK

    return respond


async def run_polls(kis: FakeKis, tmp_path: Path, outcomes: str) -> PollOutages:
    """Poll 005930's investor estimates through the fake KIS, as the collector does, until every
    answer of ``outcomes`` is counted; alerts go to the fake Telegram."""
    clock = FakeClock(START)
    stop = asyncio.Event()
    kis.rest_responses[FOREIGN] = answers(clock, outcomes, stop)
    recorder = ParquetRecorder(tmp_path, flush_rows=1, flush_seconds=3600)
    async with aiohttp.ClientSession() as session:
        _, client = tokens_and_client(session, settings_for(kis, tmp_path), clock.now)
        alerts = Alerts(session, "123:fake", "42", kis.rest_url)
        outages = PollOutages(clock.now, alerts)
        poller = Poller(client, recorder, clock.now, stop, outages=outages)
        poller.add(PollJob(FOREIGN_ESTIMATE, "005930", 0.01))
        await until(lambda: sum(s.attempts for s in outages.snapshot()) == len(outcomes))
        await poller.close()
        await alerts.close()
    recorder.close()
    return outages


def refused(code: str, status: int) -> KisError:
    return KisError(f"{FOREIGN} {{}}: HTTP {status} {{...}}", code=code, status=status)


def state_of(outages: PollOutages, key: tuple[str, str] = KEY) -> PollState:
    [state] = [s for s in outages.snapshot() if (s.feed, s.key) == key]
    return state


@pytest.fixture
async def alerts() -> AsyncIterator[Alerts]:
    """Alerts without Telegram: only logged, and known by ``is_active``."""
    async with aiohttp.ClientSession() as session:
        yield Alerts(session, None, None, "")


async def test_scattered_failures_are_short_outages_counted_by_code_and_not_alerted(
    kis: FakeKis, tmp_path: Path
) -> None:
    outages = await run_polls(kis, tmp_path, "oSoEoSSo")

    state = state_of(outages)
    assert (state.attempts, state.failures, state.outages) == (8, 4, 3)
    assert state.errors == {"EGW00316": 1, "OPSQ1002": 3}
    assert (state.consecutive_failures, state.max_consecutive_failures) == (0, 2)
    assert state.outage_since is None
    assert state.last_recovery_seconds == (2 * EVERY).total_seconds(), "the last two failures, then OK"
    assert state.last_success_at == START + 8 * EVERY
    assert kis.telegram_messages == [], "two in a row is not an outage that goes on"
    assert len(requests_for(kis, FOREIGN)) == 8, "no call was sent again at once"
    assert len(kis.tokens_issued) == 1, "no token was issued again"


async def test_an_outage_that_goes_on_alerts_once_and_its_recovery_once(
    kis: FakeKis, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="krx_quant.poll_outages")
    outages = await run_polls(kis, tmp_path, "oSESESo")

    texts = [m["text"] for m in kis.telegram_messages]
    assert texts == [
        "[장애] KIS REST 조회 HHPTJ04160200의 005930 키가 3번 잇달아 실패했습니다"
        " (10분째: EGW00316 1번, OPSQ1002 2번). 재시도와 간격은 그대로라 다음 간격에 다시 묻습니다.",
        "[복구] KIS REST 조회 HHPTJ04160200의 모든 키가 다시 답합니다 (장애 시작부터 25분).",
    ]
    assert [r.getMessage() for r in caplog.records if r.name == "krx_quant.poll_outages"] == [
        "poll outage of HHPTJ04160200 005930 ended after 1505 s: 5 failures in a row (EGW00316 2, OPSQ1002 3)"
    ]
    state = state_of(outages)
    assert (state.outages, state.max_consecutive_failures, state.consecutive_failures) == (1, 5, 0)
    assert state.last_recovery_seconds == (5 * EVERY).total_seconds()
    assert len(requests_for(kis, FOREIGN)) == 7, "each failure waited for the next interval"
    assert len(kis.tokens_issued) == 1


def test_a_success_sets_the_failures_in_a_row_back_to_zero() -> None:
    clock = FakeClock(START)
    outages = PollOutages(clock.now)
    outages.failed(KEY, refused("OPSQ1002", 200))
    clock.set(START + EVERY)
    outages.failed(KEY, refused("EGW00316", 500))
    assert (state_of(outages).consecutive_failures, state_of(outages).outage_since) == (2, START)

    clock.set(START + 2 * EVERY)
    outages.succeeded(KEY)

    state = state_of(outages)
    assert (state.consecutive_failures, state.outage_since) == (0, None)
    assert (state.max_consecutive_failures, state.last_recovery_seconds) == (2, (2 * EVERY).total_seconds())
    assert (state.attempts, state.failures, state.outages) == (3, 2, 1)


async def test_an_outage_is_alerted_only_after_three_failures_over_ten_minutes(alerts: Alerts) -> None:
    clock = FakeClock(START)
    outages = PollOutages(clock.now, alerts)
    vi = (VI_STATUS.tr_id, "005930")
    outages.failed(KEY, refused("OPSQ1002", 200))
    for i in range(119):  # asked every 5 s: 119 failures in a row within 10 minutes
        clock.set(START + timedelta(seconds=5 * i))
        outages.failed(vi, TimeoutError())
    assert not alerts.active(ALERT_PREFIX), "many failures, in under ten minutes"

    clock.set(START + timedelta(seconds=600))
    outages.failed(vi, TimeoutError())
    assert alerts.active(ALERT_PREFIX) == [ALERT_PREFIX + VI_STATUS.tr_id]

    clock.set(START + timedelta(minutes=20))  # asked every 20 minutes
    outages.failed(KEY, refused("OPSQ1002", 200))
    assert alerts.active(ALERT_PREFIX) == [ALERT_PREFIX + VI_STATUS.tr_id], "20 minutes, but two failures"


async def test_an_endpoint_recovers_once_every_key_of_it_answers_again(alerts: Alerts) -> None:
    clock = FakeClock(START)
    outages = PollOutages(clock.now, alerts)
    other = (FOREIGN, "000660")
    vi = (VI_STATUS.tr_id, "005930")
    for i in range(3):
        clock.set(START + i * EVERY)
        outages.failed(KEY, refused("OPSQ1002", 200))
    assert alerts.is_active(ALERT)
    outages.failed(other, refused("EGW00316", 500))
    outages.failed(vi, TimeoutError())  # another endpoint: it holds nothing back

    clock.set(START + 3 * EVERY)
    outages.succeeded(KEY)
    assert alerts.is_active(ALERT), "000660 still fails"
    for i in range(4, 7):  # 000660 now goes on too: still the one alert
        clock.set(START + i * EVERY)
        outages.failed(other, refused("EGW00316", 500))
    assert alerts.active(ALERT_PREFIX) == [ALERT]

    outages.succeeded(other)
    assert not alerts.is_active(ALERT)
    assert state_of(outages, vi).consecutive_failures == 1


def test_today_s_counts_start_again_at_midnight_and_an_outage_going_on_carries_over() -> None:
    late = datetime(2026, 10, 8, 23, 50, tzinfo=KST)
    clock = FakeClock(late)
    outages = PollOutages(clock.now)
    other = (FOREIGN, "000660")
    outages.succeeded(other)
    outages.failed(KEY, refused("OPSQ1002", 200))
    clock.set(late + timedelta(minutes=5))
    outages.failed(KEY, refused("OPSQ1002", 200))

    clock.set(late + timedelta(minutes=15))  # 00:05 on 10-09
    outages.failed(KEY, refused("EGW00316", 500))

    state = state_of(outages)
    assert (state.attempts, state.failures, state.errors) == (1, 1, {"EGW00316": 1})
    assert (state.outages, state.consecutive_failures, state.max_consecutive_failures) == (1, 3, 3)
    assert state.outage_since == late
    quiet = state_of(outages, other)
    assert (quiet.attempts, quiet.outages, quiet.last_success_at) == (0, 0, late)


def test_a_failure_is_kept_as_its_kis_code_its_http_status_or_its_type() -> None:
    assert error_kind(refused("OPSQ1002", 200)) == "OPSQ1002"
    assert error_kind(KisError("an answer that is no JSON object", status=502)) == "HTTP 502"
    assert error_kind(KisError("x", code="not a <code>", status=500)) == "HTTP 500", "never the text"
    assert error_kind(KisError("access token not issued")) == "KisError"
    assert error_kind(aiohttp.ServerDisconnectedError()) == "ServerDisconnectedError"
    assert error_kind(TimeoutError()) == "TimeoutError"


class _Hanging:
    """A REST client whose first call is refused and whose next one never answers."""

    def __init__(self) -> None:
        self.calls = 0
        self.hanging = asyncio.Event()

    async def get(self, tr_id: str, path: str, params: dict[str, str]) -> dict[str, Any]:
        self.calls += 1
        if self.calls == 1:
            raise refused("OPSQ1002", 200)
        self.hanging.set()
        await asyncio.Event().wait()
        raise AssertionError("never answered")


async def test_closing_cancels_a_call_in_an_outage_at_once_and_does_not_count_it(tmp_path: Path) -> None:
    clock = FakeClock(START)
    client = _Hanging()
    outages = PollOutages(clock.now)
    recorder = ParquetRecorder(tmp_path, flush_rows=1, flush_seconds=3600)
    poller = Poller(cast(RestClient, client), recorder, clock.now, asyncio.Event(), outages=outages)
    poller.add(PollJob(FOREIGN_ESTIMATE, "005930", 0.01))
    await asyncio.wait_for(client.hanging.wait(), 5)

    await asyncio.wait_for(poller.close(), 1)
    recorder.close()

    state = state_of(outages)
    assert (state.attempts, state.failures, state.consecutive_failures) == (1, 1, 1)


async def test_the_status_and_the_doctor_show_the_polls(kis: FakeKis, tmp_path: Path) -> None:
    kis.rest_responses[FOREIGN] = lambda q: session_full()
    kis.rest_responses[VI_STATUS.tr_id] = lambda q: {**OK, "output": []}
    port = free_port()
    settings = replace(settings_for(kis, tmp_path), api_port=port)
    stop = asyncio.Event()
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=FakeClock(START)))

    def counted(status: dict[str, Any]) -> bool:
        polls = {p["feed"]: p for p in status["kis"]["polls"]}
        return FOREIGN in polls and VI_STATUS.tr_id in polls

    try:
        status: dict[str, Any] = await poll(f"http://127.0.0.1:{port}/v1/status", counted)
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)

    [estimate] = [p for p in status["kis"]["polls"] if p["feed"] == FOREIGN]
    assert estimate == {
        "feed": FOREIGN,
        "key": "005930",
        "attempts": 1,
        "failures": 1,
        "errors": {"OPSQ1002": 1},
        "outages": 1,
        "consecutive_failures": 1,
        "max_consecutive_failures": 1,
        "outage_since": "2026-10-08T09:20:00+09:00",
        "last_success_at": None,
        "last_recovery_seconds": None,
    }
    [vi] = [p for p in status["kis"]["polls"] if p["feed"] == VI_STATUS.tr_id]
    assert (vi["failures"], vi["outage_since"]) == (0, None)
    assert vi["last_success_at"] == "2026-10-08T09:20:00+09:00"
    lines = report(settings, START, Status(status, "x")).splitlines()
    [total] = [line for line in lines if line.startswith("  REST 조회(오늘, 엔진 시작 뒤, #441): 키 2개 · ")]
    assert total.endswith(" · 실패 1번 · 장애 1번 · 지금 장애 중인 키 1개")
    assert f"  실패(끝점·오류 코드별): {FOREIGN} OPSQ1002 1" in lines
    head = lines.index(
        "  feed key attempts failures outages max_consecutive_failures consecutive_failures"
        " outage_since last_success_at last_recovery_seconds"
    )
    assert lines[head + 1 : head + 3] == [
        f"  {FOREIGN} 005930 1 1 1 1 1 09:20:00 - -",  # the key that failed, not the VI's
        "  feed key last_received_at seconds_since gap",
    ]
