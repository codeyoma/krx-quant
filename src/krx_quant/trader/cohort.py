"""Challengers enter as a cohort and are judged once, on the review night (#473, spec #468 5 "도전
정책").

**The cohort** (``form_tonight``, ``choose``). At the start of a review cycle (#469, ``review.py``)
at most ``policy.CHALLENGERS`` (3) policies enter the challengers together: of the policies waiting
(kept by the nightly re-fit, ``policy.waiting_versions``), the newest first, each with values (its
cells by their predictors' names, their order, each θ) unlike the champion's and those taken before
it. The champion they are judged against is fixed then (``Cohort.champion``). A policy fitted during
a cycle waits for the next cohort; every policy waiting that a cohort does not take is dropped then
(``passed_over``, with why: ``older``, three newer were taken; ``champion_values``; ``unreadable``;
``earlier_evaluation``, kept before the evaluation version began): the next cohort is the next
cycle's fits.

**When.** At the end of the nightly job (``refit.run_refit``), when the cycle in force
(``review.cycle_at``: after a day's close, the next trading day's) has no cohort yet and its first
day's pick (08:30) is still ahead: on a review night the next cycle's, after the review's judgment and
the night's fit (so that the night's fit may enter); and on the night before an evaluation version's
first cycle begins. Never during a cycle: a cycle that began without one (its review night's job did
not run; the first cycle after the deploy, which began at the trader's start) runs no challenger, and
its fits wait for its review night. Nor before the first champion: a cohort would have nothing to be
judged against.

**Running** (``policy.running_versions``): each member runs in paper beside the champion on the
trading days of its cycle, from its first to its review day, as every shadow book does (#471: sized
by the loss budget, held by the risk rules and the simulated loss limits).

**The calendar now** (``resolved``): a cohort keeps the first and review days of its cycle as they
were counted when it was formed, but it runs, is judged and is closed by its cycle counted on the
market calendar as it is now (``review.nth``), as the review night itself is (``review.reviewing``):
a holiday announced after it was formed moves its review day, and its members run to that day and
are judged that night.

**Judged once** (``promotion.promote``): on the cycle's review night, after its close, never before:
each member against the cohort's champion on the days of the cycle both ran
(``promotion.judge_cohort``), Bonferroni over the cohort's members; the one crowned and every other
member dropped in one transaction with the verdict (``settle``). A cohort still unjudged once its
review night is over (the job did not run that night) is closed unjudged (``close_missed``,
``MISSED``) and its members dropped: its window is never stretched, nor is it judged on a later
night.

**Kept** (``cohorts``, ``cohort_members``, ``cohort_verdicts``: ``policy.SCHEMA``): each cohort as
formed (its cycle, when, its first and review days, its champion, its members newest first, the
policies it passed over and why) and its verdict as judged (when, the champion, the outcome, the
member crowned, the level, each member's common days, mean, lower bound and verdict), each written
once: triggers refuse to update or delete them.

**Stale challengers** (``retire_stale``): on a night after the market, a challenger in no cohort still
to be judged (one admitted to a running slot before #473) and a policy waiting since before the
evaluation version began (fitted under the rules before, #469) are dropped: neither can enter a
cohort.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any

from krx_quant.feeds.base import KST
from krx_quant.market_calendar import Session
from krx_quant.trader import PICK_AT, review
from krx_quant.trader import policy as policies

Sessions = Callable[[date], Session | None]

# Why a policy waiting is not taken into a cohort (``passed_over``): three newer were taken; its
# values are the champion's or a newer one's taken; it cannot be read; it was kept before the
# evaluation version began.
OLDER, CHAMPION_VALUES, ALIKE, UNREADABLE = "older", "champion_values", "alike", "unreadable"
EARLIER_EVALUATION = "earlier_evaluation"
# A verdict's outcome: a member crowned; the champion kept; the review night missed (not judged).
PROMOTED, KEPT, MISSED = "promoted", "kept", "missed"
# A member's verdict: crowned; passed but another's lower bound was greater; its lower bound at or
# below 0; fewer common days than ``promotion.PROMOTION_DAYS`` (판정 불가); its review night missed.
PASSED, NOT_ABOVE, UNDECIDED = "passed", "not_above", "undecided"
# Why the night's end formed no cohort (``Tonight.why``): no champion yet; the cycle in force began
# already without one.
NO_CHAMPION, BEGUN = "no_champion", "begun"


@dataclass(frozen=True)
class Cohort:
    """A cohort as formed: its evaluation version and review cycle, when, the cycle's first and
    review days, the champion it is judged against, its members (newest first) and the policies
    waiting it passed over, each with why."""

    evaluation: int
    cycle: int
    formed_at: datetime
    first: date
    review: date
    champion: int | None
    members: tuple[int, ...]
    passed_over: tuple[Mapping[str, Any], ...] = ()

    def as_json(self) -> dict[str, Any]:
        return {
            "evaluation": self.evaluation,
            "cycle": self.cycle,
            "formed_at": _iso(self.formed_at),
            "start": self.first.isoformat(),
            "review": self.review.isoformat(),
            "champion": self.champion,
            "members": list(self.members),
            "passed_over": [dict(p) for p in self.passed_over],
        }


@dataclass(frozen=True)
class Verdict:
    """A cohort's verdict as judged: when, the champion it was judged against, the outcome
    (``PROMOTED``, ``KEPT``, ``MISSED``), the member crowned, each lower bound's two-sided level, and
    each member's figures: common days, mean and lower bound of the daily (challenger − champion)
    net P&L in won, and its verdict."""

    evaluation: int
    cycle: int
    judged_at: datetime
    champion: int | None
    outcome: str
    promoted: int | None
    level: float | None
    members: tuple[Mapping[str, Any], ...]

    def as_json(self) -> dict[str, Any]:
        return {
            "judged_at": _iso(self.judged_at),
            "champion": self.champion,
            "outcome": self.outcome,
            "promoted": self.promoted,
            "level": self.level,
            "members": [dict(m) for m in self.members],
        }


@dataclass(frozen=True)
class Tonight:
    """What the night's end found (``form_tonight``): the cycle in force, its cohort (formed that
    night when ``formed``), the policies still waiting, newest first, and why no cohort was formed
    (``NO_CHAMPION``, ``BEGUN``; None when one is)."""

    cycle: review.Cycle
    cohort: Cohort | None
    formed: bool = False
    waiting: tuple[int, ...] = ()
    why: str | None = None


# Reading.


def of(db: sqlite3.Connection, evaluation: int, cycle: int) -> Cohort | None:
    """The cohort of ``cycle`` of ``evaluation``; None before one was formed (or a store from before
    #473)."""
    if not _has(db, "cohorts"):
        return None
    row = db.execute(
        "SELECT evaluation, cycle, formed_at, first_day, review_day, champion_id, passed_over FROM cohorts"
        " WHERE evaluation = ? AND cycle = ?",
        (evaluation, cycle),
    ).fetchone()
    return None if row is None else _cohort(db, row)


def cohorts(db: sqlite3.Connection) -> list[Cohort]:
    """Every cohort, the first formed first."""
    if not _has(db, "cohorts"):
        return []
    rows = db.execute(
        "SELECT evaluation, cycle, formed_at, first_day, review_day, champion_id, passed_over FROM cohorts"
        " ORDER BY evaluation, cycle"
    ).fetchall()
    return [_cohort(db, r) for r in rows]


def unjudged(db: sqlite3.Connection) -> list[Cohort]:
    """The cohorts without a verdict yet, the first formed first."""
    return [c for c in cohorts(db) if verdict_of(db, c.evaluation, c.cycle) is None]


def latest(db: sqlite3.Connection) -> Cohort | None:
    """The cohort formed last; None before the first."""
    found = cohorts(db)
    return found[-1] if found else None


def of_member(db: sqlite3.Connection, version: int) -> Cohort | None:
    """The cohort policy ``version`` entered; None if none."""
    if not _has(db, "cohort_members"):
        return None
    row = db.execute(
        "SELECT evaluation, cycle FROM cohort_members WHERE policy_id = ?"
        " ORDER BY evaluation DESC, cycle DESC",
        (version,),
    ).fetchone()
    return None if row is None else of(db, int(row[0]), int(row[1]))


def verdict_of(db: sqlite3.Connection, evaluation: int, cycle: int) -> Verdict | None:
    """The verdict of the cohort of ``cycle`` of ``evaluation``; None before it was judged."""
    if not _has(db, "cohort_verdicts"):
        return None
    row = db.execute(
        "SELECT evaluation, cycle, judged_at, champion_id, outcome, promoted_id, level, members"
        " FROM cohort_verdicts WHERE evaluation = ? AND cycle = ?",
        (evaluation, cycle),
    ).fetchone()
    if row is None:
        return None
    return Verdict(
        int(row[0]),
        int(row[1]),
        datetime.fromisoformat(row[2]),
        None if row[3] is None else int(row[3]),
        str(row[4]),
        None if row[5] is None else int(row[5]),
        None if row[6] is None else float(row[6]),
        tuple(json.loads(row[7])),
    )


def read_cohorts(db: sqlite3.Connection, sessions: Sessions | None = None) -> list[dict[str, Any]]:
    """``GET /v1/trader/policies``'s ``cohorts``: every cohort, the newest first, with its verdict
    (null until judged); its days counted on ``sessions`` as they are now (``resolved``), as formed
    without them."""
    out = []
    for c in reversed(cohorts(db)):
        found = verdict_of(db, c.evaluation, c.cycle)
        shown = c if sessions is None else resolved(db, c, sessions)
        out.append({**shown.as_json(), "verdict": None if found is None else found.as_json()})
    return out


def resolved(db: sqlite3.Connection, found: Cohort, sessions: Sessions) -> Cohort:
    """``found`` with its cycle's first and review days counted on ``sessions`` as they are now
    (module doc "The calendar now"); as kept without its evaluation version kept (a demo's)."""
    evaluation = review.of(db, found.evaluation)
    if evaluation is None:
        return found
    cycle = review.nth(evaluation, found.cycle, sessions)
    return replace(found, first=cycle.first, review=cycle.review)


def _cohort(db: sqlite3.Connection, row: Sequence[Any]) -> Cohort:
    evaluation, cycle = int(row[0]), int(row[1])
    members = db.execute(
        "SELECT policy_id FROM cohort_members WHERE evaluation = ? AND cycle = ? ORDER BY rank",
        (evaluation, cycle),
    )
    return Cohort(
        evaluation,
        cycle,
        datetime.fromisoformat(row[2]),
        date.fromisoformat(row[3]),
        date.fromisoformat(row[4]),
        None if row[5] is None else int(row[5]),
        tuple(int(r[0]) for r in members),
        tuple(json.loads(row[6])),
    )


# Forming.

Values = tuple[tuple[str, str, float], ...]


def choose(
    waiting: Sequence[tuple[int, Values | None]], champion: Values | None, size: int = policies.CHALLENGERS
) -> tuple[list[int], list[dict[str, Any]]]:
    """Of ``waiting`` (each version with its values, None when it cannot be read), the cohort (module
    doc "The cohort"): the newest first, at most ``size``, its values unlike ``champion``'s and those
    taken; with every other passed over and why."""
    taken: list[int] = []
    seen: set[Values] = set()
    passed: list[dict[str, Any]] = []
    for version, kept in sorted(waiting, key=lambda w: -w[0]):
        why = None
        if kept is None:
            why = UNREADABLE
        elif kept == champion:
            why = CHAMPION_VALUES
        elif kept in seen:
            why = ALIKE
        elif len(taken) >= size:
            why = OLDER
        if why is None and kept is not None:
            taken.append(version)
            seen.add(kept)
        else:
            passed.append({"version": version, "why": why})
    return taken, passed


def form(
    db: sqlite3.Connection,
    at: datetime,
    evaluation: int,
    cycle: review.Cycle,
    champion: int | None,
    members: Sequence[int],
    passed_over: Sequence[Mapping[str, Any]] = (),
) -> Cohort:
    """The cohort of ``cycle`` of ``evaluation`` kept at ``at`` (its members newest first, entering
    then: ``admitted_at``), its champion, and the policies it passed over, dropped then; unless one is
    kept for it already: the cycle's cohort, as kept."""
    with db:
        db.execute("BEGIN IMMEDIATE")
        cursor = db.execute(
            "INSERT OR IGNORE INTO cohorts (evaluation, cycle, formed_at, first_day, review_day, champion_id,"
            " passed_over) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                evaluation,
                cycle.number,
                _iso(at),
                cycle.first.isoformat(),
                cycle.review.isoformat(),
                champion,
                json.dumps([dict(p) for p in passed_over], ensure_ascii=False),
            ),
        )
        if cursor.rowcount == 1:
            for rank, version in enumerate(members, 1):
                db.execute(
                    "INSERT INTO cohort_members (evaluation, cycle, policy_id, rank) VALUES (?, ?, ?, ?)",
                    (evaluation, cycle.number, version, rank),
                )
                db.execute(
                    "UPDATE policies SET admitted_at = ? WHERE id = ? AND admitted_at IS NULL",
                    (_iso(at), version),
                )
            policies._drop(db, at, [int(p["version"]) for p in passed_over])
    found = of(db, evaluation, cycle.number)
    assert found is not None
    return found


