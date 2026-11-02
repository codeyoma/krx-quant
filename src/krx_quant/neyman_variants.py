"""Neyman's variants experiment (#455, ``krx-quant neyman-variants``, the scheduled job
``neyman-variants-283``): would neyman do better with one of the three changes #283's diagnosis put
forward, judged on days none of them was chosen on?

#283's diagnosis of neyman's pre-validation (2026-10-12–10-30, #204) put forward three changes, in
the order the user set (2026-11-02):

- (1) **the flips**: of the first model's 7,580 up and down calls, 1,397 (18%) turned to the
  opposite side after the second opinion, and on those the first side was right more often (41.7%)
  than the side they turned to (38.2%);
- (4) **the leak**: ``slot_return_5d`` and ``slot_return_20d`` hold a little of the days the folds
  held out (the embargo is one day);
- (3) **the distance over the volatility**: ``barrier_sigma`` (w ÷ σ̂₄) stays near 1 when the market
  moves, and says little then.

All three were chosen on those 15 days, so the rule is fixed before the run (pre-registered) and
they are judged on the days after only. Neyman's code is not changed: it runs on probation since
2026-11-02, and the engine keeps running a stateful bar-close algorithm whatever its report or code
(``Engine._admit``), so a change to its package would run, live, a model no new day judged. This
module is outside its package: its code digest (``prevalidation.code_digest``) stays. Adopting a
candidate is #454's way, and the user's decision.

**The arms**, on the same bar closes of the same days:

- **A**, neyman as it is: neyman's fit (its ``folds``, its first model's settings, ``_second_model``
  and ``class_moves``: what ``neyman.fitted`` does, keeping the fold models it lets go) and its
  forecast (``_second_opinion``); a test checks it is ``fitted``'s model and ``predict``'s forecasts;
- **B**, A with its flips swapped back (``swapped``): where the final call (the most likely class
  after the second opinion) is the opposite side of the first model's up or down call, the final
  p_up and p_down change places; flat, a call kept and a call turned to flat stay as they are. The
  expected ticks are taken again from the model's class moves;
- **C**, A without ``slot_return_5d`` and ``slot_return_20d`` (``WITHOUT``): its first model and its
  fold models alike, as a second model learned on probabilities of other trees than those it is
  applied to would not fit them;
- **D**, C with r = (k + h) × ``barrier_sigma`` ÷ ``barrier_width`` = (k + h) ÷ σ̂₄ as a second-model
  input more (``cost_distance``): k the barrier's flat band (the ``flat_ticks`` feature: the
  snapshot's ``pricing.flat_ticks`` of the reference price at the fee rate, the k the barrier's
  width is never below; whole ticks, not the cost they round up), h half the spread
  (``spread_ticks`` ÷ 2; 0.5 without a book, ``features.DEFAULT_HALF_SPREAD``). Unknown without σ̂₄
  or with σ̂₄ 0 (``barrier_sigma`` unknown): then at its training mean, as neyman's other inputs.

No arm combines them; D − C shows what r adds and is not judged.

**Training.** Each day is predicted by arms fitted on the 20 trading days before it, as neyman's
pre-validation's replay trains it (``prevalidation._WalkForward``: the same samples, read from the
sample store when a night stored them; the same settings and seeds). A and B share their first
models, C and D theirs. Each arm forecasts every bar close of the day with a label: the samples
training reads of the day, so a VI or a special situation is left out, as the statistics leave
them out. Before any labelled training day, every arm says a third each, as neyman does.

**Discovery** (``Discovery``, the first run, never judged). On neyman's pre-validation days
(``DISCOVERY``), for B − A, C − A and D − A, the standard deviation s_d of the days' mean paired
gains (a day's mean of correct − correct on its bar closes); N = max(``LEAST``, ⌈((``Z_LEVEL`` +
``Z_POWER``) × s_d ÷ ``DELTA``)²⌉) days: a lift of 0.3 points found with 80% power, two-sided at
98.333% (1 − 0.05 ÷ 3: the three comparisons). The largest of the three is the window's N, kept in
the report with the window's start and its days' least scored bar closes, and never computed again.
Above ``DECIDE_ABOVE`` (60) days the user decides before the evaluation whether to drop comparisons
or to wait: from the discovery days alone, so the verdict stays clean.

**The window** (``Evaluation``): the first N recorded days after ``AFTER`` (2026-11-02) with at
least ``MIN_SCORED`` (200) bar closes every arm scored; fixed, so a later run reads the same days.
Whether a day is in goes by its data, never by its results: they are counted before any arm is
fitted (``_count``), and nothing is fitted until N of them are in.

**The verdict** (fixed before the run, ``Verdict``): B, C and D each against A; each day's mean
paired gain in hit rate is one value, every day weighing the same (``stats.day_lift``), with a
two-sided ``LEVEL`` (98.333%) Student t interval (Bonferroni over the three). An arm is a
**candidate** when its paired lift is above 0 and the lower bound by trading day too. Judged once,
when the window is full: no verdict before, and the window is never made longer.

Described, never judged: each arm's Brier score, log loss and lift over the climate baseline (the
table refitted before each day on the 20 days before it); the hit rates on the bar closes B swapped;
the lifts by 30-minute slot (``consensus.slot``) and by σ̂₄'s tercile (σ̂₄ = w ÷ ``barrier_sigma``,
each symbol's cuts from the window's bar closes, ``api.volatility_cuts``); D − C; and how the
verdict's interval moves if the days depend on each other (``stats.day_lift`` takes them as
independent): the days' lag-1 autocorrelation and the interval by trading day as if they were
windows of two and three sessions (``stats.overlap_t_test``).

**(2)'s diagnosis** (``_Diagnosis``, the same report, never judged), on the window's days: the same
rows predicted by A's final first model and by the mean of its fold models: their probabilities'
differences, calibration (``stats.summarize``'s bins), up and down call share and precision, and the
classes called; A's second model's residuals (right − m on the final first model's up and down
calls) by day, by symbol and by the training window's recorded and backfilled days; and the second
model's inputs (D's r with them) live against out of sample (the fold models' calls): the shares
unknown, beyond the out-of-sample 1st and 99th percentiles, and the KS distance. The folds' mean
mixes a smaller training set with an ensemble: the two effects are not told apart.

**The report** (``state/experiments/neyman-variants-283.json``): the discovery, then the evaluation;
each with its lines. A run after the verdict prints it again and computes nothing. Counts, rates,
lifts, scores and dates only: no price, σ̂₄ or cut.

**Memory.** A pre-validation's training phase's, without its replay (#383): the window's days are
kept and the older let go as the days move on; each day logs its memory, then hands back what it
freed (``footprint.give_back``). The window's days are counted one at a time, each let go before the
next.

**Its cost.** A day fits ten tree models on the 20 days before it (about 1.7 s each on a Mac with
22,000 made-up samples; the server about three times slower) and reads one day more. Its days come
from the sample store, which keeps the nights' training days (about 61 trading days): with N above
about 40 the window's first days have left it when the window fills, and are made again (minutes a
recorded day).
"""

from __future__ import annotations

import json
import logging
import math
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol

import lightgbm as lgb
import numpy as np

from krx_quant.algorithms import ALGORITHMS, KIND_ALGORITHMS, Algorithm, KindAlgorithm
from krx_quant.algorithms.contract import Model, NothingToLearn, Sample
from krx_quant.algorithms.forecasts import LOSS_FLOOR, UNKNOWN, class_moves, number, with_outcomes
from krx_quant.algorithms.neyman import (
    _FLOOR,
    FEATURES,
    FIRST,
    INPUTS,
    MIN_CALLS,
    MIN_DAYS,
    SPREAD,
    TRAINING_DAYS,
    Neyman,
    _chance,
    _inputs,
    _logistic,
    _normalized,
    _rest,
    _second_model,
    _second_opinion,
    folds,
)
from krx_quant.algorithms.turing import CLASSES as TREE_CLASSES
from krx_quant.algorithms.turing import ROUNDS
from krx_quant.api import TERCILES, tercile, volatility_cuts
from krx_quant.baselines import Forecast
from krx_quant.climate import direction
from krx_quant.climate_prior import ArmStats, arm_stats
from krx_quant.config import Settings
from krx_quant.consensus import slot
from krx_quant.features import BARRIER_SIGMA, BARRIER_WIDTH, DEFAULT_HALF_SPREAD, FeatureVector, Leaders
from krx_quant.feeds.base import KST, kst_day
from krx_quant.footprint import DayUsage, Meter, give_back
from krx_quant.kind_registry import KINDS
from krx_quant.kinds import Kind
from krx_quant.market_calendar import calendar_store
from krx_quant.predictions import iso
from krx_quant.prevalidation import _WalkForward
from krx_quant.pricing import Direction, most_likely
from krx_quant.sample_store import night_reads
from krx_quant.stats import (
    Lift,
    Scored,
    day_lift,
    gains_by_day,
    lift,
    overlap_t_test,
    session_index,
    summarize,
)
from krx_quant.training import finished_days, is_recorded

