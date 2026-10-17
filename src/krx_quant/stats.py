"""Stats Engine: how good the predictions were, with luck accounted for.

Each scored prediction is judged on its bar close's touch label (#160, #170; ``touch.py``):
up when the best bid reached the bar close's mid-point plus k ticks within the four
minutes, else down when the best ask reached it minus k ticks, else flat. Every predictor
of a bar close shares it, however late it was displayed. Its predicted class is the most
likely one; a tie goes to flat, then up.

- **accuracy**: the predicted class happened (3 classes);
- **precision**: of the up and down calls, the share that happened; **coverage**: the
  share of predictions that called a direction;
- **Brier score**: the squared distance between the probabilities and what happened,
  summed over the three classes (0 is perfect, 2 is sure and wrong);
- **profit** (the touch P&L, research only): each up or down call traded by the label's
  own rule (``touch_profit``), in ticks, less the round-trip cost (sell tax and both
  commissions: a down call as a short of the stock);
- **profit, inverse** (#267, research only): the same, a down call traded through the
  symbol's single-stock inverse product (``pricing.INVERSE_PRODUCTS``), which pays no sell
  tax: less both commissions on its notional only. A down call on a symbol without one, or
  before it was listed, is no trade;
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

The lifts, their adjustment and the ranking hold for any kind's predictions (#179): a kind's
statistics (``kind_stats.py``) pair its records and rank its entries with the same code.
"""

from __future__ import annotations

import json
import math
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from functools import cache
from statistics import NormalDist
from typing import Any, Protocol, TypeVar

from krx_quant.feeds.base import kst_day
from krx_quant.pricing import INVERSE_PRODUCTS, SELL_TAX, Direction, most_likely

_Z = NormalDist().inv_cdf(0.975)
FDR = 0.05  # the false discovery rate a significant lift is held to
CALIBRATION_BINS = 10
# Fewer pairs than this are not tested: the normal approximation would be too rough.
MIN_PAIRS_TESTED = 30
# Scored predictions (and pairs with each baseline) an algorithm needs to be ranked.
DEFAULT_MIN_SAMPLE = 200


@dataclass(frozen=True)
class Prices:
    """What the touch P&L of a bar close reads from its touch path (#170, ``touch.touch_path``):
    the targets and whether each was reached, the best quotes at the bar close and at the end
    of its four minutes, the tick and the reference price (the mid-point the targets are set
    from) the cost is taken at."""

    reference: float
    tick: float
    upper: float
    lower: float
    upper_reached: bool
    lower_reached: bool
    bid_start: float | None
    ask_start: float | None
    bid_end: float | None
    ask_end: float | None

    @classmethod
    def of(cls, path: Mapping[str, Any]) -> Prices | None:
        """The prices of a stored touch path; None without its targets."""
        if None in (path.get("anchor"), path.get("tick"), path.get("upper"), path.get("lower")):
            return None
        return cls(
            reference=path["anchor"],
            tick=path["tick"],
            upper=path["upper"],
            lower=path["lower"],
            upper_reached=path.get("upper_at") is not None,
            lower_reached=path.get("lower_at") is not None,
            bid_start=path.get("bid_start"),
            ask_start=path.get("ask_start"),
            bid_end=path.get("bid_end"),
            ask_end=path.get("ask_end"),
        )


@dataclass(frozen=True)
class Scored:
    """One scored prediction, on its bar close's touch label."""

    algorithm: str
    model_version: str
    symbol: str
    bar_close: datetime
    p_up: float
    p_flat: float
    p_down: float
    outcome: Direction
    prices: Prices | None = None  # its bar close's touch path, for the touch P&L

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
    pnl: Profit  # a down call as a short of the stock: the sell tax paid
    pnl_inverse: Profit  # a down call through an inverse product: no sell tax (#267)
    calibration: list[CalibrationBin]  # the three classes pooled
    calibration_by_class: dict[Direction, list[CalibrationBin]]


@dataclass(frozen=True)
class Lift:
    n: int  # the pairs; the days for a lift by trading day (day_lift)
    value: float | None
    low: float | None
    high: float | None
    p: float | None  # one-sided: the chance of a lift this large with none really


Row = TypeVar("Row", bound=Mapping[str, Any])  # a stored prediction, as the store reads it


class Paired(Protocol):
    """A scored prediction, as a lift pairs it: by symbol and bar close, right or wrong. Any
    kind's records (#179)."""

    @property
    def symbol(self) -> str: ...

    @property
    def bar_close(self) -> datetime: ...

    @property
    def correct(self) -> bool: ...


