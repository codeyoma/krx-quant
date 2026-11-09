"""The queue screen (#599, ``krx-quant screen-queue``): a cheap look each night at the bar-close
algorithms waiting for their pre-validation, which only orders the queue of the replays.

A pre-validation replays a candidate on about 20 recorded days beside the live engine: 1.4 to 3.3
hours, one a night (two on a quiet night, #546), and nine waited on 2026-11-06. The folds of the
pre-validation (``prevalidation._WalkForward``), without its replay, gave shannon's and poisson's
refusals in 4 to 6 minutes (#429, #439). Two cases cannot say how often a fold would miss a good
algorithm, so the screen never admits, refuses or skips one: every waiting algorithm is still
pre-validated, in the order the screen sets, and its five checks still judge it.

**Score** (``screen``). The candidate's walk-forward folds over the pre-validation's days (the latest
``PREVALIDATION_DAYS`` recorded days, each predicted by its model fitted on its own window of the
days before it, never on the day itself), its forecasts and its kind's baselines' paired on the same
bar closes; for each baseline the lift by trading day (``stats.day_lift``: each day's mean gain one
value). The weakest of them is the score; one missing (fewer than two days paired) leaves it unknown.
Nothing is replayed: each fold day's training samples are predicted, as the nightly training makes
them (read from the sample store when a night stored them, #370), so the bar closes the statistics
leave out (a VI, a special situation) are left out.

- The candidate's own transforms (its standardization, its validation) are in its fit, on the days
  before the fold; the climate table (``baseline-climate``) and a kind's tables are fitted on the
  days before it too, as the pre-validation's replay refits them.
- The persistence baseline at a bar close t is given the answer of the window that ended at t (it
  opened at t less the horizon), the random one the classes of the day's windows over by t (their
  bar close plus the horizon at or before t): never a class of a window still open at t. A day's
  whole class shares would leak the answers into the baseline. Live, both also count the bar closes
  no sample has (a VI); here they do not. The touch target's baselines only: with
  ``KRX_TARGET_VERSION=1`` a direction candidate's score is unknown (``TOUCH_ONLY``), as the
  four-minute move's persistence reads mid-points the samples do not keep.
- A kind's candidate (#179: the range, the barrier and its horizon ladder) is scored on its kind's
  answers (``TrainingDay.of_kind``: its answer version, its horizon of 4, 15, 30 or 60 minutes), its
  barriers' width the one fixed at the bar close, as the engine fixes it; a window with no class
  (unscorable, or a special situation over its horizon) is not scored. Its baselines are its kind's
  own (its climate table, its persistence of the window one horizon back), never the direction's
  touch labels. A baseline is given what the engine gives it at the bar close (``_kind_inputs``):
  of the window it predicts, only the figures fixed when it opened (the reference mid-point, the
  tick and the flat band k, by which the range's persistence classes the previous window's range).

**Order** (``order_queue``): those passed over on ``MAX_PUSHED_NIGHTS`` nights first (the bound),
earliest come first; then a seat by score (highest first) and a seat by arrival, alternately
(``FIFO_EVERY``): the first in turn of a night is the score's, a quiet night's second the earliest
come's. A candidate is passed over on a night its place in that night's order was behind its place
by arrival; arrival is the first night of its latest stretch in the screens' record (``stretches``),
ties in the registries' turn (``scheduled.prevalidations.prevalidation_order``). An unknown score
takes no score seat and keeps its arrival seats and the bound: no candidate is dropped, and none loses
its turn for a limit it hit. No significance threshold, no permanent exclusion, no refusal for a
correlation.

**Fixed and recorded.** The rule's version (``SCREEN_VERSION``) is in each score's key with the
candidate's code digest, its target or kind's answer version and the samples' key (the days, the
symbols, the fee rate, what the nights read, what the samples mean (``sample_store.semantics``) and
the sample store's file of each day the folds read, ``screen_key``): a score of the same key is not
screened again (``cached``), and a new recorded day screens every one again. Every screen is kept
(``HISTORY``); the latest (``QUEUE``) is what the night's pre-validations go by. A night's screen of
the same days is not new evidence beside the one before it: tuning the score on what it ordered needs
days it has not seen.
#262's correction counts the pre-validations run on the same days, not this choice among them.

**Resources.** Each candidate is screened in its own process (``screen-queue --candidate``), one after
another, under a time limit (``CANDIDATE_TIMEOUT``) and a limit on its resident memory
(``CANDIDATE_MEMORY_MB``, read from ``/proc``: none where it cannot be read), within the night's
budget (``SCREEN_BUDGET``); on Linux it is killed with the screen's own process. A sample store's
day is the whole day of every feature, decompressed (``SampleStore.get``) before it is cut to the
candidate's. A limit, an error or the budget leaves the candidate's score unknown.

**What it cannot see.** The special situations as the engine marks them live, the ten seconds' time
budget, the CPU shared with the other algorithms and the engine's schedule: the pre-validation's
checks judge those.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import logging
import signal
import subprocess
import sys
import tempfile
from collections import Counter, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any, cast

from krx_quant.algorithms import ALGORITHMS, KIND_ALGORITHMS, Algorithm, KindAlgorithm, Model, kind_of
from krx_quant.algorithms.contract import KindSample, NotPredicted, PredictionContext, Sample, forecast_of
from krx_quant.baselines import Baseline, Forecast, Inputs, baselines_for
from krx_quant.climate import CLIMATE_DAYS, direction
from krx_quant.config import Settings
from krx_quant.features import Leaders
from krx_quant.feeds.base import KST
from krx_quant.footprint import DayUsage, Meter, give_back, resident_mb
from krx_quant.kind_registry import KINDS, kind_named
from krx_quant.kinds import DIRECTION, Answer, Kind, KindBaseline, KindInputs, TableBaseline, most_likely
from krx_quant.market_calendar import calendar_store
from krx_quant.predictions import DB_NAME, read_statuses
from krx_quant.prevalidation import (
    PREVALIDATION_DAYS,
    _WalkForward,
    admits,
    code_digest,
    refusals,
)
from krx_quant.pricing import Direction
from krx_quant.pricing import most_likely as most_likely_direction
from krx_quant.process_log import KRX_QUANT
from krx_quant.sample_store import STORE_DIR, NightReads, night_reads
from krx_quant.sample_store import semantics as samples_semantics
from krx_quant.stats import Lift, day_lift
from krx_quant.touch import WINDOW
from krx_quant.training import finished_days, is_recorded
from krx_quant.training_nights import exit_text

log = logging.getLogger(__name__)

COMMAND = "screen-queue"
# The scoring rule's version: in every score's key, so a change of the rule screens each again.
SCREEN_VERSION = 1
QUEUE = "prevalidations/queue.json"  # under the state directory: the latest screen
HISTORY = "prevalidations/queue.history.jsonl"  # every screen, a JSON line each, oldest first
# The seats after the bound: every second one goes to the earliest come.
FIFO_EVERY = 2
# A candidate passed over on this many nights of its stretch goes first (the bound).
MAX_PUSHED_NIGHTS = 3
SCORE, FIRST_COME, BOUND = "score", "first come", "bound"
CANDIDATE_TIMEOUT = timedelta(minutes=10)
CANDIDATE_MEMORY_MB = 1_000.0
SCREEN_BUDGET = timedelta(hours=1)  # the candidates' processes' time a night, all told
SHORTEST = timedelta(minutes=1)  # less budget than this left: the rest are not screened
POLL_SECONDS = 1.0  # how often a candidate's process is looked at
# A queue made more days than this before is not gone by: its seats were set on other nights.
QUEUE_KEPT_DAYS = 4
# Why a direction candidate is not scored on the four-minute move (``KRX_TARGET_VERSION=1``).
TOUCH_ONLY = "the touch target only: KRX_TARGET_VERSION 1's persistence needs mid-points the samples lack"


@dataclass(frozen=True)
class Call:
    """A forecast of a bar close, as a lift pairs it (``stats.Paired``): right or wrong."""

    symbol: str
    bar_close: datetime
    correct: bool


Calls = dict[str, list[Call]]  # by predictor: the candidate's name and each baseline's


def direction_calls(
    candidate: Algorithm,
    model: Model,
    climate: Model,
    samples: Sequence[Sample],
    baselines: Sequence[Baseline],
) -> tuple[Calls, int]:
    """The calls on a fold day's ``samples`` with a label (of one symbol or several): the
    candidate's with ``model`` (none where it does not predict), and each baseline's from what it
    knows at the bar close (module doc): the label of the window that ended there, the classes of
    the day's windows over by then, ``climate`` (the table fitted before the day). And how many of
    the candidate's forecasts raised an error: not scored, as live."""
    ordered = sorted(samples, key=lambda s: (s.bar_close, s.symbol))
    labels = {(s.symbol, s.bar_close): s.outcome for s in ordered}
    over = _Over(WINDOW)
    calls: Calls = {candidate.name: [], **{b.name: [] for b in baselines}}
    errors = 0
    for s in ordered:
        seen = over.at(s.symbol, s.bar_close)
        if s.outcome is not None:
            try:
                f = forecast_of(candidate, s.features, model, PredictionContext(s.symbol, s.bar_close))
            except NotPredicted:
                pass
            except Exception:
                errors += 1
            else:
                calls[candidate.name].append(Call(s.symbol, s.bar_close, _called(f) is s.outcome))
            inputs = Inputs(
                symbol=s.symbol,
                bar_close=s.bar_close,
                mid=None,
                mid_before=None,
                flat_ticks=None,
                tick=None,
                day_classes=seen,
                previous=labels.get((s.symbol, s.bar_close - WINDOW)),
                climate=direction(climate, s.symbol, s.bar_close),
            )
            for b in baselines:
                g = b.predict(inputs)
                if g is not None:
                    calls[b.name].append(Call(s.symbol, s.bar_close, _called(g) is s.outcome))
        over.add(s.symbol, s.bar_close, s.outcome)
    return calls, errors


