"""Algorithm lifecycle: each algorithm's status, changed by rules after each day's close.

- **probation**: new. Judged on set days of probation only (10, 20 and 40 by default):
  made active if the lower bound of its weakest lift over the baselines by trading day
  (all its probation days) is above 0. Judged daily, chance alone would promote an
  algorithm with no edge far more often than once in forty. Past the last of those days
  without it, it is a retirement candidate: an alert is sent, once (#87).
- **active**: in the consensus (#18). Benched if its weakest lift over the last 20
  trading days is 0 or less.
- **bench**: still predicted and scored. Judged on set days on the bench only (20, 40
  and 60 by default): made active again if its weakest lift by trading day over the last
  20 trading days has a lower bound above 0. Past the last of those days without it, it
  is a retirement candidate: an alert is sent, once.
- **retired**: by command only (``krx-quant retire``); it is no longer run. Its records
  stay. ``krx-quant unretire`` puts it back on probation.
- **clone**: an algorithm on probation whose predictions move with an earlier registered
  one's (correlation of p_up − p_down above 0.9, on the bar closes both predicted) is a
  clone, kept out of the consensus; back on probation once the correlation falls.

The statistics are the leaderboard's: the touch label (#170), special predictions left
out, and a decision only with at least ``min_sample`` scored predictions and as many pairs
with each baseline; with fewer (or, for a clone, too few pairs to correlate), the status
stays. Only the predictions of models trained on the touch target count (``stats.on_target``):
an algorithm's old target's or its defaults' are left out, the baselines' are all in. A
lower bound is the lift's by trading day (``stats.day_lift``): the overlapping four-minute
outcomes of one day are not counted as independent evidence. Its interval is wider than
the stats' 95% (98.3% by default), so that the three looks together let a lift of nothing
through no more often than one look at 95% did. A trading day counts when the engine
predicted on it, from the first day counted (``counted_from``, the engine's): the first day
reviewed after the status began (a status set after a day's review starts counting the
next day) and, on probation or as a clone, the first day the algorithm predicted with a
model of the touch target since (none before it: nothing is counted, no review is due).

When the target changed (#170) every algorithm not retired restarted on probation
(``RESTARTED``), waiting for a model trained on the new target: the night's training
promotes one whatever its score, and its probation counts from the trading day the morning
swap installed it.

Where an algorithm on probation or on the bench stands toward its next review (``stages``,
#165) is counted and judged by the same code, on the days reviewed so far.
"""

from __future__ import annotations

import enum
import statistics
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from krx_quant.baselines import BASELINE_NAMES
from krx_quant.feeds.base import kst_day
from krx_quant.stats import (
    MIN_PAIRS_TESTED,
    Entry,
    Lift,
    enough,
    entry,
    on_target,
    scored_by_algorithm,
    split,
)
from krx_quant.touch import TARGET_VERSION

if TYPE_CHECKING:
    from krx_quant.config import Settings

RECENT_DAYS = 20
CLONE_CORRELATION = 0.9
# The reason of the probation every algorithm not retired restarts on when the target changes (#170).
RESTARTED = "the target changed to the touch target (#170): probation restarts with a model trained on it"


class Status(enum.Enum):
    PROBATION = "probation"
    ACTIVE = "active"
    BENCH = "bench"
    RETIRED = "retired"
    CLONE = "clone"


@dataclass(frozen=True)
class Standing:
    status: Status
    since: datetime


@dataclass(frozen=True)
class Change:
    algorithm: str
    previous: Status
    status: Status
    reason: str


@dataclass(frozen=True)
class Rules:
    """When the statuses are judged, and how sure a promotion must be (#87)."""

    probation_days: tuple[int, ...] = (10, 20, 40)  # days on probation it is judged on; the last ends it
    bench_days: tuple[int, ...] = (20, 40, 60)  # days on the bench it may recover on; likewise
    confidence: float = 0.983  # the two-sided level of the lift-by-day interval it needs

    @classmethod
    def of(cls, settings: Settings) -> Rules:
        return cls(settings.probation_review_days, settings.bench_review_days, settings.review_confidence)

    @property
    def max_probation(self) -> int:
        return self.probation_days[-1]

    @property
    def max_bench(self) -> int:
        return self.bench_days[-1]


@dataclass(frozen=True)
class Candidate:
    """An algorithm to retire, for the operator to approve: on probation or on the bench past
    the last day it could be made active on."""

    algorithm: str
    days: int  # in that status
    status: Status  # probation or bench


@dataclass(frozen=True)
class Review:
    changes: list[Change]
    candidates: list[Candidate]


Rows = Callable[[Sequence[date], bool], list[dict[str, Any]]]  # (days, with special) -> scored rows


