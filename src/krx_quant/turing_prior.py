"""The symbol and climate prior experiment for turing (#448, ``krx-quant turing-prior``): would
turing win back what it loses to the climate baseline if its trees were told the symbol, or each
symbol and 30-minute slot's class frequencies, or both?

#431's breakdown of turing's live predictions (``error_breakdown.py``) found that turing knows the
time of day (``minutes_since_open``) but not the symbol, while the classes come at rates far apart
from one symbol to another; and that where it was quiet (σ̂₄'s lowest tercile) flat came most of
the time, yet turing called a direction there often and lost to the climate baseline
(``baseline-climate``: each symbol and slot's most frequent label). This tests the hypothesis
before anything is changed: turing runs the same code (its code digest drives its pre-validation).

Five arms, on the same folds and the same bar closes:

- (a) **climate**: the climate baseline's table refitted before each day on the ``CLIMATE_DAYS``
  days before it, as the pre-validation's replay refits it (``_WalkForward.climate_before``);
- (b) **turing**: turing as it is (``Turing``, the registered algorithm), trained before each day
  as its pre-validation trains it (``_WalkForward.before``), on its own window of 60 trading days;
- (c) **turing + symbol** (``TuringPlus(symbol=True)``): turing's trees on the very samples (b) is
  trained on, with one input more: the bar close's symbol, a categorical input of the trees
  (LightGBM's ``categorical_feature``). Each symbol of the window has a code, kept in the model
  (``symbol.<symbol>``); a symbol the window never had is a missing input;
- (d) **turing + prior** (``TuringPlus(prior=True)``): two inputs more, the climate prior's
  (``priors.PRIOR_INPUTS``, #429's definition): the log-odds of up and of down against flat in the
  smoothed class frequencies of the bar close's symbol and slot;
- (e) **turing + symbol + prior**: the three.

(c) to (e) boost as turing does (``turing.BOOSTING``, ``ROUNDS``: one thread, a fixed seed), on
turing's features in turing's order and the inputs added after them. They predict through the
engine's dispatcher with the symbol and the bar close (``forecast_of``, ``PredictionContext``).

**Folds.** The pre-validation's: the latest ``PREVALIDATION_DAYS`` recorded days, each predicted
by models fitted on turing's window of the 60 trading days before it (backfilled days among them),
never on the day itself; the climate's on its own 20. Nothing is replayed: each day's samples are
predicted by each arm. They are the training data (the live feature code on the day's events, as
the nightly training makes them, read from the sample store when a night stored them, #370); a
recorded day's leave out the bar closes the statistics leave out (a VI, a special situation) and
carry the engine's touch labels, so they are the bar closes the pre-validation scored. Turing
predicts every bar close (a third each before its first training): every arm is scored on each of
the day's bar closes with a label.

**The prior** (``algorithms/priors.py``, shannon's since #429, imported and never changed here:
it is in shannon's code digest): label counts by symbol and slot, and by symbol over the whole
day, of the fold's training window alone (turing's 60 days, recorded and backfilled days alike),
smoothed toward the coarser one with ``PRIOR_STRENGTH`` pseudo-counts. A training sample's prior
counts the window's days before its own only (causal, ``priors.training_priors``); the prior a
fold's model predicts with counts every day of its window and is held in the model's parameters,
never a feature (a label aggregate depends on the fold, and the feature cache is shared).

**The report** (``state/experiments/turing-prior-448.json``, and the command's lines): each arm's
bar closes scored, hit rate, recall and precision of each class, confusion matrix, directional
coverage (the share of up and down calls) and precision, and Brier score; the lifts over the
climate and, for (c) to (e), over (b), paired by bar close and by trading day (a day's mean gain
one value, with its 95% Student t interval, whose lower bound is the pre-validation's
``day_low``). The same by tercile of σ̂₄ at the bar close and by 30-minute slot (``consensus.slot``).
σ̂₄ is #431's: the barrier's at the bar close in basis points of the mid-point, read from the
engine DB without writing to it (``error_breakdown.read_sigmas``), each symbol's tercile cuts from
its bar closes of the days predicted that have one (``api.volatility_cuts``); a bar close without
one (fewer than 20 bars, or none recorded) is in ``none``. And the share of the trees' gain each
added input took in the last day's models. Counts, rates, lifts, shares and dates only, never a
price, a σ̂₄ or a cut.

**The rule, set before it ran** (#448, ``Experiment.candidates``): an arm of (c), (d) and (e) is a
**candidate** when it is better than (b) (its lift over (b) paired by bar close above 0) and its
lower bound by trading day over the climate is above (b)'s. A candidate is not put into turing by
this: that would change turing's code digest and run its pre-validation again, and the final
verdict would be on the days after. An agent proposes from the numbers on #448; the user decides.

**Memory.** A pre-validation's training phase's, without its replay (#383): the window's days are
kept and the older let go as the folds move on; the arms of a day share turing's inputs of its
training samples (``inputs_of``); each day logs its memory, then hands back what it freed
(``footprint.give_back``).
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np

from krx_quant.algorithms import ALGORITHMS, KIND_ALGORITHMS, Algorithm, KindAlgorithm
from krx_quant.algorithms.contract import MissingContext, Model, PredictionContext, Sample, forecast_of
from krx_quant.algorithms.forecasts import CLASSES, UNKNOWN, class_moves, number, param, with_outcomes
from krx_quant.algorithms.priors import (
    PRIOR_INPUTS,
    PRIOR_STRENGTH,
    PriorTally,
    prior_inputs,
    prior_of,
    training_priors,
)
from krx_quant.algorithms.turing import _FLOOR, BOOSTING, ROUNDS, Turing, _normalized
from krx_quant.algorithms.turing import CLASSES as TREE_CLASSES
from krx_quant.api import TERCILES, tercile, volatility_cuts
from krx_quant.baselines import Forecast
from krx_quant.climate import direction
from krx_quant.climate_prior import ArmStats, PairedLift, _lifted, _ratio, _value, _yes, arm_stats
from krx_quant.config import Settings
from krx_quant.consensus import slot
from krx_quant.error_breakdown import Key, read_sigmas
from krx_quant.features import FeatureVector, Leaders
from krx_quant.feeds.base import KST
from krx_quant.footprint import DayUsage, Meter, give_back
from krx_quant.kind_registry import KINDS
from krx_quant.kinds import Kind
from krx_quant.market_calendar import calendar_store
from krx_quant.poisson_link import Confusion, confusion
from krx_quant.predictions import DB_NAME, iso, read_only
from krx_quant.prevalidation import PREVALIDATION_DAYS, _WalkForward
from krx_quant.pricing import Direction
from krx_quant.sample_store import night_reads
from krx_quant.stats import Scored
from krx_quant.training import finished_days, is_recorded

log = logging.getLogger(__name__)

EXPERIMENT = "turing-prior-448"
REPORT = f"experiments/{EXPERIMENT}.json"  # under the state directory
CLIMATE_ARM, TURING_ARM = "climate", "turing"
SYMBOL_ARM, PRIOR_ARM, BOTH_ARM = "turing_symbol", "turing_prior", "turing_symbol_prior"
VARIANTS = (SYMBOL_ARM, PRIOR_ARM, BOTH_ARM)  # (c), (d), (e): the arms that add inputs to turing
ARMS = (CLIMATE_ARM, TURING_ARM, *VARIANTS)
LABELS = {
    CLIMATE_ARM: "(a) climate",
    TURING_ARM: "(b) turing",
    SYMBOL_ARM: "(c) turing + symbol",
    PRIOR_ARM: "(d) turing + prior",
    BOTH_ARM: "(e) turing + both",
}
SYMBOL = "symbol"  # the symbol's input, and its codes' parameters: symbol.<symbol>
NO_SIGMA = "none"  # the bar closes without σ̂₄, after the terciles (``api.TERCILES``)
VOLATILITY = (*TERCILES, NO_SIGMA)


def inputs_of(scored: Sequence[Sample]) -> np.ndarray:
    """Turing's inputs of ``scored``, a row each, as ``Turing.fit`` makes them: its features in its
    order, NaN where unknown."""
    rows = [[number(s.features[n]) for n in Turing.features] for s in scored]
    return np.array(rows, dtype=float).reshape(len(scored), len(Turing.features))


def symbol_codes(scored: Sequence[Sample]) -> dict[str, int]:
    """The symbols of ``scored``, each a code of the trees' categorical input: in their order."""
    return {symbol: i for i, symbol in enumerate(sorted({s.symbol for s in scored}))}


