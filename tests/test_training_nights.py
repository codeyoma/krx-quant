"""The nightly training's failures, alerted and counted across restarts (#355).

Input: a training process that ends as told (killed, stopped at its time limit, finished), the
nights' outcomes in a state folder, the last night the morning swap applied, the clock.
Output: why a night failed (exit code, last step started), the alerts sent, the nights in a row
not applied, and ``/v1/status``'s and ``doctor``'s fields of them.
"""

from __future__ import annotations

import json
import signal
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from krx_quant.config import Settings
from krx_quant.doctor import Status, report
from krx_quant.feeds.base import KST
from krx_quant.market_calendar import MarketCalendar
from krx_quant.notify import Alerts
from krx_quant.swap import SWAP_AT, TrainingWatch, _train
from krx_quant.training_nights import Failure, Outcome, TrainingNights
from tests.api_schema import check

# The week of #355: the night of 10-13 was the last applied, 10-17 and 10-18 a weekend.
APPLIED = date(2026, 10, 13)
CALENDAR = MarketCalendar(
    {date(2026, 10, 13) + timedelta(days=i): (i + 1) % 7 not in (5, 6) for i in range(14)}
)
KILLED = Failure("exit -9 (SIGKILL), 마지막으로 시작한 단계 turing", -9, "turing")


def at(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm), KST)


class Sent(Alerts):
    """Alerts without Telegram, keeping what they would send."""

    def __init__(self) -> None:
        super().__init__(cast(Any, None), None, None, "http://127.0.0.1:9")
        self.sent: list[str] = []

    def _send(self, text: str) -> None:
        self.sent.append(text)


def applied(state: Path, night: date) -> None:
    """The morning swap applied ``night``'s list (``Shelf.mark_applied``)."""
    (state / "models").mkdir(parents=True, exist_ok=True)
    (state / "models" / "applied.json").write_text(json.dumps({"night": night.isoformat()}))


# The training process


def script(body: str) -> list[str]:
    return [sys.executable, "-c", f"import logging, os, signal, sys, time\n{body}"]


STEPS = (
    "logging.basicConfig(level=logging.INFO)\n"
    "log = logging.getLogger('krx_quant.nightly')\n"
    "log.info('started step newton')\nlog.info('newton: 0.9 < 1.0')\nlog.info('started step turing')\n"
)


async def test_a_training_killed_from_outside_fails_with_its_exit_code_and_last_step() -> None:
    """The OOM killer's SIGKILL: no Python error, the output ends in the step it was in."""
    failure = await _train(APPLIED, script(STEPS + "os.kill(os.getpid(), signal.SIGKILL)"))

    assert failure == Failure("exit -9 (SIGKILL), 마지막으로 시작한 단계 turing", -signal.SIGKILL, "turing")


async def test_a_training_past_its_time_limit_says_the_step_it_was_in() -> None:
    failure = await _train(
        APPLIED, script(STEPS + "sys.stderr.flush(); time.sleep(60)"), timeout=timedelta(seconds=2)
    )

    assert failure is not None
    assert failure.reason == "not done in 0:00:02, 마지막으로 시작한 단계 turing"
    assert (failure.exit_code, failure.step) == (None, "turing")


async def test_a_training_that_ends_well_is_no_failure_and_one_with_an_error_says_its_code() -> None:
    assert await _train(APPLIED, script(STEPS)) is None
    failure = await _train(APPLIED, script("print('no archived bars', file=sys.stderr); sys.exit(1)"))
    assert failure == Failure("exit 1, 마지막으로 시작한 단계 없음", 1, None)


# The nights in a row


def test_the_nights_not_applied_are_the_failed_ones_and_those_whose_morning_passed(tmp_path: Path) -> None:
    nights = TrainingNights(tmp_path)
    applied(tmp_path, APPLIED)
    # 10-14 failed; 10-15 trained but its list came too late; 10-16 no outcome (the engine was down).
    nights.record(Outcome(date(2026, 10, 14), False, at(date(2026, 10, 14), "23:46"), -9, "turing", "x"))
    nights.record(Outcome(date(2026, 10, 15), True, at(date(2026, 10, 16), "07:40")))

    friday_noon = at(date(2026, 10, 16), "12:00")
    assert nights.not_applied(CALENDAR, SWAP_AT, friday_noon) == [date(2026, 10, 14), date(2026, 10, 15)]
    # Monday before the swap: the 16th's morning has come, the 19th's night is to come.
    monday = at(date(2026, 10, 19), "08:00")
    assert nights.not_applied(CALENDAR, SWAP_AT, monday) == [date(2026, 10, 14 + i) for i in range(3)]
    # A night that trained, before its morning swap: not counted yet; a failed one at once.
    nights.record(Outcome(date(2026, 10, 19), True, at(date(2026, 10, 19), "23:00")))
    nights.record(Outcome(date(2026, 10, 20), False, at(date(2026, 10, 20), "23:00"), 1, None, "x"))
    assert nights.not_applied(CALENDAR, SWAP_AT, at(date(2026, 10, 20), "23:30"))[-2:] == [
        date(2026, 10, 19),
        date(2026, 10, 20),
    ], "the 19th's morning has passed by the 20th's night"
    applied(tmp_path, date(2026, 10, 19))
    assert nights.not_applied(CALENDAR, SWAP_AT, at(date(2026, 10, 20), "23:30")) == [date(2026, 10, 20)]
    assert TrainingNights(tmp_path / "nowhere").not_applied(CALENDAR, SWAP_AT, monday) == []


