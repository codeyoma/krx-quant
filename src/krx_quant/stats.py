"""Stats Engine: how good the predictions were, with luck accounted for.

Each scored prediction is judged on one basis (A: from the bar close, B: from when it
was displayed). Its predicted class is the most likely one; a tie goes to flat, then up.

- **accuracy**: the predicted class happened (3 classes);
- **precision**: of the up and down calls, the share that happened; **coverage**: the
  share of predictions that called a direction;
- **Brier score**: the squared distance between the probabilities and what happened,
  summed over the three classes (0 is perfect, 2 is sure and wrong);
- **profit**: each up or down call traded in its direction from the start mid-point to
  the target, in ticks, less the round-trip cost (sell tax and both commissions);
- **calibration**: every class probability binned by tenths, against how often that
  class happened;
- **lift**: the gain in accuracy over a baseline, paired by symbol and bar close, with a
  one-sided p-value for "no better than the baseline".

Ratios come with 95% Wilson intervals, means with 95% normal intervals (none from a
single value). A lift is tested only on enough pairs for the normal approximation; the
lifts of the algorithms a leaderboard could rank are adjusted together by
Benjamini–Hochberg (false discovery rate).
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from statistics import NormalDist
from typing import Any

from krx_quant.pricing import SELL_TAX, Direction, most_likely, tick_size

_Z = NormalDist().inv_cdf(0.975)
FDR = 0.05  # the false discovery rate a significant lift is held to
CALIBRATION_BINS = 10
# Fewer pairs than this are not tested: the normal approximation would be too rough.
MIN_PAIRS_TESTED = 30
# Scored predictions (and pairs with each baseline) an algorithm needs to be ranked.
DEFAULT_MIN_SAMPLE = 200


@dataclass(frozen=True)
class Scored:
    """One scored prediction, on one basis."""

    algorithm: str
    model_version: str
    symbol: str
    bar_close: datetime
    p_up: float
    p_flat: float
    p_down: float
    outcome: Direction
    start_mid: float  # the bar close's (A) or the display's (B)
    target_mid: float

    @property
    def predicted(self) -> Direction:
        return most_likely(self.p_up, self.p_flat, self.p_down)

    @property
    def correct(self) -> bool:
        return self.predicted is self.outcome

    def probability(self, d: Direction) -> float:
        return {Direction.UP: self.p_up, Direction.FLAT: self.p_flat, Direction.DOWN: self.p_down}[d]


@dataclass(frozen=True)
class Interval:
    value: float
    low: float | None  # None when one value cannot tell how sure it is
    high: float | None
    n: int


@dataclass(frozen=True)
class Profit:
    trades: int
    total: float  # ticks
    per_trade: Interval | None


@dataclass(frozen=True)
class CalibrationBin:
    low: float
    high: float
    count: int
    mean_p: float | None
    observed: Interval | None  # how often the class happened


@dataclass(frozen=True)
class Summary:
    n: int
    accuracy: Interval | None
    precision: Interval | None
    coverage: Interval | None
    brier: Interval | None
    pnl: Profit
    calibration: list[CalibrationBin]


@dataclass(frozen=True)
class Lift:
    n: int
    value: float | None
    low: float | None
    high: float | None
    p: float | None  # one-sided: the chance of a lift this large with none really


def scored_by_algorithm(rows: Sequence[Mapping[str, Any]], basis: str) -> dict[str, list[Scored]]:
    """Each algorithm's stored predictions (``PredictionStore.scored_predictions``) scored
    on ``basis`` (``a`` or ``b``); those its basis could not score are left out.
    """
    out: dict[str, list[Scored]] = {}
    for r in rows:
        outcome = r["class_a"] if basis == "a" else r["class_b"]
        start = r["mid_close"] if basis == "a" else r["mid_display"]
        if outcome is None or start is None or r["mid_target"] is None:
            continue
        out.setdefault(r["algorithm"], []).append(
            Scored(
                algorithm=r["algorithm"],
                model_version=r["model_version"],
                symbol=r["symbol"],
                bar_close=datetime.fromisoformat(r["bar_close"]),
                p_up=r["p_up"],
                p_flat=r["p_flat"],
                p_down=r["p_down"],
                outcome=Direction(outcome),
                start_mid=start,
                target_mid=r["mid_target"],
            )
        )
    return out


def split(
    by_algorithm: Mapping[str, list[Scored]], baseline_names: Collection[str]
) -> tuple[dict[str, list[Scored]], dict[str, list[Scored]]]:
    """The algorithms' records, and the baselines' (every one named, empty if none)."""
    baselines = {name: list(by_algorithm.get(name, [])) for name in sorted(baseline_names)}
    algorithms = {name: records for name, records in by_algorithm.items() if name not in baseline_names}
    return algorithms, baselines


def wilson(successes: int, n: int) -> Interval | None:
    if n == 0:
        return None
    p = successes / n
    z2 = _Z**2
    denominator = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denominator
    half = _Z * math.sqrt(p * (1 - p) / n + z2 / (4 * n**2)) / denominator
    return Interval(p, max(0.0, center - half), min(1.0, center + half), n)


def mean_interval(values: Sequence[float]) -> Interval | None:
    n = len(values)
    if n == 0:
        return None
    mean = sum(values) / n
    if n == 1:
        return Interval(mean, None, None, 1)
    half = _Z * _standard_error(values, mean)
    return Interval(mean, mean - half, mean + half, n)


def _standard_error(values: Sequence[float], mean: float) -> float:
    n = len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1)) / math.sqrt(n)


def summarize(records: Sequence[Scored], fee_rate: float) -> Summary:
    directional = [r for r in records if r.predicted is not Direction.FLAT]
    trades = [_profit(r, fee_rate) for r in directional]
    return Summary(
        n=len(records),
        accuracy=wilson(sum(r.correct for r in records), len(records)),
        precision=wilson(sum(r.correct for r in directional), len(directional)),
        coverage=wilson(len(directional), len(records)),
        brier=mean_interval([_brier(r) for r in records]),
        pnl=Profit(len(trades), sum(trades), mean_interval(trades)),
        calibration=_calibration(records),
    )


def _brier(r: Scored) -> float:
    return sum((r.probability(d) - (d is r.outcome)) ** 2 for d in Direction)


def _profit(r: Scored, fee_rate: float) -> float:
    tick = tick_size(r.start_mid)
    move = (r.target_mid - r.start_mid) / tick
    cost = r.start_mid * (SELL_TAX + 2 * fee_rate) / tick
    return (move if r.predicted is Direction.UP else -move) - cost


def _calibration(records: Sequence[Scored]) -> list[CalibrationBin]:
    sums = [[0, 0.0, 0] for _ in range(CALIBRATION_BINS)]  # count, sum of p, happened
    for r in records:
        for d in Direction:
            p = r.probability(d)
            i = min(int(round(p * CALIBRATION_BINS, 9)), CALIBRATION_BINS - 1)
            sums[i][0] += 1
            sums[i][1] += p
            sums[i][2] += d is r.outcome
    width = 1 / CALIBRATION_BINS
    return [
        CalibrationBin(
            low=i * width,
            high=(i + 1) * width,
            count=int(count),
            mean_p=total / count if count else None,
            observed=wilson(int(happened), int(count)),
        )
        for i, (count, total, happened) in enumerate(sums)
    ]


def lift(records: Sequence[Scored], baseline: Sequence[Scored]) -> Lift:
    """Accuracy gained over ``baseline`` on the bar closes both predicted."""
    theirs = {(b.symbol, b.bar_close): b.correct for b in baseline}
    gains = [float(r.correct) - theirs[key] for r in records if (key := (r.symbol, r.bar_close)) in theirs]
    interval = mean_interval(gains)
    if interval is None:
        return Lift(0, None, None, None, None)
    if interval.n < MIN_PAIRS_TESTED:
        return Lift(interval.n, interval.value, interval.low, interval.high, None)
    error = _standard_error(gains, interval.value)
    if error == 0:
        p = 0.0 if interval.value > 0 else 1.0
    else:
        p = 1 - NormalDist().cdf(interval.value / error)
    return Lift(interval.n, interval.value, interval.low, interval.high, p)


def benjamini_hochberg(p_values: Sequence[float]) -> list[float]:
    """q-values: each p-value adjusted for the false discovery rate, in the given order."""
    m = len(p_values)
    order = sorted(range(m), key=lambda i: p_values[i])
    q = [0.0] * m
    running = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        running = min(running, p_values[i] * m / rank)
        q[i] = running
    return q


@dataclass(frozen=True)
class Tested:
    """A lift with its false-discovery-rate adjustment, when it was tested with others."""

    lift: Lift
    q: float | None = None

    @property
    def significant(self) -> bool | None:
        """None when it was not tested with the others (a breakdown, or not a candidate)."""
        if self.q is None:
            return None
        return self.q <= FDR and (self.lift.value or 0) > 0


@dataclass(frozen=True)
class Entry:
    algorithm: str
    model_versions: list[str]
    summary: Summary
    lifts: dict[str, Tested]  # by baseline; empty for a baseline
    rank: int | None = None

    @property
    def lift_low(self) -> float | None:
        """The lower bound of the weakest lift: better than every baseline, how surely."""
        lows = [t.lift.low for t in self.lifts.values()]
        if not lows or any(low is None for low in lows):
            return None
        return min(low for low in lows if low is not None)

    @property
    def weakest_lift(self) -> float | None:
        """The weakest lift: better than every baseline, by how much at least."""
        values = [t.lift.value for t in self.lifts.values()]
        if not values or any(v is None for v in values):
            return None
        return min(v for v in values if v is not None)


@dataclass(frozen=True)
class Leaderboard:
    ranked: list[Entry]
    unranked: list[Entry]  # too few scored predictions, or no lift to rank by
    baselines: list[Entry]


def entry(
    algorithm: str, records: Sequence[Scored], baselines: dict[str, list[Scored]], fee_rate: float
) -> Entry:
    """One algorithm's (or baseline's) stats, with its lift over each baseline."""
    lifts = (
        {} if algorithm in baselines else {name: Tested(lift(records, b)) for name, b in baselines.items()}
    )
    versions = sorted({r.model_version for r in records})
    return Entry(algorithm, versions, summarize(records, fee_rate), lifts)


def enough(e: Entry, min_sample: int) -> bool:
    """Whether ``e`` has the scored predictions, and pairs with each baseline, to be judged."""
    return (
        bool(e.lifts) and e.summary.n >= min_sample and all(t.lift.n >= min_sample for t in e.lifts.values())
    )


def leaderboard(
    by_algorithm: dict[str, list[Scored]],
    baselines: dict[str, list[Scored]],
    min_sample: int,
    fee_rate: float,
) -> Leaderboard:
    """Algorithms ranked by the lower bound of their weakest lift.

    The candidates have at least ``min_sample`` scored predictions, and as many pairs with
    each baseline; their lifts, and only theirs, share one FDR adjustment.
    """
    raw = [entry(name, records, baselines, fee_rate) for name, records in sorted(by_algorithm.items())]
    candidates = {e.algorithm for e in raw if enough(e, min_sample)}
    tests = [
        (e.algorithm, name, t.lift.p)
        for e in raw
        if e.algorithm in candidates
        for name, t in e.lifts.items()
        if t.lift.p is not None
    ]
    q_values = {
        (a, b): q for (a, b, _), q in zip(tests, benjamini_hochberg([p for *_, p in tests]), strict=True)
    }
    adjusted = [
        Entry(
            e.algorithm,
            e.model_versions,
            e.summary,
            {name: Tested(t.lift, q_values.get((e.algorithm, name))) for name, t in e.lifts.items()},
        )
        for e in raw
    ]
    eligible = [e for e in adjusted if e.algorithm in candidates and e.lift_low is not None]
    eligible.sort(key=lambda e: (-(e.lift_low or 0.0), e.algorithm))
    ranked = [Entry(e.algorithm, e.model_versions, e.summary, e.lifts, i + 1) for i, e in enumerate(eligible)]
    chosen = {e.algorithm for e in eligible}
    return Leaderboard(
        ranked,
        [e for e in adjusted if e.algorithm not in chosen],
        [entry(name, records, baselines, fee_rate) for name, records in sorted(baselines.items())],
    )