def kind_calls(
    kind: Kind,
    candidate: KindAlgorithm,
    model: Model,
    tables: Mapping[str, Model],
    samples: Sequence[KindSample],
) -> tuple[Calls, int]:
    """A kind's ``direction_calls`` (#179): the calls on a fold day's ``samples`` with a class of
    the kind's answers, the candidate's and its kind's baselines' (a table one with its table fitted
    before the day, of ``tables``), each baseline given the answer of the window one horizon back and
    the classes of the windows over by the bar close; and the forecasts that raised an error."""
    ordered = sorted(samples, key=lambda s: (s.bar_close, s.symbol))
    answers = {(s.symbol, s.bar_close): s.answer for s in ordered}
    over = _Over(kind.horizon)
    baselines = [_bound(b, tables) for b in kind.baselines]
    calls: Calls = {candidate.name: [], **{b.name: [] for b in baselines}}
    errors = 0
    for s in ordered:
        seen = over.at(s.symbol, s.bar_close)
        label = s.label
        if label is not None:
            truth = kind.index(label)
            try:
                f = candidate.predict(s.features, model)
            except NotPredicted:
                pass
            except Exception:
                errors += 1
            else:
                right = most_likely(f.probabilities) == truth
                calls[candidate.name].append(Call(s.symbol, s.bar_close, right))
            previous = answers.get((s.symbol, s.bar_close - kind.horizon))
            inputs = _kind_inputs(s, previous, seen)
            for b in baselines:
                g = b.predict(inputs)
                if g is not None:
                    calls[b.name].append(Call(s.symbol, s.bar_close, most_likely(g.probabilities) == truth))
        over.add(s.symbol, s.bar_close, label)
    return calls, errors


