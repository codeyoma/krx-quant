"""The climate prior experiment (#429, ``krx-quant climate-prior``): would shannon win back what it
loses to the climate baseline if it were told each symbol and 30-minute slot's class frequencies?

Shannon's pre-validation of 2026-10-30 (#204) was refused: it hit 43.1% and called a direction at
4.5% of the bar closes, close to always flat, and lost 5.5 points to the climate baseline
(``baseline-climate``: each symbol and slot's most frequent label), which knows only the time of
day. Its inputs say nothing of the time of day, so its one bias a class cannot follow the open's
wider moves. This tests the hypothesis before anything is changed: shannon runs the same code.

Three arms, on the same folds and the same bar closes:

- (a) **climate**: the climate baseline's table refitted before each day on the ``CLIMATE_DAYS``
  days before it, as the pre-validation's replay refits it (``_WalkForward.climate_before``);
- (b) **shannon**: shannon as it is, trained before each day as its pre-validation trained it
  (``_WalkForward.before``);
- (c) **shannon + prior** (``ShannonPrior``): shannon's regression on the very samples (b) is
  trained on, with two inputs more: the log-odds of up and of down against flat in the smoothed
  class frequencies of the bar close's symbol and slot (``PRIOR_INPUTS``). The same penalties,
  the same validation on whole trading days, the same standardization (``regression``).

**Folds.** The pre-validation's: the latest ``PREVALIDATION_DAYS`` recorded days, each predicted
by models fitted on shannon's window of the 20 trading days before it (backfilled days among
them), never on the day itself. Nothing is replayed: each day's samples are predicted by each
arm. They are the training data (the live feature code on the day's events, as the nightly
training makes them, read from the sample store when a night stored them, #370); a recorded day's
leave out the bar closes the statistics leave out (a VI, a special situation) and carry the
engine's touch labels, so they are the bar closes the pre-validation scored.

**The prior** (``PriorTally``): label counts by symbol and slot (``consensus.slot``), and by
symbol over the whole day, of the fold's training window alone: the samples (b) and (c) are
given, recorded and backfilled days alike (the climate counts both). Smoothed: a symbol's day is
its counts plus ``PRIOR_STRENGTH`` pseudo-counts split evenly among the classes; a slot is its
counts plus as many in its symbol's day's frequencies. A slot never seen takes its symbol's day;
a symbol never seen, the even split.

- **Causal** (``training_priors``). A training sample's prior counts the window's days before its
  own only: each of their labels was decided before its bar close (a four-minute window ends the
  day it starts). Its own day's labels and the days after it never reach it.
- **Kept with the model.** The prior a fold's model predicts with counts every day of its window,
  and is held in the model's parameters (``prior.<symbol>.<slot>.<class>``,
  ``prior.<symbol>.all.<class>``): not a feature, as a label aggregate depends on the fold and a
  feature cache is shared by every fold and algorithm.

**The report** (``state/experiments/climate-prior-429.json``, and the command's lines): each arm's
bar closes scored, hit rate, recall and precision of each class, directional coverage (the share
of up and down calls) and precision, and Brier score; the lifts over the climate, paired by bar
close and by trading day (a day's mean gain one value, with its 95% Student t interval, whose
lower bound is the pre-validation's ``day_low``), and the lift of (c) over (b). Counts, rates,
lifts, weights and dates only, never a price. #429's rule (``Experiment.rule``): (c) better than
(b) (its lift over (b) above 0), and its lower bound by trading day over the climate above (b)'s.
Whether shannon takes the prior is decided from the posted numbers, in another change.

**Memory.** A pre-validation's training phase's, without its replay (#383): the window's days are
kept and the older let go as the folds move on; each day logs its memory, then hands back what it
freed (``footprint.give_back``).
"""

from __future__ import annotations

import json
import logging
import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np

