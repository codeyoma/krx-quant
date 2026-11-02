"""The trader's review calendar and evaluation version (#469, spec #468 "버전과 검토 주기").

**The frozen version.** The champion policy's values (its cells by their predictors' names, not the
cost map's roles; their order; each cell's θ; the exit rules and the training procedure being the
code's) hold from one review to the next: between reviews nothing replaces the champion
(``promotion.promote`` judges on a review night alone). The nightly training and the 08:00 swap go
on within a version: each trade keeps the model version of the prediction that bought it
(``trades.model_version``).

**The calendar.** A review falls every ``REVIEW_DAYS`` (20) trading days, counted on the trader's
market calendar (``sessions``: the collector's ``state/market_calendar.json``) from the evaluation's
first day. Cycle n runs from its first trading day to its 20th, the review day; the review is judged
after that day's close and applies from the next trading day, the next cycle's first. The first
cycle begins on the first trading day whose continuous trading starts at or after the evaluation was
begun (``begin``: at the trader's first start after the deploy, or the nightly job's, whichever comes
first). The first day is kept (``evaluations``), so that a restart goes on with the same calendar;
the trading days after it are counted on the calendar each time (a holiday announced later moves the
reviews after it, never one before it).

**The evaluation version.** The rules the evidence is judged under. 1 (``PREVIOUS``): #400's and
#408's, the promotion every night from a challenger's tenth common day. 2 (``EVALUATION``, #469):
the same records and live gate, counted from its first day alone, and the promotion on review nights
alone. The evidence of an earlier version stays in the store (``policy_days``, the trades,
``gate_checks``) and a later one does not read it (``promotion.since``): the records start again,
and no live gate passed before is inherited.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from krx_quant.feeds.base import KST, kst_day
from krx_quant.market_calendar import Session
from krx_quant.trader import PICK_AT

Sessions = Callable[[date], Session | None]

# The evaluation version of these rules (#469), and the one before it (#400, #408), which a store
# without an ``evaluations`` row is read under.
EVALUATION = 2
PREVIOUS = 1
# A cycle's trading days: its last is the review day (#468).
REVIEW_DAYS = 20
# How far a search for a trading day looks before it gives up (a calendar without one is broken).
SEARCH_DAYS = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS evaluations (  -- each evaluation version begun (#469)
    version INTEGER PRIMARY KEY,
    begun_at TEXT NOT NULL,              -- when it was first kept
    first_day TEXT NOT NULL,             -- its first cycle's first trading day
    review_days INTEGER NOT NULL         -- a cycle's trading days, the last the review day
);
"""


@dataclass(frozen=True)
class Evaluation:
    """An evaluation version as kept: from when, its first cycle's first day, a cycle's length."""

    version: int
    begun_at: datetime
    first_day: date
    review_days: int = REVIEW_DAYS


@dataclass(frozen=True)
class Cycle:
    """Review cycle ``number`` of evaluation ``evaluation``: its first trading day (``first``) and its
    review day (``review``, its ``days``-th); ``day``: the place in it of the trading day asked about
    (1 to ``days``), None when that is before its first."""

    evaluation: int
    number: int
    first: date
    review: date
    day: int | None
    days: int = REVIEW_DAYS

    def as_json(self, since: date) -> dict[str, Any]:
        return {
            "evaluation": self.evaluation,
            "since": since.isoformat(),
            "number": self.number,
            "start": self.first.isoformat(),
            "review": self.review.isoformat(),
            "day": self.day,
            "days": self.days,
        }


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(f"BEGIN;{SCHEMA}COMMIT;")


def first_day(now: datetime, sessions: Sessions) -> date:
    """The first trading day whose continuous trading starts at or after ``now``."""
    day = kst_day(now)
    for _ in range(SEARCH_DAYS):
        session = sessions(day)
        if session is not None and now <= session.open:
            return day
        day += timedelta(days=1)
    raise ValueError(f"no trading day within {SEARCH_DAYS} days of {now}")


def begin(db: sqlite3.Connection, now: datetime, sessions: Sessions) -> Evaluation:
    """The evaluation version of these rules (``EVALUATION``), kept at ``now`` with its first day if it
    was not yet (module doc); as kept."""
    with db:
        db.execute(
            "INSERT OR IGNORE INTO evaluations (version, begun_at, first_day, review_days)"
            " VALUES (?, ?, ?, ?)",
            (EVALUATION, now.astimezone(KST).isoformat(), first_day(now, sessions).isoformat(), REVIEW_DAYS),
        )
    found = current(db)
    assert found is not None
    return found