def _kind_inputs(s: KindSample, previous: Answer | None, seen: Mapping[str, int]) -> KindInputs:
    """What a kind's baseline knows at ``s``'s bar close, as the engine gives it (``_predict_kinds``):
    the bar close's reference mid-point, tick and flat band k (the figures of the answer's path fixed
    when the window opened at the bar close, nothing it saw after), the answer of the window over at
    the bar close if it has a class, and the classes of the windows over by then."""
    path = {} if s.answer is None else s.answer.path
    k = path.get("k")
    return KindInputs(
        symbol=s.symbol,
        bar_close=s.bar_close,
        mid=_number(path.get("anchor")),
        tick=_number(path.get("tick")),
        flat_ticks=k if isinstance(k, int) and not isinstance(k, bool) else None,
        previous=previous if previous is not None and previous.label is not None else None,
        day_classes=seen,
    )


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


class _Over:
    """The classes of each symbol's windows over by a bar close: a window opened at b is over at
    b + ``horizon``. Asked in time order."""

    def __init__(self, horizon: timedelta) -> None:
        self._horizon = horizon
        self._open: dict[str, deque[tuple[datetime, Any]]] = {}
        self._counts: dict[str, Counter[Any]] = {}

    def at(self, symbol: str, t: datetime) -> Counter[Any]:
        """The classes of ``symbol``'s windows over by ``t``: a copy, the bar close's own."""
        pending = self._open.setdefault(symbol, deque())
        counts = self._counts.setdefault(symbol, Counter())
        while pending and pending[0][0] <= t:
            counts[pending.popleft()[1]] += 1
        return Counter(counts)

    def add(self, symbol: str, bar_close: datetime, label: Any) -> None:
        """The window opened at ``bar_close``, of class ``label`` (None: not counted)."""
        if label is not None:
            self._open.setdefault(symbol, deque()).append((bar_close + self._horizon, label))


def _bound(baseline: KindBaseline, tables: Mapping[str, Model]) -> KindBaseline:
    """``baseline`` with its table of ``tables``, if it predicts from one."""
    if isinstance(baseline, TableBaseline) and baseline.name in tables:
        return baseline.using(tables[baseline.name])
    return baseline


def _called(f: Forecast) -> Direction:
    return most_likely_direction(f.p_up, f.p_flat, f.p_down)