def form_tonight(db: sqlite3.Connection, now: datetime, sessions: Sessions) -> Tonight:
    """The night's end (module doc "When"): the cycle in force's cohort formed from the policies
    waiting if it has none and has not begun; what it found either way. The evaluation version is
    begun here if it was not yet (``review.begin``: a store the night's fit made, before the trader
    first started, as the promotion begins it)."""
    evaluation = review.begin(db, now, sessions)
    cycle = review.cycle_at(evaluation, now, sessions)
    kept = of(db, evaluation.version, cycle.number)
    if kept is not None:
        return Tonight(cycle, resolved(db, kept, sessions), waiting=tuple(policies.waiting_versions(db)))
    if now >= datetime.combine(cycle.first, PICK_AT, KST):
        return Tonight(cycle, None, waiting=tuple(policies.waiting_versions(db)), why=BEGUN)
    champion = policies.champion_version(db)
    if champion is None:
        return Tonight(cycle, None, waiting=tuple(policies.waiting_versions(db)), why=NO_CHAMPION)
    earlier: list[dict[str, Any]] = []
    waiting: list[tuple[int, Values | None]] = []
    created = _created(db)
    for version in policies.waiting_versions(db):
        if created[version] < evaluation.begun_at:
            earlier.append({"version": version, "why": EARLIER_EVALUATION})
        else:
            waiting.append((version, _values(db, version)))
    members, passed = choose(waiting, _values(db, champion))
    found = form(db, now, evaluation.version, cycle, champion, members, [*passed, *earlier])
    return Tonight(cycle, found, formed=True, waiting=tuple(policies.waiting_versions(db)))