def review(
    algorithms: Sequence[str],
    standings: Mapping[str, Standing],
    counted_from: Mapping[str, date | None],
    days: Sequence[date],
    rows: Rows,
    *,
    min_sample: int,
    fee_rate: float,
    alerted: set[str],
    rules: Rules | None = None,
    baselines: Collection[str] = BASELINE_NAMES,
) -> Review:
    """The status changes due at a day's review.

    ``algorithms`` are the registered ones in registration order; ``counted_from`` the
    first day each one's status counts (None: none yet, waiting for a model of the target);
    ``days`` the days with predictions up to the day reviewed, latest first; ``rows`` reads
    scored predictions, of which only the target's models' count (``stats.on_target``);
    ``alerted`` the benched algorithms already alerted as retirement candidates; ``rules``
    when to judge (defaults); ``baselines`` the baselines' names, the lifts' (those of the
    target version, #169).
    """
    rules = rules or Rules()
    read: dict[tuple[tuple[date, ...], bool], list[dict[str, Any]]] = {}

    def cached(chosen: Sequence[date], special: bool) -> list[dict[str, Any]]:
        key = (tuple(chosen), special)
        if key not in read:
            read[key] = on_target(rows(chosen, special), baselines, TARGET_VERSION)
        return read[key]

    judge = _Judge(cached, list(days[:RECENT_DAYS]), min_sample, fee_rate, rules, baselines)
    changes: list[Change] = []
    candidates: list[Candidate] = []
    for i, name in enumerate(algorithms):
        standing = standings.get(name)
        if standing is None or standing.status is Status.RETIRED:
            continue
        held = _held(days, counted_from[name])
        earlier = [
            a for a in algorithms[:i] if (s := standings.get(a)) is None or s.status is not Status.RETIRED
        ]
        change = judge.decide(name, standing.status, held, earlier)
        last = {Status.PROBATION: rules.max_probation, Status.BENCH: rules.max_bench}.get(standing.status)
        if change is not None:
            changes.append(change)
        elif last is not None and len(held) >= last and name not in alerted:
            candidates.append(Candidate(name, len(held), standing.status))
    return Review(changes, candidates)


@dataclass
class _Judge:
    rows: Rows
    recent: list[date]
    min_sample: int
    fee_rate: float
    rules: Rules
    baselines: Collection[str] = BASELINE_NAMES

    def decide(self, name: str, status: Status, held: list[date], earlier: list[str]) -> Change | None:
        """The change ``name``'s status is due for, if any."""
        if status in (Status.PROBATION, Status.CLONE):
            measured = correlations(name, earlier, self.rows(held, True))
            twin = most_correlated(measured)
            if twin is not None and twin[1] > CLONE_CORRELATION:
                if status is Status.PROBATION:
                    return Change(
                        name, status, Status.CLONE, f"p_up - p_down moves with {twin[0]} (r = {twin[1]:.3f})"
                    )
                return None
            if status is Status.CLONE:
                if not measured:
                    return None  # too few pairs to tell: it stays
                return Change(name, status, Status.PROBATION, "no longer moves with an earlier algorithm")
            if len(held) in self.rules.probation_days:
                passed = self._passed_by_day(name, held)
                if passed is not None:
                    return Change(
                        name,
                        status,
                        Status.ACTIVE,
                        f"{len(held)} days on probation; weakest lift low {passed.low:.4f}"
                        f" by trading day at {self.rules.confidence:.1%} ({passed.n} days scored)",
                    )
        elif status is Status.ACTIVE:
            judged = self._judged(name, self.recent)
            weakest = None if judged is None else judged.weakest_lift
            if weakest is not None and weakest <= 0:
                return Change(
                    name,
                    status,
                    Status.BENCH,
                    f"weakest lift {weakest:.4f} over the last {len(self.recent)} days",
                )
        elif status is Status.BENCH and len(held) in self.rules.bench_days:
            passed = self._passed_by_day(name, self.recent)
            if passed is not None:
                return Change(
                    name,
                    status,
                    Status.ACTIVE,
                    f"recovered: weakest lift low {passed.low:.4f} by trading day at"
                    f" {self.rules.confidence:.1%} ({passed.n} days scored) over the last"
                    f" {len(self.recent)} days, {len(held)} on the bench",
                )
        return None

    def _judged(self, name: str, days: Sequence[date]) -> Entry | None:
        """``name``'s stats against the baselines on ``days``, if there are enough to judge by;
        its lifts by trading day at the rules' level."""
        judged = self.measure(name, days)
        return judged if enough(judged, self.min_sample) else None

    def measure(self, name: str, days: Sequence[date]) -> Entry:
        """``name``'s stats against the baselines on ``days``, enough to judge by or not."""
        algorithms, baselines = split(scored_by_algorithm(self.rows(days, False)), self.baselines)
        return entry(name, algorithms.get(name, []), baselines, self.fee_rate, self.rules.confidence)

    def _passed_by_day(self, name: str, days: Sequence[date]) -> Lift | None:
        """``name``'s weakest lift by trading day on ``days``, if its lower bound is above 0."""
        judged = self._judged(name, days)
        weakest = None if judged is None else judged.weakest_by_day
        return weakest if weakest is not None and weakest.low is not None and weakest.low > 0 else None