log = logging.getLogger(__name__)

ISSUE = 455
EXPERIMENT = "neyman-variants-283"  # the scheduled job's name, as #455 set it (#283's follow-up)
REPORT = f"experiments/{EXPERIMENT}.json"  # under the state directory

# Fixed in #455 before the run: none of these is tuned.
DISCOVERY = (date(2026, 10, 12), date(2026, 10, 30))  # neyman's pre-validation's days (#204)
AFTER = date(2026, 11, 2)  # the window's days are after it: neyman's probation began on it
MIN_SCORED = 200  # a window day's bar closes every arm scored
LEAST = 30  # the window's days, at least
DECIDE_ABOVE = 60  # with more, the user decides before the evaluation
COMPARISONS = 3  # B, C and D against A: the Bonferroni divisor
LEVEL = 1 - 0.05 / COMPARISONS  # 98.333%, two-sided
Z_LEVEL = 2.394  # the two-sided z at LEVEL (#455's)
Z_POWER = 0.842  # the z of 80% power
DELTA = 0.003  # the lift to find: 0.3 points of hit rate

A, B, C, D = "A", "B", "C", "D"
CLIMATE_ARM = "climate"  # described only: the lift over the climate baseline
ARMS = (A, B, C, D)
VARIANTS = (B, C, D)  # each judged against A
LABELS = {
    A: "A neyman as it is",
    B: "B A, its flips swapped back",
    C: "C A without slot_return_5d, slot_return_20d",
    D: "D C, r a second-model input more",
    CLIMATE_ARM: "climate",
}
DROPPED = ("slot_return_5d", "slot_return_20d")
WITHOUT: tuple[str, ...] = tuple(n for n in FEATURES if n not in DROPPED)  # C's and D's first models'
_KEPT = [FEATURES.index(n) for n in WITHOUT]
FLAT_TICKS = "flat_ticks"  # k, the barrier's flat band
R = "cost_distance"  # D's input r
D_INPUTS = (*INPUTS, R)
NO_SIGMA = "none"  # the bar closes without σ̂₄, after the terciles
VOLATILITY = (*TERCILES, NO_SIGMA)
SERIAL = (2, 3)  # the sessions a window of the sensitivity to serial dependence spans
PERCENTILES = (1, 99)  # the out-of-sample range a live input is held to

UP, FLAT, DOWN = Direction.UP, Direction.FLAT, Direction.DOWN
CALL_CLASSES = (UP, FLAT, DOWN)

Inputs = Callable[[float, float, float, Direction, FeatureVector], list[float]]


# The arms


def cost_distance(features: FeatureVector) -> float:
    """D's input r = (k + h) × ``barrier_sigma`` ÷ ``barrier_width``, which is (k + h) ÷ σ̂₄ (σ̂₄ =
    w ÷ ``barrier_sigma``): the cost's distance in four-minute volatilities. k is the ``flat_ticks``
    feature (the barrier's flat band, whole ticks), h half the spread (``spread_ticks`` ÷ 2, 0.5
    without a book). NaN without σ̂₄, or with σ̂₄ 0 (``barrier_sigma`` unknown), or without k or w."""
    k, width = number(features[FLAT_TICKS]), number(features[BARRIER_WIDTH])
    ratio, spread = number(features[BARRIER_SIGMA]), number(features[SPREAD])
    if math.isnan(k) or math.isnan(width) or math.isnan(ratio) or width <= 0 or ratio <= 0:
        return math.nan
    h = DEFAULT_HALF_SPREAD if math.isnan(spread) else spread / 2
    return (k + h) * ratio / width


def d_inputs(
    p_up: float, p_flat: float, p_down: float, call: Direction, features: FeatureVector
) -> list[float]:
    """What D's second model reads of a call: neyman's inputs (``_inputs``), then r."""
    return [*_inputs(p_up, p_flat, p_down, call, features), cost_distance(features)]


def swapped(trees: Sequence[float] | Any, forecast: Forecast, params: Mapping[str, float]) -> Forecast:
    """B: ``forecast`` (A's, from the first model's ``trees``: down, flat, up) with p_up and p_down
    swapped where its call is the opposite side of the first model's up or down call; as it is
    where the first model called flat or the final call is flat or the first model's. The expected
    ticks again from the class moves."""
    p_down, p_flat, p_up = _normalized(trees)
    call = most_likely(p_up, p_flat, p_down)
    final = most_likely(forecast.p_up, forecast.p_flat, forecast.p_down)
    if call is FLAT or final is FLAT or final is call:
        return forecast
    up, down = forecast.p_down, forecast.p_up
    expected = up * params["move.up"] + forecast.p_flat * params["move.flat"] + down * params["move.down"]
    return Forecast(up, forecast.p_flat, down, expected)


def second_model(
    scored: Sequence[Sample], out_of_fold: np.ndarray, names: Sequence[str] = INPUTS, inputs: Inputs = _inputs
) -> dict[str, float]:
    """Neyman's second model (``_second_model``, the same steps) on the inputs ``names``, a call's
    as ``inputs`` gives them: D's are neyman's and r."""
    rows: list[list[float]] = []
    right: list[float] = []
    days: set[date] = set()
    for p, sample in zip(out_of_fold, scored, strict=True):
        if np.isnan(p).any():
            continue
        p_down, p_flat, p_up = _normalized(p)
        call = most_likely(p_up, p_flat, p_down)
        if call is FLAT:
            continue
        rows.append(inputs(p_up, p_flat, p_down, call, sample.features))
        right.append(float(sample.outcome is call))
        days.add(kst_day(sample.bar_close))
    params: dict[str, float] = {"meta.calls": float(len(right)), "meta.days": float(len(days))}
    params["meta.precision"] = sum(right) / len(right) if right else 0.0
    data = np.array(rows, dtype=float).reshape(len(rows), len(names))
    means = [float(np.nanmean(c)) if np.isfinite(c).any() else 0.0 for c in data.T]
    scales = []
    for c in data.T:
        known = c[np.isfinite(c)]
        spread = float(np.std(known)) if known.size else 0.0
        scales.append(spread if spread > 0 else 1.0)
    if len(right) >= MIN_CALLS and len(days) >= MIN_DAYS:
        standard = np.nan_to_num((data - np.array(means)) / np.array(scales), nan=0.0)
        weights = _logistic(standard, np.array(right))
    else:
        share = min(max(params["meta.precision"], _FLOOR), 1 - _FLOOR)
        weights = np.zeros(len(names) + 1)
        weights[0] = math.log(share / (1 - share)) if right else 0.0
    params["meta.intercept"] = float(weights[0])
    for name, w, mean, scale in zip(names, weights[1:], means, scales, strict=True):
        params |= {f"meta.coef.{name}": float(w), f"meta.mean.{name}": mean, f"meta.scale.{name}": scale}
    return params


def chance(params: Mapping[str, float], values: Sequence[float], names: Sequence[str] = INPUTS) -> float:
    """Neyman's ``_chance`` on the inputs ``names``: an unknown value at its training mean."""
    z = params["meta.intercept"]
    for name, v in zip(names, values, strict=True):
        if math.isfinite(v):
            standard = (v - params[f"meta.mean.{name}"]) / params[f"meta.scale.{name}"]
            z += params[f"meta.coef.{name}"] * standard
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))


