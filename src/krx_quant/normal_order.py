"""The label-order experiment (#529, ``krx-quant normal-order``): would the algorithms that make their
three probabilities with ``normal_forecast`` do better if it gave the touch target in the label's order?

``normal_forecast`` (``algorithms/forecasts.py``) gives the touch target's up as the chance of reaching
the upper target (the reflection principle) and its down as the chance of reaching the lower one times
the chance of not reaching the upper one, as if the two were independent. The label is ordered: up if
the bid reaches the upper target, else down if the ask reaches the lower one, else flat. So the exact
down is the chance of reaching the lower target and not the upper one, never below the product: a path
that goes down is less likely to reach up. The approximation gives down too little and flat too much
(k = 2 ticks, half a tick of spread, no drift, a spread of 2 ticks: down 0.167 against 0.211).
``ordered_forecast`` (#518) gives the exact order: up reaches the upper target, flat stays between the
two (``stay_probability``), down is the rest.

The five algorithms are not changed: ``forecasts.py`` is not in their code digests
(``prevalidation.code_digest``: each one's package), so a change to ``normal_forecast`` would change
their live predictions without #454's new verdict, and newton is active and in the consensus. The
switch is ``forecasts.label_order``: inside it, and only there, ``normal_forecast`` gives the label's
order. Adopting it is the user's decision, from this report: then ``normal_forecast`` changes and
``forecasts.py`` goes into the five's ``code_from``, so they go back on probation (#454).

**Two arms**, for each algorithm that uses ``normal_forecast`` (``COMPARED``), on the same bar closes:

- **independent**: the algorithm as it runs, trained before each day as its pre-validation trains it
  (``_WalkForward.before``);
- **ordered**: the same, trained and predicting inside ``label_order``. Newton and gauss choose their
  parameters by the lowest log loss over a grid, so theirs may differ; granger, markov and kalman
  estimate theirs otherwise, so their model is the same and only their forecasts differ.

**Folds.** The pre-validation's: the latest ``PREVALIDATION_DAYS`` recorded days, each predicted by
models fitted on the algorithm's window of the 20 trading days before it (backfilled days among them),
never on the day itself. Nothing is replayed: each day's samples are predicted by each arm. They are
the training data (the live feature code on the day's events, as the nightly training makes them, read
from the sample store when a night stored them, #370); a recorded day's leave out the bar closes the
statistics leave out (a VI, a special situation) and carry the engine's touch labels. A bar close an
arm does not predict (kalman without the book) is left out of both. One algorithm at a time, its days
let go before the next.

**The report** (``state/experiments/normal-order-529.json``, and the command's lines): for each
algorithm and arm, the bar closes scored, hit rate, mean log loss, Brier score, directional coverage
and precision, and for each class its recall, precision, calls, mean probability and mean log loss
where it happened; the ordered arm's lift over the independent one, paired by bar close and by trading
day, and its log loss gained by trading day (a day's mean of the independent arm's loss minus the
ordered one's). Counts, rates, losses and dates only, never a price.

**The rule, set before it ran** (#529, ``Comparison.candidate``): the ordered arm is a candidate for an
algorithm when its lift over the independent arm paired by bar close is above 0 and its mean log loss
is lower. A candidate changes nothing by itself: an agent proposes from the numbers on #529, and the
user decides.

**Memory.** A pre-validation's training phase's, without its replay (#383): the window's days are kept
and the older let go as the folds move on; each day logs its memory, then hands back what it freed
(``footprint.give_back``).
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from krx_quant.algorithms import ALGORITHMS, KIND_ALGORITHMS, Algorithm, KindAlgorithm
from krx_quant.algorithms.contract import Model, NotPredicted, PredictionContext, Sample, forecast_of
from krx_quant.algorithms.forecasts import CLASSES, LOSS_FLOOR, forecasting, label_order
from krx_quant.climate_prior import ArmStats, PairedLift, _interval, _lifted, _ratio, _value, _yes, arm_stats
from krx_quant.config import Settings
from krx_quant.features import Leaders
from krx_quant.feeds.base import KST, kst_day
from krx_quant.footprint import DayUsage, Meter, give_back
from krx_quant.kind_registry import KINDS
from krx_quant.kinds import Kind
from krx_quant.market_calendar import MarketCalendar, calendar_store
from krx_quant.prevalidation import PREVALIDATION_DAYS, _WalkForward
from krx_quant.pricing import Direction
from krx_quant.sample_store import NightReads, night_reads
from krx_quant.stats import Interval, Scored, mean_interval
from krx_quant.training import finished_days, is_recorded

log = logging.getLogger(__name__)

EXPERIMENT = "normal-order-529"
REPORT = f"experiments/{EXPERIMENT}.json"  # under the state directory
INDEPENDENT, ORDERED = "independent", "ordered"
ARMS = (INDEPENDENT, ORDERED)
# The algorithms whose three probabilities are ``normal_forecast``'s, in the registry's order.
COMPARED = ("newton", "gauss", "granger", "markov", "kalman")


@dataclass(frozen=True)
class Fold:
    """A day of an algorithm's comparison: the models that predicted it and its bar closes."""

    day: date
    models: dict[str, str]  # arm -> the version of the model that predicted it
    scored: int  # its bar closes with a label both arms predicted
    not_predicted: int  # its bar closes with a label the algorithm did not predict (no book, say)

    def as_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "models": self.models,
            "scored": self.scored,
            "not_predicted": self.not_predicted,
        }