from krx_quant.algorithms import ALGORITHMS, KIND_ALGORITHMS, Algorithm, KindAlgorithm
from krx_quant.algorithms.contract import Abstention, Model, NothingToLearn, NotPredicted, Sample
from krx_quant.algorithms.forecasts import CLASSES, class_moves, number, param
from krx_quant.algorithms.shannon import (
    _MIN_SD,
    INPUTS,
    MIN_SPLIT,
    PENALTIES,
    VALIDATION_SHARE,
    Shannon,
    _cross_entropy,
    _fista,
    _probabilities,
    _validated_on,
    default_params,
)
from krx_quant.baselines import Forecast
from krx_quant.climate import ALL_DAY, direction
from krx_quant.config import Settings
from krx_quant.consensus import slot
from krx_quant.features import FeatureVector, Leaders
from krx_quant.feeds.base import KST, kst_day
from krx_quant.footprint import DayUsage, Meter, give_back
from krx_quant.kind_registry import KINDS
from krx_quant.kinds import Kind
from krx_quant.market_calendar import calendar_store
from krx_quant.prevalidation import PREVALIDATION_DAYS, _WalkForward
from krx_quant.pricing import Direction
from krx_quant.sample_store import night_reads
from krx_quant.stats import Interval, Lift, Scored, day_lift, lift, mean_interval, wilson
from krx_quant.training import finished_days, is_recorded

log = logging.getLogger(__name__)

EXPERIMENT = "climate-prior-429"
REPORT = f"experiments/{EXPERIMENT}.json"  # under the state directory
PRIOR = "prior"  # the prior's parameters: prior.<symbol>.<slot>.<class>
# The prior's inputs: log(p_up / p_flat) and log(p_down / p_flat) at the bar close's symbol and slot.
PRIOR_INPUTS = ("climate_up", "climate_down")
NAMES = (*INPUTS, *PRIOR_INPUTS)  # (c)'s inputs
# Pseudo-counts a slot's (and a symbol's day's) frequencies are pulled toward the next coarser one
# with: about a day of a slot's bar closes (30 a symbol).
PRIOR_STRENGTH = 30.0
CLIMATE_ARM, SHANNON_ARM, PRIOR_ARM = "climate", "shannon", "shannon_prior"
ARMS = (CLIMATE_ARM, SHANNON_ARM, PRIOR_ARM)
LABELS = {CLIMATE_ARM: "(a) climate", SHANNON_ARM: "(b) shannon", PRIOR_ARM: "(c) shannon + prior"}

Prior = tuple[float, float, float]  # p_up, p_flat, p_down
_EVEN: Prior = (1 / 3, 1 / 3, 1 / 3)


def _smoothed(counts: Counter[Direction], base: Prior) -> Prior:
    n = sum(counts.values())
    up, flat, down = (
        (counts[c] + PRIOR_STRENGTH * b) / (n + PRIOR_STRENGTH) for c, b in zip(CLASSES, base, strict=True)
    )
    return up, flat, down


def _row(symbol: str, where: str, p: Prior) -> dict[str, float]:
    return {param(PRIOR, symbol, where, c.value): q for c, q in zip(CLASSES, p, strict=True)}


class PriorTally:
    """Label counts by symbol and 30-minute slot, and by symbol over the whole day: the prior's."""

    def __init__(self) -> None:
        self._slots: dict[tuple[str, str], Counter[Direction]] = {}
        self._days: dict[str, Counter[Direction]] = {}

    def add(self, samples: Iterable[Sample]) -> None:
        """Count the labels of ``samples`` (those without one are not counted)."""
        for s in samples:
            if s.outcome is not None:
                self._slots.setdefault((s.symbol, slot(s.bar_close)), Counter())[s.outcome] += 1
                self._days.setdefault(s.symbol, Counter())[s.outcome] += 1

    def table(self) -> dict[str, float]:
        """The smoothed frequencies, as a model keeps them (module doc): each symbol's day,
        ``prior.<symbol>.all.<class>``, and each slot, ``prior.<symbol>.<slot>.<class>``; empty
        before any label."""
        days = {symbol: _smoothed(counts, _EVEN) for symbol, counts in self._days.items()}
        out: dict[str, float] = {}
        for symbol, p in sorted(days.items()):
            out |= _row(symbol, ALL_DAY, p)
        for (symbol, where), counts in sorted(self._slots.items()):
            out |= _row(symbol, where, _smoothed(counts, days[symbol]))
        return out