def opinion(
    trees: Sequence[float] | Any,
    features: FeatureVector,
    params: Mapping[str, float],
    names: Sequence[str] = INPUTS,
    inputs: Inputs = _inputs,
) -> Forecast:
    """Neyman's ``_second_opinion`` with a second model on the inputs ``names`` (D's)."""
    p_down, p_flat, p_up = _normalized(trees)
    call = most_likely(p_up, p_flat, p_down)
    if call is not FLAT and params["meta.calls"] > 0:
        m = chance(params, inputs(p_up, p_flat, p_down, call, features), names)
        if call is UP:
            p_up, (p_flat, p_down) = m, _rest(1 - m, p_flat, p_down)
        else:
            p_down, (p_flat, p_up) = m, _rest(1 - m, p_flat, p_up)
    expected = p_up * params["move.up"] + p_flat * params["move.flat"] + p_down * params["move.down"]
    return Forecast(p_up, p_flat, p_down, expected)


def _boost(x: np.ndarray, y: np.ndarray, features: Sequence[str]) -> lgb.Booster:
    """A first model on ``x`` and ``y`` (``features``' columns), as neyman's ``_boost`` trains it."""
    data = lgb.Dataset(x, y, feature_name=list(features), free_raw_data=True)
    return lgb.train(FIRST, data, num_boost_round=ROUNDS)


@dataclass(frozen=True)
class FirstModels:
    """A first model on ``features``, its fold models (``neyman.folds``) and their out-of-sample
    probabilities (down, flat, up) of the training samples (NaN where no fold holds one)."""

    features: tuple[str, ...]
    final: lgb.Booster
    folds: list[lgb.Booster]
    out_of_fold: np.ndarray

    def predict(self, x: np.ndarray) -> np.ndarray:
        return np.asarray(self.final.predict(x, num_threads=1), dtype=float).reshape(-1, 3)

    def fold_mean(self, x: np.ndarray) -> np.ndarray | None:
        """The fold models' mean probabilities of ``x``; None without fold models."""
        if not self.folds:
            return None
        each = [np.asarray(f.predict(x, num_threads=1), dtype=float).reshape(-1, 3) for f in self.folds]
        mean: np.ndarray = np.mean(each, axis=0)
        return mean


def first_models(
    scored: Sequence[Sample], x: np.ndarray, y: np.ndarray, features: Sequence[str]
) -> FirstModels:
    """The first models of ``scored`` as neyman's fit makes them (``fitted``: the final one, then one
    per fold), the fold models kept."""
    final = _boost(x, y, features)
    out_of_fold = np.full((len(scored), len(TREE_CLASSES)), np.nan)
    kept = []
    for held, trained in folds(scored):
        model = _boost(x[trained], y[trained], features)
        out_of_fold[held] = np.asarray(model.predict(x[held], num_threads=1))
        kept.append(model)
    return FirstModels(tuple(features), final, kept, out_of_fold)


def rows(samples: Sequence[Sample]) -> np.ndarray:
    """Neyman's features of ``samples``, a row each in its order, NaN where unknown."""
    values = [[number(s.features[n]) for n in FEATURES] for s in samples]
    return np.array(values, dtype=float).reshape(len(samples), len(FEATURES))


@dataclass(frozen=True)
class Fit:
    """The arms fitted on a day's window: A's and B's first models (neyman's), C's and D's (without
    the two slot returns), and A's, C's and D's second models with their class moves (B's are A's)."""

    trained_on: int  # the window's samples with a label
    recorded: int  # the window's recorded days
    backfilled: int  # and its days with backfilled bars only
    first: FirstModels
    without: FirstModels
    params: dict[str, dict[str, float]]  # A, C and D
    # What D's second model would read of A's fold models' up and down calls (``d_inputs``), a row
    # each: (2)'s out-of-sample inputs; None unless asked for.
    out_of_sample: np.ndarray | None = None

    @property
    def composition(self) -> str:
        """The window's days, recorded + backfilled."""
        return f"{self.recorded}+{self.backfilled}"


def fit_arms(window: Sequence[Sample], *, diagnose: bool = False) -> Fit | None:
    """The arms fitted on ``window`` (neyman's training samples of a day); None without a sample
    with a label (every arm then says a third each, as neyman does untrained). With ``diagnose``,
    the inputs of A's out-of-sample calls too (``Fit.out_of_sample``)."""
    try:
        scored = with_outcomes(window)
    except NothingToLearn:
        return None
    x = rows(scored)
    y = np.array([TREE_CLASSES.index(s.outcome or FLAT) for s in scored])  # all have one
    first = first_models(scored, x, y, FEATURES)
    without = first_models(scored, x[:, _KEPT], y, WITHOUT)
    moves = {"rounds": float(ROUNDS)} | class_moves(scored)
    params = {
        A: moves | _second_model(scored, first.out_of_fold),
        C: moves | _second_model(scored, without.out_of_fold),
        D: moves | second_model(scored, without.out_of_fold, D_INPUTS, d_inputs),
    }
    days = {kst_day(s.bar_close) for s in window}
    recorded = {kst_day(s.bar_close) for s in window if s.reference == "mid"}
    calls = _call_inputs(scored, first.out_of_fold) if diagnose else None
    return Fit(len(scored), len(recorded), len(days - recorded), first, without, params, calls)


def _call_inputs(scored: Sequence[Sample], out_of_fold: np.ndarray) -> np.ndarray:
    """D's second-model inputs (``d_inputs``) of the up and down calls in ``out_of_fold``, a row each."""
    found = []
    for p, sample in zip(out_of_fold, scored, strict=True):
        if np.isnan(p).any():
            continue
        p_down, p_flat, p_up = _normalized(p)
        call = most_likely(p_up, p_flat, p_down)
        if call is not FLAT:
            found.append(d_inputs(p_up, p_flat, p_down, call, sample.features))
    return np.array(found, dtype=float).reshape(len(found), len(D_INPUTS))


@dataclass(frozen=True)
class Forecasts:
    """Each arm's forecasts of a day's bar closes, and A's first model's probabilities (down, flat,
    up) of them (None untrained)."""

    by_arm: dict[str, list[Forecast]]
    first: np.ndarray | None
    x: np.ndarray | None


def forecasts(fit: Fit | None, samples: Sequence[Sample]) -> Forecasts:
    """Each arm's forecast of each of ``samples``."""
    if fit is None:
        return Forecasts({arm: [UNKNOWN] * len(samples) for arm in ARMS}, None, None)
    x = rows(samples)
    first, without = fit.first.predict(x), fit.without.predict(x[:, _KEPT])
    out: dict[str, list[Forecast]] = {arm: [] for arm in ARMS}
    for i, s in enumerate(samples):
        a = _second_opinion(first[i], s.features, fit.params[A])
        out[A].append(a)
        out[B].append(swapped(first[i], a, fit.params[A]))
        out[C].append(_second_opinion(without[i], s.features, fit.params[C]))
        out[D].append(opinion(without[i], s.features, fit.params[D], D_INPUTS, d_inputs))
    return Forecasts(out, first, x)


# Walking the days


class Days(Protocol):
    """Where the days' samples come from: a pre-validation's walk-forward (``_WalkForward``)."""

    def samples_before(self, day: date) -> list[Sample]: ...

    def samples_on(self, day: date) -> list[Sample]: ...

    def climate_before(self, day: date) -> Model: ...


@dataclass(frozen=True)
class Fold:
    """A day predicted: on how many samples its arms learned, from which days, and its bar closes
    scored (every arm on each)."""

    day: date
    trained_on: int
    composition: str  # the window's recorded + backfilled days; "-" untrained
    scored: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "trained_on": self.trained_on,
            "window": self.composition,
            "scored": self.scored,
        }