class TuringPlus:
    """Turing's trees with the symbol, the climate prior or both as inputs more: (c), (d) and (e)
    (module doc). An experiment's arm: not registered, and the engine never runs it."""

    card = Turing.card
    features = Turing.features
    training_days = Turing.training_days
    param_meanings = Turing.param_meanings

    def __init__(self, *, symbol: bool = False, prior: bool = False) -> None:
        if not (symbol or prior):
            raise ValueError("an arm adds the symbol, the prior or both: turing alone is (b)")
        self.symbol, self.prior = symbol, prior
        self.added: tuple[str, ...] = ((SYMBOL,) if symbol else ()) + (PRIOR_INPUTS if prior else ())
        self.name = "-".join(["turing", *(["symbol"] if symbol else []), *(["prior"] if prior else [])])
        self._trees: tuple[str, lgb.Booster] | None = None  # the last model's, read once

    def default_model(self) -> Model:
        """No trees before training: a third each, as turing's."""
        return Model(f"{self.name}-default", Turing().default_model().params)

    def fit(self, samples: Sequence[Sample]) -> Model:
        """Trained as turing is, on the samples with a label, with the inputs added (``fit_on``)."""
        scored = with_outcomes(samples)
        return self.fit_on(samples, scored, inputs_of(scored))

    def fit_on(self, samples: Sequence[Sample], scored: Sequence[Sample], base: np.ndarray) -> Model:
        """``fit`` with turing's inputs of ``scored`` (``inputs_of``) given: a day's arms share them.
        ``scored`` are ``samples``' with a label, in their order. The model keeps the symbols'
        codes, and the prior of all of ``samples``' labels."""
        codes = symbol_codes(scored) if self.symbol else {}
        x = np.hstack([base, self.training_inputs(samples, scored, codes)])
        y = np.array([TREE_CLASSES.index(s.outcome or Direction.FLAT) for s in scored])  # all have one
        data = lgb.Dataset(
            x,
            y,
            feature_name=[*self.features, *self.added],
            categorical_feature=[SYMBOL] if self.symbol else "auto",
            free_raw_data=True,
        )
        booster = lgb.train(BOOSTING, data, num_boost_round=ROUNDS)
        probabilities = np.asarray(booster.predict(x, num_threads=1), dtype=float)
        loss = -float(np.mean(np.log(np.maximum(probabilities[np.arange(len(y)), y], _FLOOR))))
        params = {"rounds": float(ROUNDS)} | class_moves(scored)
        params |= {param(SYMBOL, symbol): float(code) for symbol, code in codes.items()}
        if self.prior:
            tally = PriorTally()
            tally.add(samples)
            params |= tally.table()
        return Model.fitted(self.name, params, scored, loss, blob=booster.model_to_string())

    def training_inputs(
        self, samples: Sequence[Sample], scored: Sequence[Sample], codes: Mapping[str, int]
    ) -> np.ndarray:
        """The added inputs of ``scored``, a row each (``added``'s order): the symbol's code, and
        each sample's causal prior (``priors.training_priors``: the days of ``samples`` before its
        own only)."""
        columns = []
        if self.symbol:
            columns.append(np.array([float(codes[s.symbol]) for s in scored], dtype=float).reshape(-1, 1))
        if self.prior:
            columns.append(
                np.array(training_priors(samples, scored), dtype=float).reshape(-1, len(PRIOR_INPUTS))
            )
        return np.hstack(columns)

    def predict(self, features: FeatureVector, model: Model) -> Forecast:
        """Never: the arm predicts with the symbol and the bar close (``predict_with_context``)."""
        raise MissingContext(f"{self.name} predicts with the symbol and the bar close (predict_with_context)")

    def predict_with_context(
        self, features: FeatureVector, model: Model, context: PredictionContext
    ) -> Forecast:
        """The trees' forecast of ``features`` and the inputs added at the context's symbol and bar
        close, from ``model``'s codes and prior; a third each before training, as turing."""
        if model.blob is None:
            return UNKNOWN
        p = model.params
        added: list[float] = []
        if self.symbol:
            added.append(p.get(param(SYMBOL, context.symbol), math.nan))
        if self.prior:
            added.extend(prior_inputs(prior_of(p, context.symbol, context.bar_close)))
        row = np.array([[*(number(features[n]) for n in self.features), *added]], dtype=float)
        p_down, p_flat, p_up = _normalized(self._trees_of(model).predict(row, num_threads=1)[0])
        expected = p_up * p["move.up"] + p_flat * p["move.flat"] + p_down * p["move.down"]
        return Forecast(p_up, p_flat, p_down, expected)

    def _trees_of(self, model: Model) -> lgb.Booster:
        if self._trees is None or self._trees[0] != model.version:
            self._trees = (model.version, lgb.Booster(model_str=model.blob))
        return self._trees[1]


