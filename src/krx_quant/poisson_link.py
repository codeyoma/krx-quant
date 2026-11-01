"""The asymmetric link experiment (#439, ``krx-quant poisson-link``): does poisson always say flat
because of its link, or because its lean knows too little?

Poisson's pre-validation of 2026-10-31 (#204) was refused: it hit 43.5%, called a direction at none
of its bar closes (always flat) and lost 5.2 points to the climate baseline. Its link, the ordered
logistic of ``poisson._probabilities``, gives up and down the same chance at a lean of 0 and mirrors
them around it: P(up | lean) = P(down | −lean). If up and down come at different rates, or answer
the lean differently, that link can hold the calls back; or the lean has too little in it. This
tells the two apart before anything is changed: poisson runs the same code.

Three arms, on the same folds and the same bar closes:

- (a) **climate**: the climate baseline's table refitted before each day on the ``CLIMATE_DAYS``
  days before it, as the pre-validation's replay refits it (``_WalkForward.climate_before``);
- (b) **poisson**: poisson as it is, trained before each day as its pre-validation trained it
  (``_WalkForward.before``), its lean through the ordered logistic;
- (c) **asymmetric link** (``AsymmetricLink``): (b)'s intensities, so the same lean at every bar
  close (the same Hawkes features, the same estimate), through a multinomial logistic of its own:
  each class its bias and its weight on the standardized lean (``link.*``), so that up and down
  need not be as likely at a lean of 0, nor answer it alike. L1-penalized as shannon's regression
  (``shannon._fista``, its ``PENALTIES``); the penalty is chosen on whole trading days held out.

**Folds.** The pre-validation's: the latest ``PREVALIDATION_DAYS`` recorded days, each predicted
by models fitted on poisson's window of the 20 trading days before it, never on the day itself.
Nothing is replayed: each day's samples are predicted by each arm. They are the training data (the
live feature code on the day's events, as the nightly training makes them, read from the sample
store when a night stored them, #370); a recorded day's leave out the bar closes the statistics
leave out and carry the engine's touch labels, so they are the bar closes the pre-validation
scored. A bar close before the day's first trade with a side has no lean: (b) and (c) say a third
each there, as poisson does, and it counts as theirs (``unknown``).

**Tuning never sees what it validates on** (``tuned``). The intensities learn from each sample's
next four minutes of arrivals (``poisson._pairs``), so a model's lean on a day it trained on knows
that day's arrivals. The penalty is validated on the window's latest whole days, as many as bring
them nearest ``VALIDATION_SHARE`` of its samples (shannon's rule): the leans there come from
intensities fitted on the days before them only, as the day predicted gets them. A four-minute
window ends the day it starts, so no arrival crosses from one day into the next. The link is then
fitted with that penalty on the whole window, with (b)'s intensities.

**The comparison, set before it ran** (#439, ``Experiment.rule``): (c) better than (b), its hit
rate lift over (b) paired by bar close above 0, and its lower bound by trading day over the climate
above (b)'s. Met: the ordered link held poisson back. Not met: not the link alone; the report then
tells whether the lean has anything in it: (c)'s weights on it (L1 zeroes a weight the held-out
days do not bear out) and the classes that came in each fifth of the leans (``Leans``).

**The report** (``state/experiments/poisson-link-439.json``, and the command's lines): each arm's
bar closes scored, hit rate, recall and precision of each class, its confusion matrix, directional
coverage and precision, and Brier score; the lifts over the climate and of (c) over (b), paired by
bar close and by trading day; the leans' spread and quintiles; each fold's coefficients ((b)'s
slope and cut, (c)'s penalty, lean standardization, weights and biases). Counts, rates, lifts,
leans, coefficients and dates only, never a price. Whether poisson takes the link is decided from
the posted numbers, in another change: its code digest would change and its pre-validation run
again.

**Memory.** A pre-validation's training phase's, without its replay (#383): the window's days are
kept and the older let go as the folds move on; each day logs its memory, then hands back what it
freed (``footprint.give_back``).
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np

from krx_quant.algorithms import ALGORITHMS, KIND_ALGORITHMS, Algorithm, KindAlgorithm
from krx_quant.algorithms.contract import Model, NothingToLearn, Sample
from krx_quant.algorithms.forecasts import CLASSES, UNKNOWN, class_moves, param
from krx_quant.algorithms.poisson import EXCITATIONS, Poisson, _known, _lean
from krx_quant.algorithms.poisson import _probabilities as _ordered_probabilities
from krx_quant.algorithms.poisson import default_params as poisson_defaults
from krx_quant.algorithms.shannon import (
    _MIN_SD,
    MIN_SPLIT,
    PENALTIES,
    VALIDATION_SHARE,
    _cross_entropy,
    _fista,
    _validated_on,
)
from krx_quant.algorithms.shannon import _probabilities as _softmax
from krx_quant.baselines import Forecast
from krx_quant.climate import direction
from krx_quant.climate_prior import ArmStats, PairedLift, _lifted, _ratio, _value, _yes, arm_stats
from krx_quant.config import Settings
from krx_quant.features import FeatureVector, Leaders
from krx_quant.feeds.base import KST, kst_day
from krx_quant.footprint import DayUsage, Meter, give_back
from krx_quant.kind_registry import KINDS
from krx_quant.kinds import Kind
from krx_quant.market_calendar import calendar_store
from krx_quant.prevalidation import PREVALIDATION_DAYS, _WalkForward
from krx_quant.pricing import Direction
from krx_quant.sample_store import night_reads
from krx_quant.stats import Scored
from krx_quant.training import finished_days, is_recorded

log = logging.getLogger(__name__)

EXPERIMENT = "poisson-link-439"
REPORT = f"experiments/{EXPERIMENT}.json"  # under the state directory
LINK = "link"  # (c)'s own parameters: link.penalty, link.mean, link.sd, link.bias.<class>, link.w.<class>
CLIMATE_ARM, POISSON_ARM, ASYMMETRIC_ARM = "climate", "poisson", "asymmetric"
ARMS = (CLIMATE_ARM, POISSON_ARM, ASYMMETRIC_ARM)
LINKED = (POISSON_ARM, ASYMMETRIC_ARM)  # the arms with a lean
LABELS = {CLIMATE_ARM: "(a) climate", POISSON_ARM: "(b) poisson", ASYMMETRIC_ARM: "(c) asymmetric link"}
QUANTILES = (0.05, 0.25, 0.5, 0.75, 0.95)  # the leans' spread, as the report gives it
BINS = 5  # the leans' quintiles, each with the classes that came in it


def lean_of(params: Mapping[str, float], features: FeatureVector) -> float | None:
    """Poisson's lean at a bar close with ``params``' intensities; None before the day's first
    trade with a side (an excitation unknown)."""
    x = [features[f] for f in EXCITATIONS]
    if any(v is None for v in x):
        return None
    return _lean(params, [float(v) for v in x if v is not None])


def _scored(samples: Sequence[Sample]) -> list[Sample]:
    """The samples a link learns from, in time order: with a label and a lean (``poisson._known``)."""
    return sorted(
        (s for s in samples if s.outcome is not None and _known(s.features)),
        key=lambda s: (s.bar_close, s.symbol),
    )


def _leans(params: Mapping[str, float], scored: Sequence[Sample]) -> np.ndarray:
    return np.array([lean_of(params, s.features) for s in scored], dtype=float)


def _one_hot(scored: Sequence[Sample]) -> np.ndarray:
    return np.array([[float(s.outcome is c) for c in CLASSES] for s in scored], dtype=float).reshape(
        len(scored), len(CLASSES)
    )


def _link(leans: np.ndarray, y: np.ndarray, penalty: float) -> dict[str, float]:
    """The multinomial logistic on the standardized lean, L1-penalized (the biases are not), as
    (c)'s parameters."""
    mean, sd = float(leans.mean()), float(leans.std())
    sd = sd if sd > _MIN_SD else 1.0
    w, b = _fista(((leans - mean) / sd).reshape(-1, 1), y, penalty)
    params = {param(LINK, "penalty"): penalty, param(LINK, "mean"): mean, param(LINK, "sd"): sd}
    for i, c in enumerate(CLASSES):
        params[param(LINK, "bias", c.value)] = float(b[i])
        params[param(LINK, "w", c.value)] = float(w[i, 0]) + 0.0  # a zeroed weight is 0, not −0
    return params