class Collected:
    """The arms' records of the days walked, and what the descriptions read of them."""

    def __init__(self, diagnose: bool = False) -> None:
        self.records: dict[str, list[Scored]] = {arm: [] for arm in (*ARMS, CLIMATE_ARM)}
        self.folds: list[Fold] = []
        self.sigmas: dict[tuple[str, str], float] = {}  # σ̂₄ at a bar close, ticks: its tercile only
        self.diagnosis = _Diagnosis() if diagnose else None

    @property
    def days(self) -> list[date]:
        return [f.day for f in self.folds]

    def add(self, day: date, fit: Fit | None, climate: Model, samples: Sequence[Sample]) -> None:
        """``day``'s bar closes with a label (``samples``), each arm's forecast of them."""
        made = forecasts(fit, samples)
        for arm, made_by in made.by_arm.items():
            for s, f in zip(samples, made_by, strict=True):
                assert s.outcome is not None
                self.records[arm].append(
                    Scored(
                        arm,
                        f"{EXPERIMENT}-{arm}",
                        s.symbol,
                        s.bar_close,
                        f.p_up,
                        f.p_flat,
                        f.p_down,
                        s.outcome,
                    )
                )
        for s in samples:
            assert s.outcome is not None
            called = Forecast.certain(direction(climate, s.symbol, s.bar_close) or FLAT)
            self.records[CLIMATE_ARM].append(
                Scored(
                    CLIMATE_ARM, climate.version, s.symbol, s.bar_close, *_probabilities(called), s.outcome
                )
            )
            width, ratio = number(s.features[BARRIER_WIDTH]), number(s.features[BARRIER_SIGMA])
            if math.isfinite(width) and math.isfinite(ratio) and ratio > 0:
                self.sigmas[(s.symbol, iso(s.bar_close))] = width / ratio
        composition = "-" if fit is None else fit.composition
        self.folds.append(Fold(day, 0 if fit is None else fit.trained_on, composition, len(samples)))
        if self.diagnosis is not None and fit is not None and made.first is not None and made.x is not None:
            self.diagnosis.day(day, fit, samples, made.x, made.first)


def _probabilities(f: Forecast) -> tuple[float, float, float]:
    return f.p_up, f.p_flat, f.p_down


def _labelled(samples: Sequence[Sample]) -> list[Sample]:
    return [s for s in samples if s.outcome is not None]


def walk(
    trainer: Days, days: Sequence[date], min_scored: int, *, diagnose: bool = False
) -> tuple[Collected, list[date]]:
    """Each of ``days`` with at least ``min_scored`` bar closes with a label predicted by the arms
    fitted on the days before it; the days with fewer are left out (the second value). Its memory a
    day (#383)."""
    collected = Collected(diagnose)
    short: list[date] = []
    meter = Meter()
    for n, day in enumerate(days, 1):
        usage = DayUsage(meter)
        with usage.phase("training"):
            samples = _labelled(trainer.samples_on(day))
            enough = len(samples) >= min_scored
            fit = fit_arms(trainer.samples_before(day), diagnose=diagnose) if enough else None
            climate = trainer.climate_before(day) if enough else None
        if climate is None:
            short.append(day)
            log.info(
                "neyman variants: %s has %d scored bar closes, fewer than %d", day, len(samples), min_scored
            )
        else:
            with usage.phase("scoring"):
                collected.add(day, fit, climate, samples)
        del samples, fit
        give_back()
        log.info("neyman variants: memory on %s (day %d of %d): %s", day, n, len(days), usage.line())
    return collected, short


def _count(make: Callable[[], Days], day: date) -> int:
    """``day``'s bar closes with a label, read by a walk-forward of its own and let go at once: a
    window's day is counted before any arm is fitted, and none is kept for it."""
    count = len(_labelled(make().samples_on(day)))
    give_back()
    return count


# Discovery


@dataclass(frozen=True)
class Spread:
    """A comparison's days' mean paired gains over A: how many, their mean and standard deviation
    (s_d), and the days N it asks for; None from fewer than two days."""

    days: int
    mean: float | None
    sd: float | None
    n: int | None

    def as_dict(self) -> dict[str, Any]:
        return {"days": self.days, "mean": self.mean, "sd": self.sd, "n": self.n}


def required_days(sd: float, least: int = LEAST, delta: float = DELTA) -> int:
    """N = max(``least``, ⌈((``Z_LEVEL`` + ``Z_POWER``) × s_d ÷ ``delta``)²⌉): the days a lift of
    ``delta`` needs to be found with 80% power at ``LEVEL``, two-sided."""
    days = ((Z_LEVEL + Z_POWER) * sd / delta) ** 2
    # Rounded first so a whole number of days is not pushed up by float error.
    return max(least, math.ceil(round(days, 9)))


def spread(
    records: Sequence[Scored], baseline: Sequence[Scored], least: int = LEAST, delta: float = DELTA
) -> Spread:
    """``records``' days' mean paired gains over ``baseline``, and the N they ask for."""
    means = [sum(g) / len(g) for g in gains_by_day(records, baseline).values() if g]
    if len(means) < 2:
        return Spread(len(means), means[0] if means else None, None, None)
    sd = statistics.stdev(means)
    return Spread(len(means), statistics.fmean(means), sd, required_days(sd, least, delta))


@dataclass(frozen=True)
class Discovery:
    """The discovery run (never judged): its days, the arms on them, each comparison's spread, and
    the window it fixed (N days after ``after``, each with ``min_scored`` scored bar closes)."""

    made_at: datetime
    period: tuple[date, date]
    days: list[date]  # the period's recorded days, oldest first
    short: list[date]  # of them, those with fewer than min_scored scored bar closes: left out
    folds: list[Fold]
    arms: dict[str, ArmStats]
    spreads: dict[str, Spread]
    n: int
    after: date
    min_scored: int
    least: int
    delta: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "made_at": self.made_at.isoformat(),
            "period": [d.isoformat() for d in self.period],
            "days": [d.isoformat() for d in self.days],
            "short": [d.isoformat() for d in self.short],
            "folds": [f.as_dict() for f in self.folds],
            "arms": {arm: s.as_dict() for arm, s in self.arms.items()},
            "spreads": {arm: s.as_dict() for arm, s in self.spreads.items()},
            "rule": {
                "z_level": Z_LEVEL,
                "z_power": Z_POWER,
                "delta": self.delta,
                "least": self.least,
                "level": LEVEL,
                "decide_above": DECIDE_ABOVE,
            },
            "n": self.n,
            "after": self.after.isoformat(),
            "min_scored": self.min_scored,
        }


def discover(
    trainer: Days,
    days: Sequence[date],
    now: datetime,
    *,
    period: tuple[date, date] = DISCOVERY,
    after: date = AFTER,
    min_scored: int = MIN_SCORED,
    least: int = LEAST,
    delta: float = DELTA,
) -> Discovery:
    """The arms on ``days`` (the period's recorded days) and the window's N. Raises ValueError when
    a comparison has fewer than two days: N cannot be set."""
    collected, short = walk(trainer, days, min_scored)
    records = collected.records
    spreads = {arm: spread(records[arm], records[A], least, delta) for arm in VARIANTS}
    found = [s.n for s in spreads.values() if s.n is not None]
    if len(found) < len(spreads):
        raise ValueError(
            f"neyman variants: fewer than two discovery days with {min_scored} scored bar closes"
            f" ({len(collected.days)} of {len(days)}): N cannot be set"
        )
    return Discovery(
        now,
        period,
        list(days),
        short,
        collected.folds,
        {arm: arm_stats(records[arm]) for arm in ARMS},
        spreads,
        max(found),
        after,
        min_scored,
        least,
        delta,
    )


# The verdict and what is described


@dataclass(frozen=True)
class Verdict:
    """A variant against A: its paired lift (by bar close) and by trading day, both at ``LEVEL``."""

    pairs: Lift
    by_day: Lift | None

    @property
    def candidate(self) -> bool:
        """#455's rule: the paired lift above 0, and the lower bound by trading day too."""
        value, low = self.pairs.value, None if self.by_day is None else self.by_day.low
        return value is not None and value > 0 and low is not None and low > 0

    @classmethod
    def of(cls, records: Sequence[Scored], baseline: Sequence[Scored]) -> Verdict:
        return cls(lift(records, baseline, LEVEL), day_lift(records, baseline, LEVEL))

    def as_dict(self) -> dict[str, Any]:
        return {"pairs": _lift(self.pairs), "by_day": None if self.by_day is None else _lift(self.by_day)}