def _values(db: sqlite3.Connection, version: int) -> Values | None:
    try:
        return policies.values(policies.read(db, version).cells)
    except policies.PolicyError:
        return None


def _created(db: sqlite3.Connection) -> dict[int, datetime]:
    return {
        int(r[0]): datetime.fromisoformat(r[1]) for r in db.execute("SELECT id, created_at FROM policies")
    }


# Judging.


def settle(
    db: sqlite3.Connection,
    at: datetime,
    found: Cohort,
    members: Sequence[Mapping[str, Any]],
    level: float | None,
    promoted: int | None,
    reason: str,
    detail: Mapping[str, Any],
) -> Verdict:
    """``found``'s verdict kept at ``at`` (module doc "Judged once"), in one transaction: its members'
    figures (``members``, each with its verdict), the member ``promoted`` crowned (``reason``,
    ``detail``) while the cohort's champion is still the champion (else none is), and every other
    member dropped; unless it was judged already: its verdict, as kept."""
    with db:
        db.execute("BEGIN IMMEDIATE")
        kept = verdict_of(db, found.evaluation, found.cycle)
        if kept is not None:
            return kept
        if promoted is not None and policies.champion_version(db) != found.champion:
            promoted = None  # another writer changed the champion meanwhile: the comparison is void
        if promoted is not None:
            policies._crown(db, at, promoted, reason, detail)
        figures = [{**m, "verdict": PROMOTED if m["version"] == promoted else m["verdict"]} for m in members]
        _keep(db, at, found, PROMOTED if promoted is not None else KEPT, promoted, level, figures)
        policies._drop(db, at, [v for v in found.members if v != promoted])
    judged = verdict_of(db, found.evaluation, found.cycle)
    assert judged is not None
    return judged


