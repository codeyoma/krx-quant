"""Stats Engine for the kinds besides the direction (#179): how good a kind's predictions
were, with luck accounted for, as for the direction's (``stats.py``).

Each scored prediction is judged on its bar close's answer of the kind (``kinds.py``),
every predictor of the kind there on the same one. Its predicted class is the most likely
one; a tie goes to the middle class, then the first.

- **accuracy**: the predicted class happened (3 classes);
- **precision**: of the calls of the kind's action classes, the share that happened;
  **coverage**: the share of predictions that called one of them (the direction's are up
  and down; the range's the small and the large moves);
- **Brier score** and **calibration**, over the kind's three classes, pooled and by class;
- **lift** over each of the kind's baselines, paired by symbol and bar close and by trading
  day, adjusted for the false discovery rate on the leaderboard (``stats.lift``,
  ``stats.day_lift``, ``stats.q_values``: the same code as the direction's);
- the kind's own statistics (``Kind.stats``), beside these, given the kind's baselines'
  records to compare against (the range's pinball loss against its climate's, #180).

There is no touch P&L: it is the direction's. The lifecycle judges a kind's algorithms by
these, against the kind's baselines, within the kind (``kind_scoring``).

A kind whose windows run several sessions (#310, ``Kind.overlap``: a daily kind's window of
five) has two days' windows share sessions, so its accuracy, precision, coverage and Brier score
have Driscoll–Kraay intervals (``stats.overlap_interval``, not Wilson's), its paired lifts the
same (``stats.overlap_lift``), and its lifts by trading day a Newey–West one
(``stats.overlap_day_lift``), each over about the independent windows (``stats.overlap_df``). The
days are placed among the trading days any of the predictors given predicted on
(``stats.session_index``), so a day one did not predict is a blank. The calibration bins keep
their Wilson intervals: they are not corrected for the overlap. Every entry says how many
sessions its kind's windows run (``overlap_sessions``) and about how many independent windows
its days hold (``independent_days``).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any

from krx_quant.feeds.base import kst_day
from krx_quant.kinds import Kind, KindScored
from krx_quant.lifecycle import Scoring
from krx_quant.market_calendar import MarketCalendar
from krx_quant.stats import (
    CALIBRATION_BINS,
    INDEPENDENT,
    CalibrationBin,
    DayOverlap,
    Interval,
    Lift,
    Lifted,
    Sessions,
    Tested,
    day_lift,
    enough,
    independent_days,
    interval_json,
    lift,
    lift_low,
    lifts_json,
    mean_interval,
    overlap_day_lift,
    overlap_interval,
    overlap_lift,
    q_values,
    session_index,
    weakest_by_day,
    weakest_lift,
    wilson,
)


@dataclass(frozen=True)
class KindSummary:
    n: int
    accuracy: Interval | None
    precision: Interval | None  # of the action classes' calls
    coverage: Interval | None  # the share of the action classes' calls
    brier: Interval | None
    calibration: list[CalibrationBin]  # the three classes pooled
    calibration_by_class: dict[str, list[CalibrationBin]]  # by class name, in the kind's order
    extra: dict[str, Any] | None  # the kind's own statistics; None for a kind without
    days: int = 0  # the trading days with a scored prediction
    overlap: int = 1  # the sessions its kind's windows run (#310, ``Kind.overlap``)


@dataclass(frozen=True)
class KindEntry:
    """One predictor's statistics of a kind, with its lifts over the kind's baselines."""

    algorithm: str
    model_versions: list[str]
    summary: KindSummary
    lifts: dict[str, Tested]  # by baseline; empty for a baseline
    rank: int | None = None

    @property
    def lift_low(self) -> float | None:
        return lift_low(self.lifts)

    @property
    def weakest_by_day(self) -> Lift | None:
        return weakest_by_day(self.lifts)

    @property
    def day_low(self) -> float | None:
        weakest = self.weakest_by_day
        return None if weakest is None else weakest.low

    @property
    def weakest_lift(self) -> float | None:
        return weakest_lift(self.lifts)


@dataclass(frozen=True)
class KindBoard:
    ranked: list[KindEntry]
    unranked: list[KindEntry]
    baselines: list[KindEntry]


def scored_of_kind(rows: Sequence[Mapping[str, Any]], kind: Kind) -> dict[str, list[KindScored]]:
    """Each predictor's stored predictions of ``kind`` (``PredictionStore.kind_scored``) scored
    on their bar close's answer; those without an answer class are left out."""
    out: dict[str, list[KindScored]] = {}
    for r in rows:
        label, output = r.get("answer_class"), r.get("output")
        if label is None or output is None:
            continue
        out.setdefault(r["algorithm"], []).append(
            KindScored(
                algorithm=r["algorithm"],
                model_version=r["model_version"],
                symbol=r["symbol"],
                bar_close=datetime.fromisoformat(r["bar_close"]),
                probabilities=kind.probabilities(output),
                outcome=kind.index(label),
                output=output,
                path=r.get("answer_path") or {},
            )
        )
    return out