def _lift(lifted: Lift) -> dict[str, Any]:
    return {"n": lifted.n, "value": lifted.value, "low": lifted.low, "high": lifted.high, "p": lifted.p}


def log_loss(records: Sequence[Scored]) -> float | None:
    """The mean log loss of ``records`` (each class's probability floored at ``LOSS_FLOOR``)."""
    if not records:
        return None
    return -statistics.fmean(math.log(max(r.probability(r.outcome), LOSS_FLOOR)) for r in records)


def _cells(
    records: Mapping[str, Sequence[Scored]], cell: Callable[[Scored], str], cells: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """In each of ``cells`` (the cell of each bar close: ``cell``), its bar closes, A's hit rate and
    each variant against A; a cell without a bar close is left out."""
    grouped: dict[str, dict[str, list[Scored]]] = {c: {arm: [] for arm in ARMS} for c in cells}
    for arm in ARMS:
        for r in records[arm]:
            grouped[cell(r)][arm].append(r)
    out: dict[str, dict[str, Any]] = {}
    for c, by_arm in grouped.items():
        if not by_arm[A]:
            continue
        out[c] = {
            "n": len(by_arm[A]),
            "hit": {arm: sum(r.correct for r in rs) / len(rs) for arm, rs in by_arm.items()},
            "over_a": {arm: Verdict.of(by_arm[arm], by_arm[A]).as_dict() for arm in VARIANTS},
        }
    return out


def _sigma_cell(sigmas: Mapping[tuple[str, str], float]) -> Callable[[Scored], str]:
    cuts = volatility_cuts(sigmas)

    def cell(r: Scored) -> str:
        sigma = sigmas.get((r.symbol, iso(r.bar_close)))
        return NO_SIGMA if sigma is None else tercile(sigma, cuts[r.symbol])

    return cell


def serial(records: Sequence[Scored], baseline: Sequence[Scored]) -> dict[str, Any]:
    """How ``records``' lift over ``baseline`` by trading day moves if the days depend on each other:
    the days' gains' lag-1 autocorrelation, and the interval at ``LEVEL`` as if they were windows of
    ``SERIAL`` sessions (Newey–West, ``stats.overlap_t_test``)."""
    by_day = gains_by_day(records, baseline)
    days = sorted(d for d, g in by_day.items() if g)
    means = [sum(by_day[d]) / len(by_day[d]) for d in days]
    places = session_index(days)
    positions = [places[d] for d in days]
    out: dict[str, Any] = {"lag1": _autocorrelation(means)}
    for sessions in SERIAL:
        found = overlap_t_test(means, positions, sessions, LEVEL)
        out[f"sessions_{sessions}"] = None if found is None else _lift(found)
    return out


def _autocorrelation(values: Sequence[float]) -> float | None:
    """The lag-1 autocorrelation of ``values`` about their mean; None from fewer than three or
    without spread."""
    if len(values) < 3:
        return None
    x = np.asarray(values, dtype=float) - float(np.mean(values))
    total = float(x @ x)
    return None if total <= 0 else float(x[:-1] @ x[1:]) / total


def described(collected: Collected) -> dict[str, Any]:
    """What the evaluation describes and never judges (module doc)."""
    records = collected.records
    stats = {arm: arm_stats(records[arm]).as_dict() | {"log_loss": log_loss(records[arm])} for arm in ARMS}
    swaps = [(a, b) for a, b in zip(records[A], records[B], strict=True) if a.predicted is not b.predicted]
    slots = sorted({slot(r.bar_close) for r in records[A]})
    sigma = _sigma_cell(collected.sigmas)
    return {
        "arms": stats,
        "over_climate": {arm: Verdict.of(records[arm], records[CLIMATE_ARM]).as_dict() for arm in ARMS},
        "climate_hit": _share(sum(r.correct for r in records[CLIMATE_ARM]), len(records[CLIMATE_ARM])),
        "swapped": {
            "n": len(swaps),
            "a_hit": _share(sum(a.correct for a, _ in swaps), len(swaps)),
            "b_hit": _share(sum(b.correct for _, b in swaps), len(swaps)),
        },
        "d_over_c": Verdict.of(records[D], records[C]).as_dict(),
        "by_slot": _cells(records, lambda r: slot(r.bar_close), slots),
        "by_volatility": {
            "with_sigma": sum(1 for r in records[A] if sigma(r) != NO_SIGMA),
            "cells": _cells(records, sigma, VOLATILITY),
        },
        "serial": {arm: serial(records[arm], records[A]) for arm in VARIANTS},
    }


def _share(part: float, whole: float) -> float | None:
    return part / whole if whole else None


# (2)'s diagnosis


@dataclass
class _Range:
    """A second-model input, out of sample (the fold models' calls) against live (the final first
    model's calls) over the window's days: the values known and unknown, the live ones beyond the
    out-of-sample percentiles, and the days' KS distances."""

    oof: int = 0
    oof_unknown: int = 0
    live: int = 0
    live_unknown: int = 0
    below: int = 0
    above: int = 0
    ks: list[float] = field(default_factory=list)

    def add(self, oof: np.ndarray, live: np.ndarray) -> None:
        known_oof, known_live = oof[np.isfinite(oof)], live[np.isfinite(live)]
        self.oof += len(oof)
        self.oof_unknown += len(oof) - len(known_oof)
        self.live += len(live)
        self.live_unknown += len(live) - len(known_live)
        if len(known_oof) and len(known_live):
            low, high = np.percentile(known_oof, PERCENTILES)
            self.below += int(np.sum(known_live < low))
            self.above += int(np.sum(known_live > high))
            self.ks.append(_ks(known_oof, known_live))

    def as_dict(self) -> dict[str, Any]:
        known = self.live - self.live_unknown
        return {
            "out_of_sample": self.oof,
            "live": self.live,
            "unknown_out_of_sample": _share(self.oof_unknown, self.oof),
            "unknown_live": _share(self.live_unknown, self.live),
            "live_below_p01": _share(self.below, known),
            "live_above_p99": _share(self.above, known),
            "mean_ks": statistics.fmean(self.ks) if self.ks else None,
            "ks_days": len(self.ks),
        }


def _ks(a: np.ndarray, b: np.ndarray) -> float:
    """The two-sample Kolmogorov–Smirnov distance of ``a`` and ``b`` (both non-empty)."""
    a, b = np.sort(a), np.sort(b)
    grid = np.concatenate([a, b])
    cdf_a = np.searchsorted(a, grid, side="right") / len(a)
    cdf_b = np.searchsorted(b, grid, side="right") / len(b)
    return float(np.max(np.abs(cdf_a - cdf_b)))


@dataclass
class _Residuals:
    calls: int = 0
    chance: float = 0.0  # the sum of m
    right: int = 0

    def add(self, m: float, right: bool) -> None:
        self.calls += 1
        self.chance += m
        self.right += right

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "mean_m": _share(self.chance, self.calls),
            "right": _share(self.right, self.calls),
            "residual": _share(self.right - self.chance, self.calls),
        }


