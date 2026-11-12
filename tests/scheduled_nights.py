"""Representative nights for the scheduled jobs' conditions (#651): each job's due decision on each is
kept in ``tests/scheduled_due.json``, written before the jobs read their own facts, so the move is shown
to change no job's decision (``tests/test_scheduled_due.py``).

A night is a day, this job's last run and every other job's (one shape each, ``SHAPES``), and what the
engine knows, by the names of the one ``Facts`` every job's condition was asked with before #651
(``FACTS``). The test builds each job's own facts from those names.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from krx_quant.algorithms import ALGORITHMS, DAILY_ALGORITHMS, KIND_ALGORITHMS
from krx_quant.feeds.base import KST
from krx_quant.kind_registry import DAILY_KINDS
from krx_quant.nightly_hit_rates import HIT_RATE_NIGHTS
from krx_quant.prevalidation import MIN_DAYS, LastReport
from krx_quant.scheduled import FAILED, INTERRUPTED, OK, RUNNING, JobRecord, Revising
from krx_quant.trader.notice_columns import APART

NOW, BEFORE = "new-commit", "old-commit"  # the code running, and an older one
# A Wednesday, a Saturday, a Friday (the night book's verdict day, #605, until #694 removed it) and the
# first of a month (a Tuesday).
DAYS = {
    date(2026, 11, 11): True,
    date(2026, 11, 14): False,
    date(2026, 11, 20): True,
    date(2026, 12, 1): True,
}

BAR_CLOSE = [*(a.name for a in ALGORITHMS), *(a.name for a in KIND_ALGORITHMS)]
DAILY = [a.name for a in DAILY_ALGORITHMS]


def _started(day: date, hhmm: str = "23:00") -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm), KST)


def _record(
    outcome: str, back: int, commit: str = NOW, timed_out: bool = False
) -> Callable[[date], JobRecord]:
    def make(day: date) -> JobRecord:
        ran = day - timedelta(days=back)
        return JobRecord(ran, outcome, _started(ran), commit, 0 if outcome == OK else 1, 600.0, timed_out)

    return make


# A job's last run, relative to the night's day: none, tonight's, the night before's, and older ones.
SHAPES: dict[str, Callable[[date], JobRecord] | None] = {
    "none": None,
    "ok-tonight": _record(OK, 0),
    "failed-tonight": _record(FAILED, 0),
    "cut-tonight": _record(INTERRUPTED, 0),
    "running-before": _record(RUNNING, 1),
    "ok-before": _record(OK, 1, BEFORE),
    "ok-before-same-code": _record(OK, 1),
    "failed-before-same-code": _record(FAILED, 1),
    "failed-before-other-code": _record(FAILED, 1, BEFORE),
    "timed-out-before": _record(FAILED, 1, timed_out=True),
    "cut-before": _record(INTERRUPTED, 1),
    "ok-last-month": _record(OK, 35, BEFORE),
    "failed-a-week-ago": _record(FAILED, 7),
}
# Every other job's last run: fewer shapes.
OTHERS = ("none", "ok-tonight", "failed-tonight", "cut-tonight", "ok-before", "failed-before-other-code")


def _reports(day: date) -> dict[str, LastReport]:
    """Reports of every kind a pre-validation's turn tells apart: judged, a look on fewer days, made
    since the run on older code or target, made after a run ended (by hand), and none."""
    names = [*BAR_CLOSE, *DAILY]
    made = [
        LastReport(_started(day - timedelta(days=1), "23:01"), True, True, 20),  # passed: judged
        LastReport(_started(day - timedelta(days=1), "23:01"), True, False, 20),  # refused: judged
        LastReport(_started(day - timedelta(days=1), "23:01"), True, False, 3),  # a look
        LastReport(_started(day - timedelta(days=1), "23:01"), False, True, 20),  # older code, since the run
        LastReport(_started(day - timedelta(days=1), "23:30"), True, False, 3),  # by hand, after the run
        LastReport(None, False, False, 20),
        LastReport(_started(day, "23:30"), False, False, 20),  # since tonight's run
    ]
    return {name: made[i % len(made)] for i, name in enumerate(names) if i % (len(made) + 1) != len(made)}


def _revising(day: date, report: bool) -> dict[str, Revising]:
    boundary = _started(day - timedelta(days=1), "23:30")
    out = {}
    for i, name in enumerate(BAR_CLOSE[:4]):
        found = LastReport(_started(day, "23:30"), i % 2 == 0, False, 20) if report else None
        out[name] = Revising(boundary, day - timedelta(days=3), MIN_DAYS + i - 1, found)
    return out


def _facts(level: str, day: date) -> dict[str, Any]:
    """What the engine knows, by the names of the facts every condition was asked with before #651."""
    if level == "nothing":
        return {}
    features = sorted({f for a in DAILY_ALGORITHMS for f in a.features})
    if level == "short":
        return {
            "recorded_days": MIN_DAYS - 1,
            "waiting": (*BAR_CLOSE, *DAILY),
            "hit_rate_nights": HIT_RATE_NIGHTS - 1,
            "smt_holdout_days": 9,
            "fill_notice_apart": APART - 1,
        }
    if level == "ready":
        return {
            "recorded_days": 30,
            "waiting": (*BAR_CLOSE, *DAILY),
            "simulating": tuple(BAR_CLOSE[:1]),
            "simulating_daily": tuple(DAILY[:1]),
            "revising": _revising(day, report=False),
            "trader_kis": True,
            "trader_live_kis": True,
            "hit_rate_nights": HIT_RATE_NIGHTS,
            "smt_holdout_days": 10,
            "state_backup": day - timedelta(days=1),
            "queue": tuple(reversed(BAR_CLOSE[::2])),
            "fill_notice_apart": APART,
        }
    if level == "reported":
        return {
            "recorded_days": MIN_DAYS,
            "waiting": (*BAR_CLOSE[1:], *DAILY),
            "reports": _reports(day),
            "revising": _revising(day, report=True),
            "simulating": tuple(BAR_CLOSE),
            "state_backup": day,
            "queue": ("nobody", *BAR_CLOSE[1:3]),
        }
    if level == "held":
        return {
            "recorded_days": 30,
            "waiting": tuple(DAILY),
            "reports": _reports(day),
            "pending_inputs": tuple(features),
            "pending_answers": tuple(k.name for k in DAILY_KINDS),
            "simulating_daily": tuple(DAILY),
        }
    raise ValueError(level)