def prior_of(params: Mapping[str, float], symbol: str, bar_close: datetime) -> Prior:
    """The prior ``params`` hold for ``symbol`` at ``bar_close``: its slot's, else its symbol's
    day's, else the even split."""
    for where in (slot(bar_close), ALL_DAY):
        found = [params.get(param(PRIOR, symbol, where, c.value)) for c in CLASSES]
        if all(p is not None for p in found):
            up, flat, down = (float(p) for p in found if p is not None)
            return up, flat, down
    return _EVEN


def prior_inputs(p: Prior) -> tuple[float, float]:
    """The prior's two inputs (``PRIOR_INPUTS``): the log-odds of up and of down against flat."""
    up, flat, down = p
    return math.log(up / flat), math.log(down / flat)


def training_priors(window: Sequence[Sample], scored: Sequence[Sample]) -> list[tuple[float, float]]:
    """The prior inputs of each of ``scored``, the training samples of ``window``: the table of
    ``window``'s labels on the days before the sample's own, never its own day's or a later one's
    (module doc, "Causal")."""
    by_day: dict[date, list[Sample]] = {}
    for s in window:
        by_day.setdefault(kst_day(s.bar_close), []).append(s)
    tally = PriorTally()
    tables: dict[date, dict[str, float]] = {}
    for day in sorted({*by_day, *(kst_day(s.bar_close) for s in scored)}):
        tables[day] = tally.table()
        tally.add(by_day.get(day, ()))
    return [prior_inputs(prior_of(tables[kst_day(s.bar_close)], s.symbol, s.bar_close)) for s in scored]