def added_gain(model: Model, added: Sequence[str]) -> dict[str, float] | None:
    """The share of all the trees' gain each of the ``added`` inputs took; None without trees."""
    if model.blob is None:
        return None
    trees = lgb.Booster(model_str=model.blob)
    gains = dict(zip(trees.feature_name(), trees.feature_importance("gain"), strict=True))
    total = float(sum(gains.values()))
    return {name: float(gains.get(name, 0.0)) / total if total > 0 else 0.0 for name in added}


@dataclass(frozen=True)
class Fold:
    """A day of the experiment: what predicted it, on how many samples it was trained, and how
    many of its bar closes were scored."""

    day: date
    models: dict[str, str]  # arm -> the version of the model (the climate's table) that predicted it
    trained_on: int  # the training samples (b) to (e) learned from: with a label
    scored: int  # its bar closes with a label: every arm predicted them

    def as_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "models": self.models,
            "trained_on": self.trained_on,
            "scored": self.scored,
        }


@dataclass(frozen=True)
class Compared:
    """An arm on some bar closes: its statistics, its confusion matrix, its lift over the climate
    (none for the climate itself) and over (b) (for (c) to (e) only)."""

    stats: ArmStats
    confusion: Confusion
    over_climate: PairedLift | None
    over_turing: PairedLift | None

    def as_dict(self) -> dict[str, Any]:
        return self.stats.as_dict() | {
            "confusion": {c.value: {h.value: n for h, n in row.items()} for c, row in self.confusion.items()},
            "over_climate": None if self.over_climate is None else self.over_climate.as_dict(),
            "over_turing": None if self.over_turing is None else self.over_turing.as_dict(),
        }