def split(
    by_algorithm: Mapping[str, list[KindScored]], baseline_names: Collection[str]
) -> tuple[dict[str, list[KindScored]], dict[str, list[KindScored]]]:
    """The algorithms' records, and the baselines' (every one named, empty if none)."""
    baselines = {name: list(by_algorithm.get(name, [])) for name in sorted(baseline_names)}
    algorithms = {name: records for name, records in by_algorithm.items() if name not in baseline_names}
    return algorithms, baselines


def sessions_of(
    records: Sequence[KindScored],
    baselines: Mapping[str, Sequence[KindScored]] | None = None,
    calendar: MarketCalendar | None = None,
) -> dict[date, int]:
    """The KRX sessions an overlapping kind's statistics pair days by (#310), each by its place
    (``stats.session_index``): the trading days the ``calendar`` has from KIS over the days
    ``records`` and the ``baselines``' were made on, and those days themselves (KIS's calendar
    keeps a year: the walk-forward's older days are those any predictor has). A day left out of
    the statistics (special, unscorable) stays a session, so its neighbours keep their distance."""
    groups = [records, *(baselines or {}).values()]
    days = {kst_day(r.bar_close) for group in groups for r in group}
    if calendar is not None and days:
        days.update(calendar.trading_days(min(days), max(days)))
    return session_index(days)


def _by_day(records: Sequence[KindScored], value: Callable[[KindScored], float]) -> dict[date, list[float]]:
    out: dict[date, list[float]] = {}
    for r in records:
        out.setdefault(kst_day(r.bar_close), []).append(value(r))
    return out


def summarize(
    records: Sequence[KindScored],
    kind: Kind,
    baselines: Mapping[str, Sequence[KindScored]] | None = None,
    algorithm: str = "",
    sessions: Sessions | None = None,
) -> KindSummary:
    """A predictor's (``algorithm``'s) statistics of ``kind``; its own (``Kind.stats``) with the
    kind's ``baselines``' records beside, to compare against (#180). A kind whose windows overlap
    (#310) has Driscoll–Kraay intervals, its own statistics' too (``stats.DayOverlap``), its days
    placed among ``sessions`` (default: every day of ``records`` and the ``baselines``')."""
    action = {kind.index(c) for c in kind.action}
    calls = [r for r in records if r.predicted in action]
    days = len({kst_day(r.bar_close) for r in records})
    overlap = INDEPENDENT
    if kind.overlap > 1:
        at = sessions if sessions is not None else sessions_of(records, baselines)
        overlap = DayOverlap(kind.overlap, at)

        def interval(
            group: Sequence[KindScored], value: Callable[[KindScored], float], ratio: bool
        ) -> Interval | None:
            return overlap_interval(_by_day(group, value), at, kind.overlap, ratio=ratio)

        accuracy = interval(records, lambda r: float(r.correct), True)
        precision = interval(calls, lambda r: float(r.correct), True)
        coverage = interval(records, lambda r: float(r.predicted in action), True)
        brier = interval(records, _brier, False)
    else:
        accuracy = wilson(sum(r.correct for r in records), len(records))
        precision = wilson(sum(r.correct for r in calls), len(calls))
        coverage = wilson(len(calls), len(records))
        brier = mean_interval([_brier(r) for r in records])
    return KindSummary(
        n=len(records),
        accuracy=accuracy,
        precision=precision,
        coverage=coverage,
        brier=brier,
        calibration=_calibration(records, (0, 1, 2)),
        calibration_by_class={c: _calibration(records, (i,)) for i, c in enumerate(kind.classes)},
        extra=None if kind.stats is None else kind.stats(algorithm, records, baselines or {}, overlap),
        days=days,
        overlap=kind.overlap,
    )


def _brier(r: KindScored) -> float:
    return sum((p - (i == r.outcome)) ** 2 for i, p in enumerate(r.probabilities))


def _calibration(records: Sequence[KindScored], classes: Sequence[int]) -> list[CalibrationBin]:
    """The probabilities of ``classes`` binned by tenths, against how often each happened
    (``stats._calibration``, by the class's place)."""
    sums = [[0, 0.0, 0] for _ in range(CALIBRATION_BINS)]  # count, sum of p, happened
    for r in records:
        for c in classes:
            p = r.probabilities[c]
            i = min(int(round(p * CALIBRATION_BINS, 9)), CALIBRATION_BINS - 1)
            sums[i][0] += 1
            sums[i][1] += p
            sums[i][2] += c == r.outcome
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


