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
baseline; with fewer, the status stays. A trading day counts when the engine predicted
on it, from the day the status began.
"""

from __future__ import annotations

import enum
import math
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from krx_quant.baselines import BASELINES
from krx_quant.feeds.base import KST
from krx_quant.stats import MIN_PAIRS_TESTED, Entry, enough, entry, scored_by_algorithm, split

BASELINE_NAMES = frozenset(b.name for b in BASELINES)


class Status(enum.Enum):
    PROBATION = "probation"
    ACTIVE = "active"
    BENCH = "bench"
    RETIRED = "retired"
    CLONE = "clone"


PROBATION_DAYS = 10
RECENT_DAYS = 20
BENCH_DAYS = 60
CLONE_CORRELATION = 0.9
BASIS = "b"  # as the leaderboard


@dataclass(frozen=True)
class Change:
    algorithm: str
    previous: Status
    status: Status
    reason: str


@dataclass(frozen=True)
class Review:
    changes: list[Change]
    retire_candidates: list[tuple[str, str]]  # (algorithm, why)


Rows = Callable[[Sequence[date], bool], list[dict[str, Any]]]  # (days, with special) -> scored rows


def review(
    algorithms: Sequence[str],
    standings: Mapping[str, tuple[Status, datetime]],
    days: Sequence[date],
    rows: Rows,
    *,
    min_sample: int,
    fee_rate: float,
    alerted: Collection[str],
) -> Review:
    """The status changes due after a day's close.

    ``algorithms`` are the registered ones in registration order; ``days`` the days with
    predictions up to that day, latest first; ``rows`` reads scored predictions; ``alerted``
    the benched algorithms already alerted as retirement candidates.
    """
    changes: list[Change] = []
    candidates: list[tuple[str, str]] = []
    recent = list(days[:RECENT_DAYS])
    for i, name in enumerate(algorithms):
        standing = standings.get(name)
        if standing is None:
            continue
        status, since = standing
        held = [d for d in days if d >= since.astimezone(KST).date()]

        def change(to: Status, reason: str) -> None:
            changes.append(Change(name, status, to, reason))  # noqa: B023 - used at once

        if status in (Status.PROBATION, Status.CLONE):
            twin = _twin(name, algorithms[:i], rows(held, True))
            if twin is not None and status is Status.PROBATION:
                change(Status.CLONE, f"p_up − p_down moves with {twin[0]} (correlation {twin[1]:.3f})")
            elif twin is None and status is Status.CLONE:
                change(Status.PROBATION, "no longer moves with an earlier algorithm")
            elif status is Status.PROBATION and len(held) >= PROBATION_DAYS:
                judged = _judge(name, rows(held, False), min_sample, fee_rate)
                if judged is not None and judged.lift_low is not None and judged.lift_low > 0:
                    change(
                        Status.ACTIVE,
                        f"{len(held)} days on probation; weakest lift low {judged.lift_low:.4f}",
                    )
        elif status is Status.ACTIVE:
            judged = _judge(name, rows(recent, False), min_sample, fee_rate)
            weakest = None if judged is None else _weakest(judged)
            if weakest is not None and weakest <= 0:
                change(Status.BENCH, f"weakest lift {weakest:.4f} over the last {len(recent)} days")
        elif status is Status.BENCH:
            judged = _judge(name, rows(recent, False), min_sample, fee_rate)
            if judged is not None and judged.lift_low is not None and judged.lift_low > 0:
                change(
                    Status.ACTIVE,
                    f"recovered: weakest lift low {judged.lift_low:.4f} over the last {len(recent)} days",
                )
            elif len(held) >= BENCH_DAYS and name not in alerted:
                candidates.append((name, f"{len(held)} days on the bench without recovering"))
    return Review(changes, candidates)


def _judge(name: str, rows: list[dict[str, Any]], min_sample: int, fee_rate: float) -> Entry | None:
    """``name``'s stats against the baselines, if there are enough to judge by."""
    algorithms, baselines = split(scored_by_algorithm(rows, BASIS), BASELINE_NAMES)
    judged = entry(name, algorithms.get(name, []), baselines, fee_rate)
    return judged if enough(judged, min_sample) else None


def _weakest(e: Entry) -> float | None:
    values = [t.lift.value for t in e.lifts.values()]
    return None if any(v is None for v in values) else min(v for v in values if v is not None)


def _twin(name: str, earlier: Sequence[str], rows: list[dict[str, Any]]) -> tuple[str, float] | None:
    """The earlier algorithm ``name``'s predictions move with most, if above the line."""
    scores: dict[str, dict[tuple[str, str], float]] = {}
    for row in rows:
        scores.setdefault(row["algorithm"], {})[(row["symbol"], row["bar_close"])] = (
            row["p_up"] - row["p_down"]
        )
    mine = scores.get(name, {})
    best: tuple[str, float] | None = None
    for other in earlier:
        theirs = scores.get(other, {})
        pairs = [(mine[k], theirs[k]) for k in mine.keys() & theirs.keys()]
        r = _correlation(pairs)
        if r is not None and r > CLONE_CORRELATION and (best is None or r > best[1]):
            best = (other, r)
    return best


def _correlation(pairs: Sequence[tuple[float, float]]) -> float | None:
    """Pearson's r, on enough pairs; None if too few or either side never moves."""
    n = len(pairs)
    if n < MIN_PAIRS_TESTED:
        return None
    mx = sum(x for x, _ in pairs) / n
    my = sum(y for _, y in pairs) / n
    sxy = sum((x - mx) * (y - my) for x, y in pairs)
    sxx = sum((x - mx) ** 2 for x, _ in pairs)
    syy = sum((y - my) ** 2 for _, y in pairs)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)
