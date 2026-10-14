"""Pre-validation: a new algorithm replayed on the latest recorded trading days before it
runs live (``krx-quant prevalidate <name>``).

The engine runs an algorithm registered after this check existed only once it has passed
with the code the engine runs (``admits``); until then it is not predicted, listed or
reviewed. The algorithms registered before (``REGISTERED_BEFORE``) and any algorithm the
engine has already enrolled run as before.

The check replays the last ``PREVALIDATION_DAYS`` recorded trading days, one at a time,
with every registered algorithm, into a database of its own
(``state/prevalidation-<name>.sqlite3``): nothing reaches the official record. The
algorithm is retrained before each day on its own window of the days before it
(walk-forward), never on the day it predicts; the others run the models the server runs.
A registry that fails the registration check is not replayed at all (RegistrationError).
It passes when all of these hold:

- **days**: at least ``MIN_DAYS`` recorded days were replayed;
- **features**: its own features, as the registration check let them through (none used
  by an algorithm registered before it, a feature set unlike any other);
- **budget**: no prediction over the time budget (``timed_out``);
- **correlation**: its p_up − p_down moves with no other algorithm's above the clone
  threshold (0.9), on the bar closes both predicted;
- **stats**: the leaderboard's statistics of the replay (basis B, special predictions left
  out) have at least ``min_sample`` scored predictions and pairs with each baseline, and
  its weakest lift over the baselines is above 0.

The report is saved to ``state/prevalidations/<name>.json``, passed or not, with a digest
of the algorithm's code; the engine reads it at each bar close.

With the touch target (``KRX_TARGET_VERSION=2``, #169) the candidate trains on its labels,
the replay forecasts it (the touch target's baselines, the climate table refitted before
each day on the days before it, as the candidate is), and the stats are scored on the
touch label instead of basis B.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from krx_quant.algorithms import ALGORITHMS, Algorithm, Model, NothingToLearn, load_models, register
from krx_quant.baselines import baselines_for
from krx_quant.climate import CLIMATE_DAYS, fit_climate
from krx_quant.config import Settings
from krx_quant.features import Leaders
from krx_quant.feeds.base import KST
from krx_quant.lifecycle import BASIS, CLONE_CORRELATION, correlations, most_correlated
from krx_quant.market_calendar import MarketCalendar, calendar_store
from krx_quant.predictions import PredictionStore
from krx_quant.replay import recorded_events, replay_recorded, sqlite_files
from krx_quant.stats import TOUCH_BASIS, Entry, enough, entry_json, leaderboard, scored_by_algorithm, split
from krx_quant.training import TrainingDay, finished_days, is_recorded, training_day

log = logging.getLogger(__name__)

PREVALIDATION_DAYS = 20
MIN_DAYS = 10  # fewer recorded days can be replayed, for a look, but never pass
# Registered before pre-validation existed (#24): they run without a report.
REGISTERED_BEFORE = frozenset({"newton", "gauss", "granger", "markov", "turing"})


def report_path(state_dir: Path, name: str) -> Path:
    return state_dir / "prevalidations" / f"{name}.json"


def code_digest(algorithm: Algorithm) -> str:
    """A digest of the algorithm's own code: its module, or every module of its package."""
    module = inspect.getmodule(type(algorithm))
    if module is None or module.__file__ is None:
        raise ValueError(f"{algorithm.name}: no source file to check")
    path = Path(module.__file__)
    root = path.parent if path.name == "__init__.py" else None
    files = sorted(root.rglob("*.py")) if root is not None else [path]
    digest = hashlib.sha256()
    for f in files:
        digest.update(f.name.encode() if root is None else f.relative_to(root).as_posix().encode())
        digest.update(f.read_bytes())
    return digest.hexdigest()[:16]


def admits(state_dir: Path, target_version: int = 1) -> Callable[[Algorithm], bool]:
    """Whether the engine may start a new algorithm: registered before pre-validation, or
    its latest report passed with the code it runs now, on the target it runs now (#169; a
    report without one was on the four-minute move, 1)."""

    def admitted(algorithm: Algorithm) -> bool:
        if algorithm.name in REGISTERED_BEFORE:
            return True
        path = report_path(state_dir, algorithm.name)
        try:
            report = json.loads(path.read_text())
        except FileNotFoundError:
            return False
        except (OSError, ValueError) as e:
            log.error("cannot read %s (%s); %s waits", path, e, algorithm.name)
            return False
        if not isinstance(report, dict) or report.get("passed") is not True:
            return False
        if report.get("code") != code_digest(algorithm):
            log.warning("%s passed with other code; it waits for a new pre-validation", algorithm.name)
            return False
        if report.get("target_version", 1) != target_version:
            log.warning("%s passed on another target; it waits for a new pre-validation", algorithm.name)
            return False
        return True

    return admitted


