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
  class happened; pooled over the three classes, and each class on its own (pooled, a
  forecast of 1/3 each when only rises happen looks perfect; on its own, up does not);
- **lift**: the gain in accuracy over a baseline, paired by symbol and bar close, with a
  one-sided p-value for "no better than the baseline".

Ratios come with 95% Wilson intervals, means with 95% normal intervals (none from a
single value). A lift is tested only on enough pairs for the normal approximation; the
lifts of the algorithms a leaderboard could rank are adjusted together by
Benjamini–Hochberg (false discovery rate).

These intervals count every prediction as independent of the others. They are not: a
four-minute outcome overlaps the next three bar closes', and the symbols move with the
market. So each lift is also taken **by trading day**: a day's mean gain is one value, and
the days' mean has a Student t interval and one-sided p-value (none from fewer than two
days). The statuses (``lifecycle.py``) are decided by these; the paired ones only describe.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from functools import cache
from statistics import NormalDist
from typing import Any

from krx_quant.feeds.base import kst_day
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
    calibration: list[CalibrationBin]  # the three classes pooled
    calibration_by_class: dict[Direction, list[CalibrationBin]]


@dataclass(frozen=True)
class Lift:
    n: int  # the pairs; the days for a lift by trading day (day_lift)
    value: float | None
    low: float | None
    high: float | None
    p: float | None  # one-sided: the chance of a lift this large with none really


# The touch label (#160) as the basis: pre-validation with the touch target (#169).
TOUCH_BASIS = "touch"


def scored_by_algorithm(rows: Sequence[Mapping[str, Any]], basis: str) -> dict[str, list[Scored]]:
    """Each algorithm's stored predictions (``PredictionStore.scored_predictions``) scored
    on ``basis`` (``a`` or ``b``; or ``TOUCH_BASIS``, the bar close's touch label, its
    mid-points basis A's); those its basis could not score are left out.
    """
    out: dict[str, list[Scored]] = {}
    for r in rows:
        if basis == TOUCH_BASIS:
            outcome, start = r.get("touch_class"), r["mid_close"]
        else:
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
        calibration=_calibration(records, tuple(Direction)),
        calibration_by_class={d: _calibration(records, (d,)) for d in Direction},
    )


def _brier(r: Scored) -> float:
    return sum((r.probability(d) - (d is r.outcome)) ** 2 for d in Direction)


def _profit(r: Scored, fee_rate: float) -> float:
    tick = tick_size(r.start_mid)
    move = (r.target_mid - r.start_mid) / tick
    cost = r.start_mid * (SELL_TAX + 2 * fee_rate) / tick
    return (move if r.predicted is Direction.UP else -move) - cost


def _calibration(records: Sequence[Scored], classes: Sequence[Direction]) -> list[CalibrationBin]:
    """The probabilities of ``classes`` binned by tenths, against how often each happened."""
    sums = [[0, 0.0, 0] for _ in range(CALIBRATION_BINS)]  # count, sum of p, happened
    for r in records:
        for d in classes:
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


def _gains(records: Sequence[Scored], baseline: Sequence[Scored]) -> Iterator[tuple[datetime, float]]:
    """Each bar close both predicted, and the accuracy gained over ``baseline`` there (1, 0 or -1)."""
    theirs = {(b.symbol, b.bar_close): b.correct for b in baseline}
    return (
        (r.bar_close, float(r.correct) - theirs[key])
        for r in records
        if (key := (r.symbol, r.bar_close)) in theirs
    )


def lift(records: Sequence[Scored], baseline: Sequence[Scored]) -> Lift:
    """Accuracy gained over ``baseline`` on the bar closes both predicted."""
    gains = [gain for _, gain in _gains(records, baseline)]
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


def day_lift(records: Sequence[Scored], baseline: Sequence[Scored], level: float = 0.95) -> Lift | None:
    """The lift over ``baseline`` taken a trading day at a time: each day's mean gain is one
    value, every day weighing the same, with a two-sided ``level`` interval. Its ``n`` is the
    days; None from fewer than two. Days that all gained the same have no spread: the
    interval is that gain alone.
    """
    by_day: dict[date, list[float]] = {}
    for at, gain in _gains(records, baseline):
        by_day.setdefault(kst_day(at), []).append(gain)
    means = [sum(gains) / len(gains) for gains in by_day.values()]
    n = len(means)
    if n < 2:
        return None
    mean = sum(means) / n
    error = _standard_error(means, mean)
    half = _t_quantile(1 - (1 - level) / 2, n - 1) * error
    if error == 0:
        p = 0.0 if mean > 0 else 1.0
    else:
        p = _t_above(mean / error, n - 1)
    return Lift(n, mean, mean - half, mean + half, p)