def current(db: sqlite3.Connection) -> Evaluation | None:
    """The latest evaluation version kept; None before the first (a store from before #469, or read
    before the trader or the nightly job began it)."""
    if not _has_table(db, "evaluations"):
        return None
    row = db.execute(
        "SELECT version, begun_at, first_day, review_days FROM evaluations ORDER BY version DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return Evaluation(int(row[0]), datetime.fromisoformat(row[1]), date.fromisoformat(row[2]), int(row[3]))


def version_on(db: sqlite3.Connection, day: date) -> int:
    """The evaluation version in force on ``day``: the latest begun by then, else ``PREVIOUS``."""
    if not _has_table(db, "evaluations"):
        return PREVIOUS
    row = db.execute(
        "SELECT max(version) FROM evaluations WHERE first_day <= ?", (day.isoformat(),)
    ).fetchone()
    return PREVIOUS if row is None or row[0] is None else int(row[0])


def _trading(day: date, sessions: Sessions) -> date:
    """``day`` if it trades, else the first trading day after it."""
    for _ in range(SEARCH_DAYS):
        if sessions(day) is not None:
            return day
        day += timedelta(days=1)
    raise ValueError(f"no trading day within {SEARCH_DAYS} days of {day}")


def _previous(day: date, sessions: Sessions) -> date:
    """The last trading day before ``day``."""
    day -= timedelta(days=1)
    for _ in range(SEARCH_DAYS):
        if sessions(day) is not None:
            return day
        day -= timedelta(days=1)
    raise ValueError(f"no trading day within {SEARCH_DAYS} days before {day}")


def cycle_of(evaluation: Evaluation, day: date, sessions: Sessions) -> Cycle:
    """The cycle of the trading day ``day`` (of the first trading day after it, when it does not
    trade; the first cycle before the evaluation's first day), counted on ``sessions`` from the
    evaluation's first day."""
    asked = _trading(day, sessions) if day >= evaluation.first_day else None
    length = evaluation.review_days
    count, cursor, number, first = 0, evaluation.first_day, 1, evaluation.first_day
    place: int | None = None
    gap = 0
    while True:
        if sessions(cursor) is not None:
            gap = 0
            count += 1
            if (count - 1) % length == 0:
                number, first = (count - 1) // length + 1, cursor
            if cursor == asked:
                place = count - (number - 1) * length
            if count % length == 0 and (asked is None or cursor >= asked):
                return Cycle(evaluation.version, number, first, cursor, place, length)
        else:
            gap += 1
            if gap > SEARCH_DAYS:
                raise ValueError(f"no trading day within {SEARCH_DAYS} days of {cursor}")
        cursor += timedelta(days=1)


def in_force(now: datetime, sessions: Sessions) -> date:
    """The trading day a moment trades for: the first whose closing auction has not ended by ``now``
    (after a day's close, the next trading day: what a review that night applies to)."""
    day = kst_day(now)
    for _ in range(SEARCH_DAYS):
        session = sessions(day)
        if session is not None and now < session.close:
            return day
        day += timedelta(days=1)
    raise ValueError(f"no trading day within {SEARCH_DAYS} days of {now}")


def cycle_at(evaluation: Evaluation, now: datetime, sessions: Sessions) -> Cycle:
    """The cycle in force at ``now`` (``in_force``): the one being traded, or after a day's close the
    next trading day's."""
    return cycle_of(evaluation, in_force(now, sessions), sessions)


def since(evaluation: Evaluation, cycle: Cycle, sessions: Sessions) -> datetime:
    """When ``cycle`` came into force: the close of the trading day before its first (the night of
    the review before it), or the evaluation's beginning for the first cycle when that is later."""
    previous = sessions(_previous(cycle.first, sessions))
    assert previous is not None
    start = previous.close
    return max(start, evaluation.begun_at) if cycle.number == 1 else start


def night_of(now: datetime, sessions: Sessions) -> date:
    """The trading day whose night ``now`` is in: the latest whose pick (``PICK_AT``) is at or before
    ``now`` (as the nightly report counts it, ``report.report_day``)."""
    day = kst_day(now)
    if now < datetime.combine(day, PICK_AT, KST):
        day -= timedelta(days=1)
    for _ in range(SEARCH_DAYS):
        if sessions(day) is not None:
            return day
        day -= timedelta(days=1)
    raise ValueError(f"no trading day within {SEARCH_DAYS} days before {now}")


def reviewing(evaluation: Evaluation, now: datetime, sessions: Sessions) -> date | None:
    """The review day whose night ``now`` is in, after its close and before the next trading day's
    pick; None on any other night, during a session, and before the evaluation's first day."""
    day = night_of(now, sessions)
    session = sessions(day)
    if session is None or now < session.close or day < evaluation.first_day:
        return None
    return day if cycle_of(evaluation, day, sessions).review == day else None


def state(db: sqlite3.Connection, now: datetime, sessions: Sessions) -> dict[str, Any] | None:
    """``GET /v1/trader``'s ``cycle``: the cycle in force at ``now`` (``cycle_at``) and its evaluation
    version; None before the evaluation was begun."""
    evaluation = current(db)
    if evaluation is None:
        return None
    return cycle_at(evaluation, now, sessions).as_json(evaluation.first_day)


def _has_table(db: sqlite3.Connection, name: str) -> bool:
    found = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone()
    return found is not None