class _Diagnosis:
    """(2)'s diagnosis over the window's days (module doc)."""

    def __init__(self) -> None:
        self.final: list[Scored] = []  # A's final first model alone
        self.folds: list[Scored] = []  # the mean of its fold models
        self.gaps = np.zeros(4)  # the sums of |final − folds|: down, flat, up, up − down
        self.residuals: dict[str, dict[str, _Residuals]] = {"day": {}, "symbol": {}, "window": {}}
        self.inputs = {name: _Range() for name in D_INPUTS}

    def day(self, day: date, fit: Fit, samples: Sequence[Sample], x: np.ndarray, first: np.ndarray) -> None:
        folds = fit.first.fold_mean(x)
        if folds is None:
            return  # one training day: no fold models, no out-of-sample calls
        final = first / first.sum(axis=1, keepdims=True)
        mean = folds / folds.sum(axis=1, keepdims=True)
        for s, f, m in zip(samples, final, mean, strict=True):
            assert s.outcome is not None
            self.final.append(Scored("final", "", s.symbol, s.bar_close, f[2], f[1], f[0], s.outcome))
            self.folds.append(Scored("folds", "", s.symbol, s.bar_close, m[2], m[1], m[0], s.outcome))
        gaps = np.abs(final - mean)
        self.gaps[:3] += gaps.sum(axis=0)
        self.gaps[3] += float(np.abs((final[:, 2] - final[:, 0]) - (mean[:, 2] - mean[:, 0])).sum())
        params = fit.params[A]
        live = []
        for s, p in zip(samples, final, strict=True):
            p_down, p_flat, p_up = (float(v) for v in p)
            call = most_likely(p_up, p_flat, p_down)
            if call is FLAT:
                continue
            values = d_inputs(p_up, p_flat, p_down, call, s.features)
            live.append(values)
            if params["meta.calls"] > 0:
                m = _chance(params, values[: len(INPUTS)])
                for group, key in (
                    ("day", day.isoformat()),
                    ("symbol", s.symbol),
                    ("window", fit.composition),
                ):
                    self.residuals[group].setdefault(key, _Residuals()).add(m, s.outcome is call)
        oof = fit.out_of_sample
        if oof is None:
            return
        live_rows = np.array(live, dtype=float).reshape(len(live), len(D_INPUTS))
        for i, name in enumerate(D_INPUTS):
            self.inputs[name].add(oof[:, i], live_rows[:, i])

    def as_dict(self) -> dict[str, Any]:
        n = len(self.final)
        return {
            "rows": n,
            "mean_abs_difference": {
                name: _share(float(v), n)
                for name, v in zip(("down", "flat", "up", "up_minus_down"), self.gaps, strict=True)
            },
            "final": _first_alone(self.final),
            "folds_mean": _first_alone(self.folds),
            "happened": {d.value: sum(r.outcome is d for r in self.final) for d in CALL_CLASSES},
            "residuals": {
                group: {key: r.as_dict() for key, r in sorted(by.items())}
                for group, by in self.residuals.items()
            },
            "inputs": {name: r.as_dict() for name, r in self.inputs.items()},
        }


def _first_alone(records: Sequence[Scored]) -> dict[str, Any]:
    """A first model's probabilities taken alone: hit rate, up and down call share and precision,
    the classes called, and its calibration (``stats.summarize``'s bins, the three classes pooled)
    with its mean gap from the diagonal."""
    if not records:
        return {"n": 0}
    summary = summarize(records, 0.0)
    bins = summary.calibration
    counted = sum(b.count for b in bins)
    error = sum(
        b.count * abs(b.mean_p - b.observed.value) for b in bins if b.mean_p is not None and b.observed
    )
    return {
        "n": summary.n,
        "hit": _value(summary.accuracy),
        "coverage": _value(summary.coverage),
        "directional_precision": _value(summary.precision),
        "called": {d.value: sum(r.predicted is d for r in records) for d in CALL_CLASSES},
        "calibration_error": _share(error, counted),
        "calibration": [
            {
                "low": round(b.low, 1),
                "high": round(b.high, 1),
                "count": b.count,
                "mean_p": b.mean_p,
                "observed": None if b.observed is None else b.observed.value,
            }
            for b in bins
        ],
    }


def _value(i: Any) -> float | None:
    return None if i is None else float(i.value)


# The evaluation


@dataclass(frozen=True)
class Evaluation:
    """The window as found (``window``: ``n`` days once full) and, once full, the arms on it: the
    verdict, what is described and (2)'s diagnosis. Its recorded days after ``after`` with fewer
    than ``min_scored`` scored bar closes before its last day are ``short``."""

    made_at: datetime
    n: int
    after: date
    min_scored: int
    window: list[date]
    short: list[date]
    # Once the window is full (nothing is fitted before): its days, the verdict, each arm's
    # statistics, what is described and (2)'s diagnosis.
    folds: list[Fold] = field(default_factory=list)
    verdicts: dict[str, Verdict] = field(default_factory=dict)
    arms: dict[str, ArmStats] = field(default_factory=dict)
    found: dict[str, Any] | None = None
    diagnosis: dict[str, Any] | None = None
    records: dict[str, list[Scored]] = field(default_factory=dict, repr=False)  # not in the report

    @classmethod
    def judged(
        cls, made_at: datetime, n: int, after: date, min_scored: int, short: list[date], collected: Collected
    ) -> Evaluation:
        records = collected.records
        return cls(
            made_at,
            n,
            after,
            min_scored,
            collected.days,
            short,
            collected.folds,
            {arm: Verdict.of(records[arm], records[A]) for arm in VARIANTS},
            {arm: arm_stats(records[arm]) for arm in ARMS},
            described(collected),
            None if collected.diagnosis is None else collected.diagnosis.as_dict(),
            records,
        )

    @property
    def full(self) -> bool:
        return self.found is not None

    @property
    def candidates(self) -> list[str]:
        return [arm for arm, v in self.verdicts.items() if v.candidate]

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "made_at": self.made_at.isoformat(),
            "n": self.n,
            "after": self.after.isoformat(),
            "min_scored": self.min_scored,
            "judged": self.full,
            "window": [d.isoformat() for d in self.window],
            "short": [d.isoformat() for d in self.short],
        }
        if not self.full:
            return out
        return out | {
            "folds": [f.as_dict() for f in self.folds],
            "verdict": {arm: v.as_dict() | {"candidate": v.candidate} for arm, v in self.verdicts.items()},
            "candidates": self.candidates,
            "described": self.found,
            "diagnosis": self.diagnosis,
        }


def evaluate(
    make: Callable[[], Days],
    candidates: Sequence[date],
    now: datetime,
    *,
    n: int,
    after: date,
    min_scored: int,
    known: Mapping[date, bool] | None = None,
) -> Evaluation:
    """The window among ``candidates`` (the recorded days after ``after``, oldest first): its days
    counted one at a time (``known``: a day an earlier run counted, in the window or not), and once
    ``n`` are in, the arms on them. Before, nothing is fitted."""
    window: list[date] = []
    short: list[date] = []
    for day in candidates:
        if len(window) >= n:
            break
        found = (known or {}).get(day)
        if found is None:
            found = _count(make, day) >= min_scored
        (window if found else short).append(day)
    if len(window) < n:
        return Evaluation(now, n, after, min_scored, window, short)
    collected, left = walk(make(), window, min_scored, diagnose=True)
    if left:
        raise ValueError(f"neyman variants: {', '.join(map(str, left))} counted again with fewer bar closes")
    return Evaluation.judged(now, n, after, min_scored, short, collected)


# The report


def read_report(path: Path) -> dict[str, Any] | None:
    """The report as saved; None without one. Raises ValueError for one that cannot be read: it holds
    the discovery's N, which is never computed again."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise ValueError(f"cannot read {path} ({e}): it holds the window's N, never computed again") from e
    if not isinstance(data, dict):
        raise ValueError(f"cannot read {path}: not a report")
    return data


def _write(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    tmp.replace(path)


@dataclass(frozen=True)
class Progress:
    """Where the experiment stands, as the scheduled job's condition reads it (``progress``)."""

    read: bool = False  # read tonight: without (the night's facts not read), the job waits
    n: int | None = None  # the window's days, from the discovery; None before it (or no report)
    judged: bool = False  # the window judged
    candidates: int = 0  # recorded days after the window's start not counted short already


def progress(settings: Settings, now: datetime) -> Progress:
    """The experiment's ``Progress`` from its report and the recorded days (``Facts.neyman_variants``):
    a small file and the day folders. One that cannot be read says no discovery (logged): the job
    then runs, and its command says why it cannot."""
    try:
        saved = read_report(settings.state_dir / REPORT)
        if not saved or not saved.get("discovery"):
            return Progress(read=True)
        discovery, evaluation = saved["discovery"], saved.get("evaluation") or {}
        after = date.fromisoformat(discovery["after"])
        short = set(evaluation.get("short", []))
        days = [
            d
            for d in finished_days(settings.data_dir, settings.symbols, now)
            if d > after
            and d.isoformat() not in short
            and is_recorded(settings.data_dir, d, settings.symbols)
        ]
        return Progress(True, int(discovery["n"]), bool(evaluation.get("judged")), len(days))
    except (OSError, ValueError, KeyError, TypeError):
        log.exception("could not read where neyman's variants experiment (#455) stands")
        return Progress(read=True)