def scored_by_algorithm(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[Scored]]:
    """Each algorithm's stored predictions (``PredictionStore.scored_predictions``) scored on
    their bar close's touch label (#170), with its path's prices; those without a label are
    left out.
    """
    out: dict[str, list[Scored]] = {}
    prices: dict[str, Prices | None] = {}  # by stored path: every predictor of a bar close shares it
    for r in rows:
        outcome, path = r.get("touch_class"), r.get("touch_path")
        if outcome is None:
            continue
        if path is not None and path not in prices:
            prices[path] = Prices.of(json.loads(path))
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
                prices=None if path is None else prices[path],
            )
        )
    return out


def on_target(rows: Sequence[Row], baselines: Collection[str], target_version: int) -> list[Row]:
    """The stored predictions the statuses and the consensus weights go by (#170): the
    baselines', and the others' made by a model trained on ``target_version`` (the touch
    target they are scored on: ``model_target``, from ``model_targets``). An algorithm's
    predictions from before its first such model (the old target's, its defaults') are left
    out."""
    return [r for r in rows if r["algorithm"] in baselines or r.get("model_target") == target_version]


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
    trades = [p for r in directional if (p := touch_profit(r.predicted, r.prices, fee_rate)) is not None]
    inverse = [p for r in directional if (p := _inverse_profit(r, fee_rate)) is not None]
    return Summary(
        n=len(records),
        accuracy=wilson(sum(r.correct for r in records), len(records)),
        precision=wilson(sum(r.correct for r in directional), len(directional)),
        coverage=wilson(len(directional), len(records)),
        brier=mean_interval([_brier(r) for r in records]),
        pnl=Profit(len(trades), sum(trades), mean_interval(trades)),
        pnl_inverse=Profit(len(inverse), sum(inverse), mean_interval(inverse)),
        calibration=_calibration(records, tuple(Direction)),
        calibration_by_class={d: _calibration(records, (d,)) for d in Direction},
    )


def _brier(r: Scored) -> float:
    return sum((r.probability(d) - (d is r.outcome)) ** 2 for d in Direction)


def touch_profit(
    predicted: Direction, prices: Prices | None, fee_rate: float, *, inverse_leverage: int | None = None
) -> float | None:
    """The touch P&L of a call (#170, research only), in ticks, by the touch label's own rule:

    - up: bought at the bar close's best ask; sold at the upper target if the best bid
      reached it within the four minutes, else at the last best bid before they ended;
    - down: sold at the bar close's best bid; bought back at the lower target if the best
      ask reached it, else at the last best ask;
    - flat: no trade (None), as a call without the quotes it needs.

    Less the round-trip cost on the reference price, in ticks: the sell tax and both
    commissions, as a short of the stock; for a down call with ``inverse_leverage``, both
    commissions only, on the inverse product's notional (an ETF pays no sell tax, #267): the
    same exposure as a share costs 1/leverage of its price in a −leverage x product. The
    product's own spread, tracking and daily reset are left out. Every share fills at the
    best quote: queues, sizes and delays are left out."""
    if prices is None or predicted is Direction.FLAT:
        return None
    if predicted is Direction.UP:
        bought = prices.ask_start
        sold = prices.upper if prices.upper_reached else prices.bid_end
    else:
        sold = prices.bid_start
        bought = prices.lower if prices.lower_reached else prices.ask_end
    if bought is None or sold is None:
        return None
    if inverse_leverage and predicted is Direction.DOWN:
        cost = prices.reference * 2 * fee_rate / inverse_leverage
    else:
        cost = prices.reference * (SELL_TAX + 2 * fee_rate)
    return (sold - bought - cost) / prices.tick


def _inverse_profit(r: Scored, fee_rate: float) -> float | None:
    """A call's touch P&L with a down call traded through the symbol's inverse product; None
    for a down call on a symbol without one, or before it was listed (#267)."""
    product = INVERSE_PRODUCTS.get(r.symbol)
    if r.predicted is not Direction.DOWN:
        return touch_profit(r.predicted, r.prices, fee_rate)
    if product is None or kst_day(r.bar_close) < product.listed:
        return None
    return touch_profit(r.predicted, r.prices, fee_rate, inverse_leverage=product.leverage)


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


def _gains(records: Sequence[Paired], baseline: Sequence[Paired]) -> Iterator[tuple[datetime, float]]:
    """Each bar close both predicted, and the accuracy gained over ``baseline`` there (1, 0 or -1)."""
    theirs = {(b.symbol, b.bar_close): b.correct for b in baseline}
    return (
        (r.bar_close, float(r.correct) - theirs[key])
        for r in records
        if (key := (r.symbol, r.bar_close)) in theirs
    )


def lift(records: Sequence[Paired], baseline: Sequence[Paired]) -> Lift:
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


def day_lift(records: Sequence[Paired], baseline: Sequence[Paired], level: float = 0.95) -> Lift | None:
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


