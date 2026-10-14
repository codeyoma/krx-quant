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
- the kind's own statistics (``Kind.stats``), beside these.

There is no touch P&L: it is the direction's. The lifecycle judges a kind's algorithms by
these, against the kind's baselines, within the kind (``kind_scoring``).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from krx_quant.kinds import Kind, KindScored
from krx_quant.lifecycle import Scoring
from krx_quant.stats import (
    CALIBRATION_BINS,
    CalibrationBin,
    Interval,
    Lift,
    Lifted,
    Tested,
    day_lift,
    enough,
    interval_json,
    lift,
    lift_low,
    lifts_json,
    mean_interval,
    q_values,
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


def summarize(records: Sequence[KindScored], kind: Kind) -> KindSummary:
    action = {kind.index(c) for c in kind.action}
    calls = [r for r in records if r.predicted in action]
    return KindSummary(
        n=len(records),
        accuracy=wilson(sum(r.correct for r in records), len(records)),
        precision=wilson(sum(r.correct for r in calls), len(calls)),
        coverage=wilson(len(calls), len(records)),
        brier=mean_interval([_brier(r) for r in records]),
        calibration=_calibration(records, (0, 1, 2)),
        calibration_by_class={c: _calibration(records, (i,)) for i, c in enumerate(kind.classes)},
        extra=None if kind.stats is None else kind.stats(records),
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
) -> KindEntry:
    """One predictor's stats of ``kind``, with its lift over each of the kind's baselines; by
    trading day, a ``day_level`` interval."""
    lifts = (
        {}
        if algorithm in baselines
        else {
            name: Tested(lift(records, b), by_day=day_lift(records, b, day_level))
            for name, b in baselines.items()
        }
    )
    versions = sorted({r.model_version for r in records})
    return KindEntry(algorithm, versions, summarize(records, kind), lifts)


def leaderboard(
    by_algorithm: dict[str, list[KindScored]],
    baselines: dict[str, list[KindScored]],
    min_sample: int,
    kind: Kind,
) -> KindBoard:
    """A kind's algorithms ranked by the lower bound of their weakest lift, as the direction's
    (``stats.leaderboard``): the candidates have at least ``min_sample`` scored predictions
    and as many pairs with each of the kind's baselines; their lifts share one FDR adjustment.
    """
    raw = [entry(name, records, baselines, kind) for name, records in sorted(by_algorithm.items())]
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
        [entry(name, records, baselines, kind) for name, records in sorted(baselines.items())],
    )


def entry_json(e: KindEntry) -> dict[str, Any]:
    """A kind's entry as the API shows it: the direction's fields without the touch P&L, and
    the kind's own statistics (``extra``)."""
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
    }
    if e.lifts:
        out["lift"] = lifts_json(e)
    return out


def kind_scoring(kind: Kind) -> Scoring:
    """How the lifecycle judges ``kind``'s algorithms (#179): their stats against the kind's
    baselines, a clone by the first class's probability less the third's, and only the
    predictions of models trained on the kind's answers (its answer version)."""
    first, _, third = kind.p_keys()

    def measure(
        name: str, rows: list[dict[str, Any]], baselines: Collection[str], fee_rate: float, level: float
    ) -> Lifted:
        algorithms, scored = split(scored_of_kind(rows, kind), baselines)
        return entry(name, algorithms.get(name, []), scored, kind, level)

    def lean(row: Mapping[str, Any]) -> float:
        output = row["output"]
        return float(output[first]) - float(output[third])

    return Scoring(measure, lean, f"{first} - {third}", kind.answer_version)