@dataclass(frozen=True)
class Check:
    name: str  # days, features, budget, correlation, stats
    passed: bool
    detail: str


@dataclass(frozen=True)
class Prevalidation:
    algorithm: str
    code: str  # code_digest of what was replayed
    prevalidated_at: datetime
    days: list[date]  # replayed, oldest first
    models: dict[date, str]  # the version the algorithm predicted each day with
    checks: list[Check]
    stats: Entry
    correlations: dict[str, float]  # with each other algorithm it could be measured against
    statuses: dict[str, int]  # its predictions by status
    skipped_for_cpu: int  # bar closes it was skipped at for the CPU budget
    db: Path
    target_version: int = 1  # the target replayed and scored on (#169)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "code": self.code,
            "prevalidated_at": self.prevalidated_at.isoformat(),
            "passed": self.passed,
            "days": [d.isoformat() for d in self.days],
            "models": {d.isoformat(): v for d, v in self.models.items()},
            "checks": [{"check": c.name, "passed": c.passed, "detail": c.detail} for c in self.checks],
            "stats": entry_json(self.stats),
            "correlations": self.correlations,
            "timed_out": self.statuses.get("timed_out", 0),
            "statuses": self.statuses,
            "skipped_for_cpu": self.skipped_for_cpu,
            "db": str(self.db),
            "target_version": self.target_version,
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

    Raises ValueError for an algorithm not registered (RegistrationError for a registry
    that fails the registration check), FileNotFoundError when no day was recorded.
    """
    candidate = next((a for a in algorithms if a.name == name), None)
    if candidate is None:
        raise ValueError(f"unknown algorithm {name!r}; registered: {', '.join(a.name for a in algorithms)}")
    register(algorithms)
    now = now or datetime.now(KST)
    available = finished_days(settings.data_dir, settings.symbols, now)
    replayed = [d for d in available if is_recorded(settings.data_dir, d, settings.symbols)][-days:]
    if not replayed:
        raise FileNotFoundError(f"no recorded day of {', '.join(settings.symbols)} in {settings.data_dir}")
    db = settings.state_dir / f"prevalidation-{name}.sqlite3"
    for leftover in sqlite_files(db):
        leftover.unlink(missing_ok=True)
    models = await _replay(settings, candidate, algorithms, available, replayed, db)
    result = _judge(
        settings, candidate, [a.name for a in algorithms if a is not candidate], replayed, models, db, now
    )
    path = report_path(settings.state_dir, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
    tmp.replace(path)
    return result


async def _replay(
    settings: Settings,
    candidate: Algorithm,
    algorithms: Sequence[Algorithm],
    available: Sequence[date],
    replayed: Sequence[date],
    db: Path,
) -> dict[date, str]:
    """Replay each day into ``db``, the candidate trained on the days before it; the
    version it predicted each day with."""
    calendar = calendar_store(settings).calendar
    leaders = Leaders.of(settings)
    running = load_models(settings.state_dir, [a for a in algorithms if a is not candidate])
    trainer = _WalkForward(settings, candidate, available, calendar, leaders)
    versions: dict[date, str] = {}
    for day in replayed:
        model = trainer.before(day)
        versions[day] = model.version
        climate = trainer.climate_before(day) if settings.target_version >= 2 else None
        events = recorded_events(settings.data_dir, day, settings.symbols, leaders)
        engine = await replay_recorded(
            settings,
            day,
            events,
            db=db,
            models={**running, candidate.name: model},
            algorithms=algorithms,
            calendar=calendar,
            climate=climate,
        )
        engine.store.close()
        log.info("%s: replayed %s with %s", candidate.name, day, model.version)
    return versions


def _judge(
    settings: Settings,
    candidate: Algorithm,
    others: Sequence[str],
    replayed: Sequence[date],
    models: Mapping[date, str],
    db: Path,
    now: datetime,
) -> Prevalidation:
    """The candidate's report, from what the replay stored in ``db``."""
    name = candidate.name
    # The touch target's label and baselines with it (#169), else basis B's.
    basis = TOUCH_BASIS if settings.target_version >= 2 else BASIS
    names = {b.name for b in baselines_for(settings.target_version)}
    store = PredictionStore(db)
    try:
        statuses = store.status_counts(name)
        skipped = store.count_runs("cpu_budget", datetime.combine(replayed[0], time(), KST), name)
        algos, baselines = split(
            scored_by_algorithm(store.scored_predictions(replayed, None, False), basis), names
        )
        measured = correlations(name, others, store.scored_predictions(replayed, None, True))
    finally:
        store.close()
    # Ranked with the others, as the leaderboard would: its lifts' FDR adjustment is theirs.
    board = leaderboard(
        {**algos, name: algos.get(name, [])}, baselines, settings.min_sample, settings.fee_rate
    )
    stats = next(e for e in [*board.ranked, *board.unranked] if e.algorithm == name)
    checks = [
        Check(
            "days",
            len(replayed) >= MIN_DAYS,
            f"{len(replayed)} recorded days (at least {MIN_DAYS}, up to {PREVALIDATION_DAYS})",
        ),
        Check(
            "features", True, f"its own: {', '.join(candidate.card.unique_features)}; no other has them first"
        ),
        _budget(statuses),
        _correlation(measured),
        _stats(stats, settings.min_sample),
    ]
    return Prevalidation(
        algorithm=name,
        code=code_digest(candidate),
        prevalidated_at=now,
        days=list(replayed),
        models=dict(models),
        checks=checks,
        stats=stats,
        correlations=measured,
        statuses=statuses,
        skipped_for_cpu=skipped,
        db=db,
        target_version=settings.target_version,
    )