# The run

DISCOVERED, WAITING, NOT_FULL, JUDGED, JUDGED_BEFORE = (
    "discovered",
    "waiting",
    "not full",
    "judged",
    "judged before",
)


@dataclass(frozen=True)
class Run:
    stage: str  # DISCOVERED, WAITING, NOT_FULL, JUDGED or JUDGED_BEFORE
    lines: list[str]
    discovery: Discovery | None = None  # this run's
    evaluation: Evaluation | None = None


def neyman_variants(
    settings: Settings,
    *,
    algorithms: Sequence[Algorithm] = ALGORITHMS,
    kind_algorithms: Sequence[KindAlgorithm] = KIND_ALGORITHMS,
    kinds: Sequence[Kind] = KINDS,
    now: datetime | None = None,
    period: tuple[date, date] = DISCOVERY,
    after: date = AFTER,
    min_scored: int = MIN_SCORED,
    least: int = LEAST,
    delta: float = DELTA,
) -> Run:
    """The experiment's next step (module doc), its report saved: the discovery when the report has
    none (``period``, ``after``, ``min_scored``, ``least`` and ``delta`` are its, and the report keeps
    them: later runs read N, the window's start and its least bar closes from it); then the
    evaluation once the window can be full, judged once. The registries name the days the nights
    stored (#370, ``night_reads``). Raises FileNotFoundError without a recorded day in the discovery
    period, ValueError for a report that cannot be read or a discovery that cannot set N."""
    now = now or datetime.now(KST)
    path = settings.state_dir / REPORT
    saved = read_report(path)
    available = finished_days(settings.data_dir, settings.symbols, now)
    recorded = [d for d in available if is_recorded(settings.data_dir, d, settings.symbols)]
    calendar = calendar_store(settings).calendar
    night = night_reads(algorithms, kind_algorithms, kinds)
    leaders = Leaders.of(settings)

    def make() -> Days:
        return _WalkForward(settings, Neyman(), available, calendar, leaders, night=night)

    if saved is None or not saved.get("discovery"):
        days = [d for d in recorded if period[0] <= d <= period[1]]
        if not days:
            symbols = ", ".join(settings.symbols)
            raise FileNotFoundError(f"no recorded day of {symbols} from {period[0]} to {period[1]}")
        found = discover(
            make(), days, now, period=period, after=after, min_scored=min_scored, least=least, delta=delta
        )
        since = sum(1 for d in recorded if d > found.after)
        lines = [*discovery_lines(found), *_waiting(found.n, found.after, found.min_scored, since)]
        _write(
            path, {"experiment": EXPERIMENT, "issue": ISSUE, "discovery": found.as_dict() | {"lines": lines}}
        )
        return Run(DISCOVERED, [*lines, f"report: {path}"], discovery=found)
    discovery, evaluation = saved["discovery"], saved.get("evaluation") or {}
    if evaluation.get("judged"):
        made = str(evaluation.get("made_at", "?"))[:10]
        head = f"neyman variants (#{ISSUE}): judged on the night of {made}; it stands, not computed again"
        return Run(JUDGED_BEFORE, [head, "", *evaluation.get("lines", []), f"report: {path}"])
    n, start, least_scored = (
        int(discovery["n"]),
        date.fromisoformat(discovery["after"]),
        int(discovery["min_scored"]),
    )
    known = {date.fromisoformat(d): True for d in evaluation.get("window", [])}
    known |= {date.fromisoformat(d): False for d in evaluation.get("short", [])}
    candidates = [d for d in recorded if d > start]
    open_days = [d for d in candidates if known.get(d) is not False]
    if len(open_days) < n:
        lines = _waiting(n, start, least_scored, len(open_days), str(discovery.get("made_at", "?"))[:10])
        return Run(WAITING, [*lines, f"report: {path}"])
    result = evaluate(make, candidates, now, n=n, after=start, min_scored=least_scored, known=known)
    lines = evaluation_lines(result, str(discovery.get("made_at", "?"))[:10])
    _write(path, saved | {"evaluation": result.as_dict() | {"lines": lines}})
    return Run(JUDGED if result.full else NOT_FULL, [*lines, f"report: {path}"], evaluation=result)


# The command's lines: counts, rates, lifts, scores and dates; no price, σ̂₄ or cut.


def _window_text(n: int, after: date, min_scored: int) -> str:
    return f"the first {n} recorded days after {after} with at least {min_scored} bar closes every arm scored"


def _waiting(n: int, after: date, min_scored: int, days: int, made: str | None = None) -> list[str]:
    since = f" from the discovery of {made}" if made else ""
    return [
        f"neyman variants (#{ISSUE}, job {EXPERIMENT}): evaluation window{since}:"
        f" {_window_text(n, after, min_scored)};"
        f" {days} recorded days so far that can be in it, of {n}. Not judged: judged once, when it is full"
    ]


def _arm_table(arms: Mapping[str, ArmStats], losses: Mapping[str, float | None] | None = None) -> list[str]:
    head = f"{'arm':<44} {'n':<8} {'hit rate (95%)':<26} {'coverage':<9} {'dir. prec.':<11} brier"
    out = [head + ("   log loss" if losses is not None else "")]
    for arm in ARMS:
        a = arms[arm]
        row = (
            f"{LABELS[arm]:<44} {a.n:<8} {_ratio(a.accuracy):<26} {_number(_value(a.coverage)):<9} "
            f"{_number(_value(a.directional_precision)):<11} {_number(_value(a.brier))}"
        )
        out.append(row + (f"   {_number(losses[arm])}" if losses is not None else ""))
    return out


def discovery_lines(d: Discovery) -> list[str]:
    first, last = d.period
    left = ", ".join(map(str, d.short)) or "none"
    out = [
        f"neyman variants (#{ISSUE}, job {EXPERIMENT}): discovery on neyman's pre-validation days"
        f" {first}..{last}:"
        f" {len(d.days)} recorded days, {len(d.folds)} with at least {d.min_scored} scored bar closes"
        f" (left out: {left})",
        f"  each day predicted by the four arms fitted on the {TRAINING_DAYS} trading days before it"
        " (neyman's"
        f" pre-validation's samples, settings and seeds); {sum(f.scored for f in d.folds)} bar closes, every"
        " arm"
        " on each. Not judged: the three changes were chosen on these days",
        "  B swaps back the final p_up and p_down where the second opinion turned the first model's call to"
        " the"
        " opposite side; C drops slot_return_5d and slot_return_20d from the first and fold models; D is C"
        " with"
        " r = (k + h) / σ̂₄ a second-model input more (k the flat band, h half the spread, 0.5 without a"
        " book)",
        "",
        *_arm_table(d.arms),
        "",
        f"the days' mean paired gain in hit rate over A, its standard deviation s_d, and N = max({d.least},"
        f" ceil((({Z_LEVEL} + {Z_POWER}) x s_d / {d.delta:g})^2)): {d.delta * 100:g} points found with 80%"
        " power,"
        f" two-sided at {LEVEL:.3%} (Bonferroni over the three)",
    ]
    for arm in VARIANTS:
        s = d.spreads[arm]
        out.append(f"  {arm} - A: {s.days} days, mean {_signed(s.mean)}, s_d {_number(s.sd)}, N {s.n}")
    out.append(
        f"N = {d.n}, the largest: the window is {_window_text(d.n, d.after, d.min_scored)}. Kept in the"
        " report:"
        " never computed again"
    )
    if d.n > DECIDE_ABOVE:
        out.append(
            f"N is above {DECIDE_ABOVE}: before the evaluation the user decides whether to drop comparisons"
            " or to"
            " wait as it is, from these days alone (the verdict stays clean)"
        )
    if d.n > 40:
        out.append(
            "cost: the evaluation reads each window day's 20 days before it from the sample store, which"
            " keeps"
            " the nights' training days (about 61 trading days): with this N the window's first days will"
            " have"
            " left it, and are made again (minutes a recorded day)"
        )
    out.append("")
    return out