def compared(records: Mapping[str, Sequence[Scored]]) -> dict[str, Compared]:
    """Each arm of ``records`` (every arm's, on the same bar closes) compared."""
    climate, turing = records[CLIMATE_ARM], records[TURING_ARM]
    return {
        arm: Compared(
            arm_stats(rs),
            confusion(rs),
            None if arm == CLIMATE_ARM else PairedLift.of(rs, climate),
            PairedLift.of(rs, turing) if arm in VARIANTS else None,
        )
        for arm, rs in records.items()
    }


def split(
    records: Mapping[str, Sequence[Scored]], cell: Callable[[Scored], str], cells: Sequence[str]
) -> dict[str, dict[str, Compared]]:
    """``records`` compared in each of ``cells`` (the cell of each bar close: ``cell``); a cell
    without a bar close is left out."""
    grouped: dict[str, dict[str, list[Scored]]] = {c: {arm: [] for arm in records} for c in cells}
    for arm, rs in records.items():
        for r in rs:
            grouped[cell(r)][arm].append(r)
    return {c: compared(by_arm) for c, by_arm in grouped.items() if by_arm[CLIMATE_ARM]}


def volatility_of(sigmas: Mapping[Key, float]) -> Callable[[Scored], str]:
    """The σ̂₄ cell of a bar close (module doc): its symbol's tercile (``api.tercile``) by the cuts
    of ``sigmas``, or ``none`` without σ̂₄ there."""
    cuts = volatility_cuts(sigmas)

    def cell(r: Scored) -> str:
        sigma = sigmas.get((r.symbol, iso(r.bar_close)))
        return NO_SIGMA if sigma is None else tercile(sigma, cuts[r.symbol])

    return cell