def _weights(params: Mapping[str, float]) -> tuple[np.ndarray, np.ndarray]:
    w = np.array([[params[param(LINK, "w", c.value)]] for c in CLASSES], dtype=float)
    b = np.array([params[param(LINK, "bias", c.value)] for c in CLASSES], dtype=float)
    return w, b


def _standardized(params: Mapping[str, float], leans: np.ndarray) -> np.ndarray:
    z: np.ndarray = ((leans - params[param(LINK, "mean")]) / params[param(LINK, "sd")]).reshape(-1, 1)
    return z


def link_probabilities(params: Mapping[str, float], leans: np.ndarray) -> np.ndarray:
    """(c)'s up, flat and down for each lean: the softmax of each class's bias plus its weight
    times the standardized lean."""
    return _softmax(*_weights(params), _standardized(params, leans))


def _link_loss(params: Mapping[str, float], leans: np.ndarray, y: np.ndarray) -> float:
    return _cross_entropy(*_weights(params), _standardized(params, leans), y)


@dataclass(frozen=True)
class Tuning:
    """The penalty (c) is fitted with, the window's days it was validated on, each penalty's log
    loss there, and the intensities those days' leans came from (fitted on the days before them)."""

    penalty: float
    held: list[date]
    losses: dict[float, float]
    intensities: Model | None