def evaluation_lines(e: Evaluation, made: str) -> list[str]:
    left = ", ".join(map(str, e.short)) or "none"
    window = f"{e.window[0]}..{e.window[-1]}" if e.window else "-"
    if e.found is None:
        return [
            f"neyman variants (#{ISSUE}, job {EXPERIMENT}): the window"
            f" ({_window_text(e.n, e.after, e.min_scored)},"
            f" from the discovery of {made}) has {len(e.window)} of its {e.n} days: {window}. Not judged:"
            " judged"
            " once, when it is full",
            f"  short of {e.min_scored} scored bar closes, never in it: {left}",
        ]
    total = sum(f.scored for f in e.folds)
    out = [
        f"neyman variants (#{ISSUE}, job {EXPERIMENT}): verdict on the window {window}, its {e.n} days"
        f" ({_window_text(e.n, e.after, e.min_scored)}; N from the discovery of {made})",
        f"  short of {e.min_scored} scored bar closes, never in it: {left}",
        f"  each day predicted by the four arms fitted on the {TRAINING_DAYS} trading days before it;"
        f" {total} bar"
        " closes, every arm on each",
        "",
        "verdict (fixed before the run): B, C and D each against A; a day's mean paired gain in hit rate is"
        " one"
        f" value, every day the same weight; a two-sided {LEVEL:.3%} Student t interval (Bonferroni over the"
        " three); a candidate when the paired lift is above 0 and the lower bound by trading day too",
    ]
    for arm, v in e.verdicts.items():
        out.append(
            f"  {LABELS[arm]} - A: {_verdict(v)} -> {'a candidate' if v.candidate else 'not a candidate'}"
        )
    chosen = ", ".join(LABELS[arm] for arm in e.candidates)
    out.append(
        f"candidates: {chosen}; none goes into neyman from this (#454, then the user decides)"
        if chosen
        else "candidates: none; neyman stays as it is"
    )
    out += ["", *_described_lines(e.found, e.arms, total)]
    if e.diagnosis is not None:
        out += ["", *_diagnosis_lines(e.diagnosis)]
    return out


def _described_lines(found: Mapping[str, Any], arms: Mapping[str, ArmStats], total: int) -> list[str]:
    losses = {arm: found["arms"][arm]["log_loss"] for arm in ARMS}
    out = [f"described, not judged (every interval at {LEVEL:.3%}):", *_arm_table(arms, losses)]
    out.append(f"over the climate baseline (its hit rate {_number(found['climate_hit'])}):")
    out += [f"  {LABELS[arm]}: {_verdict_dict(found['over_climate'][arm])}" for arm in ARMS]
    swaps = found["swapped"]
    out.append(
        f"B's swaps: {swaps['n']} bar closes; A (the side it turned to) right on {_number(swaps['a_hit'])}"
        " of them,"
        f" B (the first model's side) on {_number(swaps['b_hit'])}"
    )
    out.append(f"D - C (what r adds): {_verdict_dict(found['d_over_c'])}")
    out.append(
        "serial dependence: the days' gains' lag-1 autocorrelation; the lower bound by trading day as if the"
        " days"
        " were windows of 2 and 3 sessions (Newey-West)"
    )
    for arm in VARIANTS:
        s = found["serial"][arm]
        lows = ", ".join(
            f"{n} sessions {_signed(None if s[f'sessions_{n}'] is None else s[f'sessions_{n}']['low'])}"
            for n in SERIAL
        )
        out.append(f"  {arm} - A: lag-1 {_signed(s['lag1'])}; {lows}")
    out.append(
        "by 30-minute slot (09:00 holds the bar closes 09:01-09:30): hit rates; each variant - A, day low"
    )
    out += _cell_lines(found["by_slot"])
    by_sigma = found["by_volatility"]
    out.append(
        "by σ̂₄'s tercile at the bar close (σ̂₄ = w / barrier_sigma, each symbol's cuts from the window's bar"
        f" closes; {by_sigma['with_sigma']} of {total} with σ̂₄, the rest in none)"
    )
    out += _cell_lines(by_sigma["cells"])
    return out


def _cell_lines(cells: Mapping[str, Mapping[str, Any]]) -> list[str]:
    out = []
    for name, cell in cells.items():
        hits = " ".join(f"{arm} {_number(cell['hit'][arm])}" for arm in ARMS)
        lifts = ", ".join(f"{arm} - A {_short(cell['over_a'][arm])}" for arm in VARIANTS)
        out.append(f"  {name}: {cell['n']} bar closes; hit {hits}; {lifts}")
    return out


def _diagnosis_lines(d: Mapping[str, Any]) -> list[str]:
    gaps = d["mean_abs_difference"]
    out = [
        "(2) diagnosis (reported, not judged): A's final first model and the mean of its fold models on the"
        " same"
        f" {d['rows']} rows of the window's days. The folds' mean mixes a smaller training set (60-75% of"
        " the days)"
        " with an ensemble: the two effects are not told apart",
        f"  mean |final - folds' mean|: down {_number(gaps['down'])}, flat {_number(gaps['flat'])}, up"
        f" {_number(gaps['up'])}, p_up - p_down {_number(gaps['up_minus_down'])}",
    ]
    for key, title in (("final", "final"), ("folds_mean", "folds' mean")):
        f = d[key]
        if not f.get("n"):
            continue
        called = "/".join(str(f["called"][c.value]) for c in CALL_CLASSES)
        out.append(
            f"  {title}: hit {_number(f['hit'])}, up/down call share {_number(f['coverage'])}, their"
            " precision"
            f" {_number(f['directional_precision'])}, calibration error {_number(f['calibration_error'])};"
            f" called up/flat/down {called}"
        )
    out.append("  happened up/flat/down " + "/".join(str(d["happened"][c.value]) for c in CALL_CLASSES))
    out.append("  A's second model's residuals (right - m) on the final first model's up and down calls:")
    titles = {
        "symbol": "by symbol",
        "window": "by the training window's recorded+backfilled days",
        "day": "by day",
    }
    for group in ("symbol", "window", "day"):
        parts = [
            f"{key} {r['calls']} calls, m {_number(r['mean_m'])}, right {_number(r['right'])},"
            f" residual {_signed(r['residual'])}"
            for key, r in d["residuals"][group].items()
        ]
        out.append(f"    {titles[group]}: " + ("; ".join(parts) or "none"))
    out.append(
        "  second-model inputs live against out of sample (A's fold models' up and down calls): unknown out"
        " of"
        " sample/live; live below the out-of-sample 1st percentile / above its 99th; the days' mean KS"
        " distance"
    )
    for name, r in d["inputs"].items():
        out.append(
            f"    {name}: unknown {_number(r['unknown_out_of_sample'])}/{_number(r['unknown_live'])}; below"
            f" {_number(r['live_below_p01'])}, above {_number(r['live_above_p99'])}; KS"
            f" {_number(r['mean_ks'])}"
            f" ({r['ks_days']} days)"
        )
    return out


def _verdict(v: Verdict) -> str:
    return _verdict_dict(v.as_dict())


def _verdict_dict(v: Mapping[str, Any]) -> str:
    pairs, day = v["pairs"], v["by_day"]
    if pairs["value"] is None:
        return "no pair"
    text = f"paired {_interval(pairs)}, {pairs['n']} pairs"
    if day is None or day["low"] is None:
        return text + "; by trading day: fewer than two days"
    return text + f"; by trading day {_interval(day)}, {day['n']} days"


def _interval(lifted: Mapping[str, Any]) -> str:
    return f"{_signed(lifted['value'])} ({_signed(lifted['low'])}..{_signed(lifted['high'])})"


def _short(v: Mapping[str, Any]) -> str:
    pairs, day = v["pairs"], v["by_day"]
    if pairs["value"] is None:
        return "-"
    low = "-" if day is None else _signed(day["low"])
    return f"{_signed(pairs['value'])} (day low {low})"


def _ratio(i: Any) -> str:
    if i is None:
        return "-"
    if i.low is None or i.high is None:
        return f"{i.value:.4f}"
    return f"{i.value:.4f} ({i.low:.4f}..{i.high:.4f})"


def _number(value: float | None) -> str:
    return "-" if value is None else f"{value:.4f}"


def _signed(value: float | None) -> str:
    return "-" if value is None else f"{value:+.4f}"