@dataclass(frozen=True)
class ArmReport:
    """An arm's statistics on the bar closes both arms predicted, with its log loss."""

    stats: ArmStats
    log_loss: Interval | None
    mean_probability: dict[Direction, float | None]  # the mean probability it gave each class
    loss_where: dict[Direction, Interval | None]  # its mean log loss on the bar closes of each class

    def as_dict(self) -> dict[str, Any]:
        out = self.stats.as_dict()
        out["log_loss"] = _interval(self.log_loss)
        for d in CLASSES:
            out["by_class"][d.value] |= {
                "mean_probability": self.mean_probability[d],
                "log_loss": _interval(self.loss_where[d]),
            }
        return out


def loss(record: Scored) -> float:
    """``record``'s log loss, as training counts it (``forecasts.log_loss``)."""
    return -math.log(max(record.probability(record.outcome), LOSS_FLOOR))


def arm_report(records: Sequence[Scored]) -> ArmReport:
    return ArmReport(
        stats=arm_stats(records),
        log_loss=mean_interval([loss(r) for r in records]),
        mean_probability={
            d: sum(r.probability(d) for r in records) / len(records) if records else None for d in CLASSES
        },
        loss_where={d: mean_interval([loss(r) for r in records if r.outcome is d]) for d in CLASSES},
    )


def loss_gain_by_day(records: Sequence[Scored], baseline: Sequence[Scored]) -> Interval | None:
    """The log loss ``records`` gains over ``baseline`` on the same bar closes, a trading day at a time:
    each day's mean of the baseline's loss minus the record's is one value, every day weighing the same,
    with a 95% normal interval. None without a pair."""
    theirs = {(r.symbol, r.bar_close): loss(r) for r in baseline}
    by_day: dict[date, list[float]] = {}
    for r in records:
        key = (r.symbol, r.bar_close)
        if key in theirs:
            by_day.setdefault(kst_day(r.bar_close), []).append(theirs[key] - loss(r))
    return mean_interval([sum(gains) / len(gains) for _, gains in sorted(by_day.items())])


@dataclass(frozen=True)
class Comparison:
    """An algorithm's two arms on the same bar closes."""

    algorithm: str
    folds: list[Fold]
    arms: dict[str, ArmReport]
    lift: PairedLift  # the ordered arm's hit rate gained over the independent one
    loss_gain: Interval | None  # the ordered arm's log loss gained, by trading day

    @property
    def better(self) -> bool | None:
        """The ordered arm's lift over the independent one, paired by bar close, above 0; None without a
        pair."""
        value = self.lift.pairs.value
        return None if value is None else value > 0

    @property
    def lower_loss(self) -> bool | None:
        """The ordered arm's mean log loss below the independent one's; None without a bar close."""
        ordered, independent = (self.arms[arm].log_loss for arm in (ORDERED, INDEPENDENT))
        if ordered is None or independent is None:
            return None
        return ordered.value < independent.value

    @property
    def candidate(self) -> bool:
        """#529's rule: the label's order is a candidate for this algorithm (module doc)."""
        return self.better is True and self.lower_loss is True

    def as_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "folds": [f.as_dict() for f in self.folds],
            "arms": {arm: report.as_dict() for arm, report in self.arms.items()},
            "ordered_over_independent": {
                "hit_rate": self.lift.as_dict(),
                "log_loss_by_day": _interval(self.loss_gain),
            },
            "rule": {"better": self.better, "lower_loss": self.lower_loss, "candidate": self.candidate},
        }


