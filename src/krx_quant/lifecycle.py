"""Algorithm lifecycle: each algorithm's status, changed by rules after each day's close.

- **probation**: new. After 10 trading days, it is made active if the lower bound of its
  weakest lift over the baselines (all its probation days) is above 0.
- **active**: in the consensus (#18). Benched if its weakest lift over the last 20
  trading days is 0 or less.
- **bench**: still predicted and scored. Made active again if its weakest lift over the
  last 20 trading days has a lower bound above 0. After 60 trading days on the bench
  without that, it is a retirement candidate: an alert is sent, once.
- **retired**: by command only (``krx-quant retire``); it is no longer run. Its records
  stay. ``krx-quant unretire`` puts it back on probation.
- **clone**: an algorithm on probation whose predictions move with an earlier registered
  one's (correlation of p_up − p_down above 0.9, on the bar closes both predicted) is a
  clone, kept out of the consensus; back on probation once the correlation falls.

The statistics are the leaderboard's: basis B, special predictions left out, and a
decision only with at least ``min_sample`` scored predictions and as many pairs with each
baseline; with fewer (or, for a clone, too few pairs to correlate), the status stays. A
trading day counts when the engine predicted on it, from the first day reviewed after the
status began: a status set after a day's review starts counting the next day.
"""

from __future__ import annotations

import enum
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from krx_quant.baselines import BASELINE_NAMES
from krx_quant.stats import MIN_PAIRS_TESTED, Entry, enough, entry, scored_by_algorithm, split

PROBATION_DAYS = 10
RECENT_DAYS = 20
BENCH_DAYS = 60
CLONE_CORRELATION = 0.9
BASIS = "b"  # as the leaderboard


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
class Candidate:
    """A benched algorithm to retire, for the operator to approve."""

    algorithm: str
    days: int  # on the bench


@dataclass(frozen=True)
class Review:
    changes: list[Change]
    candidates: list[Candidate]


Rows = Callable[[Sequence[date], bool], list[dict[str, Any]]]  # (days, with special) -> scored rows


def review(
    algorithms: Sequence[str],
    standings: Mapping[str, Standing],
    counted_from: Mapping[str, date],
    days: Sequence[date],
    rows: Rows,
    *,
    min_sample: int,
    fee_rate: float,
    alerted: set[str],
) -> Review:
    """The status changes due at a day's review.

    ``algorithms`` are the registered ones in registration order; ``counted_from`` the
    first day each one's status counts; ``days`` the days with predictions up to the day
    reviewed, latest first; ``rows`` reads scored predictions; ``alerted`` the benched
    algorithms already alerted as retirement candidates.
    """
    read: dict[tuple[tuple[date, ...], bool], list[dict[str, Any]]] = {}

    def cached(chosen: Sequence[date], special: bool) -> list[dict[str, Any]]:
        key = (tuple(chosen), special)
        if key not in read:
            read[key] = rows(chosen, special)
        return read[key]

    judge = _Judge(cached, list(days[:RECENT_DAYS]), min_sample, fee_rate)
    changes: list[Change] = []
    candidates: list[Candidate] = []
    for i, name in enumerate(algorithms):
        standing = standings.get(name)
        if standing is None or standing.status is Status.RETIRED:
            continue
        held = [d for d in days if d >= counted_from[name]]
        earlier = [
            a for a in algorithms[:i] if (s := standings.get(a)) is None or s.status is not Status.RETIRED
        ]
        change = judge.decide(name, standing.status, held, earlier)
        if change is not None:
            changes.append(change)
        elif standing.status is Status.BENCH and len(held) >= BENCH_DAYS and name not in alerted:
            candidates.append(Candidate(name, len(held)))
    return Review(changes, candidates)


@dataclass
class _Judge:
    rows: Rows
    recent: list[date]
    min_sample: int
    fee_rate: float

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
            if len(held) >= PROBATION_DAYS:
                low = self._lift_low(name, held)
                if low is not None and low > 0:
                    return Change(
                        name,
                        status,
                        Status.ACTIVE,
                        f"{len(held)} days on probation; weakest lift low {low:.4f}",
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
        elif status is Status.BENCH:
            low = self._lift_low(name, self.recent)
            if low is not None and low > 0:
                return Change(
                    name,
                    status,
                    Status.ACTIVE,
                    f"recovered: weakest lift low {low:.4f} over the last {len(self.recent)} days",
                )
        return None

    def _judged(self, name: str, days: Sequence[date]) -> Entry | None:
        """``name``'s stats against the baselines on ``days``, if there are enough to judge by."""
        algorithms, baselines = split(scored_by_algorithm(self.rows(days, False), BASIS), BASELINE_NAMES)
        judged = entry(name, algorithms.get(name, []), baselines, self.fee_rate)
        return judged if enough(judged, self.min_sample) else None

    def _lift_low(self, name: str, days: Sequence[date]) -> float | None:
        judged = self._judged(name, days)
        return None if judged is None else judged.lift_low


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
