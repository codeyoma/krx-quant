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
- **stats**: the leaderboard's statistics of the replay (the touch label, special
  predictions left out) have at least ``min_sample`` scored predictions and pairs with each
  baseline, and its weakest lift over the baselines is above 0.

The report is saved to ``state/prevalidations/<name>.json``, passed or not, with a digest
of the algorithm's code; the engine reads it at each bar close. The server's scheduled job
runs this check itself, one waiting algorithm a night (``scheduled.py``, #204), and reads the
report (``last_report``) to know whether the code it runs now has been judged already.

With the touch target (``KRX_TARGET_VERSION=2``, the default since #170) the candidate
trains on its labels and the replay forecasts it (the touch target's baselines, the climate
table refitted before each day on the days before it, as the candidate is). The stats are
scored on the touch label either way (#170; before, basis B).

An algorithm of a kind besides the direction (#179, ``kinds.py``) is checked the same way
on its kind: it trains on the kind's answers, its stats are the kind's (scored on its
answers, against its baselines), its correlation is of its first class's probability less
its third's with the other algorithms of its kind only, and its report holds the kind and
the kind's answer version, which the engine's ``admits`` checks. Its kind's baselines that
predict from a table (#180, the range's climate) are refitted before each day on the kind's
answers of the days before it, as the night before would.
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
from typing import Any, cast

from krx_quant.algorithms import (
    ALGORITHMS,
    KIND_ALGORITHMS,
    Algorithm,
    KindAlgorithm,
    Model,
    NothingToLearn,
    kind_of,
    load_models,
    register,
    register_kind_algorithms,
)
from krx_quant.baselines import baselines_for
from krx_quant.climate import CLIMATE_DAYS, fit_climate
from krx_quant.config import Settings
from krx_quant.features import Leaders
from krx_quant.feeds.base import KST
from krx_quant.kind_registry import KINDS, kind_named
from krx_quant.kind_stats import KindEntry, kind_scoring, scored_of_kind
from krx_quant.kind_stats import entry_json as kind_entry_json
from krx_quant.kind_stats import leaderboard as kind_leaderboard
from krx_quant.kind_stats import split as kind_split
from krx_quant.kinds import DIRECTION, Kind, table_baselines
from krx_quant.lifecycle import CLONE_CORRELATION, correlations, most_correlated
from krx_quant.market_calendar import MarketCalendar, calendar_store
from krx_quant.predictions import PredictionStore
from krx_quant.replay import recorded_events, replay_recorded, sqlite_files
from krx_quant.stats import (
    Entry,
    Lifted,
    enough,
    entry_json,
    leaderboard,
    on_target,
    scored_by_algorithm,
    split,
)
from krx_quant.training import TrainingDay, finished_days, is_recorded, training_day

log = logging.getLogger(__name__)

PREVALIDATION_DAYS = 20
MIN_DAYS = 10  # fewer recorded days can be replayed, for a look, but never pass
# Registered before pre-validation existed (#24): they run without a report.
REGISTERED_BEFORE = frozenset({"newton", "gauss", "granger", "markov", "turing"})


def report_path(state_dir: Path, name: str) -> Path:
    return state_dir / "prevalidations" / f"{name}.json"


def code_digest(algorithm: Algorithm | KindAlgorithm) -> str:
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


def admits(
    state_dir: Path, target_version: int = 1, kinds: Sequence[Kind] = KINDS
) -> Callable[[Algorithm | KindAlgorithm], bool]:
    """Whether the engine may start a new algorithm: registered before pre-validation, or
    its latest report passed with the code it runs now, on the target it runs now (#169; a
    report without one was on the four-minute move, 1). An algorithm of a kind besides the
    direction (#179) passed on its kind's answer version (one of ``kinds``)."""

    def admitted(algorithm: Algorithm | KindAlgorithm) -> bool:
        if algorithm.name in REGISTERED_BEFORE:
            return True
        report = _read_report(state_dir, algorithm.name)
        if report is None or report.get("passed") is not True:
            return False
        stale = _stale(report, algorithm, target_version, kinds)
        if stale is not None:
            log.warning("%s passed %s; it waits for a new pre-validation", algorithm.name, stale)
            return False
        return True

    return admitted


@dataclass(frozen=True)
class LastReport:
    """An algorithm's latest report, as the scheduled pre-validation reads it (#204)."""

    made_at: datetime | None  # its ``prevalidated_at``; None in a report without one
    current: bool  # made with the code, and on the target, the algorithm runs now
    passed: bool
    days: int  # the recorded days it replayed

    @property
    def judged(self) -> bool:
        """It judged the code the algorithm runs now: passed, or refused on at least
        ``MIN_DAYS`` days (on fewer it was a look, which never passes)."""
        return self.current and (self.passed or self.days >= MIN_DAYS)


def last_report(
    state_dir: Path,
    algorithm: Algorithm | KindAlgorithm,
    target_version: int = 1,
    kinds: Sequence[Kind] = KINDS,
) -> LastReport | None:
    """The algorithm's latest report as it stands for the code and target it runs now (the
    same test as ``admits``); None without one, or with one that cannot be read."""
    report = _read_report(state_dir, algorithm.name)
    if report is None:
        return None
    try:
        made_at: datetime | None = datetime.fromisoformat(str(report["prevalidated_at"]))
    except (KeyError, ValueError):
        made_at = None
    if made_at is not None and made_at.tzinfo is None:
        made_at = None  # not comparable with the scheduler's times
    days = report.get("days")
    return LastReport(
        made_at=made_at,
        current=_stale(report, algorithm, target_version, kinds) is None,
        passed=report.get("passed") is True,
        days=len(days) if isinstance(days, list) else 0,
    )


def _read_report(state_dir: Path, name: str) -> dict[str, Any] | None:
    """``name``'s report as saved; None without one, or with one that cannot be read."""
    path = report_path(state_dir, name)
    try:
        report = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.error("cannot read %s (%s); %s waits", path, e, name)
        return None
    return report if isinstance(report, dict) else None


def _stale(
    report: Mapping[str, Any],
    algorithm: Algorithm | KindAlgorithm,
    target_version: int,
    kinds: Sequence[Kind],
) -> str | None:
    """How ``report`` is not of the code and target the algorithm runs now; None if it is."""
    if report.get("code") != code_digest(algorithm):
        return "with other code"
    if report.get("target_version", 1) != _target(algorithm, target_version, kinds):
        return "on another target"
    return None


def _target(algorithm: Algorithm | KindAlgorithm, target_version: int, kinds: Sequence[Kind]) -> int | None:
    """The target an algorithm's report must have passed on: the engine's, or its kind's answer
    version (#179); None for a kind not registered (it never passes)."""
    name = kind_of(algorithm)
    if name == DIRECTION:
        return target_version
    kind = kind_named(name, kinds)
    return None if kind is None else kind.answer_version


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
    stats: Entry | KindEntry  # its kind's (#179)
    correlations: dict[str, float]  # with each other algorithm it could be measured against
    statuses: dict[str, int]  # its predictions by status
    skipped_for_cpu: int  # bar closes it was skipped at for the CPU budget
    db: Path
    target_version: int = 1  # the target replayed and scored on (#169); a kind's answer version
    kind: str = DIRECTION  # the kind it predicts (#179)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def as_dict(self) -> dict[str, Any]:
        out = {
            "algorithm": self.algorithm,
            "code": self.code,
            "prevalidated_at": self.prevalidated_at.isoformat(),
            "passed": self.passed,
            "days": [d.isoformat() for d in self.days],
            "models": {d.isoformat(): v for d, v in self.models.items()},
            "checks": [{"check": c.name, "passed": c.passed, "detail": c.detail} for c in self.checks],
            "stats": entry_json(self.stats) if isinstance(self.stats, Entry) else kind_entry_json(self.stats),
            "correlations": self.correlations,
            "timed_out": self.statuses.get("timed_out", 0),
            "statuses": self.statuses,
            "skipped_for_cpu": self.skipped_for_cpu,
            "db": str(self.db),
            "target_version": self.target_version,
        }
        if self.kind != DIRECTION:
            out["kind"] = self.kind
        return out


async def prevalidate(
    settings: Settings,
    name: str,
    *,
    algorithms: Sequence[Algorithm] = ALGORITHMS,
    kind_algorithms: Sequence[KindAlgorithm] = KIND_ALGORITHMS,
    kinds: Sequence[Kind] = KINDS,
    days: int = PREVALIDATION_DAYS,
    now: datetime | None = None,
) -> Prevalidation:
    """Replay ``name`` on the latest ``days`` recorded trading days and save its report.

    Raises ValueError for an algorithm not registered (RegistrationError for a registry
    that fails the registration check), FileNotFoundError when no day was recorded.
    """
    candidate: Algorithm | KindAlgorithm | None = next((a for a in algorithms if a.name == name), None)
    if candidate is None:
        candidate = next((a for a in kind_algorithms if a.name == name), None)
    kind = (
        None
        if candidate is None or kind_of(candidate) == DIRECTION
        else kind_named(kind_of(candidate), kinds)
    )
    if candidate is None or (kind_of(candidate) != DIRECTION and kind is None):
        names = ", ".join([*(a.name for a in algorithms), *(a.name for a in kind_algorithms)])
        raise ValueError(f"unknown algorithm {name!r}; registered: {names}")
    register(algorithms)
    register_kind_algorithms(kind_algorithms, kinds, taken={a.name for a in algorithms})
    now = now or datetime.now(KST)
    available = finished_days(settings.data_dir, settings.symbols, now)
    replayed = [d for d in available if is_recorded(settings.data_dir, d, settings.symbols)][-days:]
    if not replayed:
        raise FileNotFoundError(f"no recorded day of {', '.join(settings.symbols)} in {settings.data_dir}")
    db = settings.state_dir / f"prevalidation-{name}.sqlite3"
    for leftover in sqlite_files(db):
        leftover.unlink(missing_ok=True)
    models = await _replay(
        settings, candidate, kind, algorithms, kind_algorithms, kinds, available, replayed, db
    )
    if kind is None:
        others = [a.name for a in algorithms if a is not candidate]
        result = _judge(settings, candidate, others, replayed, models, db, now)
    else:
        same = [a.name for a in kind_algorithms if a.kind == kind.name and a is not candidate]
        result = _judge_kind(settings, candidate, kind, same, replayed, models, db, now)
    path = report_path(settings.state_dir, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
    tmp.replace(path)
    return result


async def _replay(
    settings: Settings,
    candidate: Algorithm | KindAlgorithm,
    kind: Kind | None,
    algorithms: Sequence[Algorithm],
    kind_algorithms: Sequence[KindAlgorithm],
    kinds: Sequence[Kind],
    available: Sequence[date],
    replayed: Sequence[date],
    db: Path,
) -> dict[date, str]:
    """Replay each day into ``db``, the candidate trained on the days before it (on its kind's
    answers, #179); the version it predicted each day with."""
    calendar = calendar_store(settings).calendar
    leaders = Leaders.of(settings)
    everyone: list[Algorithm | KindAlgorithm] = [*algorithms, *kind_algorithms]
    running = load_models(settings.state_dir, [a for a in everyone if a is not candidate])
    trainer = _WalkForward(settings, candidate, available, calendar, leaders, kind)
    versions: dict[date, str] = {}
    for day in replayed:
        model = trainer.before(day)
        versions[day] = model.version
        climate = trainer.climate_before(day) if settings.target_version >= 2 else None
        tables = trainer.tables_before(day)  # its kind's baselines' (#180)
        events = recorded_events(settings.data_dir, day, settings.symbols, leaders)
        engine = await replay_recorded(
            settings,
            day,
            events,
            db=db,
            models={**running, **tables, candidate.name: model},
            algorithms=algorithms,
            calendar=calendar,
            climate=climate,
            kinds=kinds,
            kind_algorithms=kind_algorithms,
        )
        engine.store.close()
        log.info("%s: replayed %s with %s", candidate.name, day, model.version)
    return versions


def _judge(
    settings: Settings,
    candidate: Algorithm | KindAlgorithm,
    others: Sequence[str],
    replayed: Sequence[date],
    models: Mapping[date, str],
    db: Path,
    now: datetime,
) -> Prevalidation:
    """The candidate's report, from what the replay stored in ``db``."""
    name = candidate.name
    # Scored on the touch label (#170), against the target's baselines (#169).
    names = {b.name for b in baselines_for(settings.target_version)}
    store = PredictionStore(db)
    try:
        statuses = store.status_counts(name)
        skipped = store.count_runs("cpu_budget", datetime.combine(replayed[0], time(), KST), name)
        algos, baselines = split(scored_by_algorithm(store.scored_predictions(replayed, None, False)), names)
        measured = correlations(name, others, store.scored_predictions(replayed, None, True))
    finally:
        store.close()
    # Ranked with the others, as the leaderboard would: its lifts' FDR adjustment is theirs.
    board = leaderboard(
        {**algos, name: algos.get(name, [])}, baselines, settings.min_sample, settings.fee_rate
    )
    stats = next(e for e in [*board.ranked, *board.unranked] if e.algorithm == name)
    return Prevalidation(
        algorithm=name,
        code=code_digest(candidate),
        prevalidated_at=now,
        days=list(replayed),
        models=dict(models),
        checks=_checks(candidate, replayed, statuses, measured, stats, settings.min_sample),
        stats=stats,
        correlations=measured,
        statuses=statuses,
        skipped_for_cpu=skipped,
        db=db,
        target_version=settings.target_version,
    )


def _judge_kind(
    settings: Settings,
    candidate: Algorithm | KindAlgorithm,
    kind: Kind,
    others: Sequence[str],
    replayed: Sequence[date],
    models: Mapping[date, str],
    db: Path,
    now: datetime,
) -> Prevalidation:
    """A kind's candidate's report (#179): scored on the kind's answers, against its baselines,
    correlated with the other algorithms of its kind only."""
    name = candidate.name
    names = {b.name for b in kind.baselines}
    store = PredictionStore(db)
    try:
        statuses = store.kind_status_counts(name)
        skipped = store.count_runs("cpu_budget", datetime.combine(replayed[0], time(), KST), name)
        # As the API and the statuses go by: only forecasts of models trained on the kind's
        # answers (a default model, as on the first replayed day, or another version's sits out).
        version = kind.answer_version
        scored = on_target(store.kind_scored(kind.name, version, replayed, None, False), names, version)
        algos, baselines = kind_split(scored_of_kind(scored, kind), names)
        every = on_target(store.kind_scored(kind.name, version, replayed, None, True), names, version)
        measured = correlations(name, others, every, kind_scoring(kind).lean)
    finally:
        store.close()
    board = kind_leaderboard({**algos, name: algos.get(name, [])}, baselines, settings.min_sample, kind)
    stats = next(e for e in [*board.ranked, *board.unranked] if e.algorithm == name)
    return Prevalidation(
        algorithm=name,
        code=code_digest(candidate),
        prevalidated_at=now,
        days=list(replayed),
        models=dict(models),
        checks=_checks(candidate, replayed, statuses, measured, stats, settings.min_sample),
        stats=stats,
        correlations=measured,
        statuses=statuses,
        skipped_for_cpu=skipped,
        db=db,
        target_version=kind.answer_version,
        kind=kind.name,
    )


def _checks(
    candidate: Algorithm | KindAlgorithm,
    replayed: Sequence[date],
    statuses: Mapping[str, int],
    measured: Mapping[str, float],
    stats: Lifted,
    min_sample: int,
) -> list[Check]:
    return [
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
        _stats(stats, min_sample),
    ]


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


def _stats(stats: Lifted, min_sample: int) -> Check:
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
    """The algorithm retrained before each day on its own window of the days before it; an
    algorithm of a kind besides the direction (``kind``, #179) on that kind's answers."""

    def __init__(
        self,
        settings: Settings,
        algorithm: Algorithm | KindAlgorithm,
        available: Sequence[date],
        calendar: MarketCalendar,
        leaders: Leaders,
        kind: Kind | None = None,
    ) -> None:
        self._settings = settings
        self._algorithm = algorithm
        self._kind = kind
        self._available = list(available)
        self._calendar = calendar
        self._leaders = leaders
        self._loaded: dict[date, TrainingDay | None] = {}
        self._labelled: dict[date, TrainingDay | None] = {}  # the climate's days, labels only
        self._answered: dict[date, TrainingDay | None] = {}  # the kind's tables' days, answers only

    def before(self, day: date) -> Model:
        window = [d for d in self._available if d < day][-self._algorithm.training_days :]
        for gone in [d for d in self._loaded if not window or d < window[0]]:
            del self._loaded[gone]
        days = [found for d in window if (found := self._load(d)) is not None]
        try:
            if self._kind is not None:
                kind_algorithm = cast(KindAlgorithm, self._algorithm)
                answered = [s for found in days for s in found.of_kind(self._kind, kind_algorithm.features)]
                if not any(s.label is not None for s in answered):
                    return self._algorithm.default_model()
                return kind_algorithm.fit(answered)
            training = [s for found in days for s in found.samples]
            if not any(s.outcome is not None for s in training):
                return self._algorithm.default_model()
            return cast(Algorithm, self._algorithm).fit(training)
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

    def tables_before(self, day: date) -> dict[str, Model]:
        """The tables of the candidate's kind's baselines (#180), each fitted on the kind's
        answers of its latest ``days`` days before ``day``, as the night before it would; none
        for the direction's candidate."""
        if self._kind is None:
            return {}
        kind = self._kind
        tables: dict[str, Model] = {}
        for _, baseline in table_baselines([kind]):
            window = [d for d in self._available if d < day][-baseline.days :]
            for gone in [d for d in self._answered if not window or d < window[0]]:
                del self._answered[gone]
            days = [self._loaded[d] if d in self._loaded else self._answers(d) for d in window]
            tables[baseline.name] = baseline.fit(
                [s for found in days if found is not None for s in found.of_kind(kind, ())]
            )
        return tables

    def _answers(self, day: date) -> TrainingDay | None:
        if day not in self._answered:
            self._answered[day] = self._read(day, (), answered=True)
        return self._answered[day]

    def _labels(self, day: date) -> TrainingDay | None:
        if day not in self._labelled:
            self._labelled[day] = self._read(day, ())
        return self._labelled[day]

    def _load(self, day: date) -> TrainingDay | None:
        if day not in self._loaded:
            self._loaded[day] = self._read(day, self._algorithm.features)
        return self._loaded[day]

    def _read(self, day: date, features: Sequence[str], answered: bool = False) -> TrainingDay | None:
        """``day``'s samples with ``features``; with the kind's answers for the candidate's
        features, or (``answered``) for its kind's tables."""
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
            kinds=() if self._kind is None or not (features or answered) else (self._kind,),
        )