@dataclass(frozen=True)
class Experiment:
    made_at: datetime
    days: list[date]  # the days predicted, oldest first
    comparisons: list[Comparison]

    @property
    def candidates(self) -> list[str]:
        return [c.algorithm for c in self.comparisons if c.candidate]

    def as_dict(self) -> dict[str, Any]:
        return {
            "experiment": EXPERIMENT,
            "made_at": self.made_at.isoformat(),
            "days": [d.isoformat() for d in self.days],
            "comparisons": [c.as_dict() for c in self.comparisons],
            "candidates": self.candidates,
        }


def normal_order_experiment(
    settings: Settings,
    *,
    days: int = PREVALIDATION_DAYS,
    algorithms: Sequence[Algorithm] = ALGORITHMS,
    kind_algorithms: Sequence[KindAlgorithm] = KIND_ALGORITHMS,
    kinds: Sequence[Kind] = KINDS,
    now: datetime | None = None,
) -> Experiment:
    """Run the experiment on the latest ``days`` recorded days (module doc) for the algorithms of
    ``algorithms`` named in ``COMPARED``, and save its report. The registries name the days the nights
    stored (#370, ``night_reads``). Raises FileNotFoundError when no day was recorded."""
    now = now or datetime.now(KST)
    available = finished_days(settings.data_dir, settings.symbols, now)
    predicted = [d for d in available if is_recorded(settings.data_dir, d, settings.symbols)][-days:]
    if not predicted:
        symbols = ", ".join(settings.symbols)
        raise FileNotFoundError(f"no recorded day of {symbols} in {settings.data_dir}")
    calendar = calendar_store(settings).calendar
    night = night_reads(algorithms, kind_algorithms, kinds)
    comparisons = [
        _compare(settings, a, available, predicted, calendar, night) for a in algorithms if a.name in COMPARED
    ]
    result = Experiment(made_at=now, days=list(predicted), comparisons=comparisons)
    path = settings.state_dir / REPORT
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
    tmp.replace(path)
    return result


def _compare(
    settings: Settings,
    algorithm: Algorithm,
    available: Sequence[date],
    predicted: Sequence[date],
    calendar: MarketCalendar,
    night: NightReads,
) -> Comparison:
    """``algorithm``'s two arms on ``predicted``, each day's models fitted on its window before it."""
    trainer = _WalkForward(settings, algorithm, available, calendar, Leaders.of(settings), night=night)
    records: dict[str, list[Scored]] = {arm: [] for arm in ARMS}
    folds: list[Fold] = []
    meter = Meter()
    for n, day in enumerate(predicted, 1):
        usage = DayUsage(meter)  # its memory, logged (#383)
        with usage.phase("training"):
            independent = trainer.before(day)
            with label_order():
                ordered = trainer.before(day)
            models = {INDEPENDENT: independent, ORDERED: ordered}
        with usage.phase("scoring"):
            scored, not_predicted = _score(trainer.samples_on(day), algorithm, models, records)
        folds.append(Fold(day, {arm: m.version for arm, m in models.items()}, scored, not_predicted))
        give_back()
        name, versions = algorithm.name, ", ".join(folds[-1].models.values())
        log.info("normal order: %s's %s predicted by %s", name, day, versions)
        days = len(predicted)
        log.info("normal order: %s's memory on %s (day %d of %d): %s", name, day, n, days, usage.line())
    independent_records, ordered_records = records[INDEPENDENT], records[ORDERED]
    return Comparison(
        algorithm=algorithm.name,
        folds=folds,
        arms={arm: arm_report(records[arm]) for arm in ARMS},
        lift=PairedLift.of(ordered_records, independent_records),
        loss_gain=loss_gain_by_day(ordered_records, independent_records),
    )