@dataclass(frozen=True)
class Screened:
    """A candidate's folds (``screen``): the days predicted, its calls scored, its errors, and its
    lift by trading day over each of its kind's baselines (None: fewer than two days paired); or why
    it was not scored at all (``unscored``, ``TOUCH_ONLY``)."""

    algorithm: str
    kind: str
    days: tuple[date, ...]
    calls: int
    errors: int
    lifts: Mapping[str, Lift | None]
    unscored: str | None = None

    @property
    def weakest(self) -> tuple[str, float] | None:
        """The baseline of the weakest lift by trading day, and that lift; None unless every
        baseline has one."""
        values = [(b, x.value) for b, x in self.lifts.items() if x is not None and x.value is not None]
        if not self.lifts or len(values) < len(self.lifts):
            return None
        return min(values, key=lambda pair: (pair[1], pair[0]))

    @property
    def score(self) -> float | None:
        """The weakest lift by trading day: the score; None, unknown."""
        weakest = self.weakest
        return None if weakest is None else weakest[1]

    @property
    def why(self) -> str | None:
        """Why the score is unknown; None when it is known."""
        if self.score is not None:
            return None
        if self.unscored is not None:
            return self.unscored
        if self.calls == 0:
            return "no call scored"
        missing = sorted(b for b, lift in self.lifts.items() if lift is None or lift.value is None)
        return f"fewer than two trading days paired with {', '.join(missing) or 'a baseline'}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "kind": self.kind,
            "days": [d.isoformat() for d in self.days],
            "calls": self.calls,
            "errors": self.errors,
            "lifts": {b: None if lift is None else _lift_json(lift) for b, lift in self.lifts.items()},
            "score": self.score,
            "weakest": None if self.weakest is None else self.weakest[0],
            "unscored": self.unscored,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Screened:
        lifts = {
            str(b): None if x is None else Lift(int(x["n"]), x["value"], x["low"], x["high"], x["p"])
            for b, x in data["lifts"].items()
        }
        return cls(
            str(data["algorithm"]),
            str(data["kind"]),
            tuple(date.fromisoformat(d) for d in data["days"]),
            int(data["calls"]),
            int(data["errors"]),
            lifts,
            None if data.get("unscored") is None else str(data["unscored"]),
        )


def _lift_json(lift: Lift) -> dict[str, Any]:
    return {"n": lift.n, "value": lift.value, "low": lift.low, "high": lift.high, "p": lift.p}


def candidate_named(
    name: str,
    algorithms: Sequence[Algorithm] = ALGORITHMS,
    kind_algorithms: Sequence[KindAlgorithm] = KIND_ALGORITHMS,
    kinds: Sequence[Kind] = KINDS,
) -> tuple[Algorithm | KindAlgorithm, Kind | None]:
    """The bar-close algorithm ``name`` and its kind (None: the direction's). Raises ValueError for
    one not registered, or of a kind not registered."""
    found: Algorithm | KindAlgorithm | None = next((a for a in algorithms if a.name == name), None)
    if found is None:
        found = next((a for a in kind_algorithms if a.name == name), None)
    kind = None if found is None or kind_of(found) == DIRECTION else kind_named(kind_of(found), kinds)
    if found is None or (kind_of(found) != DIRECTION and kind is None):
        names = ", ".join([*(a.name for a in algorithms), *(a.name for a in kind_algorithms)])
        raise ValueError(f"unknown bar-close algorithm {name!r}; registered: {names}")
    return found, kind


def _predicted(settings: Settings, available: Sequence[date], days: int) -> list[date]:
    """The latest ``days`` recorded days of ``available``: the pre-validation's."""
    return [d for d in available if is_recorded(settings.data_dir, d, settings.symbols)][-days:]


def screen(
    settings: Settings,
    name: str,
    *,
    days: int = PREVALIDATION_DAYS,
    now: datetime | None = None,
    algorithms: Sequence[Algorithm] = ALGORITHMS,
    kind_algorithms: Sequence[KindAlgorithm] = KIND_ALGORITHMS,
    kinds: Sequence[Kind] = KINDS,
) -> Screened:
    """Screen ``name`` on the latest ``days`` recorded days (module doc): each predicted by its model
    fitted on its window of the days before it, scored against its kind's baselines. The registries
    name the days the nights stored (#370, ``night_reads``). Raises ValueError for an algorithm not
    registered (``candidate_named``), FileNotFoundError when no day was recorded."""
    candidate, kind = candidate_named(name, algorithms, kind_algorithms, kinds)
    now = now or datetime.now(KST)
    available = finished_days(settings.data_dir, settings.symbols, now)
    predicted = _predicted(settings, available, days)
    if not predicted:
        raise FileNotFoundError(f"no recorded day of {', '.join(settings.symbols)} in {settings.data_dir}")
    if kind is None and settings.target_version < 2:
        # The four-minute move's persistence (``baselines.Persistence``) reads the mid-points four
        # minutes apart, which the samples do not keep: given none, it would be always flat again.
        return Screened(name, DIRECTION, tuple(predicted), 0, 0, {}, TOUCH_ONLY)
    calendar = calendar_store(settings).calendar
    night = night_reads(algorithms, kind_algorithms, kinds)
    trainer = _WalkForward(settings, candidate, available, calendar, Leaders.of(settings), kind, night=night)
    baselines = baselines_for(settings.target_version)
    calls: Calls = {}
    errors = 0
    meter = Meter()
    for n, day in enumerate(predicted, 1):
        usage = DayUsage(meter)  # its memory, logged (#383)
        with usage.phase("training"):
            model = trainer.before(day)
            climate = trainer.climate_before(day) if kind is None else None
            tables = trainer.tables_before(day)
        with usage.phase("scoring"):
            if kind is None:
                assert climate is not None
                found, failed = direction_calls(
                    cast(Algorithm, candidate), model, climate, trainer.samples_on(day), baselines
                )
            else:
                found, failed = kind_calls(
                    kind, cast(KindAlgorithm, candidate), model, tables, trainer.kind_samples_on(day)
                )
        for who, these in found.items():
            calls.setdefault(who, []).extend(these)
        errors += failed
        give_back()
        log.info("%s: screened %s with %s", name, day, model.version)
        log.info("%s: memory on %s (day %d of %d): %s", name, day, n, len(predicted), usage.line())
    mine = calls.pop(candidate.name, [])
    lifts = {b: day_lift(mine, these) for b, these in calls.items()}
    return Screened(name, kind_of(candidate), tuple(predicted), len(mine), errors, lifts)