def _standardization(raw: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Each column's mean and standard deviation over its known values (shannon's: 0 and 1 for a
    column with none, 1 for one that does not vary)."""
    means, sds = [], []
    for j in range(raw.shape[1]):
        known = raw[~np.isnan(raw[:, j]), j]
        mean = float(known.mean()) if known.size else 0.0
        sd = float(known.std()) if known.size else 1.0
        means.append(mean)
        sds.append(sd if sd > _MIN_SD else 1.0)
    return np.array(means), np.array(sds)


def _standardized(raw: np.ndarray, means: np.ndarray, sds: np.ndarray) -> np.ndarray:
    """In standard deviations from the means; 0 (the mean) where unknown, as shannon does."""
    out: np.ndarray = np.where(np.isnan(raw), 0.0, (raw - means) / sds)
    return out


def _fitted(raw: np.ndarray, y: np.ndarray, penalty: float) -> tuple[np.ndarray, ...]:
    means, sds = _standardization(raw)
    w, b = _fista(_standardized(raw, means, sds), y, penalty)
    return means, sds, w, b


def _chosen_penalty(raw: np.ndarray, y: np.ndarray, days: Sequence[date]) -> tuple[float, list[date]]:
    """Shannon's choice of penalty (``shannon._chosen_penalty``) on rows in time order, ``days``
    their trading days: the latest days held out whole, as many as bring them nearest
    ``VALIDATION_SHARE`` of the rows; the middle penalty and no day when too few to split."""
    distinct = sorted(set(days))
    if len(raw) < MIN_SPLIT or len(distinct) < 2:
        return PENALTIES[len(PENALTIES) // 2], []
    per_day = Counter(days)
    target = len(raw) * VALIDATION_SHARE

    def off_target(k: int) -> float:
        return abs(sum(per_day[d] for d in distinct[-k:]) - target)

    held_days = distinct[-min(range(1, len(distinct)), key=off_target) :]
    held = np.array([d >= held_days[0] for d in days])
    best: tuple[float, float] | None = None
    for penalty in PENALTIES:
        means, sds, w, b = _fitted(raw[~held], y[~held], penalty)
        loss = _cross_entropy(w, b, _standardized(raw[held], means, sds), y[held])
        if best is None or loss < best[0] - 1e-12:
            best = (loss, penalty)
    assert best is not None
    return best[1], held_days


def regression(
    raw: np.ndarray, y: np.ndarray, days: Sequence[date], names: Sequence[str]
) -> dict[str, float]:
    """Shannon's L1 logistic regression on ``raw`` (a row a sample in time order, ``days`` their
    trading days; a column an input of ``names``, NaN where unknown), ``y`` the classes one-hot:
    its parameters as shannon names them (``penalty``, ``validation_*``, ``mean.<input>``,
    ``sd.<input>``, ``w.<class>.<input>``, ``bias.<class>``). On shannon's inputs it is shannon's
    model."""
    penalty, validated = _chosen_penalty(raw, y, days)
    means, sds, w, b = _fitted(raw, y, penalty)
    params = {"penalty": penalty, **_validated_on(validated)}
    for j, f in enumerate(names):
        params[param("mean", f)] = float(means[j])
        params[param("sd", f)] = float(sds[j])
    for i, c in enumerate(CLASSES):
        params[param("bias", c.value)] = float(b[i])
        for j, f in enumerate(names):
            params[param("w", c.value, f)] = float(w[i, j]) + 0.0  # a zeroed weight is 0, not −0
    return params


def _inputs(scored: Sequence[Sample]) -> np.ndarray:
    return np.array([[number(s.features[f]) for f in INPUTS] for s in scored], dtype=float).reshape(
        len(scored), len(INPUTS)
    )


def _one_hot(scored: Sequence[Sample]) -> np.ndarray:
    return np.array([[float(s.outcome is c) for c in CLASSES] for s in scored], dtype=float)


def _parameters(params: Mapping[str, float]) -> tuple[np.ndarray, ...]:
    """(c)'s means, standard deviations, weights and biases, from a model's parameters."""
    means = np.array([params[param("mean", f)] for f in NAMES])
    sds = np.array([params[param("sd", f)] for f in NAMES])
    w = np.array([[params[param("w", c.value, f)] for f in NAMES] for c in CLASSES])
    b = np.array([params[param("bias", c.value)] for c in CLASSES])
    return means, sds, w, b


class ShannonPrior:
    """(c): shannon's regression with the climate prior's two inputs (module doc). An experiment's
    arm: not registered, and the engine never runs it. It predicts with a symbol and a bar close
    besides the features, which an algorithm is not given (``contract.Algorithm.predict``)."""

    name = "shannon-prior"
    features = Shannon.features
    training_days = Shannon.training_days

    def default_model(self) -> Model:
        """Before any training: shannon's defaults, the prior's inputs unweighted."""
        params = default_params()
        for f in PRIOR_INPUTS:
            params[param("mean", f)] = 0.0
            params[param("sd", f)] = 1.0
            for c in CLASSES:
                params[param("w", c.value, f)] = 0.0
        return Model(f"{self.name}-default", params)

    def fit(self, samples: Sequence[Sample]) -> Model:
        """Trained as shannon is, on the samples with a label and the book, each with its causal
        prior (``training_priors``); the model keeps the prior of all of ``samples``' labels."""
        scored = sorted(
            (s for s in samples if s.outcome is not None and s.features["depth_imbalance"] is not None),
            key=lambda s: (s.bar_close, s.symbol),
        )
        if not scored:
            raise NothingToLearn("no training samples with the book")
        raw = np.hstack([_inputs(scored), np.array(training_priors(samples, scored), dtype=float)])
        y = _one_hot(scored)
        params = default_params() | regression(raw, y, [kst_day(s.bar_close) for s in scored], NAMES)
        tally = PriorTally()
        tally.add(samples)
        params |= class_moves(scored) | tally.table()
        means, sds, w, b = _parameters(params)
        return Model.fitted(
            self.name, params, scored, _cross_entropy(w, b, _standardized(raw, means, sds), y)
        )

    def predict(self, features: FeatureVector, model: Model, symbol: str, bar_close: datetime) -> Forecast:
        """The forecast at ``symbol``'s ``bar_close`` with the prior ``model`` holds; ``no_book``
        without the book, as shannon."""
        if features["depth_imbalance"] is None:
            raise NotPredicted(Abstention.NO_BOOK)
        p = model.params
        prior = prior_inputs(prior_of(p, symbol, bar_close))
        raw = np.array([[*(number(features[f]) for f in INPUTS), *prior]], dtype=float)
        means, sds, w, b = _parameters(p)
        up, flat, down = (float(q) for q in _probabilities(w, b, _standardized(raw, means, sds))[0])
        expected = sum(q * p[param("move", c.value)] for q, c in zip((up, flat, down), CLASSES, strict=True))
        return Forecast(up, flat, down, expected)


