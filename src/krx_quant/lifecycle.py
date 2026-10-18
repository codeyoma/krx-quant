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

When the target changes (#170: to the touch target; #187: to its version 3, and the
barrier's answer to its version 2) every algorithm of it not retired restarts on probation
(``target_changed``), waiting for a model trained on the new target: the night's training
promotes one whatever its score, and its probation counts from the trading day the morning
swap installed it.

Where an algorithm on probation or on the bench stands toward its next review (``stages``,
#165) is counted and judged by the same code, on the days reviewed so far.

The algorithms of a kind besides the direction (#179, ``kinds.py``) are reviewed by the same
rules, a kind at a time (``Scoring``: ``kind_stats.kind_scoring``): their stats against
that kind's baselines, a clone looked for among the earlier algorithms of that kind only
(by the first class's probability less the third's), and only the predictions of models
trained on that kind's answers counted.

A probation begun for a new algorithm (``REGISTERED``) or a new target (``target_changed``) can
be simulated (#298, ``simulation.py``): the latest trading days replayed as if the algorithm had
been on probation, its predictions and their answers kept apart from the live ones
(``Simulated``). Those days are its probation's first days, ahead of the live days after them
(a live day they replayed is superseded), and on them its rows and its kind's baselines' are the
simulation's, the other algorithms' (a clone's twin) the live ones. A simulation is judged as soon
as it is stored (``review_at_once``), as at the largest review day its days reach; one that does
not pass stays on probation, and the live days count on toward the next review day. The active
and bench checks of the latest days read the simulation's rows on its days too, as the probation
it came out of did; the leaderboard and the statistics never do.
"""

from __future__ import annotations

import enum
import statistics
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from krx_quant.baselines import BASELINE_NAMES
from krx_quant.feeds.base import kst_day
from krx_quant.stats import (
    MIN_PAIRS_TESTED,
    Lift,
    Lifted,
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
# The reason of a new algorithm's first status: on probation once its pre-validation passed.
REGISTERED = "registered"
# How a restart on a new target's reason ends (#170, #187).
_RESTARTS = ": probation restarts with a model trained on it"
# How a simulation's review starts its reasons (#298, ``review_at_once``).
SIMULATED = "simulated probation"


def target_changed(kind: str, version: int) -> str:
    """The reason of the probation every algorithm of ``kind`` not retired restarts on when its
    target changes to ``version`` (#187: the direction's touch target to 3, the barrier's answer
    to 2)."""
    return f"the {kind} target changed to version {version}{_RESTARTS}"


def starts_simulation(reason: str) -> bool:
    """Whether a probation begun for ``reason`` is simulated (#298): a new algorithm's, once its
    pre-validation passed (``REGISTERED``), and a restart on a new target (#170, #187). Not one
    put back by command (``unretire``) or a clone's return."""
    return reason == REGISTERED or reason.endswith(_RESTARTS)


# One predictor's stats against the baselines, from scored rows: (name, rows, baselines' names,
# fee rate, the two-sided level of the lifts by trading day).
Measure = Callable[[str, list[dict[str, Any]], Collection[str], float, float], Lifted]


@dataclass(frozen=True)
class Scoring:
    """How a kind's stored predictions are judged (#179): one predictor's stats against the
    kind's baselines (``measure``), what a clone moves with (``lean``: the first class's
    probability less the third's), and the target whose models' predictions count."""

    measure: Measure
    lean: Callable[[Mapping[str, Any]], float]
    lean_name: str  # as a clone's reason names it
    target_version: int


def _direction_measure(
    name: str, rows: list[dict[str, Any]], baselines: Collection[str], fee_rate: float, level: float
) -> Lifted:
    algorithms, scored = split(scored_by_algorithm(rows), baselines)
    return entry(name, algorithms.get(name, []), scored, fee_rate, level)


def _direction_lean(row: Mapping[str, Any]) -> float:
    return float(row["p_up"] - row["p_down"])


# The direction's: the touch label (#170), p_up − p_down.
DIRECTION_SCORING = Scoring(_direction_measure, _direction_lean, "p_up - p_down", TARGET_VERSION)


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
    reason: str = field(default="", compare=False)  # why the status began (``statuses.reason``)


@dataclass(frozen=True)
class Change:
    algorithm: str
    previous: Status
    status: Status
    reason: str
    simulated: bool = False  # it counted simulated days (#298): ``statuses.simulated``


Rows = Callable[[Sequence[date], bool], list[dict[str, Any]]]  # (days, with special) -> scored rows


@dataclass(frozen=True)
class Simulated:
    """An algorithm's simulated probation (#298, ``simulation.py``), as the reviews count it: the
    latest trading days replayed as if it had been on probation.

    ``days`` (latest first) count as the first days of the probation begun at ``stint``, ahead of
    the live days after them. On them, ``rows`` reads the simulation's scored predictions (special
    ones too, if asked): the algorithm's, its kind's baselines' and those of the algorithms
    simulated with it, each with the answer it was scored on; ``predictors`` are their names. The
    other predictors' rows there are the live ones."""

    days: tuple[date, ...]
    stint: datetime
    rows: Rows
    predictors: frozenset[str]


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
    scoring: Scoring = DIRECTION_SCORING,
    recent: int = RECENT_DAYS,
    simulated: Mapping[str, Simulated] | None = None,
) -> Review:
    """The status changes due at a day's review.

    ``algorithms`` are the registered ones in registration order; ``counted_from`` the
    first day each one's status counts (None: none yet, waiting for a model of the target);
    ``days`` the days with predictions up to the day reviewed, latest first; ``rows`` reads
    scored predictions, of which only the target's models' count (``stats.on_target``);
    ``alerted`` the benched algorithms already alerted as retirement candidates; ``rules``
    when to judge (defaults); ``baselines`` the baselines' names, the lifts' (those of the
    target version, #169). ``algorithms``, ``rows`` and ``baselines`` are of one kind, judged
    by its ``scoring`` (#179: the direction's by default): a clone is looked for within them.
    An active algorithm is benched, and a benched one recovers, on the ``recent`` latest days
    (20, a daily kind's too since #298). ``simulated`` are the algorithms with a simulated
    probation (#298), whose days count as the module doc says.
    """
    rules = rules or Rules()
    cached = _cached(rows, baselines, scoring.target_version)
    judge = _Judge(cached, list(days[:recent]), min_sample, fee_rate, rules, baselines, scoring)
    changes: list[Change] = []
    candidates: list[Candidate] = []
    for i, name in enumerate(algorithms):
        standing = standings.get(name)
        if standing is None or standing.status is Status.RETIRED:
            continue
        sim = (simulated or {}).get(name)
        held = _counted(days, counted_from[name], standing, sim)
        earlier = [
            a for a in algorithms[:i] if (s := standings.get(a)) is None or s.status is not Status.RETIRED
        ]
        change = _with(judge, sim, scoring).decide(name, standing.status, held, earlier)
        last = {Status.PROBATION: rules.max_probation, Status.BENCH: rules.max_bench}.get(standing.status)
        if change is not None:
            replayed = len(set(held) & set(sim.days)) if sim is not None else 0
            if replayed and standing.status is Status.PROBATION:
                change = replace(
                    change, reason=f"{change.reason}; {replayed} of the days simulated", simulated=True
                )
            changes.append(change)
        elif last is not None and len(held) >= last and name not in alerted:
            candidates.append(Candidate(name, len(held), standing.status))
    return Review(changes, candidates)


@dataclass(frozen=True)
class AtOnce:
    """A simulation's review (#298, ``review_at_once``): the review day its days reach, what it
    measured there and the change it made, if any."""

    algorithm: str
    days: int  # the days counted: its simulated days
    review_day: int | None  # the largest review day they reach; None below the first
    weakest: tuple[str, Lift] | None  # its weakest lift by trading day, and which baseline's
    scored: int  # its scored predictions on those days
    enough: bool  # enough of them, and pairs, to decide on (``stats.enough``)
    change: Change | None


def review_at_once(
    names: Collection[str],
    algorithms: Sequence[str],
    standings: Mapping[str, Standing],
    simulated: Mapping[str, Simulated],
    rows: Rows,
    *,
    min_sample: int,
    fee_rate: float,
    rules: Rules | None = None,
    baselines: Collection[str] = BASELINE_NAMES,
    scoring: Scoring = DIRECTION_SCORING,
) -> list[AtOnce]:
    """The review a simulation makes as soon as it is stored (#298): each of ``names`` still on
    the probation its simulation was made for, judged on the days counted, its simulated days (a
    simulation replays the latest finished days, so no live day follows them yet), as at the
    largest review day they reach, whatever their number: not judged below the first. A clone is
    looked for as at any review, among the earlier ``algorithms`` (of the kind, in registration
    order). The arguments are ``review``'s; each change's reason starts with ``SIMULATED`` and its
    days.
    """
    rules = rules or Rules()
    judge = _Judge(
        _cached(rows, baselines, scoring.target_version), [], min_sample, fee_rate, rules, baselines, scoring
    )
    out: list[AtOnce] = []
    for i, name in enumerate(algorithms):
        standing, sim = standings.get(name), simulated.get(name)
        if name not in names or standing is None or sim is None or not sim.days:
            continue
        if standing.status is not Status.PROBATION or standing.since != sim.stint:
            continue
        held = list(sim.days)
        reached = [d for d in rules.probation_days if d <= len(held)]
        earlier = [
            a for a in algorithms[:i] if (s := standings.get(a)) is None or s.status is not Status.RETIRED
        ]
        own = _with(judge, sim, scoring)
        change = own.decide(name, standing.status, held, earlier, due=bool(reached))
        if change is not None:
            span = f"{min(held)}..{max(held)}"
            change = replace(
                change, reason=f"{SIMULATED} {span} ({len(held)} days): {change.reason}", simulated=True
            )
        judged = own.measure(name, held)
        out.append(
            AtOnce(
                name,
                len(held),
                reached[-1] if reached else None,
                _weakest_by_day(judged),
                judged.summary.n,
                enough(judged, min_sample),
                change,
            )
        )
    return out


def _cached(rows: Rows, baselines: Collection[str], target_version: int) -> Rows:
    """``rows`` read once per set of days, only the target's models' kept (``stats.on_target``)."""
    read: dict[tuple[tuple[date, ...], bool], list[dict[str, Any]]] = {}

    def cached(chosen: Sequence[date], special: bool) -> list[dict[str, Any]]:
        key = (tuple(chosen), special)
        if key not in read:
            read[key] = on_target(rows(chosen, special), baselines, target_version)
        return read[key]

    return cached


def _counted(
    days: Sequence[date], counted_from: date | None, standing: Standing, sim: Simulated | None
) -> list[date]:
    """The days ``standing`` counts, latest first: of ``days``, those from ``counted_from``; on the
    probation a simulation was made for (#298), its simulated days, after the live days that
    follow them (those it replayed are not counted twice)."""
    held = _held(days, counted_from)
    if sim is None or not sim.days or standing.status is not Status.PROBATION or standing.since != sim.stint:
        return held
    last = max(sim.days)
    return [d for d in held if d > last] + sorted(sim.days, reverse=True)


def _with(judge: _Judge, sim: Simulated | None, scoring: Scoring) -> _Judge:
    """``judge`` reading, on ``sim``'s days, the simulation's rows of its predictors and the live
    rows of the others (#298); ``judge`` itself without a simulation."""
    if sim is None or not sim.days:
        return judge
    live = judge.rows
    simulated_days = set(sim.days)
    replayed = _cached(sim.rows, judge.baselines, scoring.target_version)

    def rows(chosen: Sequence[date], special: bool) -> list[dict[str, Any]]:
        mine = [d for d in chosen if d in simulated_days]
        out = live([d for d in chosen if d not in simulated_days], special) if len(mine) < len(chosen) else []
        if mine:
            out = out + [r for r in live(mine, special) if r["algorithm"] not in sim.predictors]
            out += replayed(mine, special)
        return out

    return replace(judge, rows=rows)


@dataclass
class _Judge:
    rows: Rows
    recent: list[date]
    min_sample: int
    fee_rate: float
    rules: Rules
    baselines: Collection[str] = BASELINE_NAMES
    scoring: Scoring = DIRECTION_SCORING

    def decide(
        self, name: str, status: Status, held: list[date], earlier: list[str], *, due: bool | None = None
    ) -> Change | None:
        """The change ``name``'s status is due for, if any; on probation judged on a review day
        of ``held``'s count, or as ``due`` says (a simulation's review at once, #298)."""
        if status in (Status.PROBATION, Status.CLONE):
            measured = correlations(name, earlier, self.rows(held, True), self.scoring.lean)
            twin = most_correlated(measured)
            if twin is not None and twin[1] > CLONE_CORRELATION:
                if status is Status.PROBATION:
                    return Change(
                        name,
                        status,
                        Status.CLONE,
                        f"{self.scoring.lean_name} moves with {twin[0]} (r = {twin[1]:.3f})",
                    )
                return None
            if status is Status.CLONE:
                if not measured:
                    return None  # too few pairs to tell: it stays
                return Change(name, status, Status.PROBATION, "no longer moves with an earlier algorithm")
            if len(held) in self.rules.probation_days if due is None else due:
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

    def _judged(self, name: str, days: Sequence[date]) -> Lifted | None:
        """``name``'s stats against the baselines on ``days``, if there are enough to judge by;
        its lifts by trading day at the rules' level."""
        judged = self.measure(name, days)
        return judged if enough(judged, self.min_sample) else None

    def measure(self, name: str, days: Sequence[date]) -> Lifted:
        """``name``'s stats against the baselines on ``days``, enough to judge by or not."""
        return self.scoring.measure(
            name, self.rows(days, False), self.baselines, self.fee_rate, self.rules.confidence
        )

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


def correlations(
    name: str,
    others: Sequence[str],
    rows: list[dict[str, Any]],
    lean: Callable[[Mapping[str, Any]], float] = _direction_lean,
) -> dict[str, float]:
    """``name``'s correlation of p_up − p_down (a kind's ``lean``, #179: its first class's
    probability less its third's) with each of ``others`` it can be measured against: enough
    bar closes both predicted, and neither always the same.
    """
    scores: dict[str, dict[tuple[str, str], float]] = {}
    for row in rows:
        scores.setdefault(row["algorithm"], {})[(row["symbol"], row["bar_close"])] = lean(row)
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
    simulated: int = 0  # of the days counted, those simulated (#298)


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
    scoring: Scoring = DIRECTION_SCORING,
    recent: int = RECENT_DAYS,
    simulated: Mapping[str, Simulated] | None = None,
) -> dict[str, Stage]:
    """Each algorithm on probation or on the bench, where it stands toward its next review (#165).

    The arguments are ``review``'s (one kind's, #179), with ``days`` the days reviewed already,
    latest first: the days are counted as the review counts them (a simulated probation's too,
    #298), and the weakest lift is taken by trading day on the days the review takes it on, at
    the rules' level. The next review counts one day more. The live scored predictions are read
    once, for every algorithm.
    """
    rules = rules or Rules()
    latest = list(days[:recent])
    on: dict[str, tuple[Standing, list[date], list[date]]] = {}  # standing, days counted, lift days
    for name in algorithms:
        standing = standings.get(name)
        if standing is None or standing.status not in (Status.PROBATION, Status.BENCH):
            continue
        held = _counted(days, counted_from[name], standing, (simulated or {}).get(name))
        on[name] = (standing, held, held if standing.status is Status.PROBATION else latest)
    wanted = sorted({d for _, _, lift_days in on.values() for d in lift_days}, reverse=True)
    read = on_target(rows(wanted, False), baselines, scoring.target_version) if wanted else []

    def rows_on(chosen: Sequence[date], special: bool) -> list[dict[str, Any]]:
        """``read``'s rows of ``chosen`` (special predictions are not in it)."""
        keep = {d.isoformat() for d in chosen}
        return [r for r in read if r["bar_close"][:10] in keep]

    judge = _Judge(rows_on, latest, min_sample, fee_rate, rules, baselines, scoring)
    out: dict[str, Stage] = {}
    for name, (standing, held, lift_days) in on.items():
        review_days = rules.probation_days if standing.status is Status.PROBATION else rules.bench_days
        sim = (simulated or {}).get(name)
        judged = _with(judge, sim, scoring).measure(name, lift_days)
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
            simulated=0 if sim is None else len(set(held) & set(sim.days)),
        )
    return out


def _weakest_by_day(judged: Lifted) -> tuple[str, Lift] | None:
    """The weakest lift by trading day (``Entry.weakest_by_day``), and its baseline."""
    weakest = judged.weakest_by_day
    for name, tested in judged.lifts.items():
        if weakest is not None and tested.by_day is weakest:
            return name, weakest
    return None