def _score(
    samples: Sequence[Sample],
    algorithm: Algorithm,
    models: Mapping[str, Model],
    records: Mapping[str, list[Scored]],
) -> tuple[int, int]:
    """Each arm's forecast of each of the day's bar closes with a label, onto ``records``, where both
    predict; each of its own target (``forecasting``), the ordered arm inside ``label_order``. The bar
    closes scored, and those not predicted."""
    scored = not_predicted = 0
    for s in samples:
        if s.outcome is None:
            continue
        context = PredictionContext(s.symbol, s.bar_close)
        try:
            with forecasting(s.features):
                independent = forecast_of(algorithm, s.features, models[INDEPENDENT], context)
                with label_order():
                    ordered = forecast_of(algorithm, s.features, models[ORDERED], context)
        except NotPredicted:
            not_predicted += 1
            continue
        for arm, f in ((INDEPENDENT, independent), (ORDERED, ordered)):
            version = models[arm].version
            records[arm].append(
                Scored(arm, version, s.symbol, s.bar_close, f.p_up, f.p_flat, f.p_down, s.outcome)
            )
        scored += 1
    return scored, not_predicted


def lines(result: Experiment, report: Path) -> list[str]:
    """The command's output: counts, rates, losses and dates, no price."""
    first, last = result.days[0], result.days[-1]
    out = [
        f"normal_forecast label-order experiment (#529): {len(result.days)} recorded days, {first}..{last}",
        "  folds: each day predicted by models fitted on the algorithm's 20 trading days before it",
        "  independent: as the algorithms run (down = reach down x not reach up);"
        " ordered: the label's order (flat = stay between the targets, down = the rest)",
    ]
    for c in result.comparisons:
        out += ["", *_comparison_lines(c)]
    shown = ", ".join(result.candidates) or "none"
    out += ["", f"candidates (#529 rule): {shown}", f"report: {report}"]
    return out


def _comparison_lines(c: Comparison) -> list[str]:
    trained = sum(1 for f in c.folds if not f.models[INDEPENDENT].endswith("-default"))
    scored = sum(f.scored for f in c.folds)
    not_predicted = sum(f.not_predicted for f in c.folds)
    same = sum(1 for f in c.folds if f.models[INDEPENDENT] == f.models[ORDERED])
    out = [
        f"{c.algorithm}: {scored} bar closes both arms predicted ({not_predicted} not predicted);"
        f" {trained} of {len(c.folds)} days trained; the same model on {same} days",
        "  arm          n       hit rate (95%)             log loss  brier   coverage  dir. precision",
    ]
    for arm in ARMS:
        a = c.arms[arm]
        s = a.stats
        out.append(
            f"  {arm:<12} {s.n:<7} {_ratio(s.accuracy):<26} {_value(a.log_loss):<9} {_value(s.brier):<7} "
            f"{_value(s.coverage):<9} {_value(s.directional_precision)}"
        )
    out.append("  by class: recall / precision (calls) / mean probability / log loss where it happened")
    for arm in ARMS:
        a = c.arms[arm]
        parts = [
            f"{d.value} {_value(a.stats.recall[d])} / {_value(a.stats.precision[d])} ({a.stats.predicted[d]})"
            f" / {_number(a.mean_probability[d])} / {_value(a.loss_where[d])}"
            for d in CLASSES
        ]
        out.append(f"  {arm:<12} " + "   ".join(parts))
    happened = c.arms[INDEPENDENT].stats.happened
    n = c.arms[INDEPENDENT].stats.n
    out.append(
        f"  {'happened':<12} "
        + "   ".join(f"{d.value} {happened[d]} ({_number(happened[d] / n if n else None)})" for d in CLASSES)
    )
    out.append(f"  ordered over independent: hit rate {_lifted(c.lift)}")
    out.append(f"  ordered over independent: log loss gained by trading day {_gain(c.loss_gain)}")
    verdict = "a candidate" if c.candidate else "not a candidate"
    rule = f"lift above 0: {_yes(c.better)}; lower log loss: {_yes(c.lower_loss)}"
    out.append(f"  #529 rule: {rule} -> {verdict}")
    return out


def _number(value: float | None) -> str:
    return "-" if value is None else f"{value:.4f}"


def _gain(i: Interval | None) -> str:
    if i is None:
        return "no pair"
    if i.low is None or i.high is None:
        return f"{i.value:+.4f}, {i.n} days"
    return f"{i.value:+.4f} ({i.low:+.4f}..{i.high:+.4f}), {i.n} days"
