"""The challengers against the champion, the promotion and the live gate (#400, #393 "정책과 자기
개선", "실전 관문").

**Records.** A policy's paper record is every trading day it ran in the trader (``policy_days``,
which the trader writes while the day's session is open: its champion's book and each running
challenger's, ``desk.py``), each with the net P&L of the policy's trades entered that day, 0 on a
day it made none. A trade ends on the day it is entered (by 15:15; later only when the trader was
down at its time barrier), so a day's trades are all of that day. A policy's days as a challenger
and as the champion are one record: a challenger promoted brings its own. Always paper's
(``records_of``, #405), whatever the trader's mode: every policy, the champion and the challengers
alike, on one fill model, so that a switch from paper to mock to live never starts the history
again. In paper mode the champion's record is its book's, whose trades the limits hold (#396); in
mock mode it is its paper shadow's (``desk.Desk.shadow``, held by no pause, as a challenger's book
is), never its KIS fills; a challenger's trades are held by no pause, their loss limits simulated
(#471, ``limits.Simulated``), as the shadow's are. The
records are simulated evidence: what a policy would have made filled in paper, not what an
execution makes (rejects, partial fills, the account's reconciliation, the fill gap between KIS
and the shadow, ``gap.py``), which the live mode must check apart (#408).

**Promotion** (``judge_cohort``, ``promote``, #473: spec #468 5 "도전 정책"): the challengers enter as a
cohort of at most three at the start of a review cycle (#469, ``cohort.py``), the champion they are
judged against fixed then, and run in paper for the whole cycle. They are judged once, on the cycle's
review night (``review.reviewing``: after the close of its twentieth trading day, until the next
trading day's pick), never before. A member wins when, over the cycle's trading days both it and the
cohort's champion ran (``common``), at least ``PROMOTION_DAYS`` (10) of them, the day-level lower
bound of (member − champion) net P&L is above 0. A day either made no trade counts (0), and so does a
day a loss limit stopped either (the challengers' simulated, #471; in paper mode the champion's own:
the same rules); a day the champion was paused is not a common day (``PolicyDay.stopped``: a pause,
the screen's or a check's, or a failed order cut its trades short, not its policy). The lower bound
is the engine's trading-day statistic (``stats.overlap_t_test`` with an overlap of one session: a
Student t over the days), two-sided at ``LEVEL`` with Bonferroni over the cohort's members: 1 − 0.05
÷ m. Fewer common days: undecided (``FEW_DAYS``, 판정 불가); the window is not stretched. Of several
passing, the greatest lower bound is crowned (then the newest), from the next trading day; every other
member is dropped (``policy.drop``), with the verdict and its figures kept for good
(``cohort.settle``). A cohort whose review night passed unjudged (the job did not run) is closed
unjudged and dropped (``cohort.close_missed``): the champion stays until the next review. The
promotion is judged and written only after the market (``in_market``: from the day's pick at 08:30
to the end of the closing auction it waits), so the champion never changes during a session: the
trader reads it at each pick (``desk.Desk.select``), and a change written before 08:30 holds the
whole day. The records, the promotion and the live gate read the current evaluation version's days
alone (``since``, #469, from its first day): an earlier version's evidence stays in the store,
archived, and is not inherited.

> **바뀜** (2026-11-02, #476): a challenger was judged every night from its tenth common day → on a
> review night alone, and on the current evaluation version's days alone. 이유: spec #468 "버전과
> 검토 주기": the version that trades is frozen between reviews.

> **바뀜** (2026-11-03, #481): each challenger in a running slot was judged on every review night
> from its tenth common day, Bonferroni over the challengers running, and dropped once it reached
> 20 common days unpromoted; a day the champion was stopped by a loss limit was not a common day →
> a cohort formed at a cycle's start is judged once, on that cycle's review night, Bonferroni within
> it, every member not crowned dropped; a day a loss limit stopped the champion counts. 이유: spec
> #468 5 "도전 정책": no repeated looks during a cycle, and the shadows' simulated halts count as
> trading days.

**The live gate** (#408, ``check_gate``, ``gate_of``): judged at checkpoints, not every night. The
trader's paper history is every trading day a champion ran in it (``history``: ``policy_days`` with
``champion`` 1, whichever policy), and a checkpoint falls on each ``CHECKPOINT_DAYS``-th (20) of
them: day 20, 40, 60 and so on. At checkpoint k the night's job (``promote``, after the market)
takes the champion of that day (``champion_on``) and its paper record up to that day on the days it
ran as the champion alone (``PolicyDay.champion``), at least ``GATE_DAYS`` (20) of them, and tests the
mean of the daily net P&L (no-trade days 0) one-sided at
α_k = ``GATE_ALPHA`` × 6 ÷ (π² k²) (``alpha``): the day-level lower bound two-sided at 1 − 2α_k
(``checkpoint_level``, the engine's ``stats`` statistic with an overlap of one, as before) above 0.
Over every checkpoint the α spent is at most ``GATE_ALPHA`` (Σ 6 ÷ (π² k²) = 1), so a champion
without an edge passes by chance at most 2.5% of the time over the trader's whole life (#408's
simulation, ``tests/test_trader_promotion.py``). k counts the trader's checkpoints, never a
champion's: a new champion gets no fresh α, nor a new evaluation version (#469): its history starts
again from its first day, but its checkpoints number on after the earlier versions' (``base``), so
the α spent over the trader's whole life stays at most ``GATE_ALPHA``. Each checkpoint is evaluated
once and kept (``gate_checks``), so that a trade that ends later never changes what it found; a
night that missed one evaluates it on the record up to its day. A champion that passed stays passed
(``Gate.passed``); a new one must pass at a later checkpoint, on a day it was the champion. Its
days as a challenger never count (``champion_days``): ``judge`` promoted it on them, so their record
is the one that won a comparison and is biased upward. A promoted champion's record for the gate
starts on the first trading day after it took over; the first champion's, fitted without a paper
record, on its first day. Each checkpoint then tests days its champion was not picked on, so the α
spent stays at most ``GATE_ALPHA`` across every change of the champion (#408's simulation of the
promotion and the checkpoints together, ``tests/test_trader_promotion.py``). The same checkpoint is
also judged with ``STRESS_TICKS`` (1) extra adverse tick on each leg of each trade (``stressed_low``: the
daily nets less 2 × the trades' tick × shares, the costs already in them not charged again), one of
the live mode's execution checks (``live.py``).

> **바뀜** (2026-10-31, #425): the gate was looked at every night: the champion's whole record (its
> days as a challenger too) of 20 days at least, its 95% lower bound above 0 → checkpoints with α
> spent as 1/k², on its days as the champion only. 이유: with no edge, a gate looked at nightly passed
> by chance 12% of the time by day 120 and 15% by day 250 (#408's simulation, t4 daily P&L); and a
> promoted champion's challenger days are the ones it won its promotion on: counted, zero-edge
> challengers promoted and gated together passed the checkpoints 3.2% of the time over 250 days. On
> the champion's own days the checkpoints keep it at most 2.5%.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any

from krx_quant.feeds.base import KST, kst_day
from krx_quant.market_calendar import Session
from krx_quant.stats import overlap_t_test
from krx_quant.trader import PAPER, PICK_AT, review
from krx_quant.trader import cohort as cohorts
from krx_quant.trader import policy as policies

log = logging.getLogger(__name__)

# A cohort's member is judged on this many common trading days at least (#393); fewer at its review:
# undecided (#473, 판정 불가), dropped.
PROMOTION_DAYS = 10
# The live gate (#393, #408): at a checkpoint, the champion's paper record of this many trading days
# at least.
GATE_DAYS = 20
# A checkpoint falls on every this-many-th trading day of the trader's paper history (#408).
CHECKPOINT_DAYS = 20
# The one-sided error the gate may spend over the trader's whole life (#408): α_k = this × 6 ÷ (π² k²).
GATE_ALPHA = 0.025
# The extra adverse ticks a leg of the stressed gate (#408, an execution check of the live mode).
STRESS_TICKS = 1
# The lower bounds' level, two-sided (the engine's "95% lower bound"); a promotion's is divided
# by the challengers (Bonferroni).
LEVEL = 0.95
# The sessions a day's trades span: one (a trade ends on the day it is entered), so the days are
# taken as independent (``stats.overlap_t_test`` with H = 1 is ``stats.t_test``).
OVERLAP = 1
# Why a challenger is not promoted.
FEW_DAYS, NOT_ABOVE = "few_days", "not_above"
# Why no promotion was judged (#469: not a review night; this review night's judged already; #473:
# no cohort ran in the cycle reviewed).
MARKET_OPEN, NO_CHAMPION = "market_open", "no_champion"
BETWEEN_REVIEWS, REVIEWED, NO_COHORT = "between_reviews", "reviewed", "no_cohort"


@dataclass(frozen=True)
class PolicyDay:
    """A trading day a policy ran: its trades' net P&L (0 without one), how many, whether it was
    the champion then and, if so, stopped at some time in the session (since #473 by a pause or a
    failed order alone, ``desk.Desk.record``: a loss limit is its policy's rule); and its trades'
    tick times their shares, summed (``ticks``, won: what one tick on every leg's every share costs
    a leg, #408)."""

    day: date
    net: float
    trades: int
    champion: bool
    stopped: bool
    ticks: float = 0.0

    def stressed(self, extra: int = STRESS_TICKS) -> float:
        """The day's net P&L with ``extra`` adverse ticks on each leg of each trade (#408): a buy that
        many ticks dearer, a sale that many cheaper. The costs in ``net`` are not charged again."""
        return self.net - 2 * extra * self.ticks


@dataclass(frozen=True)
class Record:
    """A policy's paper record, oldest day first."""

    version: int
    days: tuple[PolicyDay, ...]

    @property
    def trades(self) -> int:
        return sum(d.trades for d in self.days)

    @property
    def net(self) -> float:
        return sum(d.net for d in self.days)


def records_of(mode: str | None) -> str:
    """The mode whose records ``mode``'s promotion, live gate and report read: paper's for every mode
    (#405), mock and live included: one fill model for every policy, one history across the modes."""
    return PAPER


def mode_at(db: sqlite3.Connection, at: datetime | None = None) -> str:
    """The mode the trader last started in, at or before ``at`` (all its starts if None): the mode
    whose trades and ledger the report shows (#405), not the records it reads (``records_of``).
    ``paper`` before the first start."""
    if not _has_table(db, "runs"):
        return PAPER
    where, params = "", []
    if at is not None:
        where, params = "WHERE at <= ?", [at.astimezone(KST).isoformat()]
    row = db.execute(f"SELECT mode FROM runs {where} ORDER BY at DESC, id DESC LIMIT 1", params).fetchone()
    return PAPER if row is None else str(row[0])


def records(
    db: sqlite3.Connection, mode: str | None = None, versions: Iterable[int] | None = None
) -> dict[int, Record]:
    """The records of ``versions`` (all if None), paper's whatever ``mode`` (``records_of``), read
    from the trader's store on the current evaluation version's days (``since``, #469); none from a
    store the trader has not kept them in yet."""
    if not _has_table(db, "policy_days"):
        return {}
    mode = records_of(mode)
    wanted = None if versions is None else set(versions)
    ran: dict[int, list[tuple[date, bool, bool]]] = {}
    first = since(db)
    for version, day, champion, stopped in db.execute(
        "SELECT policy_id, day, champion, stopped FROM policy_days WHERE mode = ? AND day >= ?"
        " ORDER BY policy_id, day",
        (mode, "" if first is None else first.isoformat()),
    ):
        if wanted is None or version in wanted:
            ran.setdefault(version, []).append((date.fromisoformat(day), bool(champion), bool(stopped)))
    made: dict[tuple[int, date], tuple[float, int, float]] = {}
    if ran:
        for version, day, net, count, ticks in db.execute(
            "SELECT policy_id, substr(entry_at, 1, 10), coalesce(sum(net), 0), count(*),"
            " coalesce(sum(tick * quantity), 0) FROM trades"
            " WHERE mode = ? AND policy_id IS NOT NULL AND exit_at IS NOT NULL"
            " GROUP BY policy_id, substr(entry_at, 1, 10)",
            (mode,),
        ):
            if version in ran:
                made[(version, date.fromisoformat(day))] = (float(net), int(count), float(ticks))
    out = {}
    for version, days in ran.items():
        kept = []
        for day, champion, stopped in days:
            net, count, ticks = made.get((version, day), (0.0, 0, 0.0))
            kept.append(PolicyDay(day, net, count, champion, stopped, ticks))
        out[version] = Record(version, tuple(kept))
    return out


def since(db: sqlite3.Connection) -> date | None:
    """The first day of the current evaluation version (#469, ``review.current``): the records,
    the promotion and the live gate read its days alone. None before it was begun: every day."""
    evaluation = review.current(db)
    return None if evaluation is None else evaluation.first_day


def lower_bound(values: Sequence[float], level: float = LEVEL) -> float | None:
    """The day-level lower bound of the mean of ``values`` (one a trading day), two-sided at
    ``level`` (``stats.overlap_t_test``, an overlap of one session); None from fewer than two."""
    found = overlap_t_test(values, list(range(len(values))), OVERLAP, level)
    return None if found is None else found.low


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def paper(record: Record | None) -> dict[str, Any]:
    """A policy's paper record as the Engine API gives it: its first and last day, the days, the
    trades, the net P&L in won, its mean a day and that mean's lower bound (``LEVEL``)."""
    days = () if record is None else record.days
    nets = [d.net for d in days]
    return {
        "since": days[0].day.isoformat() if days else None,
        "last": days[-1].day.isoformat() if days else None,
        "days": len(days),
        "trades": sum(d.trades for d in days),
        "net": sum(nets),
        "mean": _mean(nets),
        "low": lower_bound(nets),
    }


def alpha(k: int) -> float:
    """The one-sided α the live gate spends at its ``k``-th checkpoint (#408): ``GATE_ALPHA`` × 6 ÷
    (π² k²), summing to ``GATE_ALPHA`` over every checkpoint."""
    return GATE_ALPHA * 6 / (math.pi**2 * k**2)


def checkpoint_level(k: int) -> float:
    """The two-sided level of the lower bound at checkpoint ``k``: 1 − 2α_k (its one side at α_k)."""
    return 1 - 2 * alpha(k)


def passes(values: Sequence[float], k: int) -> bool:
    """Whether a record of daily net P&L ``values`` (no-trade days 0) passes the gate at checkpoint
    ``k``: ``GATE_DAYS`` days at least, its lower bound at ``checkpoint_level(k)`` above 0."""
    low = lower_bound(values, checkpoint_level(k))
    return len(values) >= GATE_DAYS and low is not None and low > 0


def history(db: sqlite3.Connection, until: date | None = None) -> list[date]:
    """The trader's paper history (#408): each trading day a champion ran in it (``policy_days``,
    paper, ``champion`` 1) from the current evaluation version's first day (``since``, #469), oldest
    first, to ``until`` if given. The checkpoints count these."""
    if not _has_table(db, "policy_days"):
        return []
    first = since(db)
    rows = db.execute(
        "SELECT DISTINCT day FROM policy_days WHERE mode = ? AND champion = 1 AND day >= ? ORDER BY day",
        (PAPER, "" if first is None else first.isoformat()),
    )
    days = [date.fromisoformat(r[0]) for r in rows]
    return [d for d in days if until is None or d <= until]


def champion_on(db: sqlite3.Connection, day: date) -> int | None:
    """The champion that ran on ``day`` (its ``policy_days`` row as the champion); None if none."""
    if not _has_table(db, "policy_days"):
        return None
    row = db.execute(
        "SELECT policy_id FROM policy_days WHERE mode = ? AND champion = 1 AND day = ?"
        " ORDER BY updated_at DESC, policy_id DESC LIMIT 1",
        (PAPER, day.isoformat()),
    ).fetchone()
    return None if row is None else int(row[0])


@dataclass(frozen=True)
class Checkpoint:
    """The live gate at checkpoint ``k`` (#408): on ``day``, the trader's (``k`` − ``base``) × 20th
    paper day of its evaluation version, the champion then (``policy``) and its paper days as the
    champion up to it (``days``, ``mean``), its lower bound at ``level`` and the same with
    ``STRESS_TICKS`` extra adverse ticks a leg (``stressed_low``); when it was evaluated (``at``).
    ``base``: the checkpoints of the evaluation versions before its own (#469), which it numbers on
    from."""

    k: int
    day: date
    policy: int | None
    days: int
    mean: float | None
    low: float | None
    stressed_low: float | None
    at: datetime | None = None
    base: int = 0

    @property
    def history_day(self) -> int:
        """Its day of the trader's paper history in its evaluation version: (k − base) × 20."""
        return (self.k - self.base) * CHECKPOINT_DAYS

    @property
    def alpha(self) -> float:
        return alpha(self.k)

    @property
    def level(self) -> float:
        return checkpoint_level(self.k)

    @property
    def passed(self) -> bool:
        return self.policy is not None and self.days >= GATE_DAYS and self.low is not None and self.low > 0

    @property
    def stressed_passed(self) -> bool:
        return self.passed and self.stressed_low is not None and self.stressed_low > 0

    def as_json(self) -> dict[str, Any]:
        return {
            "k": self.k,
            "day": self.day.isoformat(),
            "policy": self.policy,
            "days": self.days,
            "mean": self.mean,
            "low": self.low,
            "stressed_low": self.stressed_low,
            "alpha": self.alpha,
            "level": self.level,
            "passed": self.passed,
            "stressed_passed": self.stressed_passed,
            "at": None if self.at is None else self.at.astimezone(KST).isoformat(),
        }


def champion_days(record: Record | None, until: date | None = None) -> list[PolicyDay]:
    """The days of ``record`` the gate tests (#408, module doc): the ones it ran as the champion, to
    ``until`` if given; never its days as a challenger, which its promotion was judged on."""
    found = () if record is None else record.days
    return [d for d in found if d.champion and (until is None or d.day <= until)]


def evaluate(db: sqlite3.Connection, k: int, day: date) -> Checkpoint:
    """Checkpoint ``k`` on ``day`` (module doc): the champion of that day and its days as the champion
    up to it."""
    version = champion_on(db, day)
    if version is None:
        return Checkpoint(k, day, None, 0, None, None, None)
    days = champion_days(records(db, PAPER, [version]).get(version), day)
    nets = [d.net for d in days]
    level = checkpoint_level(k)
    return Checkpoint(
        k,
        day,
        version,
        len(nets),
        _mean(nets),
        lower_bound(nets, level),
        lower_bound([d.stressed() for d in days], level),
    )


def checkpoints(db: sqlite3.Connection) -> list[Checkpoint]:
    """The checkpoints evaluated and kept (``gate_checks``), the first first, of every evaluation
    version (``current_checkpoints`` keeps the current one's): the current version's with their
    ``base`` (#469)."""
    if not _has_table(db, "gate_checks"):
        return []
    found = [
        Checkpoint(
            int(r[0]),
            date.fromisoformat(r[1]),
            None if r[2] is None else int(r[2]),
            int(r[3]),
            r[4],
            r[5],
            r[6],
            datetime.fromisoformat(r[7]),
        )
        for r in db.execute(
            "SELECT k, day, policy_id, days, mean, low, stressed_low, at FROM gate_checks ORDER BY k"
        )
    ]
    first = since(db)
    if first is None:
        return found
    base = _base(found, first)
    return [replace(c, base=base) if c.day >= first else c for c in found]


def _base(points: Sequence[Checkpoint], first: date) -> int:
    """The highest number a checkpoint of an evaluation version before the one from ``first`` took
    (0 if none): the current version's number on from it (#469)."""
    return max((c.k for c in points if c.day < first), default=0)


def current_checkpoints(
    db: sqlite3.Connection, points: Sequence[Checkpoint] | None = None
) -> tuple[list[Checkpoint], int]:
    """The checkpoints of the current evaluation version (#469: on its first day or after, ``since``)
    among ``points`` (all kept if None), and their ``base``."""
    kept = checkpoints(db)
    found = kept if points is None else list(points)
    first = since(db)
    if first is None:
        return found, 0
    return [c for c in found if c.day >= first], _base(kept, first)


def check_gate(db: sqlite3.Connection, now: datetime) -> list[Checkpoint]:
    """The checkpoints due by the trader's paper history and not evaluated yet, evaluated at ``now``
    and kept, in order (a night that missed one evaluates it on the record up to its day). The
    night's job alone writes them, after the market (``promote``). Since #469 the current
    evaluation version's, numbered on from the earlier versions' (``current_checkpoints``)."""
    days = history(db)
    points, base = current_checkpoints(db)
    done = {c.k for c in points}
    out = []
    for j in range(1, len(days) // CHECKPOINT_DAYS + 1):
        k = base + j
        if k in done:
            continue
        point = replace(evaluate(db, k, days[j * CHECKPOINT_DAYS - 1]), at=now, base=base)
        with db:
            db.execute(
                "INSERT OR IGNORE INTO gate_checks (k, day, policy_id, days, mean, low, stressed_low, alpha,"
                " level, passed, stressed_passed, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    point.k,
                    point.day.isoformat(),
                    point.policy,
                    point.days,
                    point.mean,
                    point.low,
                    point.stressed_low,
                    point.alpha,
                    point.level,
                    int(point.passed),
                    int(point.stressed_passed),
                    now.astimezone(KST).isoformat(),
                ),
            )
        out.append(point)
    return out


@dataclass(frozen=True)
class Gate:
    """The live gate (#393 "실전 관문", #408) on the champion ``policy``: its paper days as the
    champion now (``days``, ``mean``, and the lower bound ``low`` at ``level``: the next
    checkpoint's, or the one it passed at), the trader's paper history (``history`` days), the next
    checkpoint's number (``next``), the checkpoint it passed at (``passed_at``) and the first it
    passed with the extra ticks (``stressed``), and the latest checkpoint evaluated (``last``)."""

    policy: int | None
    days: int
    mean: float | None
    low: float | None
    level: float
    history: int
    next: int
    passed_at: Checkpoint | None = None
    stressed: Checkpoint | None = None
    last: Checkpoint | None = None
    required: int = GATE_DAYS
    base: int = 0  # the checkpoints of the evaluation versions before the current one (#469)

    @property
    def passed(self) -> bool:
        return self.passed_at is not None

    @property
    def next_day(self) -> int:
        """The next checkpoint's day of the trader's paper history (in the current evaluation
        version, #469)."""
        return (self.next - self.base) * CHECKPOINT_DAYS

    def as_json(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "days": self.days,
            "required": self.required,
            "mean": self.mean,
            "low": self.low,
            "level": self.level,
            "passed": self.passed,
            "history": self.history,
            "checkpoint": {
                "k": self.next,
                "day": self.next_day,
                "every": CHECKPOINT_DAYS,
                "alpha": alpha(self.next),
                "level": checkpoint_level(self.next),
            },
            "passed_at": None if self.passed_at is None else self.passed_at.as_json(),
            "stressed": self.stressed is not None,
            "last": None if self.last is None else self.last.as_json(),
        }


def no_gate() -> Gate:
    """The gate before the first champion: no record, the first checkpoint next."""
    return Gate(None, 0, None, None, checkpoint_level(1), 0, 1)


def gate_of(
    db: sqlite3.Connection,
    version: int | None,
    until: date | None = None,
    checked_by: datetime | None = None,
) -> Gate:
    """The live gate on the champion ``version`` from the store ``db``: its days as the champion and
    the trader's history up to ``until``, the checkpoints evaluated by ``checked_by`` (all if None);
    the current evaluation version's alone (#469: no checkpoint passed under an earlier one counts)."""
    by = [c for c in checkpoints(db) if checked_by is None or (c.at is not None and c.at <= checked_by)]
    points, base = current_checkpoints(db, by)
    days = len(history(db, until))
    if version is None:
        last = points[-1] if points else None
        return replace(no_gate(), history=days, next=base + len(points) + 1, last=last, base=base)
    nets = [d.net for d in champion_days(records(db, PAPER, [version]).get(version), until)]
    passed = next((c for c in points if c.policy == version and c.passed), None)
    stressed = next((c for c in points if c.policy == version and c.stressed_passed), None)
    nxt = base + len(points) + 1
    level = passed.level if passed is not None else checkpoint_level(nxt)
    return Gate(
        version,
        len(nets),
        _mean(nets),
        lower_bound(nets, level),
        level,
        days,
        nxt,
        passed,
        stressed,
        points[-1] if points else None,
        base=base,
    )


@dataclass(frozen=True)
class Judgment:
    """A challenger against the champion: their common days, the mean a day of (challenger −
    champion) net P&L and its lower bound at ``level`` (Bonferroni over ``challengers``, its cohort's
    members); ``review``: the review day its cohort is judged after (#473)."""

    challenger: int
    champion: int
    days: int
    mean: float | None
    low: float | None
    level: float
    challengers: int
    required: int = PROMOTION_DAYS
    until: int = review.REVIEW_DAYS  # a cycle's trading days: judged after its last (#473)
    review: date | None = None

    @property
    def why(self) -> str | None:
        """Why it is not promoted (``FEW_DAYS``, ``NOT_ABOVE``); None when it passes."""
        if self.days < self.required:
            return FEW_DAYS
        if self.low is None or self.low <= 0:
            return NOT_ABOVE
        return None

    @property
    def passed(self) -> bool:
        return self.why is None

    @property
    def verdict(self) -> str:
        """Its verdict at its cohort's review (``cohort.PASSED``, ``NOT_ABOVE``, ``UNDECIDED``)."""
        return {None: cohorts.PASSED, NOT_ABOVE: cohorts.NOT_ABOVE, FEW_DAYS: cohorts.UNDECIDED}[self.why]

    def as_json(self) -> dict[str, Any]:
        return {
            "champion": self.champion,
            "days": self.days,
            "required": self.required,
            "until": self.until,
            "review": None if self.review is None else self.review.isoformat(),
            "mean": self.mean,
            "low": self.low,
            "level": self.level,
            "challengers": self.challengers,
            "passed": self.passed,
            "why": self.why,
        }

    def figures(self) -> dict[str, Any]:
        """As its cohort's verdict keeps it (#473, ``cohort.settle``)."""
        return {
            "version": self.challenger,
            "days": self.days,
            "mean": self.mean,
            "low": self.low,
            "verdict": self.verdict,
        }


def common(
    champion: Record | None, challenger: Record | None, window: tuple[date, date] | None = None
) -> list[tuple[date, float]]:
    """The trading days both ran (within ``window``, both days included, if given), oldest first, but
    those the champion was stopped on (a pause, ``PolicyDay.stopped``), with each day's (challenger −
    champion) net P&L."""
    if champion is None or challenger is None:
        return []
    theirs = {d.day: d for d in champion.days}
    out = []
    for mine in challenger.days:
        if window is not None and not window[0] <= mine.day <= window[1]:
            continue
        other = theirs.get(mine.day)
        if other is not None and not (other.stopped or mine.stopped):
            out.append((mine.day, mine.net - other.net))
    return out


def bonferroni(challengers: int) -> float:
    """The two-sided level of each challenger's lower bound when ``challengers`` are judged together
    (#473: a cohort's members)."""
    return 1 - (1 - LEVEL) / max(1, challengers)


def judge(
    champion: int,
    records_: Mapping[int, Record],
    challengers: Sequence[int],
    window: tuple[date, date] | None = None,
) -> list[Judgment]:
    """Each of ``challengers`` against the ``champion`` (module doc), on their common days within
    ``window``, Bonferroni over them."""
    level = bonferroni(len(challengers))
    out = []
    for version in challengers:
        diffs = [d for _, d in common(records_.get(champion), records_.get(version), window)]
        out.append(
            Judgment(
                version,
                champion,
                len(diffs),
                _mean(diffs),
                lower_bound(diffs, level),
                level,
                len(challengers),
            )
        )
    return out


def judge_cohort(found: cohorts.Cohort, records_: Mapping[int, Record]) -> list[Judgment]:
    """Each member of ``found`` (oldest first) against the champion it was formed with, on the days of
    its cycle (``found.first`` to ``found.review``) both ran, Bonferroni over its members (#473); none
    without a champion."""
    if found.champion is None:
        return []
    window = (found.first, found.review)
    return [
        replace(j, review=found.review)
        for j in judge(found.champion, records_, sorted(found.members), window)
    ]


def pick(judgments: Iterable[Judgment]) -> Judgment | None:
    """The one of ``judgments`` promoted (module doc): of those passing, the greatest lower bound,
    then the newest; None when none passes."""
    passing = [j for j in judgments if j.passed]
    return max(passing, key=lambda j: (j.low or 0.0, j.challenger)) if passing else None


def in_market(now: datetime, sessions: Callable[[date], Session | None]) -> bool:
    """Whether ``now`` is in a trading day's market: from its pick (``PICK_AT``, when the trader
    reads the day's champion) to the end of its closing auction. No promotion is written then."""
    day = kst_day(now)
    session = sessions(day)
    return session is not None and datetime.combine(day, PICK_AT, KST) <= now < session.close


@dataclass(frozen=True)
class Promotion:
    """A night's judgment: the champion before, each member's of the cohort judged, the one promoted
    if any, why none was judged (``MARKET_OPEN``, ``NO_CHAMPION``; #469: ``BETWEEN_REVIEWS``,
    ``REVIEWED``; #473: ``NO_COHORT``), the members dropped, the live gate on the champion after, the
    gate's checkpoints evaluated that night (#408), the review day (#469): the night's on a review
    night, else the next one; and (#473) the cohort judged and its verdict, the cohorts closed unjudged
    that night (their review night missed) and the stale challengers dropped
    (``cohort.retire_stale``)."""

    at: datetime
    champion: int | None
    judgments: tuple[Judgment, ...]
    promoted: int | None
    deferred: str | None
    gate: Gate
    dropped: tuple[int, ...] = ()
    checked: tuple[Checkpoint, ...] = ()
    review: date | None = None
    cohort: cohorts.Cohort | None = None
    verdict: cohorts.Verdict | None = None
    missed: tuple[cohorts.Verdict, ...] = ()
    retired: tuple[int, ...] = ()


def promote(
    db: sqlite3.Connection,
    now: datetime,
    sessions: Callable[[date], Session | None],
    mode: str | None = None,
) -> Promotion:
    """Judge, after the market, on the records in the store ``db`` (``policy.connect``), the cohort
    whose review night ``now`` is in (#473, module doc): the member that passes made the champion and
    every other dropped, with the verdict kept (``cohort.settle``). During a trading day's market
    nothing is judged or written. The evaluation version is begun here if it was not yet
    (``review.begin``, #469), the live gate's checkpoints due evaluated, the stale challengers dropped
    (``cohort.retire_stale``) and a cohort whose review night was missed closed
    (``cohort.close_missed``); on any night but a review night nothing is judged, crowned or dropped
    (``BETWEEN_REVIEWS``), nor twice on one (``REVIEWED``). The records are paper's whatever ``mode``
    (``records_of``, #405)."""
    mode = records_of(mode)
    champion = policies.champion_version(db)
    if in_market(now, sessions):
        return Promotion(now, champion, (), None, MARKET_OPEN, gate_of(db, champion))
    evaluation = review.begin(db, now, sessions)
    # The live gate's checkpoints due (#408): on the champion of each checkpoint's day, whatever
    # tonight's judgment makes the champion from tomorrow.
    checked = tuple(check_gate(db, now))
    retired = cohorts.retire_stale(db, now, evaluation)
    missed = tuple(cohorts.close_missed(db, now, sessions))
    found: dict[str, Any] = {"checked": checked, "missed": missed, "retired": retired}
    if champion is None:
        return Promotion(now, None, (), None, NO_CHAMPION, gate_of(db, None), **found)
    day = review.reviewing(evaluation, now, sessions)
    if day is None:
        upcoming = review.cycle_at(evaluation, now, sessions).review
        gate = gate_of(db, champion)
        return Promotion(now, champion, (), None, BETWEEN_REVIEWS, gate, review=upcoming, **found)
    judged = cohorts.of(db, evaluation.version, review.cycle_of(evaluation, day, sessions).number)
    if judged is not None:  # its cycle's days on the calendar now, as ``reviewing`` counts them
        judged = cohorts.resolved(db, judged, sessions)
    if judged is None:
        return Promotion(now, champion, (), None, NO_COHORT, gate_of(db, champion), review=day, **found)
    kept = cohorts.verdict_of(db, judged.evaluation, judged.cycle)
    if kept is not None:
        gate = gate_of(db, champion)
        return Promotion(
            now, champion, (), None, REVIEWED, gate, review=day, cohort=judged, verdict=kept, **found
        )
    against = judged.champion if judged.champion is not None else champion
    records_ = records(db, mode, [against, *judged.members])
    judgments = tuple(judge_cohort(judged, records_))
    best = pick(judgments)
    verdict = cohorts.settle(
        db,
        now,
        judged,
        [j.figures() for j in judgments],
        bonferroni(len(judged.members)) if judged.members else None,
        None if best is None else best.challenger,
        "" if best is None else _reason(best, judged),
        {} if best is None else _detail(best, judged),
    )
    promoted = verdict.promoted
    if best is not None and promoted is None:
        log.warning(
            "promotion: the champion changed since cohort %s was formed (policy %s): none crowned",
            judged.cycle,
            judged.champion,
        )
    dropped = tuple(v for v in sorted(judged.members) if v != promoted)
    return Promotion(
        now,
        against,
        judgments,
        promoted,
        None,
        gate_of(db, promoted or policies.champion_version(db)),
        dropped,
        review=day,
        cohort=judged,
        verdict=verdict,
        **found,
    )


def _reason(j: Judgment, judged: cohorts.Cohort) -> str:
    return (
        f"도전 정책 {j.challenger}이 챔피언 정책 {j.champion}을 모의로 이겼다(#473): "
        f"주기 {judged.cycle} 묶음의 검토 날 {judged.review}, 함께 돈 거래일 {j.days}일, "
        f"날마다 (도전 − 챔피언) 순손익 평균 {_won(j.mean)}, 하한 {_won(j.low)}"
        f"(신뢰수준 {j.level:.2%}, 묶음의 도전 정책 {j.challengers}개로 Bonferroni 보정)"
    )


def _detail(j: Judgment, judged: cohorts.Cohort) -> dict[str, Any]:
    return {
        "from": j.champion,
        **{k: v for k, v in j.as_json().items() if k not in ("champion", "until", "review", "passed", "why")},
        "evaluation": judged.evaluation,
        "cycle": judged.cycle,
    }


def _won(value: float | None) -> str:
    return "없음" if value is None else f"{value:+,.0f}원"


def progress(
    db: sqlite3.Connection, mode: str | None = None, sessions: Callable[[date], Session | None] | None = None
) -> dict[str, Any]:
    """``GET /v1/trader``'s policies (#400): the champion's version, the live gate on its record,
    and the champion and each challenger with their records and, for a challenger, where it stands
    against its cohort's champion so far (#473: ``judge_cohort`` on the days of its cycle to now; a
    figure, not a verdict: it is judged on its cycle's review night alone); the cohort formed last
    with its verdict (``cohort``, #473); a cohort's days counted on ``sessions`` as they are now
    (``cohort.resolved``; as formed without them); the records ``mode``'s (None: the mode the trader
    last started in, #405). Read-only; empty from a store without policies."""
    if not _has_table(db, "policies") or not _has_table(db, "champions"):
        return {"champion": None, "gate": no_gate().as_json(), "policies": [], "cohort": None}
    current = policies.champion_version(db)
    running = policies.challenger_versions(db)

    def now(found: cohorts.Cohort) -> cohorts.Cohort:
        return found if sessions is None else cohorts.resolved(db, found, sessions)

    running_cohorts = [now(c) for c in cohorts.unjudged(db) if c.champion is not None]
    against = {c.champion for c in running_cohorts if c.champion is not None}
    kept = records(db, mode, {*([] if current is None else [current]), *running, *against})
    judged = {j.challenger: j for c in running_cohorts for j in judge_cohort(c, kept)}
    listed: list[dict[str, Any]] = []
    if current is not None:
        listed.append(
            {
                "version": current,
                "status": policies.CHAMPION,
                "paper": paper(kept.get(current)),
                "promotion": None,
            }
        )
    for version in running:
        found = judged.get(version)
        listed.append(
            {
                "version": version,
                "status": policies.CHALLENGER,
                "paper": paper(kept.get(version)),
                "promotion": None if found is None else found.as_json(),
            }
        )
    last = cohorts.latest(db)
    verdict = None if last is None else cohorts.verdict_of(db, last.evaluation, last.cycle)
    return {
        "champion": current,
        "gate": gate_of(db, current).as_json(),
        "policies": listed,
        "cohort": None
        if last is None
        else {**now(last).as_json(), "verdict": None if verdict is None else verdict.as_json()},
    }


VERDICTS = {
    cohorts.PROMOTED: "챔피언이 됨",
    cohorts.PASSED: "통과(하한이 더 큰 정책이 있어 내림)",
    cohorts.NOT_ABOVE: "하한 ≤ 0",
    cohorts.UNDECIDED: f"판정 불가(함께 돈 거래일 {PROMOTION_DAYS}일 미만)",
    cohorts.MISSED: "판정하지 못함(검토 날 밤의 작업이 돌지 않았다)",
}


def report(done: Promotion) -> str:
    """The night's judgment for the job's output (#400; #473: the cohort judged on a review night):
    won and counts, no price."""
    lines = ["도전 정책 판정 (#473: 묶음을 검토 날 밤에 한 번)"]
    for v in done.missed:
        lines.append(
            f"주기 {v.cycle}의 묶음은 검토 날 밤에 판정하지 못해 판정 없이 내렸다(창을 늘리지 않는다): "
            + (", ".join(f"정책 {m['version']}" for m in v.members) or "도전 정책 없음")
        )
    if done.retired:
        lines.append(
            "묶음에 들 수 없는 정책을 내렸다(묶음 밖에서 돌던 도전 정책, 평가 버전 전에 맞춘 기다리는 정책): "
            + ", ".join(f"정책 {v}" for v in done.retired)
        )
    if done.deferred == MARKET_OPEN:
        lines.append("장중이라 판정하지 않았다: 챔피언은 장이 끝난 뒤에만 바뀐다.")
    elif done.deferred == NO_CHAMPION:
        lines.append("챔피언이 아직 없다(첫 밤 정책 맞추기 전).")
    elif done.deferred == BETWEEN_REVIEWS:
        lines.append(
            "검토 날이 아니라 판정하지 않았다(#469): 도전 정책은 묶음으로 검토 날(20거래일마다) 밤에 한 번 "
            f"판정하고, 챔피언은 그때만 바뀐다. 다음 검토 날 {done.review}."
        )
    elif done.deferred == NO_COHORT:
        lines.append(f"검토 날 {done.review}: 이 주기에 돈 도전 정책 묶음이 없다. 챔피언은 그대로다.")
    elif done.deferred == REVIEWED:
        lines.append(f"검토 날 {done.review}의 판정은 이미 했다(판정 기록은 바뀌지 않는다).")
    elif done.cohort is not None and done.verdict is not None:
        c, v = done.cohort, done.verdict
        head = (
            f"검토 날 {done.review}(#469) · 주기 {c.cycle} 묶음({c.first}–{c.review}) · "
            f"챔피언 정책 {c.champion}"
        )
        if not done.judgments:
            lines.append(f"{head}: 묶음에 도전 정책이 없었다. 챔피언은 그대로다.")
        else:
            level = "?" if v.level is None else f"{v.level:.2%}"
            lines.append(
                f"{head} · 도전 정책 {len(done.judgments)}개 · 하한의 신뢰수준 {level}(묶음 안 Bonferroni) · "
                f"함께 돈 거래일 {PROMOTION_DAYS}일 미만이면 판정 불가"
            )
            verdicts = {int(m["version"]): str(m["verdict"]) for m in v.members}
            for j in done.judgments:
                lines.append(
                    f"- 정책 {j.challenger}: 함께 돈 거래일 {j.days}일, 날마다 (도전 − 챔피언) 순손익 평균 "
                    f"{_won(j.mean)}, 하한 {_won(j.low)} → {VERDICTS[verdicts[j.challenger]]}"
                )
            lines.append(
                f"→ 정책 {done.promoted}이 챔피언이 됐다(trader가 다음 08:30 고르기부터 쓴다)."
                if done.promoted is not None
                else "→ 챔피언은 그대로다."
            )
    if done.dropped:
        lines.append(
            "내린 도전 정책(묶음에서 뽑히지 않음, 다시 돌지 않는다): "
            + ", ".join(f"정책 {v}" for v in done.dropped)
        )
    for point in done.checked:
        lines.append(checkpoint_line(point))
    g = done.gate
    if g.policy is not None:
        lines.append(gate_line(g))
    return "\n".join(lines)


def checkpoint_line(c: Checkpoint) -> str:
    """A checkpoint evaluated (#408), for the night's output: no price."""
    head = f"실전 관문 판정 {c.k}번째(모의 이력 {c.history_day}거래일째, {c.day}, α {c.alpha:.3%})"
    if c.policy is None:
        return f"{head}: 그날 챔피언이 없다 → 넘지 못함"
    verdict = "넘었다" if c.passed else ("기록 부족" if c.days < GATE_DAYS else "하한 ≤ 0")
    return (
        f"{head}: 챔피언 정책 {c.policy}의 챔피언으로 돈 모의 기록 {c.days}일, "
        f"날마다 순손익 평균 {_won(c.mean)}, "
        f"하한 {_won(c.low)}(신뢰수준 {c.level:.2%}), 다리마다 {STRESS_TICKS}틱 더 불리하면 하한 "
        f"{_won(c.stressed_low)} → {verdict}"
    )


def gate_line(g: Gate) -> str:
    """The live gate on the champion now (#408), for the night's output: no price."""
    head = f"실전 관문: 챔피언 정책 {g.policy}의 챔피언으로 돈 모의 기록 {g.days}일(이력 {g.history}거래일)"
    if g.passed_at is not None:
        c = g.passed_at
        return f"{head} → 넘었다({c.k}번째 판정, {c.day}, 하한 {_won(c.low)}, 신뢰수준 {c.level:.2%})"
    return (
        f"{head}, 날마다 순손익 평균 {_won(g.mean)}, 다음 판정({g.next}번째, 이력 {g.next_day}거래일째, "
        f"α {alpha(g.next):.3%})의 신뢰수준 {g.level:.2%}로 지금 하한 {_won(g.low)} → 아직"
    )


def _has_table(db: sqlite3.Connection, name: str) -> bool:
    found = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone()
    return found is not None
