"""The probes' calls to one source (#510): spaced by their pace and counted, on a clock that moves
only when the calls wait; and a probe's KIS calls through the fake KIS, by its own pace and its
own limit of failures in a row."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from krx_quant import probe_calls, supply_probe
from krx_quant.fake_kis import FakeKis
from krx_quant.net_buys_probe import MAX_FAILED_IN_A_ROW, probe_net_buys
from krx_quant.probe_calls import Calls
from krx_quant.supply import INVESTORS
from tests.test_collector import settings_for


class Clock:
    """``asyncio.sleep`` for the calls: the wait is noted, the clock moves by it, and it returns."""

    def __init__(self) -> None:
        self.now = 0.0
        self.waits: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(probe_calls, "asyncio", SimpleNamespace(sleep=clock.sleep))
    return clock


async def test_the_first_call_goes_at_once_and_each_next_one_a_pace_after_the_one_before(
    clock: Clock,
) -> None:
    calls = Calls(0.5, 3)
    turns = []
    for _ in range(4):
        await calls.turn()
        turns.append(clock.now)

    assert turns == [0.0, 0.5, 1.0, 1.5]
    assert calls.made == 4


async def test_calls_whose_client_spaces_them_are_counted_without_waiting(clock: Clock) -> None:
    """Toss's client keeps its own interval (``TossClient``): the probe waits for nothing more."""
    calls = Calls(None, 3)
    for _ in range(3):
        await calls.turn()

    assert clock.waits == [] and calls.made == 3


def test_failures_in_a_row_stop_the_calls_and_an_answer_between_them_starts_the_count_again() -> None:
    calls = Calls(0.5, 3)
    for ok in (False, False, True, False, False):
        calls.answered(ok)

    assert (calls.failed, calls.failed_in_a_row, calls.stopped) == (4, 2, False)
    calls.answered(False)
    assert (calls.failed, calls.failed_in_a_row, calls.stopped) == (5, 3, True)


def test_calls_without_a_limit_never_stop() -> None:
    calls = Calls(0.5)
    for _ in range(100):
        calls.answered(False)

    assert calls.failed == calls.failed_in_a_row == 100 and not calls.stopped


async def test_a_probe_s_calls_go_its_pace_apart_and_stop_at_its_limit_of_failures_in_a_row(
    kis: FakeKis, tmp_path: Path, clock: Clock
) -> None:
    """``probe-net-buys`` against a KIS whose every answer lacks the row list (#297: a failure):
    the first call at once, each next one ``supply_probe.PACE`` later, none after its limit."""
    kis.rest_responses[INVESTORS.tr] = lambda params: {
        "rt_cd": "0",
        "msg_cd": "MCA00000",
        "output2": {"x": "1"},
    }
    settings = replace(settings_for(kis, tmp_path), symbols=("005930",), rest_max_per_second=1000)
    lines: list[str] = []

    calls, failed = await probe_net_buys(settings, date(2026, 10, 20), lines.append)

    assert calls == failed == MAX_FAILED_IN_A_ROW == len(kis.rest_requests)
    assert clock.waits == [supply_probe.PACE] * (MAX_FAILED_IN_A_ROW - 1)
    assert f"KIS: {calls}번 조회, {failed}번 실패" in lines