def tuned(window: Sequence[Sample]) -> Tuning:
    """The penalty whose link, fitted on the window's earlier days, has the lowest log loss on its
    latest ones; the leans of both from intensities fitted on the earlier days alone (module doc,
    "Tuning"). The middle penalty, unvalidated, from fewer than ``MIN_SPLIT`` samples with a label
    and a lean or a single day, or when the earlier days teach poisson nothing."""
    scored = _scored(window)
    days = [kst_day(s.bar_close) for s in scored]
    distinct = sorted(set(days))
    untuned = Tuning(PENALTIES[len(PENALTIES) // 2], [], {}, None)
    if len(scored) < MIN_SPLIT or len(distinct) < 2:
        return untuned
    per_day = Counter(days)
    target = len(scored) * VALIDATION_SHARE

    def off_target(k: int) -> float:
        return abs(sum(per_day[d] for d in distinct[-k:]) - target)

    held_days = distinct[-min(range(1, len(distinct)), key=off_target) :]
    try:
        intensities = Poisson().fit([s for s in window if kst_day(s.bar_close) < held_days[0]])
    except NothingToLearn:
        return untuned
    leans, y = _leans(intensities.params, scored), _one_hot(scored)
    held = np.array([d >= held_days[0] for d in days])
    losses: dict[float, float] = {}
    best: tuple[float, float] | None = None
    for penalty in PENALTIES:  # strongest first: of equals, the sparser link
        losses[penalty] = _link_loss(_link(leans[~held], y[~held], penalty), leans[held], y[held])
        if best is None or losses[penalty] < best[0] - 1e-12:
            best = (losses[penalty], penalty)
    assert best is not None
    return Tuning(best[1], held_days, losses, intensities)


class AsymmetricLink:
    """(c): poisson's intensities and lean, its own link (module doc). An experiment's arm: not
    registered, and the engine never runs it."""

    name = "poisson-asymmetric"
    features = Poisson.features
    training_days = Poisson.training_days

    def default_model(self) -> Model:
        """Before any training: poisson's defaults, the link unweighted with the biases of poisson's
        forecast at a lean of 0, so it forecasts as poisson's defaults do (their lean is always 0)."""
        params = poisson_defaults()
        at_zero = _ordered_probabilities(params["slope"], params["cut"], np.array([0.0]))[0]
        params |= {param(LINK, "mean"): 0.0, param(LINK, "sd"): 1.0}
        for c, p in zip(CLASSES, at_zero, strict=True):
            params[param(LINK, "bias", c.value)] = float(np.log(p))
            params[param(LINK, "w", c.value)] = 0.0
        return Model(f"{self.name}-default", params | _prefixed(_validated_on([])))

    def fit(self, samples: Sequence[Sample], intensities: Model) -> Model:
        """The link on ``samples``' leans under ``intensities`` (poisson's model of the same
        window), with the penalty ``tuned`` chooses; the model keeps both. Raises NothingToLearn
        without a sample with a label and a lean."""
        scored = _scored(samples)
        if not scored:
            raise NothingToLearn("no scored samples with the trades' sides")
        tuning = tuned(samples)
        leans, y = _leans(intensities.params, scored), _one_hot(scored)
        link = _link(leans, y, tuning.penalty) | _prefixed(_validated_on(tuning.held))
        params = {**intensities.params, **class_moves(scored), **link}
        return Model.fitted(self.name, params, scored, _link_loss(link, leans, y))

    def predict(self, features: FeatureVector, model: Model) -> Forecast:
        """The link's forecast of the lean at the bar close; a third each before the day's first
        trade with a side, as poisson."""
        p = model.params
        lean = lean_of(p, features)
        if lean is None:
            return UNKNOWN
        up, flat, down = (float(q) for q in link_probabilities(p, np.array([lean]))[0])
        expected = sum(q * p[param("move", c.value)] for q, c in zip((up, flat, down), CLASSES, strict=True))
        return Forecast(up, flat, down, expected)


def _prefixed(validated: Mapping[str, float]) -> dict[str, float]:
    return {param(LINK, k): v for k, v in validated.items()}


def _ordered_coefficients(model: Model) -> dict[str, float]:
    return {"slope": model.params["slope"], "cut": model.params["cut"]}


def _link_coefficients(model: Model) -> dict[str, Any] | None:
    """(c)'s fitted link, as the report gives it; None for its defaults."""
    p = model.params
    if param(LINK, "penalty") not in p:
        return None
    return {
        "penalty": p[param(LINK, "penalty")],
        "validation_days": int(p[param(LINK, "validation_days")]),
        "lean_mean": p[param(LINK, "mean")],
        "lean_sd": p[param(LINK, "sd")],
        "bias": {c.value: p[param(LINK, "bias", c.value)] for c in CLASSES},
        "w": {c.value: p[param(LINK, "w", c.value)] for c in CLASSES},
    }


@dataclass(frozen=True)
class Fold:
    """A day of the experiment: what predicted it, on how many samples (b) and (c) were trained,
    how many of its bar closes were scored (those without a lean among them), and the links'
    coefficients."""

    day: date
    models: dict[str, str]  # arm -> the version of the model (the climate's table) that predicted it
    trained_on: int  # the training samples (b)'s link and (c) learned from: with a label and a lean
    scored: int  # its bar closes with a label: every arm predicts them
    unknown: int  # of those, the ones without a lean: (b) and (c) a third each
    ordered: dict[str, float]  # (b)'s slope and cut
    link: dict[str, Any] | None  # (c)'s (``_link_coefficients``); None: its defaults

    def as_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "models": self.models,
            "trained_on": self.trained_on,
            "scored": self.scored,
            "unknown": self.unknown,
            "ordered": self.ordered,
            "link": self.link,
        }


Confusion = dict[Direction, dict[Direction, int]]  # called -> happened -> bar closes


def confusion(records: Sequence[Scored]) -> Confusion:
    """How many bar closes of each class called came out as each class."""
    counts = Counter((r.predicted, r.outcome) for r in records)
    return {called: {happened: counts[called, happened] for happened in CLASSES} for called in CLASSES}


@dataclass(frozen=True, slots=True)
class Lean:
    """A bar close compared that has a lean: the lean, what came, and what (b) and (c) called (one
    for each bar close of every fold: kept small)."""

    lean: float
    outcome: Direction
    ordered: Direction  # (b)'s call
    asymmetric: Direction  # (c)'s


@dataclass(frozen=True)
class LeanBin:
    """A fifth of the leans: from ``low`` to ``high``, the classes that came, and what (b) and (c)
    called."""

    low: float
    high: float
    happened: dict[Direction, int]
    called: dict[str, dict[Direction, int]]  # arm -> class -> bar closes

    @property
    def n(self) -> int:
        return sum(self.happened.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "low": self.low,
            "high": self.high,
            "n": self.n,
            "happened": {d.value: self.happened[d] for d in CLASSES},
            "called": {arm: {d.value: by[d] for d in CLASSES} for arm, by in self.called.items()},
        }


@dataclass(frozen=True)
class Leans:
    """The leans at the bar closes compared: their spread, and the classes in each quintile."""

    n: int
    mean: float | None
    sd: float | None
    quantiles: dict[float, float]
    bins: list[LeanBin]

    @classmethod
    def of(cls, points: Sequence[Lean]) -> Leans:
        if not points:
            return cls(0, None, None, {}, [])
        values = np.array([p.lean for p in points], dtype=float)
        quantiles = {q: float(v) for q, v in zip(QUANTILES, np.quantile(values, QUANTILES), strict=True)}
        inner = np.quantile(values, np.linspace(0, 1, BINS + 1)[1:-1])
        # Bin i: above the i-th inner edge, up to the next (the first from the lowest lean).
        where = np.searchsorted(inner, values, side="left")
        bins = []
        for i in range(BINS):
            members = [p for p, at in zip(points, where, strict=True) if at == i]
            if members:  # equal edges leave a bin empty
                bins.append(
                    LeanBin(
                        low=min(p.lean for p in members),
                        high=max(p.lean for p in members),
                        happened=_by_class(p.outcome for p in members),
                        called={
                            POISSON_ARM: _by_class(p.ordered for p in members),
                            ASYMMETRIC_ARM: _by_class(p.asymmetric for p in members),
                        },
                    )
                )
        return cls(len(points), float(values.mean()), float(values.std()), quantiles, bins)

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "mean": self.mean,
            "sd": self.sd,
            "quantiles": {f"{q:g}": v for q, v in self.quantiles.items()},
            "bins": [b.as_dict() for b in self.bins],
        }


