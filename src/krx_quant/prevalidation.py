"""Pre-validation: a new algorithm replayed on the latest recorded trading days before it
runs live (``krx-quant prevalidate <name>``).

The engine enrolls an algorithm registered after this check existed only once it has passed with the
code the engine runs (``admits``); until then it predicts in shadow alone (#696,
``lifecycle.SHADOW``), never reviewed, nor in the consensus, the signals or the verdicts. The
algorithms registered before (``REGISTERED_BEFORE``) and any algorithm the engine has already
enrolled run as before.

The check replays the last ``PREVALIDATION_DAYS`` recorded trading days, one at a time,
with every registered algorithm, into a database of its own
(``state/prevalidation-<name>.sqlite3``): no prediction reaches the official record (the status
its promotion test gives does, below). The
algorithm is retrained before each day on its own window of the days before it
(walk-forward), never on the day it predicts; the others run the models the server runs. One
the engine does not run because its latest report refused the code, target and inputs it has
(``left_out``, #544) is not replayed beside it: the live engine runs it in shadow alone (#696), after
the verdicts and the first the CPU budget skips, so no check reads it. The candidate itself always
runs: a run again by hand judges it anew.
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

**Promotion** (#681). The same walk-forward replay is the test that makes a new bar-close algorithm
active, in place of a probation waited for on live days (user decision 2026-11-10: judged on the
past, not forward). Once the five checks pass, the candidate is judged as a probation review judges
(``lifecycle``: its weakest lift over its kind's baselines by trading day, only the predictions of
models trained on the target, with at least ``min_sample`` scored predictions and pairs), on the
replayed days its model was trained for (``DayTrained``: a day its defaults predicted, with nothing
to learn before it, is skipped, not scored), and passes when that lift's lower bound is above 0 at
``promotion_level``: the reviews' level (``KRX_REVIEW_CONFIDENCE``, 98.3%) shared by Bonferroni among
the tests a family of days may hold (``KRX_PROMOTION_BUDGET``, 10: 99.83%). Every test is kept
(``state/prevalidations/promotion-attempts.jsonl``) with the days it judged; those before on any of
the same days, of any algorithm, a revised version of the same one too, are its family
(``family``): with ``budget`` of them already, it is not tested. The outcome (``Promotion``, the
report's ``promotion``, its ``promotion`` check):

- passed: **active** at once, in the engine's database (``_enroll``): its replay of the days judged
  kept as its probation's (``simulation.keep_replayed``), as a simulated probation's review keeps it
  (#298), so the active check of its first ten days reads them (#545); the engine runs it from its
  next bar close;
- failed: **refused** (the ``promotion`` check fails): it waits for new code, as any refusal;
- not tested (fewer than ``MIN_DAYS`` days scored, the days' budget spent, too few scored to decide):
  on a probation **judged forward** on live days (``lifecycle.FORWARD``), never simulated.

An algorithm on a probation begun when its pre-validation passed before this (``REGISTERED``) is
judged by it again (the scheduled job, ``scheduled/prevalidations.py``): passed, it is made active; not,
it stays on probation. Those the engine runs with any other status, a revision's run and those
registered before pre-validation are not judged by it.

A direction algorithm may offer diagnostics (``contract.Diagnostics``, #283: Neyman's flips and
its first model's probabilities): the walk-forward trains it through them (the same model), each
replayed day's samples go to them with the model that predicted the day, and the report keeps
what they measured (``diagnostics``). They are never judged, and an error in them only replaces
them in the report.

Each replayed day logs the process's memory, at its start and after its training and its replay
with their peaks (#383, ``footprint.DayUsage``), and is let go once replayed: its engine and events
are dropped before the next day is trained, and the memory they freed is handed back to the system
(``footprint.give_back``). Without, the process grew a replayed day after another until the kernel
killed it (2026-10-27). A replayed day's end (the engine's status review, which judges the days
replayed before it too, and the consensus refit) reads only the fields it judges by of each scored
prediction, the refit its members' only (#487, ``PredictionStore.reviewed``): reading whole rows,
each replayed day's peak grew by about 60 MB a day replayed before it (2026-11-03). And it reads
each day once (#546, ``lifecycle._Read``): the clone check a day at a time, keeping each predictor's
leans in place of its rows, the stats the judged algorithm's and the baselines' rows only. Each set
of days the review asked for (the days each status counts, the latest days) had kept every
predictor's rows, and the peak still grew by about 20 MB a day replayed before it (made-up days
shaped like the server's; 18 days of breiman-30m on the server reached 993 MB, 2026-11-04). The
verdict reads the fields it judges by too (``_judge``: every predictor's whole rows of every replayed
day made the run's end its peak, 1,062 MB on 14 made-up days, 442 since), and its memory is logged
as one more line.

A running bar-close algorithm whose code changed (#454, ``revisions.py``) is judged again by the
same check on the recorded days after the change only (``prevalidate --revision``, ``since``: the
first whole trading day of its revision): its report holds that revision (``revision``: its
boundary and first day), and the engine reads its verdict for that revision only
(``revision_report``). Those registered before pre-validation are judged so too: their exemption
holds for the code they first ran under the engine's revisions.

The report is saved to ``state/prevalidations/<name>.json``, passed or not, with a digest
of the algorithm's code; the engine reads it at each bar close. The server's scheduled job
runs this check itself, one waiting algorithm a night (``scheduled/prevalidations.py``, #204), and reads the
report (``last_report``) to know whether the code it runs now has been judged already. An
algorithm whose report refused the code and target it runs now is refused, not waiting
(``refusals``): the engine tells it once, until its code or target changes.

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
from dataclasses import dataclass, field, replace
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
    KindSample,
    Model,
    NothingToLearn,
    Sample,
    kind_of,
    load_models,
    register,
    register_kind_algorithms,
)
from krx_quant.baselines import TOUCH_BASELINES
from krx_quant.climate import CLIMATE_DAYS, fit_climate
from krx_quant.config import Settings
from krx_quant.daily import DailyAlgorithm
from krx_quant.daily_features import input_versions
from krx_quant.features import Leaders
from krx_quant.feeds.base import KST
from krx_quant.footprint import DayUsage, Meter, give_back
from krx_quant.kind_registry import KINDS, daily_kind_named, kind_named
from krx_quant.kind_stats import KindEntry, kind_scoring, scored_of_kind
from krx_quant.kind_stats import entry as kind_entry
from krx_quant.kind_stats import entry_json as kind_entry_json
from krx_quant.kind_stats import leaderboard as kind_leaderboard
from krx_quant.kind_stats import split as kind_split
from krx_quant.kinds import DIRECTION, Kind, table_baselines
from krx_quant.lifecycle import (
    CLONE_CORRELATION,
    DIRECTION_SCORING,
    FORWARD,
    REGISTERED,
    REPLAYED,
    Standing,
    Status,
    _weakest_by_day,
    correlations,
    most_correlated,
)
from krx_quant.market_calendar import MarketCalendar, calendar_store
from krx_quant.predictions import DB_NAME, PredictionStore, read_statuses
from krx_quant.replay import recorded_events, replay_recorded, sqlite_files
from krx_quant.sample_store import STORE_DIR, NightReads, SampleStore, night_reads, stored_night_day
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
from krx_quant.touch import TARGET_VERSION
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
    _append_line(path, line)


def code_digest(algorithm: Algorithm | KindAlgorithm | DailyAlgorithm) -> str:
    """A digest of the algorithm's own code: its module, or every module of its package; then, for
    one that runs code from elsewhere (``code_from``: a class of each, whose module or package is
    digested: #310, ``cox-5d`` fits as ``cox`` does; #429, shannon's climate prior is
    ``algorithms/priors.py``'s), that code too, so a change to it asks for a new verdict."""
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
    state_dir: Path, target_version: int = TARGET_VERSION, kinds: Sequence[Kind] = KINDS
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
    # What its walk-forward replay's promotion test made of it (#681, ``Promotion.outcome``); None in
    # a report without one (before #681, or of a run that judges no promotion).
    promotion: str | None = None

    @property
    def judged(self) -> bool:
        """It judged the code the algorithm runs now: passed, or refused on at least
        ``MIN_DAYS`` days (on fewer it was a look, which never passes)."""
        return self.current and (self.passed or self.days >= MIN_DAYS)


def last_report(
    state_dir: Path,
    algorithm: Algorithm | KindAlgorithm | DailyAlgorithm,
    target_version: int = TARGET_VERSION,
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
    promotion = report.get("promotion")
    outcome = promotion.get("outcome") if isinstance(promotion, Mapping) else None
    return LastReport(
        made_at=made_at,
        current=_stale(report, algorithm, target_version, kinds) is None,
        passed=report.get("passed") is True,
        days=len(days) if isinstance(days, list) else 0,
        promotion=outcome if isinstance(outcome, str) else None,
    )


@dataclass(frozen=True)
class Refusal:
    """An algorithm's latest report refused the code, target and inputs it runs now."""

    # The report that refused: its time, and the code digest, target and inputs it judged, as one
    # key (told once each: a run again by hand is told too).
    judged: str
    why: str  # the checks it failed, a line
    checks: tuple[str, ...] = ()  # the names of the checks it failed, in the report's order (#561)


def refusals(
    state_dir: Path, target_version: int = TARGET_VERSION, kinds: Sequence[Kind] = KINDS
) -> Callable[[Algorithm | KindAlgorithm | DailyAlgorithm], Refusal | None]:
    """The refusal of an algorithm whose latest report judged the code and target it runs now
    (``LastReport.judged``) and did not pass; None for any other: no report, one of other code,
    target or inputs (a new run is due), a pass, or a look on fewer than ``MIN_DAYS`` days. The
    engine tells a refusal once, not as waiting (#310)."""

    def refusal(algorithm: Algorithm | KindAlgorithm | DailyAlgorithm) -> Refusal | None:
        if algorithm.name in REGISTERED_BEFORE:
            return None
        return _refusal(state_dir, algorithm, target_version, kinds)

    return refusal


def left_out(
    state_dir: Path,
    statuses: Mapping[str, Standing],
    target_version: int = TARGET_VERSION,
    kinds: Sequence[Kind] = KINDS,
) -> Callable[[Algorithm | KindAlgorithm | DailyAlgorithm], Refusal | None]:
    """The refusal that leaves an algorithm out of the replays beside a pre-validation's candidate
    (#544; the night trains it since #696, as the engine runs it in shadow): its latest report
    judged the code, target and inputs it has now and refused them (``LastReport.judged``), and the
    engine does not enroll it (by ``statuses``, the engine's): not enrolled, retired (a refused
    revision retires it, #454; so one registered before pre-validation is left out too), or a daily
    kind's on probation, which runs only while ``admits`` lets it (#264). None for any other: one
    the engine runs whatever its report says (an enrolled bar-close one, a report run again by hand
    refused) replays as before. A change of its code, target or inputs makes the report stale: it
    waits for a new pre-validation."""

    def refusal(algorithm: Algorithm | KindAlgorithm | DailyAlgorithm) -> Refusal | None:
        standing = statuses.get(algorithm.name)
        if standing is None:
            if algorithm.name in REGISTERED_BEFORE:
                return None  # it runs without a report
        elif standing.status is not Status.RETIRED and not (
            standing.status is Status.PROBATION and _daily(algorithm)
        ):
            return None
        return _refusal(state_dir, algorithm, target_version, kinds)

    return refusal


def _refusal(
    state_dir: Path,
    algorithm: Algorithm | KindAlgorithm | DailyAlgorithm,
    target_version: int,
    kinds: Sequence[Kind],
) -> Refusal | None:
    """The refusal of ``algorithm``'s latest report, if it judged the code, target and inputs it has
    now and did not pass."""
    last = last_report(state_dir, algorithm, target_version, kinds)
    if last is None or not last.judged or last.passed:
        return None
    report = _read_report(state_dir, algorithm.name) or {}
    judged = {
        "prevalidated_at": report.get("prevalidated_at"),
        **judging(algorithm, target_version, kinds),
    }
    return Refusal(json.dumps(judged, sort_keys=True), failed_checks(report), failed_names(report))


def _daily(algorithm: Algorithm | KindAlgorithm | DailyAlgorithm) -> bool:
    return daily_kind_named(str(getattr(algorithm, "kind", DIRECTION))) is not None


def judging(
    algorithm: Algorithm | KindAlgorithm | DailyAlgorithm,
    target_version: int = TARGET_VERSION,
    kinds: Sequence[Kind] = KINDS,
) -> dict[str, Any]:
    """What a pre-validation of the algorithm judges now, which its report must match (``_stale``):
    the digest of its code, its target, and the versions of its inputs not 1. The engine tells a wait
    for it once for each (#508)."""
    return {
        "code": code_digest(algorithm),
        "target_version": _target(algorithm, target_version, kinds),
        "inputs": _raised(inputs_of(algorithm)),
    }


def failed_checks(report: Mapping[str, Any]) -> str:
    """The checks a report failed, as ``check: detail`` joined by ``; ``."""
    failed = [f"{c.get('check')}: {c.get('detail')}" for c in _failed(report)]
    return "; ".join(failed) or "no failed check recorded"


def failed_names(report: Mapping[str, Any]) -> tuple[str, ...]:
    """The names of the checks a report failed, in its order (#561: ``GET /v1/algorithms``)."""
    return tuple(str(c.get("check")) for c in _failed(report))


def _failed(report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    checks = report.get("checks")
    return [
        c
        for c in (checks if isinstance(checks, list) else [])
        if isinstance(c, Mapping) and c.get("passed") is not True
    ]


def revision_report(
    state_dir: Path,
    algorithm: Algorithm | KindAlgorithm | DailyAlgorithm,
    boundary: datetime,
    target_version: int = TARGET_VERSION,
    kinds: Sequence[Kind] = KINDS,
) -> tuple[LastReport, str] | None:
    """The algorithm's latest report if it was made for its revision begun at ``boundary`` (#454,
    ``prevalidate --revision``: its ``revision``), as it stands for the code and target it runs now
    (``last_report``), with the checks it failed; None for any other (none, a first pre-validation,
    another revision's)."""
    report = _read_report(state_dir, algorithm.name)
    revision = None if report is None else report.get("revision")
    if not isinstance(revision, Mapping) or revision.get("boundary") != boundary.astimezone(KST).isoformat():
        return None
    last = last_report(state_dir, algorithm, target_version, kinds)
    return None if last is None else (last, failed_checks(report or {}))


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
    if not _daily(algorithm):
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
    # The revision it judged (#454, ``prevalidate --revision``): its boundary and first day; None
    # for a first pre-validation.
    revision: dict[str, str] | None = None
    # Its walk-forward replay's promotion test (#681); None for a run that judges none (a revision's,
    # one registered before pre-validation, one the engine runs with another status).
    promotion: Promotion | None = None

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
        if self.revision is not None:
            out["revision"] = self.revision
        if self.promotion is not None:
            out["promotion"] = self.promotion.as_dict()
        return out


# The promotion test (#681): see the module doc, "Promotion".
ACTIVE, REFUSED, JUDGED_FORWARD = "active", "refused", "forward"  # ``Promotion.outcome``


def promotion_level(confidence: float, budget: int) -> float:
    """The two-sided level of the lift-by-day interval a promotion test is held to (#681): the
    probation reviews' ``confidence`` shared among the ``budget`` tests a family of days may hold
    (Bonferroni)."""
    return 1 - (1 - confidence) / budget


def attempts_path(state_dir: Path) -> Path:
    """Every promotion test, a JSON line each, oldest first (#681): the days' budget is counted on it."""
    return state_dir / "prevalidations" / "promotion-attempts.jsonl"


def family(state_dir: Path, days: Sequence[date]) -> list[dict[str, Any]]:
    """The promotion tests before that judged any of ``days`` (#681), oldest first: each one's
    algorithm, code, time, outcome and how many of ``days`` it judged. Lines that cannot be read are
    skipped."""
    path = attempts_path(state_dir)
    if not path.exists():
        return []
    asked = {d.isoformat() for d in days}
    out = []
    for line in path.read_text().splitlines():
        try:
            data = json.loads(line)
            shared = len(set(data["days"]) & asked)
            judged = {key: data[key] for key in ("algorithm", "code", "at", "outcome")}
        except (ValueError, KeyError, TypeError):
            continue
        if shared:
            out.append({**judged, "shared_days": shared})
    return out


def budget_lock_path(state_dir: Path) -> Path:
    """The promotion budget's lock (#681, #684 review): held while a test reads its family, is judged
    and is recorded."""
    return state_dir / "prevalidations" / "promotion-attempts.lock"


@contextlib.contextmanager
def _budget_held(state_dir: Path) -> Iterator[None]:
    """Hold the promotion budget's lock (``flock``, waiting for another holder): every algorithm's
    pre-validation shares the budget, while ``_held`` locks one algorithm's only. The kernel lets it
    go when the process ends."""
    path = budget_lock_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def _record_attempt(state_dir: Path, result: Prevalidation) -> None:
    promotion = result.promotion
    assert promotion is not None
    line = {
        "at": result.prevalidated_at.isoformat(),
        "algorithm": result.algorithm,
        "code": result.code,
        "kind": result.kind,
        "days": [d.isoformat() for d in promotion.judged_days],
        "level": promotion.level,
        "outcome": promotion.outcome,
    }
    _append_line(attempts_path(state_dir), line)


def _append_line(path: Path, line: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as f:
        f.seek(0, 2)
        if f.tell():  # a last line cut short (no newline) must not swallow this one
            f.seek(f.tell() - 1)
            if f.read(1) != "\n":
                f.write("\n")
        f.write(json.dumps(line) + "\n")


@dataclass(frozen=True)
class Promotion:
    """A pre-validation's promotion test (#681): what it made of the candidate (``outcome``: ACTIVE,
    REFUSED, or JUDGED_FORWARD, on a probation judged on live days), why, and what it was judged on."""

    algorithm: str
    code: str  # the digest of the code it judged
    outcome: str
    detail: str
    tested: bool  # judged, against its days' budget; not: it could not be (too few days, the budget spent)
    level: float  # the two-sided level its lift by trading day is held to (``promotion_level``)
    budget: int  # the tests its days' family may hold (``Settings.promotion_budget``)
    days: list[dict[str, Any]]  # each replayed day's training (``DayTrained.as_dict``)
    family: list[dict[str, Any]]  # the tests before on any of the days it judged (``family``)
    on_probation: bool  # on a probation begun before #681, judged again: a fail leaves it there
    judged_days: list[date] = field(default_factory=list)  # the days scored: its model was trained
    scored: int = 0  # its scored predictions on them
    enough: bool = False  # enough of them, and pairs, to decide on (``stats.enough``)
    weakest: dict[str, Any] | None = None  # its weakest lift by trading day, and which baseline's

    @property
    def earlier_versions(self) -> list[dict[str, Any]]:
        """Of ``family``, the tests of the same algorithm with other code: a revised algorithm judged
        again on days its earlier version was judged on (they count against the budget too)."""
        return [d for d in self.family if d.get("algorithm") == self.algorithm and d.get("code") != self.code]

    def check(self) -> Check:
        """The report's ``promotion`` check: failed only when it refused the candidate."""
        return Check("promotion", self.outcome != REFUSED, self.detail)

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "detail": self.detail,
            "tested": self.tested,
            "level": self.level,
            "budget": self.budget,
            "attempt": len(self.family) + 1 if self.tested else None,
            "family": self.family,
            "earlier_versions": self.earlier_versions,
            "on_probation": self.on_probation,
            "judged_days": [d.isoformat() for d in self.judged_days],
            "scored": self.scored,
            "enough": self.enough,
            "weakest": self.weakest,
            "days": self.days,
        }


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
    revision: datetime | None = None,
    since: date | None = None,
) -> Prevalidation:
    """Replay ``name`` on the latest ``days`` recorded trading days and save its report; for a
    revision of it (#454: ``revision``, when it began, and ``since``, its first whole trading
    day), only on those from ``since``, the report bound to it.

    Raises ValueError for an algorithm not registered (RegistrationError for a registry
    that fails the registration check), FileNotFoundError when no day was recorded (from
    ``since``), PrevalidationRunning while another pre-validation of ``name`` runs.
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
        replayed = [
            d
            for d in available
            if (since is None or d >= since) and is_recorded(settings.data_dir, d, settings.symbols)
        ][-days:]
        if not replayed:
            symbols = ", ".join(settings.symbols)
            after = "" if since is None else f" from {since}"
            raise FileNotFoundError(f"no recorded day of {symbols}{after} in {settings.data_dir}")
        db = settings.state_dir / f"prevalidation-{name}.sqlite3"
        for leftover in sqlite_files(db):
            leftover.unlink(missing_ok=True)
        # The direction and the candidate's own kind only (#263): the checks read nothing of
        # the other kinds, and each one would make every replayed day longer (the barrier's
        # horizon ladder alone doubles the kinds' writes). The direction's algorithms all run,
        # as their CPU comes first in the budget.
        own = [k for k in kinds if k.name == DIRECTION or (kind is not None and k.name == kind.name)]
        # Beside the candidate, the algorithms the engine enrolls (#544): not those it runs in shadow
        # alone (#696) for a refusal of the code they have (``left_out``). The candidate runs whatever its own
        # last report says: a run again by hand judges it anew.
        out = left_out(
            settings.state_dir, read_statuses(settings.state_dir / DB_NAME), settings.target_version, kinds
        )
        of_kind = [a for a in kind_algorithms if kind is not None and a.kind == kind.name]
        replayable: list[Algorithm | KindAlgorithm] = [*algorithms, *of_kind]
        refused = {a.name for a in replayable if a is not candidate and out(a) is not None}
        beside = [a for a in algorithms if a.name not in refused]
        own_algorithms = [a for a in of_kind if a.name not in refused]
        if refused:
            log.info(
                "%s: replayed without %s: their pre-validation refused the code they have, and the"
                " engine does not run them (#544)",
                name,
                ", ".join(sorted(refused)),
            )
        measured = _measured(candidate) if kind is None else None  # a direction algorithm's (#283)
        # The days the nights stored (#370) are keyed by what every registry reads.
        night = night_reads(algorithms, kind_algorithms, kinds)
        trained = await _replay(
            settings,
            candidate,
            kind,
            beside,
            own_algorithms,
            own,
            available,
            replayed,
            db,
            measured,
            night,
        )
        models = {d: how.model for d, how in trained.items()}
        seed_history(settings.state_dir, name)
        before, on_days = earlier_runs(settings.state_dir, name, replayed)
        tries = on_days + 1  # this run among those on any of these days: the bar it is held to
        usage = DayUsage(Meter())  # the verdict reads every replayed day at once: its memory (#546)
        with usage.phase("verdict"):
            if kind is None:
                others = [a.name for a in beside if a is not candidate]
                result = _judge(settings, candidate, others, replayed, models, db, now, tries)
            else:
                same = [a.name for a in own_algorithms if a is not candidate]
                result = _judge_kind(settings, candidate, kind, same, replayed, models, db, now, tries)
        log.info("%s: memory of the verdict on %d days: %s", name, len(replayed), usage.line())
        result = replace(result, earlier_runs=before, earlier_on_these_days=on_days)
        if measured is not None:
            diagnostics, lines = measured.result()
            result = replace(result, diagnostics=diagnostics, diagnostic_lines=lines)
        if revision is not None:
            bound = {
                "boundary": revision.astimezone(KST).isoformat(),
                "since": (since or replayed[0]).isoformat(),
            }
            result = replace(result, revision=bound)
        # The promotion test (#681): a new one, or one on a probation begun before it, is judged as
        # its probation would be, on this walk-forward replay.
        standing = None if revision is not None else _promotable(settings, name)
        if standing is not None:
            tested = DayUsage(Meter())  # it reads the candidate's and the baselines' rows again
            # The budget is every algorithm's: its family read, the test and its record under one
            # lock, so two pre-validations on the same days at once (by hand beside the night's)
            # cannot both be its first test. Recorded before the report: a stop in between counts it
            # once more, never once less.
            with _budget_held(settings.state_dir), tested.phase("promotion"):
                promotion = _promotion(settings, candidate, kind, trained, db, result, standing.on_probation)
                result = replace(result, promotion=promotion)
                if promotion.tested:
                    _record_attempt(settings.state_dir, result)
            log.info("%s: memory of the promotion test: %s", name, tested.line())
            if result.checks and all(c.passed for c in result.checks):
                result = replace(result, checks=[*result.checks, promotion.check()])
        path = report_path(settings.state_dir, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
        # The run is recorded before its report replaces the last one: a stop between the two
        # counts it once more, never once less (a rerun on the same days must not look fresh).
        _record_run(settings.state_dir, result)
        tmp.replace(path)
        if result.promotion is not None and standing is not None:
            _enroll(settings, candidate, kind, result, result.promotion, db, now)
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
    night: NightReads | None = None,
) -> dict[date, DayTrained]:
    """Replay each day into ``db``, the candidate trained on the days before it (on its kind's
    answers, #179); how it was trained for each day (#681: the version it predicted the day with,
    its window's days). With ``measured`` (#283), the candidate is trained by its diagnostics' fit,
    and they are given each replayed day's samples and the model that predicted it. The recorded
    days are read from the sample store when a night stored them, by ``night``'s key (#370)."""
    calendar = calendar_store(settings).calendar
    leaders = Leaders.of(settings)
    everyone: list[Algorithm | KindAlgorithm] = [*algorithms, *kind_algorithms]
    running = load_models(settings.state_dir, [a for a in everyone if a is not candidate])
    fit = None if measured is None else measured.fit
    trainer = _WalkForward(settings, candidate, available, calendar, leaders, kind, fit, night)
    target = settings.target_version if kind is None else kind.answer_version
    recorded: dict[date, bool] = {}  # each window day's, read once

    def was_recorded(d: date) -> bool:
        if d not in recorded:
            recorded[d] = is_recorded(settings.data_dir, d, settings.symbols)
        return recorded[d]

    trained: dict[date, DayTrained] = {}
    meter = Meter()
    for n, day in enumerate(replayed, 1):
        usage = DayUsage(meter)  # its memory, logged (#383)
        with usage.phase("training"):
            model = trainer.before(day)
            climate = trainer.climate_before(day)
            tables = trainer.tables_before(day)  # its kind's baselines' (#180)
        window = [d for d in available if d < day][-candidate.training_days :]
        trained[day] = DayTrained(
            model.version,
            len(window),
            sum(1 for d in window if was_recorded(d)),
            model.target_version == target,
        )
        with usage.phase("replay"):
            await _replay_day(
                settings,
                day,
                leaders,
                db=db,
                models={**running, **tables, candidate.name: model},
                algorithms=algorithms,
                calendar=calendar,
                climate=climate,
                kinds=kinds,
                kind_algorithms=kind_algorithms,
            )
        log.info("%s: replayed %s with %s", candidate.name, day, model.version)
        if measured is not None:
            with usage.phase("diagnostics"):
                # The day's samples are the next day's training too: read once (the last day's
                # alone is read for this).
                measured.day(day, model, partial(trainer.samples_on, day))
        log.info("%s: memory on %s (day %d of %d): %s", candidate.name, day, n, len(replayed), usage.line())
    return trained


@dataclass(frozen=True)
class DayTrained:
    """How the candidate was trained before a replayed day (#681): the version of the model it
    predicted the day with; its window's days before the day (``training_days`` of them at most,
    backfilled ones too) and of them the recorded ones; and whether the model was trained on its
    target. Not (nothing to learn before the day: its defaults predicted, which never count), the day
    is skipped by the promotion test, not scored."""

    model: str
    training_days: int
    recorded: int
    trained: bool

    def as_dict(self, day: date) -> dict[str, Any]:
        return {
            "day": day.isoformat(),
            "model": self.model,
            "training_days": self.training_days,
            "recorded": self.recorded,
            "scored": self.trained,
        }


async def _replay_day(
    settings: Settings,
    day: date,
    leaders: Leaders,
    *,
    db: Path,
    models: Mapping[str, Model],
    algorithms: Sequence[Algorithm],
    calendar: MarketCalendar,
    climate: Model | None,
    kinds: Sequence[Kind],
    kind_algorithms: Sequence[KindAlgorithm],
) -> None:
    """``day``'s recorded events replayed into ``db`` (``replay_recorded``), then let go (#383): what
    it predicted is in ``db``; its engine and events are dropped before the next day is trained and
    replayed, and the memory they freed is handed back to the system (``footprint.give_back``).
    Without, the process grew a replayed day after another until the kernel killed it (2026-10-27,
    #383). It keeps nothing: what the next day reads is in ``db`` and the trainer's days."""
    events = recorded_events(settings.data_dir, day, settings.symbols, leaders)
    engine = await replay_recorded(
        settings,
        day,
        events,
        db=db,
        models=models,
        algorithms=algorithms,
        calendar=calendar,
        climate=climate,
        kinds=kinds,
        kind_algorithms=kind_algorithms,
    )
    engine.store.close()
    del engine, events
    give_back()


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
    names = {b.name for b in TOUCH_BASELINES}
    store = PredictionStore(db)
    try:
        statuses = store.status_counts(name)
        skipped = store.count_runs("cpu_budget", datetime.combine(replayed[0], time(), KST), name)
        # The fields they read of each prediction (#546, ``PredictionStore.reviewed``): whole rows
        # (about 2.2 kB each, every predictor's of every replayed day) made the run's end its peak.
        algos, baselines = split(scored_by_algorithm(store.reviewed(replayed, False)), names)
        measured = correlations(name, others, store.reviewed(replayed, True))
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
        # The fields they read (#546, ``kind_reviewed``), as the direction's verdict.
        scored = on_target(store.kind_reviewed(kind.name, version, replayed, False), names, version)
        algos, baselines = kind_split(scored_of_kind(scored, kind), names)
        every = on_target(store.kind_reviewed(kind.name, version, replayed, True), names, version)
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


@dataclass(frozen=True)
class _Promotable:
    """A candidate the promotion test judges (#681): new (no status), or on a probation begun when its
    pre-validation passed before #681 (``REGISTERED``), from ``stint``."""

    on_probation: bool
    stint: datetime | None = None


def _promotable(settings: Settings, name: str) -> _Promotable | None:
    """Whether ``name``'s pre-validation judges its promotion (#681), by the engine's statuses (read
    without writing): not one registered before pre-validation (it runs without a report), nor one the
    engine runs with any other status (active, on the bench, a clone, retired, on a probation of new
    code, a new target or judged forward)."""
    if name in REGISTERED_BEFORE:
        return None
    standing = read_statuses(settings.state_dir / DB_NAME).get(name)
    if standing is None:
        return _Promotable(False)
    if standing.status is Status.PROBATION and standing.reason == REGISTERED:
        return _Promotable(True, standing.since)
    return None


def _promotion(
    settings: Settings,
    candidate: Algorithm | KindAlgorithm,
    kind: Kind | None,
    trained: Mapping[date, DayTrained],
    db: Path,
    result: Prevalidation,
    on_probation: bool,
) -> Promotion:
    """The candidate's promotion test (#681) on its replay in ``db``: judged as a probation review
    judges, on the days its model was trained (``DayTrained.trained``), held to its days' share of
    the budget. Passed, it is active at once; failed, refused (one on probation stays there, judged
    forward); not judged (its checks refused it, too few days scored, its days' budget spent, too few
    scored predictions), on a probation judged forward on live days."""
    budget = settings.promotion_budget
    level = promotion_level(settings.review_confidence, budget)
    days = sorted(trained)
    scored = [d for d in days if trained[d].trained]
    before = family(settings.state_dir, scored)
    untested = REFUSED if not on_probation else JUDGED_FORWARD
    base = partial(
        Promotion,
        candidate.name,
        result.code,
        level=level,
        budget=budget,
        days=[trained[d].as_dict(d) for d in days],
        family=before,
        on_probation=on_probation,
        judged_days=scored,
    )
    forward = "judged forward on live days"
    if not all(c.passed for c in result.checks):
        return base(untested, "not tested: its checks refused it", False)
    if len(scored) < MIN_DAYS:
        why = f"not tested: {len(scored)} days with a trained model, fewer than {MIN_DAYS}; {forward}"
        return base(JUDGED_FORWARD, why, False)
    if len(before) >= budget:
        spent = f"{len(before)} of {budget} tests judged them"
        return base(JUDGED_FORWARD, f"not tested: these days' budget is spent ({spent}); {forward}", False)
    judged = _promotion_stats(settings, candidate, kind, scored, db, level)
    weakest = _weakest_by_day(judged)
    n, decided = judged.summary.n, enough(judged, settings.min_sample)
    if not decided or weakest is None:
        few = f"{n} scored, at least {settings.min_sample}"
        return base(JUDGED_FORWARD, f"not tested: too few to decide ({few}); {forward}", False, scored=n)
    baseline, lift = weakest
    low = "-" if lift.low is None else f"{lift.low:.4f}"
    detail = (
        f"weakest lift by trading day vs {baseline} {lift.value:.4f}, low {low} at {level:.2%}"
        f" ({lift.n} days scored); test {len(before) + 1} of {budget} on these days"
    )
    lifted = {"baseline": baseline, "n": lift.n, "value": lift.value, "low": lift.low, "high": lift.high}
    judging = partial(base, tested=True, scored=n, enough=True, weakest=lifted)
    if lift.low is not None and lift.low > 0:
        return judging(ACTIVE, f"{detail}: active")
    if on_probation:
        return judging(JUDGED_FORWARD, f"{detail}: not passed, it stays on probation")
    return judging(REFUSED, f"{detail}: not passed")


def _promotion_stats(
    settings: Settings,
    candidate: Algorithm | KindAlgorithm,
    kind: Kind | None,
    days: Sequence[date],
    db: Path,
    level: float,
) -> Lifted:
    """The candidate's stats against its kind's baselines on ``days`` of its replay, as a probation
    review measures them (``lifecycle.Scoring.measure``: only the predictions of models trained on
    the target, its lifts by trading day at ``level``); its rows and the baselines' only are read."""
    store = PredictionStore(db)
    try:
        if kind is None:
            scoring = DIRECTION_SCORING
            names = {b.name for b in TOUCH_BASELINES}
            rows = store.reviewed(days, False, {candidate.name, *names})
        else:
            scoring = kind_scoring(kind)
            names = {b.name for b in kind.baselines}
            wanted = {candidate.name, *names}
            rows = [
                r
                for r in store.kind_reviewed(kind.name, kind.answer_version, days, False)
                if r["algorithm"] in wanted
            ]
    finally:
        store.close()
    rows = on_target(rows, names, scoring.target_version)
    return scoring.measure(candidate.name, rows, names, settings.fee_rate, level)


def _enroll(
    settings: Settings,
    candidate: Algorithm | KindAlgorithm,
    kind: Kind | None,
    result: Prevalidation,
    promotion: Promotion,
    db: Path,
    now: datetime,
) -> None:
    """What the promotion test made of the candidate, in the engine's database (#681), as a simulated
    probation's review keeps it (#298): made active, its replay of the days judged kept as its
    probation's (the active check of its first days reads them, #545) and the change marked
    ``simulated``; on a probation judged forward (never simulated: ``lifecycle.starts_simulation``).
    A new one is enrolled first. The engine runs it from its next bar close. Nothing is written for a
    refusal, nor for one whose status changed since the test began."""
    from krx_quant.simulation import WALK_FORWARD, keep_replayed  # it imports this module

    name = candidate.name
    store = PredictionStore(settings.state_dir / DB_NAME)
    try:
        standing = store.statuses().get(name)
        fresh = standing is None
        if not fresh and not (
            standing is not None and standing.status is Status.PROBATION and standing.reason == REGISTERED
        ):
            log.info("%s: its status changed meanwhile; the promotion test is not applied", name)
            return
        if promotion.outcome == REFUSED or (promotion.outcome == JUDGED_FORWARD and not fresh):
            return
        if fresh:
            # Judged forward until made active below: a stop in between leaves it there.
            reason = FORWARD if promotion.outcome == ACTIVE else f"{FORWARD}: {promotion.detail}"
            store.add_status(name, now, None, Status.PROBATION, reason)
            log.info("%s: enrolled on probation (%s)", name, reason)
            if promotion.outcome != ACTIVE:
                return
        stint = now if standing is None else standing.since
        days = promotion.judged_days
        run = store.add_simulation_run(now, WALK_FORWARD, days)
        keep_replayed(store, db, run, [(name, kind)], days)
        changed = f"{REPLAYED} {days[0]}..{days[-1]} ({len(days)} days): {promotion.detail}"
        review = {
            "days": len(days),
            "review_day": None,
            "scored": promotion.scored,
            "enough": promotion.enough,
            "weakest": promotion.weakest,
            "level": promotion.level,
            "status": Status.ACTIVE.value,
            "reason": changed,
        }
        training = {"cadence": WALK_FORWARD, "models": {d.isoformat(): m for d, m in result.models.items()}}
        kind_name = DIRECTION if kind is None else kind.name
        store.add_simulation(name, stint, kind_name, run, now, {"training": training, "review": review})
        if store.change_status(name, now, Status.PROBATION, Status.ACTIVE, changed, simulated=True):
            log.info("%s: probation -> active (%s)", name, changed)
    finally:
        store.close()


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
    algorithm of a kind besides the direction (``kind``, #179) on that kind's answers.

    A day a night stored in the sample store (#370, ``state/training-samples``) under the key of
    what it reads (``night``: the registries' features and kinds, as the night reads them) is read
    from there, only the features and answers asked for kept (``TrainingDay.keeping``): that is what
    making it with those alone gives. On a recorded day the Market View follows every feed whatever
    is asked; on a backfilled day the leaders' and the previous days' bars are read for every
    feature that reads them (``training.needs_leaders``, ``needs_previous_day``; #373: before, a
    backfilled day made for Kalman had no futures' moves, so backfilled days were always made). A
    day not stored under that key is made (``training.training_day``). Nothing is stored."""

    def __init__(
        self,
        settings: Settings,
        algorithm: Algorithm | KindAlgorithm,
        available: Sequence[date],
        calendar: MarketCalendar,
        leaders: Leaders,
        kind: Kind | None = None,
        fit: Callable[[Sequence[Sample]], Model] | None = None,
        night: NightReads | None = None,
    ) -> None:
        self._settings = settings
        self._night = night
        self._store = SampleStore(settings.state_dir / STORE_DIR)
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
        days = self._window(day)
        try:
            if self._kind is not None:
                kind_algorithm = cast(KindAlgorithm, self._algorithm)
                answered = [s for found in days for s in found.of_kind(self._kind, kind_algorithm.features)]
                if not any(s.label is not None for s in answered):
                    return self._algorithm.default_model()
                return kind_algorithm.fit(answered)
            training = self._training(days)
            if not any(s.outcome is not None for s in training):
                return self._algorithm.default_model()
            return (self._fit or cast(Algorithm, self._algorithm).fit)(training)
        except NothingToLearn as e:  # no book on backfilled days, say; any other error fails the check
            log.warning("%s not trained before %s, its defaults predict: %s", self._algorithm.name, day, e)
            return self._algorithm.default_model()

    def samples_before(self, day: date) -> list[Sample]:
        """The samples ``before`` trains a direction algorithm on for ``day``: its window's days
        before it, with its features. The climate prior experiment (#429) fits its arms on them."""
        return self._training(self._window(day))

    def _window(self, day: date) -> list[TrainingDay]:
        """The algorithm's window of days before ``day``, the days older than it let go."""
        window = [d for d in self._available if d < day][-self._algorithm.training_days :]
        for gone in [d for d in self._loaded if not window or d < window[0]]:
            del self._loaded[gone]
        return [found for d in window if (found := self._load(d)) is not None]

    def _training(self, days: Sequence[TrainingDay]) -> list[Sample]:
        # As the nightly training does (``nightly._samples``): each sample names its day's
        # reference price, recorded ("mid") or backfilled ("close"), which Neyman's folds
        # and a model's days by source read (#283).
        return [s for found in days for s in found.of(self._algorithm.features)]

    def samples_on(self, day: date) -> list[Sample]:
        """``day``'s samples with the algorithm's features (a direction algorithm's), each naming
        the day's reference price, as the nightly training reads them; none if the day has none."""
        found = self._load(day)
        return [] if found is None else found.of(self._algorithm.features)

    def kind_samples_on(self, day: date) -> list[KindSample]:
        """``day``'s samples of a kind's algorithm (``kind``): its features, each with its kind's answer
        of the bar close (the queue screen scores them, #599); none if the day has none."""
        if self._kind is None:
            raise ValueError(f"{self._algorithm.name} predicts the direction: it has no kind's samples")
        found = self._load(day)
        return [] if found is None else found.of_kind(self._kind, self._algorithm.features)

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
        features, or (``answered``) for its kind's tables. From the sample store when a night
        stored the day (#370; a backfilled one too since #373)."""
        s = self._settings
        kinds = () if self._kind is None or not (features or answered) else (self._kind,)
        night = self._night
        if night is not None and set(features) <= set(night.names) and set(kinds) <= set(night.kinds):
            stored = stored_night_day(
                self._store,
                night,
                s.data_dir,
                day,
                self._calendar,
                s.symbols,
                s.fee_rate,
                self._leaders,
                target_version=s.target_version,
            )
            if stored is not None:
                return stored.keeping(features, [k.name for k in kinds])
        return training_day(
            s.data_dir,
            day,
            self._calendar,
            s.symbols,
            features,
            s.fee_rate,
            self._leaders,
            kinds=kinds,
        )