def save_screened(path: Path, screened: Screened) -> None:
    """``screened`` as the parent reads it (``run_candidate``): whole or not at all."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(screened.as_dict(), indent=2))
    tmp.replace(path)


def waiting(
    settings: Settings,
    algorithms: Sequence[Algorithm] = ALGORITHMS,
    kind_algorithms: Sequence[KindAlgorithm] = KIND_ALGORITHMS,
    kinds: Sequence[Kind] = KINDS,
) -> list[str]:
    """The bar-close algorithms the engine waits to pre-validate (``PredictionEngine.waiting``), read
    from the state as a process beside the engine reads it: no status in the engine DB (read only),
    not admitted by their reports (``prevalidation.admits``: those registered before it are) nor
    refused by them (``refusals``). In the registries' order."""
    statuses = read_statuses(settings.state_dir / DB_NAME)
    admitted = admits(settings.state_dir, settings.target_version, kinds)
    refused = refusals(settings.state_dir, settings.target_version, kinds)
    out = []
    everyone: list[Algorithm | KindAlgorithm] = [*algorithms, *kind_algorithms]
    for a in everyone:
        if kind_of(a) != DIRECTION and kind_named(kind_of(a), kinds) is None:
            continue  # the engine predicts no such kind
        if a.name not in statuses and not admitted(a) and refused(a) is None:
            out.append(a.name)
    return out


def screen_key(
    settings: Settings,
    candidate: Algorithm | KindAlgorithm,
    kind: Kind | None,
    available: Sequence[date],
    predicted: Sequence[date],
    night: NightReads,
) -> str:
    """The key of ``candidate``'s score (module doc, "Fixed and recorded")."""
    tables = [b.days for b in (kind.baselines if kind is not None else ()) if isinstance(b, TableBaseline)]
    back = max(candidate.training_days, CLIMATE_DAYS, *tables)  # the folds' windows reach back so far
    first = available.index(predicted[0]) if predicted and predicted[0] in available else 0
    read = [d for d in available[max(first - back, 0) :] if not predicted or d <= predicted[-1]]
    store = settings.state_dir / STORE_DIR
    parts = {
        "screen": SCREEN_VERSION,
        "code": code_digest(candidate),
        "kind": DIRECTION if kind is None else kind.name,
        "target": settings.target_version if kind is None else kind.answer_version,
        "samples": {
            "days": [d.isoformat() for d in predicted],
            "symbols": list(settings.symbols),
            "fee_rate": repr(float(settings.fee_rate)),
            "reads": [list(night.names), [k.name for k in night.kinds]],
            "semantics": samples_semantics(),
            "store": {d.isoformat(): _stored(store, d) for d in read},
        },
    }
    canonical = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def _stored(store: Path, day: date) -> list[str]:
    """The sample store's files of ``day``: their names are their keys' digests."""
    with contextlib.suppress(OSError):
        return sorted(p.name for p in (store / f"date={day.isoformat()}").glob("*.json.gz"))
    return []


@dataclass(frozen=True)
class Outcome:
    """What screening a candidate in its own process gave: its folds, or why none."""

    screened: Screened | None
    why: str | None = None


# Screens a candidate in a process of its own: its name, the days, the time and memory limits.
Runner = Callable[[str, int, timedelta, float], Outcome]