@dataclass(frozen=True)
class Experiment:
    made_at: datetime
    days: list[date]  # the days predicted, oldest first
    folds: list[Fold]
    arms: dict[str, Compared]  # on every bar close compared
    by_volatility: dict[str, dict[str, Compared]]  # σ̂₄ cell -> arm -> ...
    by_slot: dict[str, dict[str, Compared]]  # 30-minute slot -> arm -> ...
    with_sigma: int  # the bar closes compared with σ̂₄
    gain: dict[str, dict[str, float] | None]  # (c) to (e): the last day's model's gain shares added
    records: dict[str, list[Scored]] = field(repr=False)  # each arm's, as scored (not in the report)

    def better(self, arm: str) -> bool | None:
        """``arm`` better than (b): its lift over (b) above 0; None without a pair."""
        over = self.arms[arm].over_turing
        value = None if over is None else over.pairs.value
        return None if value is None else value > 0

    def lower_bound_up(self, arm: str) -> bool | None:
        """``arm``'s lower bound by trading day over the climate above (b)'s; None without both."""
        ours, theirs = (self.arms[a].over_climate for a in (arm, TURING_ARM))
        lows = [None if o is None or o.by_day is None else o.by_day.low for o in (ours, theirs)]
        if lows[0] is None or lows[1] is None:
            return None
        return lows[0] > lows[1]

    def candidate(self, arm: str) -> bool:
        """#448's rule (module doc): better than (b), and its lower bound by trading day over the
        climate above (b)'s."""
        return self.better(arm) is True and self.lower_bound_up(arm) is True

    @property
    def candidates(self) -> list[str]:
        return [arm for arm in VARIANTS if self.candidate(arm)]

    def as_dict(self) -> dict[str, Any]:
        return {
            "experiment": EXPERIMENT,
            "made_at": self.made_at.isoformat(),
            "days": [d.isoformat() for d in self.days],
            "turing_days": Turing.training_days,
            "prior": {"inputs": list(PRIOR_INPUTS), "strength": PRIOR_STRENGTH},
            "folds": [f.as_dict() for f in self.folds],
            "arms": {arm: c.as_dict() for arm, c in self.arms.items()},
            "by_volatility": {
                "with_sigma": self.with_sigma,
                "cells": {
                    cell: {arm: c.as_dict() for arm, c in arms.items()}
                    for cell, arms in self.by_volatility.items()
                },
            },
            "by_slot": {
                cell: {arm: c.as_dict() for arm, c in arms.items()} for cell, arms in self.by_slot.items()
            },
            "rule": {
                arm: {
                    "better": self.better(arm),
                    "lower_bound_up": self.lower_bound_up(arm),
                    "candidate": self.candidate(arm),
                }
                for arm in VARIANTS
            },
            "candidates": self.candidates,
            "gain": self.gain,
        }