@dataclass(frozen=True)
class Fold:
    """A day of the experiment: what predicted it, on how many samples it was trained, and how
    many of its bar closes were scored."""

    day: date
    models: dict[str, str]  # arm -> the version of the model (the climate's table) that predicted it
    trained_on: int  # the training samples (b) and (c) learned from: with a label and the book
    scored: int  # its bar closes with a label that every arm predicted
    no_book: int  # its bar closes with a label (b) and (c) did not predict, without the book

    def as_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "models": self.models,
            "trained_on": self.trained_on,
            "scored": self.scored,
            "no_book": self.no_book,
        }


@dataclass(frozen=True)
class ArmStats:
    """An arm's statistics on the bar closes every arm predicted."""

    n: int
    accuracy: Interval | None
    coverage: Interval | None  # the up and down calls' share
    directional_precision: Interval | None  # of the up and down calls, those that happened
    brier: Interval | None
    recall: dict[Direction, Interval | None]  # of each class that happened, the share called
    precision: dict[Direction, Interval | None]  # of each class called, the share that happened
    predicted: dict[Direction, int]
    happened: dict[Direction, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "accuracy": _interval(self.accuracy),
            "coverage": _interval(self.coverage),
            "directional_precision": _interval(self.directional_precision),
            "brier": _interval(self.brier),
            "by_class": {
                d.value: {
                    "recall": _interval(self.recall[d]),
                    "precision": _interval(self.precision[d]),
                    "predicted": self.predicted[d],
                    "happened": self.happened[d],
                }
                for d in CLASSES
            },
        }


def arm_stats(records: Sequence[Scored]) -> ArmStats:
    """``records``' hit rate, coverage, Brier score and each class's recall and precision, with
    95% Wilson intervals (normal for the Brier score's mean), as the leaderboard's."""
    predicted = Counter(r.predicted for r in records)
    happened = Counter(r.outcome for r in records)
    hits = Counter(r.outcome for r in records if r.correct)
    directional = [r for r in records if r.predicted is not Direction.FLAT]
    return ArmStats(
        n=len(records),
        accuracy=wilson(sum(hits.values()), len(records)),
        coverage=wilson(len(directional), len(records)),
        directional_precision=wilson(sum(r.correct for r in directional), len(directional)),
        brier=mean_interval(
            [sum((r.probability(d) - (d is r.outcome)) ** 2 for d in CLASSES) for r in records]
        ),
        recall={d: wilson(hits[d], happened[d]) for d in CLASSES},
        precision={d: wilson(hits[d], predicted[d]) for d in CLASSES},
        predicted={d: predicted[d] for d in CLASSES},
        happened={d: happened[d] for d in CLASSES},
    )


@dataclass(frozen=True)
class PairedLift:
    """The hit rate gained over another arm on the same bar closes: paired, and by trading day."""

    pairs: Lift
    by_day: Lift | None  # None from fewer than two days

    @classmethod
    def of(cls, records: Sequence[Scored], other: Sequence[Scored]) -> PairedLift:
        return cls(lift(records, other), day_lift(records, other))

    def as_dict(self) -> dict[str, Any]:
        return {"pairs": _lift(self.pairs), "by_day": None if self.by_day is None else _lift(self.by_day)}