def close_missed(db: sqlite3.Connection, now: datetime, sessions: Sessions) -> list[Verdict]:
    """Each cohort still unjudged once its review night is over (module doc "Judged once"): kept
    ``MISSED`` at ``now``, its members dropped."""
    night = review.night_of(now, sessions)
    out = []
    for found in unjudged(db):
        if night <= resolved(db, found, sessions).review:  # its review day on the calendar now
            continue
        with db:
            db.execute("BEGIN IMMEDIATE")
            if verdict_of(db, found.evaluation, found.cycle) is not None:
                continue
            figures = [
                {"version": v, "days": None, "mean": None, "low": None, "verdict": MISSED}
                for v in found.members
            ]
            _keep(db, now, found, MISSED, None, None, figures)
            policies._drop(db, now, found.members)
        judged = verdict_of(db, found.evaluation, found.cycle)
        assert judged is not None
        out.append(judged)
    return out


def _keep(
    db: sqlite3.Connection,
    at: datetime,
    found: Cohort,
    outcome: str,
    promoted: int | None,
    level: float | None,
    members: Sequence[Mapping[str, Any]],
) -> None:
    db.execute(
        "INSERT INTO cohort_verdicts (evaluation, cycle, judged_at, champion_id, outcome, promoted_id, level,"
        " members) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            found.evaluation,
            found.cycle,
            _iso(at),
            found.champion,
            outcome,
            promoted,
            level,
            json.dumps([dict(m) for m in members], ensure_ascii=False),
        ),
    )


def retire_stale(db: sqlite3.Connection, at: datetime, evaluation: review.Evaluation) -> tuple[int, ...]:
    """The challengers in no cohort still to be judged and the policies waiting since before
    ``evaluation`` began (module doc "Stale challengers"), dropped at ``at``: their versions."""
    members = {v for c in unjudged(db) for v in c.members}
    stale = [v for v in policies.challenger_versions(db) if v not in members]
    created = _created(db)
    stale += [v for v in policies.waiting_versions(db) if created[v] < evaluation.begun_at]
    if stale:
        policies.drop(db, at, stale)
    return tuple(sorted(stale))


def _has(db: sqlite3.Connection, name: str) -> bool:
    found = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone()
    return found is not None


def _iso(at: datetime) -> str:
    return at.astimezone(KST).isoformat()
