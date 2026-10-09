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

Ratios come with 95% Wilson intervals, means with 95% normal intervals. Several lifts
tested at once are adjusted by Benjamini–Hochberg (false discovery rate).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from statistics import NormalDist

from krx_quant.pricing import SELL_TAX, Direction, tick_size

_Z = NormalDist().inv_cdf(0.975)
FDR = 0.05  # the false discovery rate a significant lift is held to
CALIBRATION_BINS = 10


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
        best = max(self.p_up, self.p_flat, self.p_down)
        if self.p_flat == best:
            return Direction.FLAT
        return Direction.UP if self.p_up == best else Direction.DOWN

    @property
    def correct(self) -> bool:
        return self.predicted is self.outcome

    def probability(self, d: Direction) -> float:
        return {Direction.UP: self.p_up, Direction.FLAT: self.p_flat, Direction.DOWN: self.p_down}[d]


@dataclass(frozen=True)
class Interval:
    value: float
    low: float
    high: float
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
    observed: float | None


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


def wilson(successes: int, n: int) -> Interval | None:
    if n == 0:
        return None
    p = successes / n
    z2 = _Z**2
    denominator = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denominator
    half = _Z * math.sqrt(p * (1 - p) / n + z2 / (4 * n**2)) / denominator
    return Interval(p, center - half, center + half, n)


def mean_interval(values: Sequence[float]) -> Interval | None:
    n = len(values)
    if n == 0:
        return None
    mean = sum(values) / n
    if n == 1:
        return Interval(mean, mean, mean, 1)
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
            observed=happened / count if count else None,
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
    if interval.n < 2:
        return Lift(interval.n, interval.value, None, None, None)
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
    def significant(self) -> bool:
        return self.q is not None and self.q <= FDR and (self.lift.value or 0) > 0


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


def leaderboard(
    by_algorithm: dict[str, list[Scored]],
    baselines: dict[str, list[Scored]],
    min_sample: int,
    fee_rate: float,
) -> Leaderboard:
    """Algorithms ranked by the lower bound of their weakest lift, among those with at least
    ``min_sample`` scored predictions; every lift tested here shares one FDR adjustment.
    """
    entries = [entry(name, records, baselines, fee_rate) for name, records in sorted(by_algorithm.items())]
    tests = [(e, name) for e in entries for name, t in e.lifts.items() if t.lift.p is not None]
    q_values = benjamini_hochberg([e.lifts[name].lift.p or 0.0 for e, name in tests])
    for (e, name), q in zip(tests, q_values, strict=True):
        e.lifts[name] = Tested(e.lifts[name].lift, q)
    eligible = [e for e in entries if e.summary.n >= min_sample and e.lift_low is not None]
    eligible.sort(key=lambda e: (-(e.lift_low or 0.0), e.algorithm))
    ranked = [Entry(e.algorithm, e.model_versions, e.summary, e.lifts, i + 1) for i, e in enumerate(eligible)]
    chosen = {e.algorithm for e in eligible}
    unranked = [e for e in entries if e.algorithm not in chosen]
    return Leaderboard(
        ranked,
        unranked,
        [entry(name, records, baselines, fee_rate) for name, records in sorted(baselines.items())],
    )