@dataclass(frozen=True)
class Experiment:
    made_at: datetime
    days: list[date]  # the days predicted, oldest first
    folds: list[Fold]
    arms: dict[str, ArmStats]
    over_climate: dict[str, PairedLift]  # (b)'s and (c)'s lifts over (a)
    prior_over_shannon: PairedLift  # (c)'s over (b)
    last_model: Model  # (c)'s model of the last day: its weights on the prior

    @property
    def better(self) -> bool | None:
        """(c) better than (b): its lift over (b) above 0; None without a pair."""
        value = self.prior_over_shannon.pairs.value
        return None if value is None else value > 0

    @property
    def lower_bound_up(self) -> bool | None:
        """(c)'s lower bound by trading day over the climate above (b)'s; None without both."""
        prior, shannon = (self.over_climate[arm].by_day for arm in (PRIOR_ARM, SHANNON_ARM))
        if prior is None or shannon is None or prior.low is None or shannon.low is None:
            return None
        return prior.low > shannon.low

    @property
    def rule(self) -> bool:
        """#429's rule for shannon to take the prior: (c) better than (b), and its lower bound by
        trading day over the climate improved."""
        return self.better is True and self.lower_bound_up is True

    def as_dict(self) -> dict[str, Any]:
        return {
            "experiment": EXPERIMENT,
            "made_at": self.made_at.isoformat(),
            "days": [d.isoformat() for d in self.days],
            "prior": {"inputs": list(PRIOR_INPUTS), "strength": PRIOR_STRENGTH},
            "folds": [f.as_dict() for f in self.folds],
            "arms": {arm: stats.as_dict() for arm, stats in self.arms.items()},
            "lift": {
                "over_climate": {arm: lifted.as_dict() for arm, lifted in self.over_climate.items()},
                "prior_over_shannon": self.prior_over_shannon.as_dict(),
            },
            "rule": {"better": self.better, "lower_bound_up": self.lower_bound_up, "met": self.rule},
            "last_model": {
                "version": self.last_model.version,
                "penalty": self.last_model.params.get("penalty"),
                "weights": _prior_weights(self.last_model),
            },
        }


def _interval(i: Interval | None) -> dict[str, Any] | None:
    return None if i is None else {"value": i.value, "low": i.low, "high": i.high, "n": i.n}


def _lift(lifted: Lift) -> dict[str, Any]:
    return {"n": lifted.n, "value": lifted.value, "low": lifted.low, "high": lifted.high, "p": lifted.p}


def _prior_weights(model: Model) -> dict[str, dict[str, float]]:
    return {
        c.value: {f: model.params.get(param("w", c.value, f), 0.0) for f in PRIOR_INPUTS} for c in CLASSES
    }


def climate_prior_experiment(
    settings: Settings,
    *,
    days: int = PREVALIDATION_DAYS,
    algorithms: Sequence[Algorithm] = ALGORITHMS,
    kind_algorithms: Sequence[KindAlgorithm] = KIND_ALGORITHMS,
    kinds: Sequence[Kind] = KINDS,
    now: datetime | None = None,
) -> Experiment:
    """Run the experiment on the latest ``days`` recorded days (module doc) and save its report.
    The registries name the days the nights stored (#370, ``night_reads``). Raises
    FileNotFoundError when no day was recorded."""
    now = now or datetime.now(KST)
    available = finished_days(settings.data_dir, settings.symbols, now)
    predicted = [d for d in available if is_recorded(settings.data_dir, d, settings.symbols)][-days:]
    if not predicted:
        symbols = ", ".join(settings.symbols)
        raise FileNotFoundError(f"no recorded day of {symbols} in {settings.data_dir}")
    calendar = calendar_store(settings).calendar
    night = night_reads(algorithms, kind_algorithms, kinds)
    shannon, variant = Shannon(), ShannonPrior()
    trainer = _WalkForward(settings, shannon, available, calendar, Leaders.of(settings), night=night)
    records: dict[str, list[Scored]] = {arm: [] for arm in ARMS}
    folds: list[Fold] = []
    last = variant.default_model()
    meter = Meter()
    for n, day in enumerate(predicted, 1):
        usage = DayUsage(meter)  # its memory, logged (#383)
        with usage.phase("training"):
            models, trained_on = _models(trainer, variant, day)
        with usage.phase("scoring"):
            scored, no_book = _score(trainer.samples_on(day), models, shannon, variant, records)
        folds.append(Fold(day, {arm: m.version for arm, m in models.items()}, trained_on, scored, no_book))
        last = models[PRIOR_ARM]
        give_back()
        log.info("climate prior: %s predicted by %s", day, ", ".join(folds[-1].models.values()))
        log.info("climate prior: memory on %s (day %d of %d): %s", day, n, len(predicted), usage.line())
    climate = records[CLIMATE_ARM]
    result = Experiment(
        made_at=now,
        days=list(predicted),
        folds=folds,
        arms={arm: arm_stats(records[arm]) for arm in ARMS},
        over_climate={arm: PairedLift.of(records[arm], climate) for arm in (SHANNON_ARM, PRIOR_ARM)},
        prior_over_shannon=PairedLift.of(records[PRIOR_ARM], records[SHANNON_ARM]),
        last_model=last,
    )
    path = settings.state_dir / REPORT
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
    tmp.replace(path)
    return result