def run_candidate(
    name: str, days: int, timeout: timedelta, memory_mb: float, *, program: Sequence[str] = KRX_QUANT
) -> Outcome:
    """Screen ``name`` in a process of its own (``program screen-queue --candidate``), killed past
    ``timeout`` or once its resident memory is above ``memory_mb`` (where ``/proc`` tells it). Its
    log goes to this process's stderr; its result comes back in a file."""
    with tempfile.TemporaryDirectory(prefix="screen-") as folder:
        out = Path(folder) / "screened.json"
        command = [*program, COMMAND, "--candidate", name, "--days", str(days), "--out", str(out)]
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, preexec_fn=_with_this_process()
        )
        began = monotonic()
        while True:
            try:
                code = process.wait(timeout=POLL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                pass
            limit = _over(process.pid, began, timeout, memory_mb)
            if limit is not None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                process.wait()
                log.warning("%s: its screen stopped at its %s", name, limit)
                return Outcome(None, limit)
        if code != 0:
            return Outcome(None, f"failed: {exit_text(code)}")
        try:
            return Outcome(Screened.from_dict(json.loads(out.read_text())))
        except (OSError, ValueError, KeyError, TypeError) as e:
            return Outcome(None, f"no result: {type(e).__name__}")


def _with_this_process() -> Callable[[], None] | None:
    """What a candidate's process runs before the program, on Linux: it is killed when this process
    ends (``prctl(PR_SET_PDEATHSIG, SIGKILL)``), so a screen stopped at its job's time limit leaves no
    candidate running beside the next job. None elsewhere (macOS: the tests)."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        prctl = ctypes.CDLL(None, use_errno=True).prctl  # looked up here: the child only calls it
    except (OSError, AttributeError):
        return None

    def set_death_signal() -> None:
        prctl(_PR_SET_PDEATHSIG, int(signal.SIGKILL))

    return set_death_signal


_PR_SET_PDEATHSIG = 1  # linux/prctl.h


def _over(pid: int, began: float, timeout: timedelta, memory_mb: float) -> str | None:
    """The limit a candidate's process is past, if any."""
    resident = resident_mb(pid)
    if resident is not None and resident > memory_mb:
        return f"memory limit ({resident:.0f} MB resident, at most {memory_mb:.0f})"
    if monotonic() - began > timeout.total_seconds():
        return f"time limit ({_minutes(timeout)})"
    return None


def _minutes(span: timedelta) -> str:
    return f"{span.total_seconds() / 60:g} min"


@dataclass(frozen=True)
class Candidate:
    """A waiting algorithm in a night's screen."""

    name: str
    key: str  # its score's (``screen_key``)
    screened: Screened | None  # None: not screened (a limit, an error, the budget)
    why: str | None  # why its score is unknown; None when it is known
    cached: bool  # its score kept from the last screen, of the same key
    since: date  # its arrival: the first night of its latest stretch in the screens
    pushed: int  # the nights of that stretch, before tonight, it was passed over

    @property
    def score(self) -> float | None:
        return None if self.screened is None else self.screened.score

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "score": self.score,
            "why": self.why,
            "cached": self.cached,
            "since": self.since.isoformat(),
            "pushed": self.pushed,
            "screened": None if self.screened is None else self.screened.as_dict(),
        }


@dataclass(frozen=True)
class Queue:
    """A night's screen: the waiting algorithms by arrival, and the order it set, each with its
    seat (``SCORE``, ``FIRST_COME``, ``BOUND``)."""

    made_at: datetime
    night: date
    days: tuple[date, ...]  # the days the folds predicted
    candidates: tuple[Candidate, ...]  # by arrival
    order: tuple[tuple[str, str], ...]  # (name, seat), in turn

    def as_dict(self) -> dict[str, Any]:
        return {
            "screen_version": SCREEN_VERSION,
            "made_at": self.made_at.isoformat(),
            "night": self.night.isoformat(),
            "days": [d.isoformat() for d in self.days],
            "first_come": [c.name for c in self.candidates],
            "order": [name for name, _ in self.order],
            "seats": dict(self.order),
            "candidates": {c.name: c.as_dict() for c in self.candidates},
        }

    def line(self) -> dict[str, Any]:
        """Its line in ``HISTORY``: what the next nights' arrivals and passes read, and the scores."""
        return {
            "screen_version": SCREEN_VERSION,
            "made_at": self.made_at.isoformat(),
            "night": self.night.isoformat(),
            "days": [d.isoformat() for d in self.days],
            "first_come": [c.name for c in self.candidates],
            "order": [name for name, _ in self.order],
            "seats": dict(self.order),
            "scores": {c.name: c.score for c in self.candidates},
            "cached": [c.name for c in self.candidates if c.cached],
        }


def night_of(now: datetime) -> date:
    """The night ``now`` is in: from noon to the next noon, the day it began on (a night's jobs run
    from 22:30 to 05:30)."""
    return (now.astimezone(KST) - timedelta(hours=12)).date()