def _t_above(x: float, df: int) -> float:
    """P(T > x) for Student's t with ``df`` degrees of freedom."""
    tail = 0.5 * _incomplete_beta(df / 2, 0.5, df / (df + x * x))
    return tail if x >= 0 else 1 - tail


@cache
def _t_quantile(q: float, df: int) -> float:
    """The ``q`` quantile (above the median) of Student's t with ``df`` degrees of freedom."""
    low, high = 0.0, 1.0
    while _t_above(high, df) > 1 - q:
        high *= 2
    for _ in range(100):
        middle = (low + high) / 2
        low, high = (middle, high) if _t_above(middle, df) > 1 - q else (low, middle)
    return (low + high) / 2


def _incomplete_beta(a: float, b: float, x: float) -> float:
    """The regularized incomplete beta function I_x(a, b), by its continued fraction."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    log_beta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    front = math.exp(a * math.log(x) + b * math.log1p(-x) - log_beta)
    if x < (a + 1) / (a + b + 2):
        return front * _beta_fraction(a, b, x) / a
    return 1 - front * _beta_fraction(b, a, 1 - x) / b


def _beta_fraction(a: float, b: float, x: float) -> float:
    """The incomplete beta function's continued fraction, by Lentz's method."""
    tiny = 1e-300

    def step(value: float) -> float:
        return value if abs(value) > tiny else tiny

    c, d = 1.0, 1 / step(1 - (a + b) * x / (a + 1))
    fraction = d
    for m in range(1, 300):
        for numerator in (
            m * (b - m) * x / ((a + 2 * m - 1) * (a + 2 * m)),
            -(a + m) * (a + b + m) * x / ((a + 2 * m) * (a + 2 * m + 1)),
        ):
            d = 1 / step(1 + numerator * d)
            c = step(1 + numerator / c)
            fraction *= d * c
        if abs(d * c - 1) < 1e-15:
            break
    return fraction


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
    """A lift with its false-discovery-rate adjustment, when it was tested with others, and
    the same lift by trading day."""

    lift: Lift
    q: float | None = None
    by_day: Lift | None = None  # the same lift taken by trading day (day_lift)

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
    def weakest_by_day(self) -> Lift | None:
        """The lift by trading day with the lowest lower bound: better than every baseline,
        day after day, how surely. None unless every baseline has one."""
        by_day = [t.by_day for t in self.lifts.values()]
        lows = [(d.low, d) for d in by_day if d is not None and d.low is not None]
        if not by_day or len(lows) < len(by_day):
            return None
        return min(lows, key=lambda pair: pair[0])[1]

    @property
    def day_low(self) -> float | None:
        """The lower bound of the weakest lift by trading day: what the statuses go by."""
        weakest = self.weakest_by_day
        return None if weakest is None else weakest.low

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
    algorithm: str,
    records: Sequence[Scored],
    baselines: dict[str, list[Scored]],
    fee_rate: float,
    day_level: float = 0.95,
) -> Entry:
    """One algorithm's (or baseline's) stats, with its lift over each baseline; by trading
    day, a ``day_level`` interval."""
    lifts = (
        {}
        if algorithm in baselines
        else {
            name: Tested(lift(records, b), by_day=day_lift(records, b, day_level))
            for name, b in baselines.items()
        }
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
            {name: replace(t, q=q_values.get((e.algorithm, name))) for name, t in e.lifts.items()},
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


def interval_json(i: Interval | None) -> dict[str, Any] | None:
    return None if i is None else {"value": i.value, "low": i.low, "high": i.high, "n": i.n}


def entry_json(e: Entry) -> dict[str, Any]:
    s = e.summary
    out: dict[str, Any] = {
        "rank": e.rank,
        "algorithm": e.algorithm,
        "model_versions": e.model_versions,
        "n": s.n,
        "accuracy": interval_json(s.accuracy),
        "precision": interval_json(s.precision),
        "coverage": interval_json(s.coverage),
        "brier": interval_json(s.brier),
        "pnl": {
            "trades": s.pnl.trades,
            "total_ticks": s.pnl.total,
            "per_trade": interval_json(s.pnl.per_trade),
        },
    }
    if e.lifts:
        out["lift"] = {
            "low": e.lift_low,
            "day_low": e.day_low,
            "vs": {
                name: {
                    **_lift_json(t.lift),
                    "q": t.q,
                    "significant": t.significant,
                    "by_day": None if t.by_day is None else _lift_json(t.by_day),
                }
                for name, t in e.lifts.items()
            },
        }
    return out


def _lift_json(lift: Lift) -> dict[str, Any]:
    return {"n": lift.n, "value": lift.value, "low": lift.low, "high": lift.high, "p": lift.p}