def _models(trainer: _WalkForward, variant: ShannonPrior, day: date) -> tuple[dict[str, Model], int]:
    """The arms' models for ``day``, each fitted on the days before it, and how many samples (b)
    and (c) learned from: (b) as its pre-validation trains it, (c) on the samples (b) was given."""
    shannon = trainer.before(day)
    window = trainer.samples_before(day)
    learned = sum(1 for s in window if s.outcome is not None and s.features["depth_imbalance"] is not None)
    prior = variant.default_model()
    if any(s.outcome is not None for s in window):  # else (b) has its defaults too
        try:
            prior = variant.fit(window)
        except NothingToLearn as e:  # no book in the window: (b) has its defaults too
            log.warning("%s not trained before %s, its defaults predict: %s", variant.name, day, e)
    return {CLIMATE_ARM: trainer.climate_before(day), SHANNON_ARM: shannon, PRIOR_ARM: prior}, learned


def _score(
    samples: Sequence[Sample],
    models: Mapping[str, Model],
    shannon: Shannon,
    variant: ShannonPrior,
    records: Mapping[str, list[Scored]],
) -> tuple[int, int]:
    """Each arm's forecast of each of the day's bar closes with a label, onto ``records``, where
    all three predict: (b) and (c) do not without the book. The bar closes scored, and those not
    for the book."""
    scored = no_book = 0
    for s in samples:
        if s.outcome is None:
            continue
        try:
            forecasts = {
                SHANNON_ARM: shannon.predict(s.features, models[SHANNON_ARM]),
                PRIOR_ARM: variant.predict(s.features, models[PRIOR_ARM], s.symbol, s.bar_close),
            }
        except NotPredicted:
            no_book += 1
            continue
        called = direction(models[CLIMATE_ARM], s.symbol, s.bar_close) or Direction.FLAT
        forecasts[CLIMATE_ARM] = Forecast.certain(called)  # as the baseline (``baselines.Climate``)
        for arm, f in forecasts.items():
            version = models[arm].version
            records[arm].append(
                Scored(arm, version, s.symbol, s.bar_close, f.p_up, f.p_flat, f.p_down, s.outcome)
            )
        scored += 1
    return scored, no_book