def entry(
    algorithm: str,
    records: Sequence[KindScored],
    baselines: dict[str, list[KindScored]],
    kind: Kind,
    day_level: float = 0.95,
    calendar: MarketCalendar | None = None,
) -> KindEntry:
    """One predictor's stats of ``kind``, with its lift over each of the kind's baselines; by
    trading day, a ``day_level`` interval. A kind whose windows overlap (#310) pairs the days
    among the KRX sessions over every day ``records`` and the ``baselines``' were made on, the
    ``calendar``'s trading days among them (``sessions_of``)."""
    sessions = sessions_of(records, baselines, calendar) if kind.overlap > 1 else None

    def tested(b: Sequence[KindScored]) -> Tested:
        if sessions is None:
            return Tested(lift(records, b), by_day=day_lift(records, b, day_level))
        return Tested(
            overlap_lift(records, b, sessions, kind.overlap),
            by_day=overlap_day_lift(records, b, sessions, kind.overlap, day_level),
        )

    lifts = {} if algorithm in baselines else {name: tested(b) for name, b in baselines.items()}
    versions = sorted({r.model_version for r in records})
    summary = summarize(records, kind, baselines, algorithm, sessions)
    return KindEntry(algorithm, versions, summary, lifts)


def leaderboard(
    by_algorithm: dict[str, list[KindScored]],
    baselines: dict[str, list[KindScored]],
    min_sample: int,
    kind: Kind,
    calendar: MarketCalendar | None = None,
) -> KindBoard:
    """A kind's algorithms ranked by the lower bound of their weakest lift, as the direction's
    (``stats.leaderboard``): the candidates have at least ``min_sample`` scored predictions
    and as many pairs with each of the kind's baselines; their lifts share one FDR adjustment.
    An overlapping kind's days are placed among the ``calendar``'s sessions too (#310, ``entry``).
    """
    raw = [
        entry(name, records, baselines, kind, calendar=calendar)
        for name, records in sorted(by_algorithm.items())
    ]
    candidates = {e.algorithm for e in raw if enough(e, min_sample)}
    adjusted_q = q_values(raw, candidates)
    adjusted = [
        replace(e, lifts={n: replace(t, q=adjusted_q.get((e.algorithm, n))) for n, t in e.lifts.items()})
        for e in raw
    ]
    eligible = [e for e in adjusted if e.algorithm in candidates and e.lift_low is not None]
    eligible.sort(key=lambda e: (-(e.lift_low or 0.0), e.algorithm))
    ranked = [replace(e, rank=i + 1) for i, e in enumerate(eligible)]
    chosen = {e.algorithm for e in eligible}
    return KindBoard(
        ranked,
        [e for e in adjusted if e.algorithm not in chosen],
        [
            entry(name, records, baselines, kind, calendar=calendar)
            for name, records in sorted(baselines.items())
        ],
    )


def entry_json(e: KindEntry) -> dict[str, Any]:
    """A kind's entry as the API shows it: the direction's fields without the touch P&L, the
    kind's own statistics (``extra``), and its windows' overlap (#310): the sessions they run and
    about how many independent windows its days hold."""
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
        "extra": s.extra,
        "overlap_sessions": s.overlap,  # #310
        "independent_days": independent_days(s.days, s.overlap),
    }
    if e.lifts:
        out["lift"] = lifts_json(e)
    return out


def kind_scoring(kind: Kind, calendar: MarketCalendar | None = None) -> Scoring:
    """How the lifecycle judges ``kind``'s algorithms (#179): their stats against the kind's
    baselines, a clone by the first class's probability less the third's, and only the
    predictions of models trained on the kind's answers (its answer version). An overlapping
    kind's days are placed among the ``calendar``'s sessions too (#310): a day the review leaves
    out (special, unscorable) is a gap, not a neighbour."""
    first, _, third = kind.p_keys()

    def measure(
        name: str, rows: list[dict[str, Any]], baselines: Collection[str], fee_rate: float, level: float
    ) -> Lifted:
        algorithms, scored = split(scored_of_kind(rows, kind), baselines)
        return entry(name, algorithms.get(name, []), scored, kind, level, calendar)

    def lean(row: Mapping[str, Any]) -> float:
        output = row["output"]
        return float(output[first]) - float(output[third])

    return Scoring(measure, lean, f"{first} - {third}", kind.answer_version)
