"""Pre-validation: a new algorithm replayed on the latest recorded trading days before it
runs live (``krx-quant prevalidate <name>``).

The engine runs an algorithm registered after this check existed only once it has passed
(``admits``); until then it is not predicted, listed or reviewed. The algorithms
registered before (``REGISTERED_BEFORE``) and any algorithm the engine has already
enrolled run as before.

The check replays the last ``PREVALIDATION_DAYS`` recorded trading days, one at a time,
with every registered algorithm, into a database of its own
(``state/prevalidation-<name>.sqlite3``): nothing reaches the official record. The
algorithm is retrained before each day on its own window of the days before it
(walk-forward), never on the day it predicts; the others run the models the server runs.
It passes when all of these hold:

- **features**: its features pass the registration check (none of its own used by
  another algorithm registered before it, a feature set unlike any other);
- **budget**: no prediction over the time budget (``timed_out``);
- **correlation**: its p_up − p_down moves with no other algorithm's above the clone
  threshold (0.9), on the bar closes both predicted;
- **stats**: the leaderboard's statistics (basis B, special predictions left out) have at
  least ``min_sample`` scored predictions and pairs with each baseline, and its weakest
  lift over the baselines is above 0.

The report is saved to ``state/prevalidations/<name>.json``, passed or not; the engine
reads it at each bar close.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import partial
from pathlib import Path
from typing import Any

from krx_quant.algorithms import ALGORITHMS, Algorithm, Model, RegistrationError, load_models, register
from krx_quant.api import entry_json
from krx_quant.baselines import BASELINE_NAMES
from krx_quant.config import Settings
from krx_quant.engine import Budget
from krx_quant.features import Leaders
from krx_quant.feeds.base import KST
from krx_quant.lifecycle import BASIS, CLONE_CORRELATION, correlations
from krx_quant.macro import day_regime
from krx_quant.market_calendar import MarketCalendar, calendar_store
from krx_quant.predictions import PredictionStore
from krx_quant.replay import recorded_events, replay, sqlite_files
from krx_quant.stats import Entry, enough, entry, scored_by_algorithm, split
from krx_quant.training import TrainingDay, finished_days, is_recorded, training_day

log = logging.getLogger(__name__)

PREVALIDATION_DAYS = 20
# Registered before pre-validation existed (#24): they run without a report.
REGISTERED_BEFORE = frozenset({"newton", "gauss", "granger", "markov", "turing"})


def report_path(state_dir: Path, name: str) -> Path:
    return state_dir / "prevalidations" / f"{name}.json"


def admits(state_dir: Path) -> Callable[[str], bool]:
    """Whether the engine may start a new algorithm: registered before pre-validation, or
    its latest report passed."""

    def admitted(name: str) -> bool:
        if name in REGISTERED_BEFORE:
            return True
        path = report_path(state_dir, name)
        try:
            return json.loads(path.read_text()).get("passed") is True
        except FileNotFoundError:
            return False
        except (OSError, ValueError) as e:
            log.error("cannot read %s (%s); %s waits", path, e, name)
            return False

    return admitted


@dataclass(frozen=True)
class Check:
    check: str  # features, budget, correlation, stats
    passed: bool
    detail: str


@dataclass(frozen=True)
class Prevalidation:
    algorithm: str
    prevalidated_at: datetime
    days: list[date]  # replayed, oldest first
    models: dict[date, str]  # the version the algorithm predicted each day with
    checks: list[Check]
    stats: Entry | None  # None when the features check failed and nothing was replayed
    correlations: dict[str, float]  # with each other algorithm it could be measured against
    statuses: dict[str, int]  # its predictions by status
    skipped_for_cpu: int  # bar closes it was skipped at for the CPU budget
    db: Path

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def timed_out(self) -> int:
        return self.statuses.get("timed_out", 0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "prevalidated_at": self.prevalidated_at.isoformat(),
            "passed": self.passed,
            "days": [d.isoformat() for d in self.days],
            "models": {d.isoformat(): v for d, v in self.models.items()},
            "checks": [{"check": c.check, "passed": c.passed, "detail": c.detail} for c in self.checks],
            "stats": None if self.stats is None else entry_json(self.stats),
            "correlations": self.correlations,
            "timed_out": self.timed_out,
            "statuses": self.statuses,
            "skipped_for_cpu": self.skipped_for_cpu,
            "db": str(self.db),
        }


async def prevalidate(
    settings: Settings,
    name: str,
    *,
    algorithms: Sequence[Algorithm] = ALGORITHMS,
    days: int = PREVALIDATION_DAYS,
    now: datetime | None = None,
) -> Prevalidation:
    """Replay ``name`` on the latest ``days`` recorded trading days and save its report.

    Raises ValueError for an algorithm not registered, FileNotFoundError when no day was
    recorded.
    """
    candidate = next((a for a in algorithms if a.name == name), None)
    if candidate is None:
        raise ValueError(f"unknown algorithm {name!r}; registered: {', '.join(a.name for a in algorithms)}")
    now = now or datetime.now(KST)
    available = finished_days(settings.data_dir, settings.symbols, now)
    replayed = [d for d in available if is_recorded(settings.data_dir, d, settings.symbols)][-days:]
    if not replayed:
        raise FileNotFoundError(f"no recorded day of {', '.join(settings.symbols)} in {settings.data_dir}")
    db = settings.state_dir / f"prevalidation-{name}.sqlite3"
    for leftover in sqlite_files(db):
        leftover.unlink(missing_ok=True)
    try:
        register(algorithms)
    except RegistrationError as e:
        result = Prevalidation(
            name, now, replayed, {}, [Check("features", False, str(e))], None, {}, {}, 0, db
        )
    else:
        result = await _replayed(settings, candidate, algorithms, available, replayed, db, now)
    path = report_path(settings.state_dir, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
    tmp.replace(path)
    return result


async def _replayed(
    settings: Settings,
    candidate: Algorithm,
    algorithms: Sequence[Algorithm],
    available: Sequence[date],
    replayed: Sequence[date],
    db: Path,
    now: datetime,
) -> Prevalidation:
    calendar = calendar_store(settings).calendar
    leaders = Leaders.of(settings)
    others = [a for a in algorithms if a is not candidate]
    running = load_models(settings.state_dir, others)
    trainer = _WalkForward(settings, candidate, available, calendar, leaders)
    models: dict[date, str] = {}
    for day in replayed:
        model = trainer.before(day)
        models[day] = model.version
        start = datetime.combine(day, time(), KST)
        engine = await replay(
            recorded_events(settings.data_dir, day, settings.symbols, leaders),
            db=db,
            calendar=calendar,
            symbols=settings.symbols,
            fee_rate=settings.fee_rate,
            start=start,
            until=start + timedelta(days=1) - timedelta(microseconds=1),
            algorithms=algorithms,
            models={**running, candidate.name: model},
            budget=Budget.of(settings),
            min_sample=settings.min_sample,
            leaders=leaders,
            regimes=partial(day_regime, settings.data_dir),
        )
        engine.store.close()
        log.info("%s: replayed %s with %s", candidate.name, day, model.version)
    store = PredictionStore(db)
    try:
        name = candidate.name
        statuses = store.status_counts(name)
        skipped = store.count_runs("cpu_budget", datetime.combine(replayed[0], time(), KST), name)
        algos, baselines = split(
            scored_by_algorithm(store.scored_predictions(replayed, None, False), BASIS), BASELINE_NAMES
        )
        stats = entry(name, algos.get(name, []), baselines, settings.fee_rate)
        measured = correlations(
            name, [a.name for a in others], store.scored_predictions(replayed, None, True)
        )
    finally:
        store.close()
    checks = [
        Check("features", True, f"its own: {', '.join(candidate.card.unique_features)}"),
        _budget(statuses),
        _correlation(measured),
        _stats(stats, settings.min_sample),
    ]
    return Prevalidation(name, now, list(replayed), models, checks, stats, measured, statuses, skipped, db)


def _budget(statuses: dict[str, int]) -> Check:
    over = statuses.get("timed_out", 0)
    total = sum(statuses.values())
    return Check("budget", over == 0, f"{over} of {total} predictions over the time budget")


def _correlation(measured: dict[str, float]) -> Check:
    twin = max(measured.items(), key=lambda kv: kv[1], default=None)
    if twin is None:
        return Check("correlation", True, "no other algorithm to measure against")
    passed = twin[1] <= CLONE_CORRELATION
    return Check(
        "correlation", passed, f"highest with {twin[0]}: r = {twin[1]:.3f} (clone above {CLONE_CORRELATION})"
    )


def _stats(stats: Entry, min_sample: int) -> Check:
    if not enough(stats, min_sample):
        pairs = min((t.lift.n for t in stats.lifts.values()), default=0)
        return Check(
            "stats",
            False,
            f"too few to judge: {stats.summary.n} scored, {pairs} pairs (at least {min_sample})",
        )
    weakest = stats.weakest_lift
    passed = weakest is not None and weakest > 0
    return Check("stats", passed, f"weakest lift {weakest:.4f} over {stats.summary.n} scored predictions")


class _WalkForward:
    """The algorithm retrained before each day on its own window of the days before it."""

    def __init__(
        self,
        settings: Settings,
        algorithm: Algorithm,
        available: Sequence[date],
        calendar: MarketCalendar,
        leaders: Leaders,
    ) -> None:
        self._settings = settings
        self._algorithm = algorithm
        self._available = list(available)
        self._calendar = calendar
        self._leaders = leaders
        self._loaded: dict[date, TrainingDay | None] = {}

    def before(self, day: date) -> Model:
        window = [d for d in self._available if d < day][-self._algorithm.training_days :]
        for gone in [d for d in self._loaded if not window or d < window[0]]:
            del self._loaded[gone]
        training = [s for d in window if (found := self._load(d)) is not None for s in found.samples]
        if not any(s.outcome is not None for s in training):
            return self._algorithm.default_model()
        return self._algorithm.fit(training)

    def _load(self, day: date) -> TrainingDay | None:
        if day not in self._loaded:
            s = self._settings
            self._loaded[day] = training_day(
                s.data_dir,
                day,
                self._calendar,
                s.symbols,
                self._algorithm.features,
                s.fee_rate,
                self._leaders,
            )
        return self._loaded[day]