def lines(result: Experiment, report: Path) -> list[str]:
    """The command's output: counts, rates, lifts, weights and dates, no price."""
    first, last = result.days[0], result.days[-1]
    trained = sum(1 for f in result.folds if not f.models[SHANNON_ARM].endswith("-default"))
    scored = sum(f.scored for f in result.folds)
    no_book = sum(f.no_book for f in result.folds)
    out = [
        f"climate prior experiment (#429): {len(result.days)} recorded days, {first}..{last}",
        f"  folds: each day predicted by models fitted on shannon's {Shannon.training_days} trading days"
        f" before it; {trained} of {len(result.days)} days with a trained shannon",
        f"  compared: {scored} bar closes all three predicted ({no_book} without the book left out:"
        " shannon does not predict them)",
        f"  prior: class frequencies by symbol and 30-minute slot, {PRIOR_STRENGTH:g} pseudo-counts toward"
        " the symbol's day; a training sample's from the days before its own",
        "",
        "arm                   n       hit rate (95%)             coverage  dir. precision  brier",
    ]
    for arm in ARMS:
        a = result.arms[arm]
        out.append(
            f"{LABELS[arm]:<21} {a.n:<7} {_ratio(a.accuracy):<26} {_value(a.coverage):<9} "
            f"{_value(a.directional_precision):<15} {_value(a.brier)}"
        )
    out += ["", "by class: recall / precision (calls)"]
    for arm in ARMS:
        a = result.arms[arm]
        parts = [
            f"{d.value} {_value(a.recall[d])} / {_value(a.precision[d])} ({a.predicted[d]})" for d in CLASSES
        ]
        out.append(f"{LABELS[arm]:<21} " + "   ".join(parts))
    happened = result.arms[CLIMATE_ARM].happened
    out.append(f"{'happened':<21} " + "   ".join(f"{d.value} {happened[d]}" for d in CLASSES))
    out += ["", "lift (hit rate gained on the same bar closes; paired, then by trading day)"]
    for arm in (SHANNON_ARM, PRIOR_ARM):
        out.append(f"{LABELS[arm] + ' over (a)':<29} {_lifted(result.over_climate[arm])}")
    out.append(f"{'(c) over (b)':<29} {_lifted(result.prior_over_shannon)}")
    out += ["", _rule_line(result)]
    over = result.over_climate[PRIOR_ARM].pairs.value
    if over is not None:
        verdict = "above 0" if over > 0 else "not above 0: its pre-validation's stats check would still fail"
        out.append(f"  (c) over the climate: {over:+.4f}, {verdict}")
    weights = _prior_weights(result.last_model)
    shown = "; ".join(
        f"{c} " + ", ".join(f"{f} {w:+.3f}" for f, w in by_input.items()) for c, by_input in weights.items()
    )
    penalty = result.last_model.params.get("penalty")
    out.append(
        f"last day's (c) model {result.last_model.version}: penalty "
        f"{'-' if penalty is None else f'{penalty:g}'}; weights on the prior: {shown}"
    )
    out.append(f"report: {report}")
    return out


def _rule_line(result: Experiment) -> str:
    prior = result.over_climate[PRIOR_ARM].by_day
    shannon = result.over_climate[SHANNON_ARM].by_day
    lows = (
        "-"
        if prior is None or shannon is None or prior.low is None or shannon.low is None
        else f"{prior.low:+.4f} against {shannon.low:+.4f}"
    )
    return (
        f"#429 rule: (c) over (b) above 0: {_yes(result.better)}; (c)'s lower bound by trading day over"
        f" the climate above (b)'s: {_yes(result.lower_bound_up)} ({lows}) -> "
        f"{'met: shannon would take the prior' if result.rule else 'not met: shannon stays as it is'}"
    )


def _yes(value: bool | None) -> str:
    return "-" if value is None else "yes" if value else "no"


def _value(i: Interval | None) -> str:
    return "-" if i is None else f"{i.value:.4f}"


def _ratio(i: Interval | None) -> str:
    if i is None:
        return "-"
    if i.low is None or i.high is None:
        return f"{i.value:.4f}"
    return f"{i.value:.4f} ({i.low:.4f}..{i.high:.4f})"


def _lifted(lifted: PairedLift) -> str:
    pairs = lifted.pairs
    if pairs.value is None:
        return "no pair"
    text = f"{pairs.value:+.4f}"
    if pairs.low is not None and pairs.high is not None:
        text += f" ({pairs.low:+.4f}..{pairs.high:+.4f})"
    text += f", {pairs.n} pairs"
    day = lifted.by_day
    if day is None or day.value is None or day.low is None or day.high is None:
        return text + "; by trading day: fewer than two days"
    return text + f"; by trading day {day.value:+.4f} ({day.low:+.4f}..{day.high:+.4f}), {day.n} days"