def order_queue(
    first_come: Sequence[str], scores: Mapping[str, float | None], pushed: Mapping[str, int]
) -> list[tuple[str, str]]:
    """The order of ``first_come`` (the candidates by arrival), each with its seat (module doc): the
    bound first, by arrival; then a score's seat (the highest of the known scores, ties by arrival)
    and an arrival's seat (the earliest come), alternately. A score's seat with no known score left
    goes to the earliest come."""
    bound = [n for n in first_come if pushed.get(n, 0) >= MAX_PUSHED_NIGHTS]
    rest = [n for n in first_come if n not in bound]
    known = sorted((n for n in rest if scores.get(n) is not None), key=lambda n: -cast(float, scores[n]))
    order = [(n, BOUND) for n in bound]
    for seat in range(len(rest)):
        best = next((n for n in known if n in rest), None)
        if seat % FIFO_EVERY == FIFO_EVERY - 1 or best is None:
            name, why = rest[0], FIRST_COME
        else:
            name, why = best, SCORE
        rest.remove(name)
        order.append((name, why))
    return order


def stretches(history: Sequence[Mapping[str, Any]], tonight: date) -> tuple[dict[str, date], dict[str, int]]:
    """Each candidate's latest stretch in the screens before ``tonight``: the night it began (its
    arrival), and the nights of it it was passed over (its place in the order behind its place by
    arrival). A night's last screen counts; a night's screen it is not in ends its stretch; a night
    without a screen ends none."""
    nights: dict[date, Mapping[str, Any]] = {}
    for line in history:
        try:
            night = date.fromisoformat(str(line["night"]))
        except (KeyError, ValueError, TypeError):
            continue
        if night < tonight:
            nights[night] = line
    since: dict[str, date] = {}
    pushed: dict[str, int] = {}
    for night in sorted(nights):
        line = nights[night]
        first = [str(n) for n in line.get("first_come", [])]
        order = [str(n) for n in line.get("order", [])]
        for name in [n for n in since if n not in first]:
            del since[name]
            pushed.pop(name, None)
        for name in first:
            since.setdefault(name, night)
            if name in order and order.index(name) > first.index(name):
                pushed[name] = pushed.get(name, 0) + 1
    return since, pushed


def read_history(state_dir: Path) -> list[dict[str, Any]]:
    """Every screen's line (``HISTORY``), oldest first; lines that cannot be read are skipped."""
    path = state_dir / HISTORY
    try:
        text = path.read_text()
    except FileNotFoundError:
        return []
    out = []
    for raw in text.splitlines():
        with contextlib.suppress(ValueError):
            line = json.loads(raw)
            if isinstance(line, dict):
                out.append(line)
    return out


def read_queue(state_dir: Path) -> dict[str, Any] | None:
    """The latest screen (``QUEUE``) as saved; None without one, or with one that cannot be read."""
    path = state_dir / QUEUE
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.error("cannot read %s (%s): the pre-validations go in the registries' turn", path, e)
        return None
    return data if isinstance(data, dict) else None


def queue_order(state_dir: Path, now: datetime) -> tuple[str, ...]:
    """The order the latest screen set, as the night's pre-validations go by it (``Facts.queue``, the
    Engine API's ``queue``); none without one, one that cannot be read, or one of a night more than
    ``QUEUE_KEPT_DAYS`` days before ``now``'s."""
    data = read_queue(state_dir)
    if data is None:
        return ()
    try:
        night = date.fromisoformat(str(data["night"]))
        order = tuple(str(n) for n in data["order"])
    except (KeyError, ValueError, TypeError):
        return ()
    if (night_of(now) - night).days > QUEUE_KEPT_DAYS:
        return ()
    return order


def _kept(data: Mapping[str, Any] | None) -> dict[str, tuple[str, Screened]]:
    """The scores the last screen made, by candidate: its key and its folds (those it screened)."""
    out: dict[str, tuple[str, Screened]] = {}
    found = (data or {}).get("candidates")
    if not isinstance(found, Mapping):
        return out
    for name, c in found.items():
        with contextlib.suppress(KeyError, ValueError, TypeError, AttributeError):
            if c.get("screened") is not None:
                out[str(name)] = (str(c["key"]), Screened.from_dict(c["screened"]))
    return out


