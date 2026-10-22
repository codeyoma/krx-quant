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

A direction algorithm may offer diagnostics (``contract.Diagnostics``, #283: Neyman's flips and
its first model's probabilities): the walk-forward trains it through them (the same model), each
replayed day's samples go to them with the model that predicted the day, and the report keeps
what they measured (``diagnostics``). They are never judged, and an error in them only replaces
them in the report.

The report is saved to ``state/prevalidations/<name>.json``, passed or not, with a digest
of the algorithm's code; the engine reads it at each bar close. The server's scheduled job
runs this check itself, one waiting algorithm a night (``scheduled.py``, #204), and reads the
report (``last_report``) to know whether the code it runs now has been judged already.

With the touch target (``KRX_TARGET_VERSION=3``, the default since #170; 2 before #187) the candidate
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

A daily kind's algorithm (#264, ``daily_training``) is judged on the data its features read as
well as on its code: its report holds the versions of the data it read (``inputs``,
``daily_features.stored_versions``: a backfill not in yet keeps the old version), and ``admits``
takes it only on the versions its features have now (``DailyFeature.version``). A feature's
version is raised when what it reads changes (the flows' days before NXT opened from KRX,
#297), so the algorithm waits for a new pre-validation on the new data, held to the stricter
level of a rerun on the same days (#262).
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import inspect
import json
import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from functools import partial
from pathlib import Path
from typing import Any, cast

from krx_quant.algorithms import (
    ALGORITHMS,
    KIND_ALGORITHMS,
    Algorithm,
    Diagnostics,
    KindAlgorithm,
    Model,
    NothingToLearn,
    Sample,
    kind_of,
    load_models,
    register,
    register_kind_algorithms,
)
from krx_quant.baselines import baselines_for
from krx_quant.climate import CLIMATE_DAYS, fit_climate
from krx_quant.config import Settings
from krx_quant.daily import DailyAlgorithm
from krx_quant.daily_features import input_versions
from krx_quant.features import Leaders
from krx_quant.feeds.base import KST
from krx_quant.kind_registry import KINDS, daily_kind_named, kind_named
from krx_quant.kind_stats import KindEntry, kind_scoring, scored_of_kind
from krx_quant.kind_stats import entry as kind_entry
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
    entry,
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
# The two-sided level a rerun's weakest lift by trading day is held to, over every run on the
# same days (#262): the N-th run's bound is at 1 − 0.05/N (Bonferroni).
FAMILY_LEVEL = 0.95


def tried_level(tries: int) -> float:
    """The two-sided level of the lift-by-day interval the ``tries``-th pre-validation on the
    same days is held to (#262): Bonferroni over the runs, 1 − 0.05/``tries``."""
    return 1 - (1 - FAMILY_LEVEL) / tries


def report_path(state_dir: Path, name: str) -> Path:
    return state_dir / "prevalidations" / f"{name}.json"


def history_path(state_dir: Path, name: str) -> Path:
    """Every pre-validation of ``name``, a JSON line each, oldest first (#262): the report keeps
    the last one only."""
    return state_dir / "prevalidations" / f"{name}.history.jsonl"


def earlier_runs(state_dir: Path, name: str, days: Sequence[date]) -> tuple[int, int]:
    """How many pre-validations of ``name`` ran before, and how many of them replayed any of
    ``days`` (#262): a day replayed again after a fix is no longer out of sample. Lines that
    cannot be read are skipped."""
    path = history_path(state_dir, name)
    if not path.exists():
        return 0, 0
    asked = {d.isoformat() for d in days}
    runs = same = 0
    for line in path.read_text().splitlines():
        try:
            replayed = set(json.loads(line)["days"])
        except (ValueError, KeyError, TypeError):
            continue
        runs += 1
        same += bool(replayed & asked)
    return runs, same


def seed_history(state_dir: Path, name: str) -> None:
    """Start ``name``'s run history from its report when the history does not exist yet: a
    report from before the history (#262) is a run too. A report that cannot be read starts
    none."""
    history, report = history_path(state_dir, name), report_path(state_dir, name)
    if history.exists() or not report.exists():
        return
    try:
        data = json.loads(report.read_text())
        line = {key: data[key] for key in ("prevalidated_at", "code", "passed", "days")}
        line["target_version"] = data.get("target_version")
    except (OSError, ValueError, KeyError, TypeError):
        return
    tmp = history.with_name(f".{history.name}.tmp")
    tmp.write_text(json.dumps(line) + "\n")
    tmp.replace(history)  # whole or not at all: a cut-short seed would never be seeded again


def _record_run(state_dir: Path, result: Prevalidation) -> None:
    path = history_path(state_dir, result.algorithm)
    line = {
        "prevalidated_at": result.prevalidated_at.isoformat(),
        "code": result.code,
        "target_version": result.target_version,
        "passed": result.passed,
        "days": [d.isoformat() for d in result.days],
        # Its stats (#262): what a best-of-the-runs correction would need.
        "weakest_lift": result.stats.weakest_lift,
        "day_low": result.stats.day_low,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as f:
        f.seek(0, 2)
        if f.tell():  # a last line cut short (no newline) must not swallow this one
            f.seek(f.tell() - 1)
            if f.read(1) != "\n":
                f.write("\n")
        f.write(json.dumps(line) + "\n")


def code_digest(algorithm: Algorithm | KindAlgorithm | DailyAlgorithm) -> str:
    """A digest of the algorithm's own code: its module, or every module of its package; then, for
    one that runs another algorithm's code (``code_from``: those algorithms' classes, #310:
    ``cox-5d`` fits as ``cox`` does), that code too, so a change to it asks for a new verdict."""
    digest = hashlib.sha256()
    _digest_code(digest, type(algorithm), algorithm.name)
    for shared in getattr(type(algorithm), "code_from", ()):
        digest.update(b"\0code_from\0")
        _digest_code(digest, shared, algorithm.name)
    return digest.hexdigest()[:16]


def _digest_code(digest: Any, cls: type, name: str) -> None:
    module = inspect.getmodule(cls)
    if module is None or module.__file__ is None:
        raise ValueError(f"{name}: no source file to check")
    path = Path(module.__file__)
    root = path.parent if path.name == "__init__.py" else None
    files = sorted(root.rglob("*.py")) if root is not None else [path]
    for f in files:
        digest.update(f.name.encode() if root is None else f.relative_to(root).as_posix().encode())
        digest.update(f.read_bytes())


def admits(
    state_dir: Path, target_version: int = 1, kinds: Sequence[Kind] = KINDS
) -> Callable[[Algorithm | KindAlgorithm | DailyAlgorithm], bool]:
    """Whether the engine may start a new algorithm: registered before pre-validation, or
    its latest report passed with the code it runs now, on the target it runs now (#169; a
    report without one was on the four-minute move, 1). An algorithm of a kind besides the
    direction (#179) passed on its kind's answer version (one of ``kinds``, or a daily kind's,
    #264)."""

    def admitted(algorithm: Algorithm | KindAlgorithm | DailyAlgorithm) -> bool:
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
    algorithm: Algorithm | KindAlgorithm | DailyAlgorithm,
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
    algorithm: Algorithm | KindAlgorithm | DailyAlgorithm,
    target_version: int,
    kinds: Sequence[Kind],
) -> str | None:
    """How ``report`` is not of the code, target and inputs the algorithm runs now; None if
    it is."""
    if report.get("code") != code_digest(algorithm):
        return "with other code"
    if report.get("target_version", 1) != _target(algorithm, target_version, kinds):
        return "on another target"
    if _raised(report.get("inputs")) != _raised(inputs_of(algorithm)):
        return "on other inputs"
    return None


def inputs_of(algorithm: Algorithm | KindAlgorithm | DailyAlgorithm) -> dict[str, int]:
    """The versions a daily kind's algorithm's inputs have now (#264, ``daily_features``),
    feature by feature, which its report must have been on; none for the other kinds' (their
    features have no version)."""
    if daily_kind_named(str(getattr(algorithm, "kind", DIRECTION))) is None:
        return {}
    return input_versions(algorithm.features)


def _raised(inputs: Any) -> dict[str, int]:
    """The inputs whose version is not 1: a report without ``inputs`` (before #297) was on
    version 1 of each."""
    if not isinstance(inputs, Mapping):
        return {}
    return {str(name): int(v) for name, v in inputs.items() if isinstance(v, int) and v != 1}


def _target(
    algorithm: Algorithm | KindAlgorithm | DailyAlgorithm, target_version: int, kinds: Sequence[Kind]
) -> int | None:
    """The target an algorithm's report must have passed on: the engine's, or its kind's answer
    version (#179; a daily kind's, #264); None for a kind not registered (it never passes)."""
    name = str(getattr(algorithm, "kind", DIRECTION))
    if name == DIRECTION:
        return target_version
    kind = kind_named(name, kinds) or daily_kind_named(name)
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
    db: Path | None  # the replay's database; None for a daily kind's walk-forward (#264)
    target_version: int = 1  # the target replayed and scored on (#169); a kind's answer version
    kind: str = DIRECTION  # the kind it predicts (#179)
    earlier_runs: int = 0  # pre-validations of the algorithm before this one (#262)
    earlier_on_these_days: int = 0  # of them, those that replayed any of these days
    # A daily kind's walk-forward (#264, ``daily_training``): its folds, by symbol and by year.
    walk_forward: dict[str, Any] | None = None
    # The versions of the data a daily kind's algorithm read (``daily_features.stored_versions``,
    # #297); None for the others.
    inputs: dict[str, int] | None = None
    # What the candidate measured besides the checks (#283, ``contract.Diagnostics``), as the
    # report keeps it and as the command prints it; None for one that offers none. Never judged.
    diagnostics: dict[str, Any] | None = None
    diagnostic_lines: tuple[str, ...] = ()

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
            "db": None if self.db is None else str(self.db),
            "target_version": self.target_version,
            "earlier_runs": self.earlier_runs,
            "earlier_runs_on_these_days": self.earlier_on_these_days,
        }
        if self.kind != DIRECTION:
            out["kind"] = self.kind
        if self.walk_forward is not None:
            out["walk_forward"] = self.walk_forward
        if self.inputs is not None:
            out["inputs"] = self.inputs
        if self.diagnostics is not None:
            out["diagnostics"] = self.diagnostics
        return out


class PrevalidationRunning(Exception):
    """A pre-validation of the same algorithm runs already: it holds the algorithm's lock."""


@contextlib.contextmanager
def _held(state_dir: Path, name: str) -> Iterator[None]:
    """Hold ``name``'s pre-validation lock (``prevalidation-<name>.lock``, ``flock``) while it
    runs: a second one beside it (by hand while the scheduled job runs, #204) would delete its
    database and write the same report. The kernel lets it go when the process ends."""
    path = state_dir / f"prevalidation-{name}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PrevalidationRunning(
                f"a pre-validation of {name} runs already ({path.name} is held): wait for it to end"
            ) from None
        yield


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
    that fails the registration check), FileNotFoundError when no day was recorded,
    PrevalidationRunning while another pre-validation of ``name`` runs.
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
    with _held(settings.state_dir, name):
        now = now or datetime.now(KST)
        available = finished_days(settings.data_dir, settings.symbols, now)
        replayed = [d for d in available if is_recorded(settings.data_dir, d, settings.symbols)][-days:]
        if not replayed:
            symbols = ", ".join(settings.symbols)
            raise FileNotFoundError(f"no recorded day of {symbols} in {settings.data_dir}")
        db = settings.state_dir / f"prevalidation-{name}.sqlite3"
        for leftover in sqlite_files(db):
            leftover.unlink(missing_ok=True)
        # The direction and the candidate's own kind only (#263): the checks read nothing of
        # the other kinds, and each one would make every replayed day longer (the barrier's
        # horizon ladder alone doubles the kinds' writes). The direction's algorithms all run,
        # as their CPU comes first in the budget.
        own = [k for k in kinds if k.name == DIRECTION or (kind is not None and k.name == kind.name)]
        own_algorithms = [a for a in kind_algorithms if kind is not None and a.kind == kind.name]
        measured = _measured(candidate) if kind is None else None  # a direction algorithm's (#283)
        models = await _replay(
            settings, candidate, kind, algorithms, own_algorithms, own, available, replayed, db, measured
        )
        seed_history(settings.state_dir, name)
        before, on_days = earlier_runs(settings.state_dir, name, replayed)
        tries = on_days + 1  # this run among those on any of these days: the bar it is held to
        if kind is None:
            others = [a.name for a in algorithms if a is not candidate]
            result = _judge(settings, candidate, others, replayed, models, db, now, tries)
        else:
            same = [a.name for a in kind_algorithms if a.kind == kind.name and a is not candidate]
            result = _judge_kind(settings, candidate, kind, same, replayed, models, db, now, tries)
        result = replace(result, earlier_runs=before, earlier_on_these_days=on_days)
        if measured is not None:
            diagnostics, lines = measured.result()
            result = replace(result, diagnostics=diagnostics, diagnostic_lines=lines)
        path = report_path(settings.state_dir, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
        # The run is recorded before its report replaces the last one: a stop between the two
        # counts it once more, never once less (a rerun on the same days must not look fresh).
        _record_run(settings.state_dir, result)
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
    measured: _Measured | None = None,
) -> dict[date, str]:
    """Replay each day into ``db``, the candidate trained on the days before it (on its kind's
    answers, #179); the version it predicted each day with. With ``measured`` (#283), the
    candidate is trained by its diagnostics' fit, and they are given each replayed day's samples
    and the model that predicted it."""
    calendar = calendar_store(settings).calendar
    leaders = Leaders.of(settings)
    everyone: list[Algorithm | KindAlgorithm] = [*algorithms, *kind_algorithms]
    running = load_models(settings.state_dir, [a for a in everyone if a is not candidate])
    fit = None if measured is None else measured.fit
    trainer = _WalkForward(settings, candidate, available, calendar, leaders, kind, fit)
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
        if measured is not None:
            # The day's samples are the next day's training too: read once (the last day's alone
            # is read for this).
            measured.day(day, model, partial(trainer.samples_on, day))
    return versions


def _measured(candidate: Algorithm | KindAlgorithm) -> _Measured | None:
    """The direction candidate's diagnostics (#283), if it offers them (``diagnostics()``)."""
    offered = getattr(candidate, "diagnostics", None)
    if not callable(offered):
        return None
    return _Measured(cast(Algorithm, candidate), cast(Callable[[], Diagnostics], offered))


class _Measured:
    """A candidate's diagnostics (#283), kept from changing the run: an error in them is logged
    and reported in their place, and the algorithm's own fit trains it from then on. They never
    reach the checks."""

    def __init__(self, algorithm: Algorithm, offered: Callable[[], Diagnostics]) -> None:
        self._algorithm = algorithm
        self.error: str | None = None
        try:
            self._diagnostics = offered()
        except Exception as e:
            self._failed("start", e)

    def fit(self, samples: Sequence[Sample]) -> Model:
        """The diagnostics' fit, which gives the algorithm's model; the algorithm's own once they
        failed (an error of the algorithm's fit then comes from it again, and fails the run as
        it would)."""
        if self.error is None:
            try:
                return self._diagnostics.fit(samples)
            except NothingToLearn:
                raise  # the algorithm's: the day is predicted by its defaults
            except Exception as e:
                self._failed("fit", e)
        return self._algorithm.fit(samples)

    def day(self, day: date, model: Model, samples: Callable[[], Sequence[Sample]]) -> None:
        """``day``'s samples (read here, as a failure to read them is the diagnostics' too) and
        the model that predicted it, to the diagnostics."""
        if self.error is None:
            try:
                self._diagnostics.day(day, model, samples())
            except Exception as e:
                self._failed(f"day {day}", e)

    def result(self) -> tuple[dict[str, Any], tuple[str, ...]]:
        """The report's ``diagnostics`` and the command's lines; the error in their place."""
        if self.error is None:
            try:
                return self._diagnostics.report(), tuple(self._diagnostics.lines())
            except Exception as e:
                self._failed("report", e)
        name = self._algorithm.name
        return {"error": self.error}, (f"{name} diagnostics: not measured ({self.error})",)

    def _failed(self, where: str, e: Exception) -> None:
        self.error = f"{where}: {type(e).__name__}: {e}"
        log.exception(
            "%s's diagnostics failed (%s); the pre-validation goes on without them",
            self._algorithm.name,
            where,
        )


def _judge(
    settings: Settings,
    candidate: Algorithm | KindAlgorithm,
    others: Sequence[str],
    replayed: Sequence[date],
    models: Mapping[date, str],
    db: Path,
    now: datetime,
    tries: int = 1,
) -> Prevalidation:
    """The candidate's report, from what the replay stored in ``db``; the ``tries``-th run on
    these days (#262)."""
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
    # A rerun on these days (#262): its lifts by trading day again, at the Bonferroni level.
    strict = (
        None
        if tries < 2
        else entry(name, algos.get(name, []), baselines, settings.fee_rate, tried_level(tries)).day_low
    )
    return Prevalidation(
        algorithm=name,
        code=code_digest(candidate),
        prevalidated_at=now,
        days=list(replayed),
        models=dict(models),
        checks=_checks(candidate, replayed, statuses, measured, stats, settings.min_sample, tries, strict),
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
    tries: int = 1,
) -> Prevalidation:
    """A kind's candidate's report (#179): scored on the kind's answers, against its baselines,
    correlated with the other algorithms of its kind only; the ``tries``-th run on these days."""
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
    strict = (
        None
        if tries < 2
        else kind_entry(name, algos.get(name, []), baselines, kind, tried_level(tries)).day_low
    )
    return Prevalidation(
        algorithm=name,
        code=code_digest(candidate),
        prevalidated_at=now,
        days=list(replayed),
        models=dict(models),
        checks=_checks(candidate, replayed, statuses, measured, stats, settings.min_sample, tries, strict),
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
    tries: int = 1,
    strict_low: float | None = None,
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
        _stats(stats, min_sample, tries, strict_low),
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


def _stats(stats: Lifted, min_sample: int, tries: int = 1, strict_low: float | None = None) -> Check:
    """Enough scored predictions and pairs, and the weakest lift above 0. The ``tries``-th run on
    the same days (#262), from the second, also needs ``strict_low`` above 0: the weakest lift
    by trading day's lower bound at ``tried_level(tries)``, as a fix tried on days already seen
    could pass by luck."""
    weakest = stats.weakest_lift
    if not enough(stats, min_sample) or weakest is None:
        pairs = min((t.lift.n for t in stats.lifts.values()), default=0)
        return Check(
            "stats",
            False,
            f"too few to judge: {stats.summary.n} scored, {pairs} pairs (at least {min_sample})",
        )
    detail = f"weakest lift {weakest:.4f} over {stats.summary.n} scored predictions"
    if tries < 2:
        return Check("stats", weakest > 0, detail)
    detail += f"; run {tries} on these days, held to {tried_level(tries):.2%} (Bonferroni): "
    if strict_low is None:
        return Check("stats", False, detail + "no lift by trading day (fewer than two days paired)")
    return Check(
        "stats",
        weakest > 0 and strict_low > 0,
        detail + f"weakest lower bound by trading day {strict_low:.4f} (above 0 to pass)",
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
        fit: Callable[[Sequence[Sample]], Model] | None = None,
    ) -> None:
        self._settings = settings
        self._algorithm = algorithm
        self._kind = kind
        self._fit = fit  # in place of the direction algorithm's (its diagnostics', #283)
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
            # As the nightly training does (``nightly._samples``): each sample names its day's
            # reference price, recorded ("mid") or backfilled ("close"), which Neyman's folds
            # and a model's days by source read (#283).
            training = [s for found in days for s in found.of(self._algorithm.features)]
            if not any(s.outcome is not None for s in training):
                return self._algorithm.default_model()
            return (self._fit or cast(Algorithm, self._algorithm).fit)(training)
        except NothingToLearn as e:  # no book on backfilled days, say; any other error fails the check
            log.warning("%s not trained before %s, its defaults predict: %s", self._algorithm.name, day, e)
            return self._algorithm.default_model()

    def samples_on(self, day: date) -> list[Sample]:
        """``day``'s samples with the algorithm's features (a direction algorithm's), each naming
        the day's reference price, as the nightly training reads them; none if the day has none."""
        found = self._load(day)
        return [] if found is None else found.of(self._algorithm.features)

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