def test_a_failure_is_alerted_once_then_each_night_of_two_or_more_in_a_row(tmp_path: Path) -> None:
    applied(tmp_path, APPLIED)
    alerts, clock = Sent(), [at(date(2026, 10, 14), "23:46")]
    watch = TrainingWatch(TrainingNights(tmp_path), alerts, CALENDAR, lambda: clock[0])

    watch.ended(date(2026, 10, 14), KILLED)
    assert alerts.sent == [
        "[장애] 2026-10-14 밤 학습이 실패했습니다: exit -9 (SIGKILL), 마지막으로 시작한 단계 turing"
    ]
    clock[0] = at(date(2026, 10, 15), "08:00")
    watch.morning()
    assert len(alerts.sent) == 1, "one night: alerted when it failed, not again"

    clock[0] = at(date(2026, 10, 15), "23:40")
    watch.ended(date(2026, 10, 15), KILLED)
    assert len(alerts.sent) == 2, "the second in a row is alerted though the first is still on"
    assert alerts.sent[1].startswith("[장애] 2026-10-15 밤 학습이 실패했습니다: exit -9 (SIGKILL)")
    assert (
        "밤 학습이 거래일 밤 2번 연속 적용되지 않았습니다(마지막으로 적용된 밤 2026-10-13" in alerts.sent[1]
    )
    assert "그 뒤 10-14, 10-15, 그중 학습 실패 2번" in alerts.sent[1]

    # A restart: nothing said again for the night alerted, and the next good night clears it.
    alerts = Sent()
    watch = TrainingWatch(TrainingNights(tmp_path), alerts, CALENDAR, lambda: clock[0])
    clock[0] = at(date(2026, 10, 16), "08:00")
    watch.morning()
    assert alerts.sent == []
    clock[0] = at(date(2026, 10, 16), "23:00")
    watch.ended(date(2026, 10, 16), None)
    assert alerts.sent == ["[복구] 밤 학습이 다시 됩니다."]


def test_a_night_that_trained_but_did_not_run_by_its_morning_counts_and_is_alerted(tmp_path: Path) -> None:
    """Its list was final after 07:30: the morning swap passes it, a night in a row more."""
    applied(tmp_path, APPLIED)
    nights = TrainingNights(tmp_path)
    nights.record(Outcome(date(2026, 10, 14), False, at(date(2026, 10, 14), "23:46"), -9, "turing", "x"))
    alerts, clock = Sent(), [at(date(2026, 10, 16), "07:50")]
    watch = TrainingWatch(nights, alerts, CALENDAR, lambda: clock[0])
    watch.ended(date(2026, 10, 15), None)  # finished at 07:50: too late for the 08:00 swap
    clock[0] = at(date(2026, 10, 16), "08:00")

    watch.morning()

    assert alerts.sent[-1].startswith("[장애] 밤 학습이 거래일 밤 2번 연속 적용되지 않았습니다")
    assert "그중 학습 실패 1번" in alerts.sent[-1]
    watch.morning()
    assert len([t for t in alerts.sent if "연속" in t]) == 1, "once per night"


def test_the_status_and_doctor_show_the_last_night_applied_and_the_nights_failed_since(
    tmp_path: Path,
) -> None:
    applied(tmp_path / "state", APPLIED)
    nights = TrainingNights(tmp_path / "state")
    nights.record(Outcome(date(2026, 10, 21), True, at(date(2026, 10, 22), "07:50")))
    nights.record(
        Outcome(date(2026, 10, 22), False, at(date(2026, 10, 22), "23:46:33"), -9, "turing", KILLED.reason)
    )
    now = at(date(2026, 10, 23), "00:30")
    watch = TrainingWatch(nights, Sent(), CALENDAR, lambda: now)

    fields = watch.fields()
    missed = [
        "2026-10-14",
        "2026-10-15",
        "2026-10-16",
        "2026-10-19",
        "2026-10-20",
        "2026-10-21",
        "2026-10-22",
    ]
    assert fields == {
        "last_applied": "2026-10-13",
        "nights_not_applied": 7,
        "not_applied": missed,
        "failures": [
            {
                "day": "2026-10-22",
                "ended_at": "2026-10-22T23:46:33+09:00",
                "exit_code": -9,
                "step": "turing",
                "reason": KILLED.reason,
            }
        ],
    }
    status = {
        "day": "2026-10-13",
        "finalized_at": "2026-10-14T00:15:00+09:00",
        "promoted": {},
        "reports": [],
        "applied": True,
        "reason": "applied",
    }
    check("training", {**status, **fields})
    check("training", status)  # the fields are added: an engine without them still keeps the contract

    settings = Settings(
        kis_app_key="", kis_app_secret="", state_dir=tmp_path / "state", data_dir=tmp_path / "data"
    )
    text = report(settings, now, Status(None, ""))
    part = text[text.index("7. 밤 학습") :]
    assert f"마지막으로 적용된 밤 2026-10-13 · 그 뒤 적용되지 않은 거래일 밤 7번: {', '.join(missed)}" in part
    assert "2026-10-22 실패 · 2026-10-22 23:46:33 · 종료 코드 -9 · 마지막으로 시작한 단계 turing" in part
    assert "2026-10-21 끝남 · 2026-10-22 07:50:00" in part


@pytest.mark.parametrize("broken", ["{", "[]"])
def test_an_outcomes_file_it_cannot_read_starts_afresh(tmp_path: Path, broken: str) -> None:
    (tmp_path / "training_nights.json").write_text(broken)
    nights = TrainingNights(tmp_path)
    assert nights.outcomes() == {} and nights.alerted_for() is None
    nights.record(Outcome(APPLIED, True, at(APPLIED, "23:00")))
    assert list(nights.outcomes()) == [APPLIED]