def screen_queue(
    settings: Settings,
    turn: Sequence[str],
    *,
    days: int = PREVALIDATION_DAYS,
    now: datetime | None = None,
    run: Runner = run_candidate,
    algorithms: Sequence[Algorithm] = ALGORITHMS,
    kind_algorithms: Sequence[KindAlgorithm] = KIND_ALGORITHMS,
    kinds: Sequence[Kind] = KINDS,
) -> Queue | None:
    """Screen each bar-close algorithm waiting (``waiting``), by arrival, each in its own process
    (``run``) unless the last screen scored it under the same key; order them (``order_queue``); keep
    the screen (``QUEUE``, a line of ``HISTORY``). ``turn`` is the registries' turn of the
    pre-validations, which breaks ties of arrival. None when none waits (nothing is kept). Raises
    FileNotFoundError when no day was recorded."""
    now = now or datetime.now(KST)
    tonight = night_of(now)
    names = waiting(settings, algorithms, kind_algorithms, kinds)
    if not names:
        return None
    available = finished_days(settings.data_dir, settings.symbols, now)
    predicted = _predicted(settings, available, days)
    if not predicted:
        raise FileNotFoundError(f"no recorded day of {', '.join(settings.symbols)} in {settings.data_dir}")
    since, pushed = stretches(read_history(settings.state_dir), tonight)
    places = {name: i for i, name in enumerate(turn)}
    first_come = sorted(names, key=lambda n: (since.get(n, tonight), places.get(n, len(places)), n))
    kept = _kept(read_queue(settings.state_dir))
    night = night_reads(algorithms, kind_algorithms, kinds)
    spent = 0.0
    candidates = []
    for name in first_come:
        candidate, kind = candidate_named(name, algorithms, kind_algorithms, kinds)
        key = screen_key(settings, candidate, kind, available, predicted, night)
        arrived, passed = since.get(name, tonight), pushed.get(name, 0)
        last = kept.get(name)
        if last is not None and last[0] == key:
            candidates.append(Candidate(name, key, last[1], last[1].why, True, arrived, passed))
            continue
        left = SCREEN_BUDGET - timedelta(seconds=spent)
        if left < SHORTEST:
            spent_why = f"not screened: tonight's budget ({_minutes(SCREEN_BUDGET)}) was spent"
            candidates.append(Candidate(name, key, None, spent_why, False, arrived, passed))
            continue
        began = monotonic()
        outcome = run(name, days, min(CANDIDATE_TIMEOUT, left), CANDIDATE_MEMORY_MB)
        spent += monotonic() - began
        found = outcome.screened
        why = outcome.why if found is None else found.why
        candidates.append(Candidate(name, key, found, why, False, arrived, passed))
    order = order_queue(first_come, {c.name: c.score for c in candidates}, pushed)
    queue = Queue(now, tonight, tuple(predicted), tuple(candidates), tuple(order))
    _keep(settings.state_dir, queue)
    return queue


def _keep(state_dir: Path, queue: Queue) -> None:
    """``queue`` as the latest screen (whole or not at all), after its line in the record: a stop
    between the two leaves the record one screen ahead, never behind."""
    path = state_dir / QUEUE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(queue.as_dict(), indent=2, ensure_ascii=False))
    history = state_dir / HISTORY
    with history.open("a+") as f:
        f.seek(0, 2)
        if f.tell():  # a last line cut short (no newline) must not swallow this one
            f.seek(f.tell() - 1)
            if f.read(1) != "\n":
                f.write("\n")
        f.write(json.dumps(queue.line()) + "\n")
    tmp.replace(path)


def lines(queue: Queue, path: Path) -> list[str]:
    """The command's output: the order with each candidate's seat and score, counts, lifts and
    dates, no price."""
    by_name = {c.name: c for c in queue.candidates}
    first, last = queue.days[0], queue.days[-1]
    out = [
        f"queue screen (#599): {len(queue.candidates)} waiting; folds on {len(queue.days)} recorded days,"
        f" {first}..{last}; the order of the bar-close pre-validations from the night of {queue.night}",
    ]
    for i, (name, seat) in enumerate(queue.order, 1):
        c = by_name[name]
        found = c.screened
        weakest = None if found is None else found.weakest
        if weakest is not None and found is not None:
            on = f"{found.calls} calls on {len(found.days)} days"
            what = f"score {weakest[1]:+.4f} (over {weakest[0]}, {on})"
        else:
            what = f"unknown: {c.why}"
        kept = ", kept" if c.cached else ""
        out.append(
            f"  {i}. {name:<12} seat: {seat:<10} {what}{kept}; waiting since {c.since},"
            f" passed over {c.pushed} of {MAX_PUSHED_NIGHTS} nights"
        )
    screened = [c for c in queue.candidates if c.screened is not None]
    unknown = [c for c in queue.candidates if c.score is None]
    out += [
        f"scored {len(queue.candidates) - len(unknown)}, unknown {len(unknown)}"
        f" ({sum(c.cached for c in queue.candidates)} of {len(screened)} screened kept from the last"
        " screen of the same code and days)",
        f"seats: {BOUND} (passed over on {MAX_PUSHED_NIGHTS} nights: first), then {SCORE} (the highest"
        f" score) and {FIRST_COME} (the earliest come) by turns; an unknown score takes no {SCORE} seat",
        "it orders the queue only: every waiting algorithm is still pre-validated, and its five checks"
        " judge it",
        f"queue: {path}",
    ]
    return out