def _held(days: Sequence[date], counted_from: date | None) -> list[date]:
    """The ``days`` counted from ``counted_from``; none while there is none."""
    return [] if counted_from is None else [d for d in days if d >= counted_from]


def most_correlated(measured: Mapping[str, float]) -> tuple[str, float] | None:
    """The algorithm with the highest correlation, and that correlation; None if none."""
    return max(measured.items(), key=lambda kv: kv[1], default=None)


def correlations(name: str, others: Sequence[str], rows: list[dict[str, Any]]) -> dict[str, float]:
    """``name``'s correlation of p_up − p_down with each of ``others`` it can be measured
    against: enough bar closes both predicted, and neither always the same.
    """
    scores: dict[str, dict[tuple[str, str], float]] = {}
    for row in rows:
        scores.setdefault(row["algorithm"], {})[(row["symbol"], row["bar_close"])] = (
            row["p_up"] - row["p_down"]
        )
    mine = scores.get(name, {})
    measured: dict[str, float] = {}
    for other in others:
        theirs = scores.get(other, {})
        shared = sorted(mine.keys() & theirs.keys())
        if len(shared) < MIN_PAIRS_TESTED:
            continue
        try:
            measured[other] = statistics.correlation([mine[k] for k in shared], [theirs[k] for k in shared])
        except statistics.StatisticsError:
            continue  # one side never moves
    return measured


@dataclass(frozen=True)
class Stage:
    """Where an algorithm on probation or on the bench stands toward the review that may make
    it active (#165), as of the latest review: what that review counts and judges by."""

    status: Status
    began: date  # the day its status began
    # The first day counted: the next, for a status set after that day's review; on probation,
    # the first day with a model of the target (#170). None: waiting for one, nothing counted.
    counted_from: date | None
    days: int  # trading days counted, through the latest review
    review_days: tuple[int, ...]  # the days counted it is judged on (``Rules``)
    next_review: int | None  # the next of them; None past the last (a retirement candidate)
    lift_days: int  # the days its lift is taken over: those counted (probation), the latest 20 (bench)
    level: float  # the two-sided level of the lift-by-day interval (``Rules.confidence``)
    weakest: tuple[str, Lift] | None  # its weakest lift by trading day there, and which baseline's
    enough: bool  # whether those days hold enough scored predictions and pairs to decide on
    # The day the next review falls on if every trading day from the next review on is predicted:
    # the engine's to tell (it knows the calendar), None here.
    next_review_on: date | None = None


def stages(
    algorithms: Sequence[str],
    standings: Mapping[str, Standing],
    counted_from: Mapping[str, date | None],
    days: Sequence[date],
    rows: Rows,
    *,
    min_sample: int,
    fee_rate: float,
    rules: Rules | None = None,
    baselines: Collection[str] = BASELINE_NAMES,
) -> dict[str, Stage]:
    """Each algorithm on probation or on the bench, where it stands toward its next review (#165).

    The arguments are ``review``'s, with ``days`` the days reviewed already, latest first: the
    days are counted as the review counts them, and the weakest lift is taken by trading day
    on the days the review takes it on, at the rules' level. The next review counts one day
    more. The scored predictions are read once, for every algorithm.
    """
    rules = rules or Rules()
    recent = list(days[:RECENT_DAYS])
    on: dict[str, tuple[Standing, list[date], list[date]]] = {}  # standing, days counted, lift days
    for name in algorithms:
        standing = standings.get(name)
        if standing is None or standing.status not in (Status.PROBATION, Status.BENCH):
            continue
        held = _held(days, counted_from[name])
        on[name] = (standing, held, held if standing.status is Status.PROBATION else recent)
    wanted = sorted({d for _, _, lift_days in on.values() for d in lift_days}, reverse=True)
    read = on_target(rows(wanted, False), baselines, TARGET_VERSION) if wanted else []

    def rows_on(chosen: Sequence[date], special: bool) -> list[dict[str, Any]]:
        """``read``'s rows of ``chosen`` (special predictions are not in it)."""
        keep = {d.isoformat() for d in chosen}
        return [r for r in read if r["bar_close"][:10] in keep]

    judge = _Judge(rows_on, recent, min_sample, fee_rate, rules, baselines)
    out: dict[str, Stage] = {}
    for name, (standing, held, lift_days) in on.items():
        review_days = rules.probation_days if standing.status is Status.PROBATION else rules.bench_days
        judged = judge.measure(name, lift_days)
        out[name] = Stage(
            standing.status,
            kst_day(standing.since),
            counted_from[name],
            len(held),
            review_days,
            next((d for d in review_days if d > len(held)), None),
            len(lift_days),
            rules.confidence,
            _weakest_by_day(judged),
            enough(judged, min_sample),
        )
    return out


def _weakest_by_day(judged: Entry) -> tuple[str, Lift] | None:
    """The weakest lift by trading day (``Entry.weakest_by_day``), and its baseline."""
    weakest = judged.weakest_by_day
    for name, tested in judged.lifts.items():
        if weakest is not None and tested.by_day is weakest:
            return name, weakest
    return None