def _budget(statuses: Mapping[str, int]) -> Check:
    over = statuses.get("timed_out", 0)
    total = sum(statuses.values())
    return Check("budget", over == 0, f"{over} of {total} predictions over the time budget")


def _correlation(measured: Mapping[str, float]) -> Check:
    twin = most_correlated(measured)
    if twin is None:
        return Check("correlation", True, "no other algorithm to measure against")
    passed = twin[1] <= CLONE_CORRELATION
    return Check(
        "correlation",
        passed,
        f"highest with {twin[0]}: r = {twin[1]:.3f} (a clone above {CLONE_CORRELATION})",
    )


def _stats(stats: Entry, min_sample: int) -> Check:
    weakest = stats.weakest_lift
    if not enough(stats, min_sample) or weakest is None:
        pairs = min((t.lift.n for t in stats.lifts.values()), default=0)
        return Check(
            "stats",
            False,
            f"too few to judge: {stats.summary.n} scored, {pairs} pairs (at least {min_sample})",
        )
    return Check(
        "stats", weakest > 0, f"weakest lift {weakest:.4f} over {stats.summary.n} scored predictions"
    )


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
        self._labelled: dict[date, TrainingDay | None] = {}  # the climate's days, labels only

    def before(self, day: date) -> Model:
        window = [d for d in self._available if d < day][-self._algorithm.training_days :]
        for gone in [d for d in self._loaded if not window or d < window[0]]:
            del self._loaded[gone]
        training = [s for d in window if (found := self._load(d)) is not None for s in found.samples]
        if not any(s.outcome is not None for s in training):
            return self._algorithm.default_model()
        try:
            return self._algorithm.fit(training)
        except NothingToLearn as e:  # no book on backfilled days, say; any other error fails the check
            log.warning("%s not trained before %s, its defaults predict: %s", self._algorithm.name, day, e)
            return self._algorithm.default_model()

    def climate_before(self, day: date) -> Model:
        """The climate baseline's table fitted on the latest ``CLIMATE_DAYS`` days before
        ``day``, as the night before it would (#169)."""
        window = [d for d in self._available if d < day][-CLIMATE_DAYS:]
        for gone in [d for d in self._labelled if not window or d < window[0]]:
            del self._labelled[gone]
        days = [self._loaded[d] if d in self._loaded else self._labels(d) for d in window]
        return fit_climate([s for found in days if found is not None for s in found.samples])

    def _labels(self, day: date) -> TrainingDay | None:
        if day not in self._labelled:
            self._labelled[day] = self._read(day, ())
        return self._labelled[day]

    def _load(self, day: date) -> TrainingDay | None:
        if day not in self._loaded:
            self._loaded[day] = self._read(day, self._algorithm.features)
        return self._loaded[day]

    def _read(self, day: date, features: Sequence[str]) -> TrainingDay | None:
        s = self._settings
        return training_day(
            s.data_dir,
            day,
            self._calendar,
            s.symbols,
            features,
            s.fee_rate,
            self._leaders,
            target_version=s.target_version,
        )