def turing_prior_experiment(
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
    turing = Turing()
    variants = {
        SYMBOL_ARM: TuringPlus(symbol=True),
        PRIOR_ARM: TuringPlus(prior=True),
        BOTH_ARM: TuringPlus(symbol=True, prior=True),
    }
    trainer = _WalkForward(settings, turing, available, calendar, Leaders.of(settings), night=night)
    records: dict[str, list[Scored]] = {arm: [] for arm in ARMS}
    folds: list[Fold] = []
    last: dict[str, Model] = {arm: v.default_model() for arm, v in variants.items()}
    meter = Meter()
    for n, day in enumerate(predicted, 1):
        usage = DayUsage(meter)  # its memory, logged (#383)
        with usage.phase("training"):
            models, trained_on = _models(trainer, variants, day)
        with usage.phase("scoring"):
            scored = _score(trainer.samples_on(day), models, turing, variants, records)
        folds.append(Fold(day, {arm: m.version for arm, m in models.items()}, trained_on, scored))
        last = {arm: models[arm] for arm in VARIANTS}
        give_back()
        log.info("turing prior: %s predicted by %s", day, ", ".join(folds[-1].models.values()))
        log.info("turing prior: memory on %s (day %d of %d): %s", day, n, len(predicted), usage.line())
    with read_only(settings.state_dir / DB_NAME) as conn:
        sigmas = {} if conn is None else read_sigmas(conn, predicted)
    by_sigma = volatility_of(sigmas)
    slots = sorted({slot(r.bar_close) for r in records[CLIMATE_ARM]})
    result = Experiment(
        made_at=now,
        days=list(predicted),
        folds=folds,
        arms=compared(records),
        by_volatility=split(records, by_sigma, VOLATILITY),
        by_slot=split(records, lambda r: slot(r.bar_close), slots),
        with_sigma=sum(1 for r in records[CLIMATE_ARM] if by_sigma(r) != NO_SIGMA),
        gain={arm: added_gain(last[arm], variants[arm].added) for arm in VARIANTS},
        records=records,
    )
    path = settings.state_dir / REPORT
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
    tmp.replace(path)
    return result


def _models(
    trainer: _WalkForward, variants: Mapping[str, TuringPlus], day: date
) -> tuple[dict[str, Model], int]:
    """The arms' models for ``day``, each fitted on the days before it, and how many samples (b)
    to (e) learned from: (b) as its pre-validation trains it, (c) to (e) on the samples (b) was
    given, sharing turing's inputs of them."""
    models = {CLIMATE_ARM: trainer.climate_before(day), TURING_ARM: trainer.before(day)}
    window = trainer.samples_before(day)
    scored = [s for s in window if s.outcome is not None]
    if not scored:  # (b) has its defaults too
        return models | {arm: v.default_model() for arm, v in variants.items()}, 0
    base = inputs_of(scored)
    for arm, variant in variants.items():
        models[arm] = variant.fit_on(window, scored, base)
    return models, len(scored)


def _score(
    samples: Sequence[Sample],
    models: Mapping[str, Model],
    turing: Turing,
    variants: Mapping[str, TuringPlus],
    records: Mapping[str, list[Scored]],
) -> int:
    """Each arm's forecast of each of the day's bar closes with a label, onto ``records``, through
    the engine's dispatcher (``forecast_of``) with the bar close's symbol; the bar closes scored."""
    scored = 0
    for s in samples:
        if s.outcome is None:
            continue
        context = PredictionContext(s.symbol, s.bar_close)
        called = direction(models[CLIMATE_ARM], s.symbol, s.bar_close) or Direction.FLAT
        forecasts = {
            CLIMATE_ARM: Forecast.certain(called),  # as the baseline (``baselines.Climate``)
            TURING_ARM: forecast_of(turing, s.features, models[TURING_ARM], context),
        }
        for arm, variant in variants.items():
            forecasts[arm] = forecast_of(variant, s.features, models[arm], context)
        for arm, f in forecasts.items():
            version = models[arm].version
            records[arm].append(
                Scored(arm, version, s.symbol, s.bar_close, f.p_up, f.p_flat, f.p_down, s.outcome)
            )
        scored += 1
    return scored


# The command's lines


def lines(result: Experiment, report: Path) -> list[str]:
    """The command's output: counts, rates, lifts, shares and dates, no price, σ̂₄ or cut."""
    first, last = result.days[0], result.days[-1]
    trained = sum(1 for f in result.folds if not f.models[TURING_ARM].endswith("-default"))
    scored = sum(f.scored for f in result.folds)
    out = [
        f"turing symbol and prior experiment (#448): {len(result.days)} recorded days, {first}..{last}",
        f"  folds: each day predicted by models fitted on turing's {Turing.training_days} trading days before"
        f" it (the climate's on its 20); {trained} of {len(result.days)} days with a trained turing",
        f"  compared: {scored} bar closes, every arm on each",
        "  added: (c) the symbol, a categorical input; (d) the prior: class frequencies by symbol and"
        f" 30-minute slot, {PRIOR_STRENGTH:g} pseudo-counts toward the symbol's day, a training sample's from"
        " the days before its own; (e) both",
        "",
        *_table(result.arms),
        "",
        "by class: recall / precision (calls)",
    ]
    for arm in ARMS:
        a = result.arms[arm].stats
        parts = [
            f"{d.value} {_value(a.recall[d])} / {_value(a.precision[d])} ({a.predicted[d]})" for d in CLASSES
        ]
        out.append(f"{LABELS[arm]:<21} " + "   ".join(parts))
    happened = result.arms[CLIMATE_ARM].stats.happened
    out.append(f"{'happened':<21} " + "   ".join(f"{d.value} {happened[d]}" for d in CLASSES))
    out += ["", "confusion: called up, flat, down; each the bar closes that came up/flat/down"]
    out += [f"{LABELS[arm]:<21} {_confusion(result.arms[arm].confusion)}" for arm in ARMS]
    out += ["", "lift (hit rate gained on the same bar closes; paired, then by trading day)"]
    for arm in ARMS[1:]:
        out.append(f"{LABELS[arm] + ' over (a)':<29} {_lifted(_over(result.arms[arm].over_climate))}")
    for arm in VARIANTS:
        out.append(f"{LABELS[arm][:3] + ' over (b)':<29} {_lifted(_over(result.arms[arm].over_turing))}")
    out += ["", *_rule_lines(result)]
    out += ["", "the last day's trees: the share of their gain each added input took"]
    for arm in VARIANTS:
        gain = result.gain[arm]
        shown = (
            "untrained" if gain is None else ", ".join(f"{name} {share:.3f}" for name, share in gain.items())
        )
        out.append(f"{LABELS[arm]:<21} {shown}")
    out += [
        "",
        f"by σ̂₄ tercile at the bar close (#431's: each symbol's cuts from its bar closes of these days;"
        f" {result.with_sigma} of {scored} bar closes with σ̂₄, the rest in none)",
    ]
    out += _cells(result.by_volatility)
    out += ["", "by 30-minute slot (09:00 holds the bar closes 09:01-09:30)"]
    out += _cells(result.by_slot)
    out.append("")
    out.append(f"report: {report}")
    return out


def _over(lifted: PairedLift | None) -> PairedLift:
    assert lifted is not None  # every arm but the climate has one over it, (c) to (e) over (b)
    return lifted


def _table(arms: Mapping[str, Compared]) -> list[str]:
    out = ["arm                   n       hit rate (95%)             coverage  dir. precision  brier"]
    for arm in ARMS:
        a = arms[arm].stats
        out.append(
            f"{LABELS[arm]:<21} {a.n:<7} {_ratio(a.accuracy):<26} {_value(a.coverage):<9} "
            f"{_value(a.directional_precision):<15} {_value(a.brier)}"
        )
    return out


def _confusion(matrix: Confusion) -> str:
    return "   ".join(
        f"{called.value} " + "/".join(str(matrix[called][happened]) for happened in CLASSES)
        for called in CLASSES
    )


def _rule_lines(result: Experiment) -> list[str]:
    turing = result.arms[TURING_ARM].over_climate
    theirs = None if turing is None or turing.by_day is None else turing.by_day.low
    out = []
    for arm in VARIANTS:
        ours_lift = result.arms[arm].over_climate
        ours = None if ours_lift is None or ours_lift.by_day is None else ours_lift.by_day.low
        lows = "-" if ours is None or theirs is None else f"{ours:+.4f} against {theirs:+.4f}"
        out.append(
            f"#448 rule, {LABELS[arm][:3]}: over (b) above 0: {_yes(result.better(arm))}; lower bound by"
            f" trading day over the climate above (b)'s: {_yes(result.lower_bound_up(arm))} ({lows}) -> "
            f"{'a candidate' if result.candidate(arm) else 'not a candidate'}"
        )
    chosen = ", ".join(LABELS[arm] for arm in result.candidates)
    out.append(
        f"candidates: {chosen}; none goes into turing from this (its code digest would change and its"
        " pre-validation run again; the final verdict is on the days after)"
        if chosen
        else "candidates: none; turing stays as it is"
    )
    return out


def _cells(cells: Mapping[str, Mapping[str, Compared]]) -> list[str]:
    """A table of each cell: the arms' hit rates, coverage, Brier scores, lifts and confusion."""
    out = []
    for name, arms in cells.items():
        climate = arms[CLIMATE_ARM].stats
        out.append(
            f"  {name}: {climate.n} bar closes; came up/flat/down "
            + "/".join(str(climate.happened[d]) for d in CLASSES)
        )
        for arm in ARMS:
            c = arms[arm]
            out.append(
                f"    {LABELS[arm][:3]} hit {_value(c.stats.accuracy)} cov {_value(c.stats.coverage)}"
                f" brier {_value(c.stats.brier)} | over (a) {_short(c.over_climate)}"
                f" | over (b) {_short(c.over_turing)} | {_confusion(c.confusion)}"
            )
    return out


def _short(lifted: PairedLift | None) -> str:
    """A lift in a cell: paired, and the lower bound by trading day (``day low``)."""
    if lifted is None or lifted.pairs.value is None:
        return "-"
    day = lifted.by_day
    low = "-" if day is None or day.low is None else f"{day.low:+.4f}"
    return f"{lifted.pairs.value:+.4f} (day low {low})"