def _by_class(classes: Iterable[Direction]) -> dict[Direction, int]:
    counts = Counter(classes)
    return {d: counts[d] for d in CLASSES}


@dataclass(frozen=True)
class Experiment:
    made_at: datetime
    days: list[date]  # the days predicted, oldest first
    folds: list[Fold]
    arms: dict[str, ArmStats]
    confusion: dict[str, Confusion]
    over_climate: dict[str, PairedLift]  # (b)'s and (c)'s lifts over (a)
    asymmetric_over_poisson: PairedLift  # (c)'s over (b)
    leans: Leans
    last_model: Model  # (c)'s model of the last day

    @property
    def better(self) -> bool | None:
        """(c) better than (b): its lift over (b) above 0; None without a pair."""
        value = self.asymmetric_over_poisson.pairs.value
        return None if value is None else value > 0

    @property
    def lower_bound_up(self) -> bool | None:
        """(c)'s lower bound by trading day over the climate above (b)'s; None without both."""
        asymmetric, ordered = (self.over_climate[arm].by_day for arm in (ASYMMETRIC_ARM, POISSON_ARM))
        if asymmetric is None or ordered is None or asymmetric.low is None or ordered.low is None:
            return None
        return asymmetric.low > ordered.low

    @property
    def rule(self) -> bool:
        """#439's comparison, set before it ran: (c) better than (b), and its lower bound by
        trading day over the climate improved; met, the ordered link held poisson back."""
        return self.better is True and self.lower_bound_up is True

    def as_dict(self) -> dict[str, Any]:
        return {
            "experiment": EXPERIMENT,
            "made_at": self.made_at.isoformat(),
            "days": [d.isoformat() for d in self.days],
            "link": {"input": "lean", "penalties": list(PENALTIES), "validation_share": VALIDATION_SHARE},
            "folds": [f.as_dict() for f in self.folds],
            "arms": {
                arm: stats.as_dict() | {"confusion": _confusion_json(self.confusion[arm])}
                for arm, stats in self.arms.items()
            },
            "lift": {
                "over_climate": {arm: lifted.as_dict() for arm, lifted in self.over_climate.items()},
                "asymmetric_over_poisson": self.asymmetric_over_poisson.as_dict(),
            },
            "rule": {"better": self.better, "lower_bound_up": self.lower_bound_up, "met": self.rule},
            "leans": self.leans.as_dict(),
        }