LEVELS = ("nothing", "short", "ready", "reported", "held")


@dataclass(frozen=True)
class Scene:
    """One night as a job's condition is asked it."""

    key: str
    day: date
    trading_day: bool
    own: str  # this job's last run (``SHAPES``)
    others: str  # every other job's
    facts: Mapping[str, Any]  # by the names of the facts before #651

    def records(self, names: list[str], name: str) -> dict[str, JobRecord]:
        out = {}
        for other in names:
            make = SHAPES[self.own if other == name else self.others]
            if make is not None:
                out[other] = make(self.day)
        return out


def scenes() -> Iterator[Scene]:
    for day, trading in DAYS.items():
        for level in LEVELS:
            facts = _facts(level, day)
            for own in SHAPES:
                for others in OTHERS:
                    yield Scene(f"{day}/{level}/{own}/{others}", day, trading, own, others, facts)


def decisions(jobs: list[Any], asked: Callable[[Any, Scene, bool], bool]) -> dict[str, str]:
    """Each job's decision on every scene, in ``scenes``' order: ``1`` due, ``0`` not, ``E`` its
    condition raised; a job with a quiet night's condition also under ``<name> quiet``."""
    out: dict[str, list[str]] = {}
    every = list(scenes())
    for job in jobs:
        for quiet in (False, True):
            if quiet and job.quiet is None:
                continue
            marks = []
            for scene in every:
                try:
                    marks.append("1" if asked(job, scene, quiet) else "0")
                except Exception:
                    marks.append("E")
            out[f"{job.name} quiet" if quiet else job.name] = marks
    return {name: "".join(marks) for name, marks in out.items()}


def runs(marks: str) -> str:
    """``marks`` as runs (``0*12 1*3``), as the kept file holds them."""
    out: list[str] = []
    for i, mark in enumerate(marks):
        if i and marks[i - 1] == mark:
            continue
        n = len(marks[i:]) - len(marks[i:].lstrip(mark))
        out.append(f"{mark}*{n}")
    return " ".join(out)


def marks(kept: str) -> str:
    """The marks ``runs`` wrote."""
    return "".join(mark * int(n) for mark, n in (run.split("*") for run in kept.split()))