def day_mean(by_day: Mapping[date, Sequence[float]], level: float = 0.95) -> Interval | None:
    """A mean taken a trading day at a time (#180, a kind's own statistics): each day's mean
    is one value, every day weighing the same, with a two-sided ``level`` Student t interval
    over the days, as ``day_lift``'s. Its ``n`` is the days; None from fewer than two."""
    means = [sum(values) / len(values) for values in by_day.values() if values]
    n = len(means)
    if n < 2:
        return None
    mean = sum(means) / n
    half = _t_quantile(1 - (1 - level) / 2, n - 1) * _standard_error(means, mean)
    return Interval(mean, mean - half, mean + half, n)


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
        return lift_low(self.lifts)

    @property
    def weakest_by_day(self) -> Lift | None:
        """The lift by trading day with the lowest lower bound: better than every baseline,
        day after day, how surely. None unless every baseline has one."""
        return weakest_by_day(self.lifts)

    @property
    def day_low(self) -> float | None:
        """The lower bound of the weakest lift by trading day: what the statuses go by."""
        weakest = self.weakest_by_day
        return None if weakest is None else weakest.low

    @property
    def weakest_lift(self) -> float | None:
        """The weakest lift: better than every baseline, by how much at least."""
        return weakest_lift(self.lifts)


def lift_low(lifts: Mapping[str, Tested]) -> float | None:
    """The lower bound of the weakest of ``lifts`` (``Entry.lift_low``)."""
    lows = [t.lift.low for t in lifts.values()]
    if not lows or any(low is None for low in lows):
        return None
    return min(low for low in lows if low is not None)


def weakest_by_day(lifts: Mapping[str, Tested]) -> Lift | None:
    """The lift by trading day of ``lifts`` with the lowest lower bound (``Entry.weakest_by_day``)."""
    by_day = [t.by_day for t in lifts.values()]
    lows = [(d.low, d) for d in by_day if d is not None and d.low is not None]
    if not by_day or len(lows) < len(by_day):
        return None
    return min(lows, key=lambda pair: pair[0])[1]


def weakest_lift(lifts: Mapping[str, Tested]) -> float | None:
    """The weakest of ``lifts`` (``Entry.weakest_lift``)."""
    values = [t.lift.value for t in lifts.values()]
    if not values or any(v is None for v in values):
        return None
    return min(v for v in values if v is not None)


class Counted(Protocol):
    @property
    def n(self) -> int: ...


class Lifted(Protocol):
    """An entry of any kind's statistics (#179), as the leaderboard ranks it and the lifecycle
    judges it: how many it scored, and its lifts over the baselines."""

    @property
    def algorithm(self) -> str: ...

    @property
    def summary(self) -> Counted: ...

    @property
    def lifts(self) -> Mapping[str, Tested]: ...

    @property
    def lift_low(self) -> float | None: ...

    @property
    def weakest_by_day(self) -> Lift | None: ...

    @property
    def day_low(self) -> float | None: ...

    @property
    def weakest_lift(self) -> float | None: ...


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


def enough(e: Lifted, min_sample: int) -> bool:
    """Whether ``e`` has the scored predictions, and pairs with each baseline, to be judged."""
    return (
        bool(e.lifts) and e.summary.n >= min_sample and all(t.lift.n >= min_sample for t in e.lifts.values())
    )


def q_values(raw: Iterable[Lifted], candidates: Collection[str]) -> dict[tuple[str, str], float]:
    """The candidates' lift tests adjusted together for the false discovery rate: the q-value by
    (algorithm, baseline), of the lifts tested (with a p-value)."""
    tests = [
        (e.algorithm, name, t.lift.p)
        for e in raw
        if e.algorithm in candidates
        for name, t in e.lifts.items()
        if t.lift.p is not None
    ]
    return {(a, b): q for (a, b, _), q in zip(tests, benjamini_hochberg([p for *_, p in tests]), strict=True)}


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
    adjusted_q = q_values(raw, candidates)
    adjusted = [
        Entry(
            e.algorithm,
            e.model_versions,
            e.summary,
            {name: replace(t, q=adjusted_q.get((e.algorithm, name))) for name, t in e.lifts.items()},
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
        "pnl": _profit_json(s.pnl),
        "pnl_inverse": _profit_json(s.pnl_inverse),
    }
    if e.lifts:
        out["lift"] = lifts_json(e)
    return out


def _profit_json(p: Profit) -> dict[str, Any]:
    return {"trades": p.trades, "total_ticks": p.total, "per_trade": interval_json(p.per_trade)}


def lifts_json(e: Lifted) -> dict[str, Any]:
    """An entry's lifts as the API shows them (any kind's, #179)."""
    return {
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


def _lift_json(lift: Lift) -> dict[str, Any]:
    return {"n": lift.n, "value": lift.value, "low": lift.low, "high": lift.high, "p": lift.p}