def _confusion_json(matrix: Confusion) -> dict[str, dict[str, int]]:
    return {called.value: {h.value: n for h, n in row.items()} for called, row in matrix.items()}


def poisson_link_experiment(
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
    poisson, variant = Poisson(), AsymmetricLink()
    trainer = _WalkForward(settings, poisson, available, calendar, Leaders.of(settings), night=night)
    records: dict[str, list[Scored]] = {arm: [] for arm in ARMS}
    points: list[Lean] = []
    folds: list[Fold] = []
    last = variant.default_model()
    meter = Meter()
    for n, day in enumerate(predicted, 1):
        usage = DayUsage(meter)  # its memory, logged (#383)
        with usage.phase("training"):
            models, trained_on = _models(trainer, variant, day)
        with usage.phase("scoring"):
            scored, unknown = _score(trainer.samples_on(day), models, poisson, variant, records, points)
        folds.append(
            Fold(
                day,
                {arm: m.version for arm, m in models.items()},
                trained_on,
                scored,
                unknown,
                _ordered_coefficients(models[POISSON_ARM]),
                _link_coefficients(models[ASYMMETRIC_ARM]),
            )
        )
        last = models[ASYMMETRIC_ARM]
        give_back()
        log.info("poisson link: %s predicted by %s", day, ", ".join(folds[-1].models.values()))
        log.info("poisson link: memory on %s (day %d of %d): %s", day, n, len(predicted), usage.line())
    climate = records[CLIMATE_ARM]
    result = Experiment(
        made_at=now,
        days=list(predicted),
        folds=folds,
        arms={arm: arm_stats(records[arm]) for arm in ARMS},
        confusion={arm: confusion(records[arm]) for arm in ARMS},
        over_climate={arm: PairedLift.of(records[arm], climate) for arm in LINKED},
        asymmetric_over_poisson=PairedLift.of(records[ASYMMETRIC_ARM], records[POISSON_ARM]),
        leans=Leans.of(points),
        last_model=last,
    )
    path = settings.state_dir / REPORT
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
    tmp.replace(path)
    return result


def _models(trainer: _WalkForward, variant: AsymmetricLink, day: date) -> tuple[dict[str, Model], int]:
    """The arms' models for ``day``, each fitted on the days before it, and how many samples (b)'s
    link and (c) learned from: (b) as its pre-validation trains it, (c) on the samples (b) was
    given, with (b)'s intensities; its defaults while (b) has its own."""
    ordered = trainer.before(day)
    window = trainer.samples_before(day)
    learned = len(_scored(window))
    asymmetric = variant.default_model()
    if not ordered.version.endswith("-default"):
        try:
            asymmetric = variant.fit(window, ordered)
        except NothingToLearn as e:  # not when (b) learned: a guard
            log.warning("%s not trained before %s, its defaults predict: %s", variant.name, day, e)
    return {
        CLIMATE_ARM: trainer.climate_before(day),
        POISSON_ARM: ordered,
        ASYMMETRIC_ARM: asymmetric,
    }, learned


def _score(
    samples: Sequence[Sample],
    models: Mapping[str, Model],
    poisson: Poisson,
    variant: AsymmetricLink,
    records: Mapping[str, list[Scored]],
    points: list[Lean],
) -> tuple[int, int]:
    """Each arm's forecast of each of the day's bar closes with a label, onto ``records``, and
    those with a lean onto ``points``. The bar closes scored, and those without a lean."""
    scored = unknown = 0
    for s in samples:
        if s.outcome is None:
            continue
        called = direction(models[CLIMATE_ARM], s.symbol, s.bar_close) or Direction.FLAT
        forecasts = {
            CLIMATE_ARM: Forecast.certain(called),  # as the baseline (``baselines.Climate``)
            POISSON_ARM: poisson.predict(s.features, models[POISSON_ARM]),
            ASYMMETRIC_ARM: variant.predict(s.features, models[ASYMMETRIC_ARM]),
        }
        for arm, f in forecasts.items():
            records[arm].append(
                Scored(arm, models[arm].version, s.symbol, s.bar_close, f.p_up, f.p_flat, f.p_down, s.outcome)
            )
        lean = lean_of(models[POISSON_ARM].params, s.features)
        if lean is None:
            unknown += 1
        else:
            ordered, asymmetric = (records[arm][-1].predicted for arm in LINKED)
            points.append(Lean(lean, s.outcome, ordered, asymmetric))
        scored += 1
    return scored, unknown


def lines(result: Experiment, report: Path) -> list[str]:
    """The command's output: counts, rates, lifts, leans, coefficients and dates, no price."""
    first, last = result.days[0], result.days[-1]
    trained = sum(1 for f in result.folds if f.link is not None)
    scored = sum(f.scored for f in result.folds)
    unknown = sum(f.unknown for f in result.folds)
    out = [
        f"poisson link experiment (#439): {len(result.days)} recorded days, {first}..{last}",
        f"  folds: each day predicted by models fitted on poisson's {Poisson.training_days} trading days"
        f" before it; {trained} of {len(result.days)} days with a trained poisson",
        f"  compared: {scored} bar closes, every arm on each ({unknown} without a lean: no trade's side yet"
        " that day, (b) and (c) a third each)",
        "  (c): (b)'s lean through a multinomial logistic, each class its bias and weight on the standardized"
        " lean; L1 penalty validated on the window's latest whole days, their leans from intensities fitted"
        " before them",
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
    out += ["", "confusion: called -> happened up / flat / down"]
    for arm in ARMS:
        rows = [
            f"{called.value} " + "/".join(str(n) for n in row.values())
            for called, row in result.confusion[arm].items()
        ]
        out.append(f"{LABELS[arm]:<21} " + "   ".join(rows))
    out += ["", "lift (hit rate gained on the same bar closes; paired, then by trading day)"]
    for arm in LINKED:
        out.append(f"{LABELS[arm] + ' over (a)':<33} {_lifted(result.over_climate[arm])}")
    out.append(f"{'(c) over (b)':<33} {_lifted(result.asymmetric_over_poisson)}")
    out += ["", _rule_line(result)]
    over = result.over_climate[ASYMMETRIC_ARM].pairs.value
    if over is not None:
        verdict = "above 0" if over > 0 else "not above 0: its pre-validation's stats check would still fail"
        out.append(f"  (c) over the climate: {over:+.4f}, {verdict}")
    out += ["", *_lean_lines(result.leans), "", *_coefficient_lines(result.folds), f"report: {report}"]
    return out


def _rule_line(result: Experiment) -> str:
    asymmetric = result.over_climate[ASYMMETRIC_ARM].by_day
    ordered = result.over_climate[POISSON_ARM].by_day
    lows = (
        "-"
        if asymmetric is None or ordered is None or asymmetric.low is None or ordered.low is None
        else f"{asymmetric.low:+.4f} against {ordered.low:+.4f}"
    )
    verdict = (
        "met: the ordered link held poisson back"
        if result.rule
        else "not met: not the link alone; see the leans' quintiles and (c)'s weights"
    )
    return (
        f"#439 rule: (c) over (b) above 0: {_yes(result.better)}; (c)'s lower bound by trading day over"
        f" the climate above (b)'s: {_yes(result.lower_bound_up)} ({lows}) -> {verdict}"
    )


def _lean_lines(leans: Leans) -> list[str]:
    if not leans.n or leans.mean is None or leans.sd is None:
        return ["leans: none (no bar close compared had a lean)"]
    spread = ", ".join(f"{q:.0%} {v:+.4f}" for q, v in leans.quantiles.items())
    out = [
        f"leans at the bar closes compared ((b)'s, the same in (c)): {leans.n}, mean {leans.mean:+.4f},"
        f" sd {leans.sd:.4f}; {spread}",
        "lean quintile          n       came up / flat / down   (b) calls u/f/d  (c) calls u/f/d",
    ]
    for b in leans.bins:
        came = " / ".join(f"{b.happened[d] / b.n:.3f}" for d in CLASSES)
        called = ["/".join(str(b.called[arm][d]) for d in CLASSES) for arm in LINKED]
        out.append(f"{f'{b.low:+.4f}..{b.high:+.4f}':<22} {b.n:<7} {came:<23} {called[0]:<16} {called[1]}")
    return out


def _coefficient_lines(folds: Sequence[Fold]) -> list[str]:
    trained = [f for f in folds if f.link is not None]
    nonzero = sum(1 for f in trained if f.link is not None and any(w != 0 for w in f.link["w"].values()))
    out = [
        "folds' links: (b) slope k, cut c; (c) penalty (days validated), lean mean ± sd, weight / bias of"
        f" up, flat, down; (c) weighs the lean on {nonzero} of {len(trained)} trained days"
    ]
    for f in folds:
        line = f"{f.day}  (b) k {f.ordered['slope']:g} c {f.ordered['cut']:g}"
        if f.link is None:
            out.append(f"{line}  (c) defaults")
            continue
        link: dict[str, Any] = f.link
        weights = " ".join(
            f"{c.value} {link['w'][c.value]:+.3f}/{link['bias'][c.value]:+.3f}" for c in CLASSES
        )
        out.append(
            f"{line}  (c) penalty {link['penalty']:g} ({link['validation_days']}), lean"
            f" {link['lean_mean']:+.4f} ± {link['lean_sd']:.4f}, {weights}"
        )
    return out
