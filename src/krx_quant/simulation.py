"""Simulated probation (#298): the latest trading days replayed as if an algorithm had been on
probation, judged at once (``krx-quant simulate``, run by the scheduled jobs).

An algorithm starts on probation once its pre-validation passed (``lifecycle.REGISTERED``) and
when its target changes (#170, #187: ``lifecycle.target_changed``). Judged live, its first review
is weeks away. Instead, the latest trading days are replayed as if it had been on probation then:
it is trained only on the data before each of them, predicts them with the live feature code, and
is scored on their answers of the current answer versions. Those days count as its probation's
first days, and the review of the largest review day they reach is made at once
(``lifecycle.review_at_once``): passed, it is active from then on; not, it stays on probation and
the live days after them count on toward its next review day. The clone check reads them too.

The replayed predictions are kept apart from the live ones (``predictions._SIMULATION_SCHEMA``),
each with the answer it was scored on: the leaderboard, the statistics, the consensus weights and
the stream never read them; the lifecycle does, and the status change a simulation's review makes
is marked (``statuses.simulated``).

**Daily kinds** (``simulate_daily``, ``krx-quant simulate daily``): the latest ``DAILY_DAYS``
(20) trading days with an answer. Before each day the algorithm is fitted as the night before it
would have been (``daily_training.DailyTrainer``: its latest ``training_days`` up to the trading
day before), on the samples ``daily_features.samples`` makes (the features as of the day's 08:50,
from the days before it); a night that learns nothing keeps the model before it, as live (one
whose first night learns nothing is not simulated); its kind's baselines predict each day as at 08:50
(``daily_training.baseline_forecasts``). A day a circuit breaker or a sidecar touched is special,
as live. A daily algorithm whose pre-validation passed but that the engine has not enrolled yet
(its report is read at the engine's next start or 08:50) is enrolled here, as the engine would: the
scheduled job runs right after its pre-validation, so it is simulated the same night.

**Bar-close kinds** (``simulate_bar_close``, ``krx-quant simulate bar-close``: the direction, the
range, the barrier and its horizon ladder): their inputs are the recorded quotes, so the days are
the latest recorded ones, as many as there are up to ``BAR_CLOSE_DAYS`` (20), at least
``prevalidation.MIN_DAYS`` (10): with fewer, nothing is simulated and the probations go on live
(the scheduled job waits for 10). Each algorithm is trained once, on its ``training_days`` before
the first of them (``prevalidation._WalkForward``: backfilled days as the nightly training reads
them), and predicts them all: a little harsher than its live nights. One whose training learns
nothing before them (its defaults would predict, which never count) is not simulated. The days are
replayed with the live engine and feature code (``replay.replay_recorded``), the algorithms waiting
for it together, with their kinds' baselines, whose tables are refitted before each day on the
days before it as the night before would; the replay goes to ``state/simulation-bar-close.sqlite3``
(made anew each run) and its predictions of those algorithms and baselines, with their touch labels
and answers, are kept. The answers are the current answer versions' (the touch target's,
``touch.TARGET_VERSION``; each kind's). Pre-validation's replay is not reused: it refits before
each day (walk-forward), not once before the days, and it replays the days before the probation.

A probation is simulated once (``simulations``, by the algorithm and when its probation began):
one that could not be (its training learned nothing before the days, say) is kept as such, with
why, and its probation goes on live.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, cast

from krx_quant.algorithms import (
    ALGORITHMS,
    DAILY_ALGORITHMS,
    KIND_ALGORITHMS,
    Algorithm,
    KindAlgorithm,
    Model,
    NothingToLearn,
    kind_of,
    register,
    register_kind_algorithms,
)
from krx_quant.baselines import baselines_for
from krx_quant.config import Settings
from krx_quant.daily import DailyAlgorithm, DailyKind, DailySample
from krx_quant.daily_features import DailyFiles, samples
from krx_quant.daily_training import baseline_forecasts, training_first
from krx_quant.features import Leaders
from krx_quant.feeds.base import KST
from krx_quant.kind_registry import DAILY_KINDS, KINDS
from krx_quant.kind_stats import kind_scoring
from krx_quant.kinds import DIRECTION, Answer, Kind
from krx_quant.lifecycle import DIRECTION_SCORING, REGISTERED, AtOnce, Rows, Rules, Status, review_at_once
from krx_quant.market_calendar import MarketCalendar, calendar_store
from krx_quant.predictions import DB_NAME, PredictionStore, SimKindPrediction
from krx_quant.prevalidation import MIN_DAYS, _WalkForward, admits
from krx_quant.replay import recorded_events, replay_recorded, sqlite_files
from krx_quant.touch import TARGET_VERSION
from krx_quant.training import finished_days, is_recorded

log = logging.getLogger(__name__)

DAILY_DAYS = 20  # a daily kind's simulated days: its first review day (``daily.DAILY_RULES``)
BAR_CLOSE_DAYS = 20  # a bar-close kind's, at most: the recorded days there are, at least MIN_DAYS
DAILY, BAR_CLOSE = "daily", "bar_close"  # how a replay trains (``simulation_runs.cadence``)
# The command's argument for each (``krx-quant simulate <which>``).
BAR_CLOSE_COMMAND = "bar-close"


@dataclass(frozen=True)
class Simulation:
    """One algorithm's simulated probation, as the command reports it."""

    algorithm: str
    kind: str
    days: list[date]  # replayed, oldest first; none when it was not simulated
    training: dict[str, Any]  # how it was trained
    verdict: AtOnce | None = None  # its review at once
    why: str | None = None  # why it was not simulated
    status: Status = Status.PROBATION  # its status after


@dataclass(frozen=True)
class SimulationRun:
    """What ``krx-quant simulate`` did."""

    enrolled: list[str] = field(default_factory=list)  # daily algorithms enrolled on probation here
    simulations: list[Simulation] = field(default_factory=list)
    recorded_days: int | None = None  # a bar-close simulation's: the recorded days there were


class SimulationRunning(Exception):
    """A simulation of the same cadence runs already: it holds its lock."""


@contextlib.contextmanager
def _held(state_dir: Path, cadence: str) -> Iterator[None]:
    """Hold ``state/simulation-<cadence>.lock`` (``flock``) while a simulation runs: two at once
    would simulate the same probations twice."""
    path = state_dir / f"simulation-{cadence}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SimulationRunning(f"a {cadence} simulation runs already ({path.name} is held)") from None
        yield


def simulate_daily(
    settings: Settings,
    *,
    algorithms: Sequence[DailyAlgorithm] = DAILY_ALGORITHMS,
    kinds: Sequence[DailyKind] = DAILY_KINDS,
    now: datetime | None = None,
) -> SimulationRun:
    """Enroll the daily algorithms admitted and not run yet, then simulate every daily probation
    that waits for it (module doc). Raises ``SimulationRunning`` while another runs."""
    now = now or datetime.now(KST)
    with _held(settings.state_dir, DAILY):
        store = PredictionStore(settings.state_dir / DB_NAME)
        try:
            enrolled = _enroll(store, settings, algorithms, now)
            waiting = set(store.to_simulate([a.name for a in algorithms]))
            files = DailyFiles(settings.data_dir, settings.state_dir)
            calendar = calendar_store(settings).calendar
            done: list[Simulation] = []
            for algorithm in algorithms:
                kind = next((k for k in kinds if k.name == algorithm.kind), None)
                if algorithm.name not in waiting or kind is None:
                    continue
                stint = store.statuses()[algorithm.name].since
                done.append(_daily(store, settings, files, calendar, algorithm, kind, stint, now))
            simulated = [a for a in algorithms if a.name in waiting]
            ruled = _judge_daily(store, algorithms, simulated, kinds, settings.fee_rate, now)
            return SimulationRun(enrolled, [ruled.get(s.algorithm, s) for s in done])
        finally:
            store.close()


def _enroll(
    store: PredictionStore, settings: Settings, algorithms: Sequence[DailyAlgorithm], now: datetime
) -> list[str]:
    """Put on probation the algorithms with no status that the engine's gate admits (its
    ``_admit``): their pre-validation passed with the code and inputs they run."""
    known = store.statuses()
    admitted = admits(settings.state_dir, settings.target_version)
    enrolled = [a.name for a in algorithms if a.name not in known and admitted(a)]
    for name in enrolled:
        store.add_status(name, now, None, Status.PROBATION, REGISTERED)
        log.info("%s: enrolled on probation (its pre-validation passed)", name)
    return enrolled


def _daily(
    store: PredictionStore,
    settings: Settings,
    files: DailyFiles,
    calendar: MarketCalendar,
    algorithm: DailyAlgorithm,
    kind: DailyKind,
    stint: datetime,
    now: datetime,
) -> Simulation:
    """Replay ``algorithm``'s latest ``DAILY_DAYS`` days, refitted before each, and keep them."""
    assert kind.answers is not None
    symbols = settings.symbols
    stored = {s: files.days(s) for s in symbols}
    answered = sorted(
        {
            d.day
            for s, days in stored.items()
            for d, a in zip(days, kind.answers(days, settings.fee_rate), strict=True)
            if a.label
        }
    )
    days = answered[-DAILY_DAYS:]
    if len(days) < DAILY_DAYS:
        why = f"{len(days)} trading days with an answer, fewer than {DAILY_DAYS}"
        return _not_simulated(store, algorithm.name, kind.name, stint, now, why)
    market = sorted({d.day for found in stored.values() for d in found})
    nights = {d: max(m for m in market if m < d) for d in days if any(m < d for m in market)}
    first = training_first(algorithm, min(nights.values()))
    found = samples(
        kind,
        files,
        symbols,
        algorithm.features,
        settings.fee_rate,
        calendar=calendar,
        first=first,
        last=days[-1],
    )
    by_day: dict[date, list[DailySample]] = defaultdict(list)
    for s in found:
        by_day[s.day].append(s)
    answers = {(s.symbol, s.day): _answer(store, s) for d in days for s in by_day[d]}
    made: list[SimKindPrediction] = []
    models: dict[str, str] = {}
    model: Model | None = None
    for day in days:
        night = nights.get(day)
        training = [
            s for s in found if night is not None and training_first(algorithm, night) <= s.day <= night
        ]
        try:
            model = algorithm.fit(training)
        except NothingToLearn as e:
            # As live: a night that learns nothing leaves the model the night before installed
            # (#298 review). Without one, the days would be predicted by defaults, never counted.
            if model is None:
                why = f"nothing to learn before {day} ({e}): its defaults would predict, which never count"
                return _not_simulated(store, algorithm.name, kind.name, stint, now, why)
            log.warning("%s not refitted before %s, the model before predicts: %s", algorithm.name, day, e)
        models[day.isoformat()] = model.version
        for sample in by_day[day]:
            forecast = algorithm.predict(sample.features, model)
            broken = kind.invalid(forecast)
            answer, special = answers[(sample.symbol, day)]
            made.append(
                SimKindPrediction(
                    symbol=sample.symbol,
                    algorithm=algorithm.name,
                    model_version=model.version,
                    model_target=model.target_version,
                    bar_close=kind.predicted_at(day),
                    output=None if broken else kind.output(forecast),
                    answer=answer,
                    special=special,
                    status="error" if broken else "ok",
                    band=None if sample.answer is None else sample.answer.path.get("band"),
                )
            )
    for name, scored in baseline_forecasts(
        kind, files, symbols, settings.fee_rate, days[0], days[-1]
    ).items():
        for r in scored:
            day = r.bar_close.astimezone(KST).date()
            if day not in by_day or (r.symbol, day) not in answers:
                continue
            answer, special = answers[(r.symbol, day)]
            made.append(
                SimKindPrediction(
                    symbol=r.symbol,
                    algorithm=name,
                    model_version=r.model_version,
                    model_target=None,
                    bar_close=r.bar_close,
                    output=r.output,
                    answer=answer,
                    special=special,
                )
            )
    run = store.add_simulation_run(now, DAILY, days)
    store.add_sim_kind_predictions(run, kind.name, kind.answer_version, made)
    training_detail = {"cadence": DAILY, "models": models}
    store.add_simulation(algorithm.name, stint, kind.name, run, now, {"training": training_detail})
    log.info("%s: simulated %s..%s (%d predictions)", algorithm.name, days[0], days[-1], len(made))
    return Simulation(algorithm.name, kind.name, list(days), training_detail)


def _answer(store: PredictionStore, sample: DailySample) -> tuple[Answer | None, bool]:
    """A day's answer as the engine writes it: the day's VIs of the symbol in its path
    (``daily._with_vis``), special when a circuit breaker or a sidecar touched the day."""
    start = datetime.combine(sample.day, time(), KST)
    windows = store.special_windows(None, start, start + timedelta(days=1))
    special = any(w["symbol"] is None for w in windows)
    if sample.answer is None:
        return None, special
    vis = [w for w in windows if w["symbol"] == sample.symbol and w["kind"] == "vi"]
    answer = sample.answer
    return Answer(answer.label, answer.unscorable, {**answer.path, "vi": len(vis)}), special


def _not_simulated(
    store: PredictionStore, name: str, kind: str, stint: datetime, now: datetime, why: str
) -> Simulation:
    """Keep that ``name``'s probation begun at ``stint`` could not be simulated, and why: it is not
    tried again, and goes on live."""
    store.add_simulation(name, stint, kind, None, now, {"why": why})
    log.info("%s: not simulated (%s)", name, why)
    return Simulation(name, kind, [], {}, why=why)


def _judge_daily(
    store: PredictionStore,
    algorithms: Sequence[DailyAlgorithm],
    judged: Sequence[DailyAlgorithm],
    kinds: Sequence[DailyKind],
    fee_rate: float,
    now: datetime,
) -> dict[str, Simulation]:
    """Each of the ``judged`` daily algorithms' review at once, applied, kind by kind; a clone
    looked for among the earlier of ``algorithms`` (the registry's order) with a status."""
    out: dict[str, Simulation] = {}
    for kind in kinds:
        names = [a.name for a in judged if a.kind == kind.name]
        if not names:
            continue
        simulated = store.simulated(kind.name, kind.answer_version)
        standings = store.statuses()
        of_kind = [a.name for a in algorithms if a.kind == kind.name and a.name in standings]

        def live(chosen: Sequence[date], special: bool, kind: DailyKind = kind) -> list[dict[str, Any]]:
            return store.kind_scored(kind.name, kind.answer_version, chosen, None, special)

        verdicts = review_at_once(
            names,
            of_kind,
            standings,
            simulated,
            live,
            min_sample=kind.min_sample,
            fee_rate=fee_rate,
            rules=kind.review_rules,
            baselines=kind.baseline_names(),
            scoring=kind_scoring(kind),
        )
        out.update(_apply(store, verdicts, kind.name, now))
    return out


def _apply(
    store: PredictionStore, verdicts: Sequence[AtOnce], kind: str, now: datetime
) -> dict[str, Simulation]:
    """Make each verdict's change (from probation, as read: a command may have landed meanwhile)
    and keep it with its simulation."""
    out: dict[str, Simulation] = {}
    records = store.simulations()
    for v in verdicts:
        status = Status.PROBATION
        c = v.change
        if c is not None and store.change_status(
            c.algorithm, now, c.previous, c.status, c.reason, simulated=True
        ):
            status = c.status
            log.info("%s: %s -> %s (%s)", c.algorithm, c.previous.value, c.status.value, c.reason)
        record = records[v.algorithm]
        review = _verdict_json(v, status)
        store.add_simulation(
            v.algorithm, record.stint, kind, record.run, record.made_at, {**record.detail, "review": review}
        )
        out[v.algorithm] = Simulation(
            v.algorithm, kind, list(record.days), dict(record.detail.get("training", {})), v, status=status
        )
    return out


def _verdict_json(v: AtOnce, status: Status) -> dict[str, Any]:
    weakest = None
    if v.weakest is not None:
        baseline, lift = v.weakest
        weakest = {"baseline": baseline, "n": lift.n, "value": lift.value, "low": lift.low, "high": lift.high}
    return {
        "days": v.days,
        "review_day": v.review_day,
        "scored": v.scored,
        "enough": v.enough,
        "weakest": weakest,
        "status": status.value,
        "reason": None if v.change is None else v.change.reason,
    }


def report(run: SimulationRun, level: float) -> list[str]:
    """The command's lines: what was enrolled, then each simulation and its review at once
    (counts, days and lifts only: no market value)."""
    lines = [f"enrolled on probation (pre-validation passed): {name}" for name in run.enrolled]
    if run.recorded_days is not None and run.recorded_days < MIN_DAYS:
        lines.append(
            f"{run.recorded_days} recorded days, fewer than {MIN_DAYS}: no simulation;"
            " the probations go on live"
        )
    elif not run.simulations:
        lines.append("no probation waits for its simulation")
    for s in run.simulations:
        if not s.days:
            lines.append(f"{s.algorithm} ({s.kind}): not simulated - {s.why}; its probation goes on live")
            continue
        how = (
            f"refitted before each day on the days before it ({len(s.training.get('models', {}))} models)"
            if s.training.get("cadence") == DAILY
            else _once(s.training)
        )
        lines.append(
            f"{s.algorithm} ({s.kind}): simulated {s.days[0]}..{s.days[-1]}, {len(s.days)} days, {how}"
        )
        v = s.verdict
        if v is None:
            lines.append("  no review: its status changed meanwhile")
            continue
        if v.weakest is None:
            lift = "no lift by trading day"
        else:
            baseline, by_day = v.weakest
            low = "-" if by_day.low is None else f"{by_day.low:.4f}"
            lift = (
                f"weakest lift by trading day vs {baseline} {by_day.value:.4f}"
                f" (low {low} at {level:.1%}, {by_day.n} days)"
            )
        lines.append(f"  {v.scored} scored, {'enough' if v.enough else 'too few'} to decide; {lift}")
        if v.review_day is None:
            lines.append("  below the first review day: not judged")
        elif s.status is Status.ACTIVE:
            lines.append(f"  review of day {v.review_day}: passed, active from now")
        elif s.status is Status.CLONE:
            lines.append(f"  a clone: {v.change.reason if v.change else ''}")
        else:
            lines.append(
                f"  review of day {v.review_day}: not passed, stays on probation;"
                f" live days count on from day {v.days + 1}"
            )
    return lines


def _once(training: Mapping[str, Any]) -> str:
    """A bar-close replay's training, in words (#298: trained once before the days)."""
    days = training.get("days") or {}
    sources = ", ".join(f"{n} {source}" for source, n in sorted(days.items())) or "no day"
    return (
        f"trained once before them ({training.get('model')}: {training.get('first_day')}.."
        f"{training.get('last_day')}, {sources})"
    )


@dataclass(frozen=True)
class _Candidate:
    """A bar-close algorithm whose probation waits for its simulation, with its once-trained model."""

    algorithm: Algorithm | KindAlgorithm
    kind: Kind | None  # None: the direction
    stint: datetime
    model: Model
    window: list[date]  # the days before the replayed ones it was trained on
    recorded: int  # of them, the recorded ones (the others backfilled)


async def simulate_bar_close(
    settings: Settings,
    *,
    algorithms: Sequence[Algorithm] = ALGORITHMS,
    kind_algorithms: Sequence[KindAlgorithm] = KIND_ALGORITHMS,
    kinds: Sequence[Kind] = KINDS,
    now: datetime | None = None,
) -> SimulationRun:
    """Simulate every bar-close probation that waits for it, all in one replay of the latest
    recorded days (module doc), and judge each at once. With fewer than ``MIN_DAYS`` recorded days
    nothing is simulated (``SimulationRun.days`` says how many there are). Raises
    ``SimulationRunning`` while another runs."""
    now = now or datetime.now(KST)
    register(algorithms)
    register_kind_algorithms(kind_algorithms, kinds, taken={a.name for a in algorithms})
    with _held(settings.state_dir, BAR_CLOSE):
        store = PredictionStore(settings.state_dir / DB_NAME)
        try:
            everyone: list[Algorithm | KindAlgorithm] = [*algorithms, *kind_algorithms]
            waiting = store.to_simulate([a.name for a in everyone])
            available = finished_days(settings.data_dir, settings.symbols, now)
            recorded = [d for d in available if is_recorded(settings.data_dir, d, settings.symbols)]
            if not waiting or len(recorded) < MIN_DAYS:
                return SimulationRun(recorded_days=len(recorded))
            days = recorded[-BAR_CLOSE_DAYS:]
            calendar = calendar_store(settings).calendar
            leaders = Leaders.of(settings)
            standings = store.statuses()
            done: list[Simulation] = []
            ready: list[_Candidate] = []
            for a in everyone:
                if a.name not in waiting:
                    continue
                kind = (
                    None
                    if kind_of(a) == DIRECTION
                    else next((k for k in kinds if k.name == kind_of(a)), None)
                )
                stint = standings[a.name].since
                if kind_of(a) != DIRECTION and kind is None:
                    continue
                trainer = _WalkForward(settings, a, available, calendar, leaders, kind)
                model = trainer.before(days[0])
                target = settings.target_version if kind is None else kind.answer_version
                if model.target_version != target:
                    why = f"nothing to learn before {days[0]} (its defaults would predict, which never count)"
                    done.append(_not_simulated(store, a.name, kind_of(a), stint, now, why))
                    continue
                window = [d for d in available if d < days[0]][-a.training_days :]
                held = sum(1 for d in window if d in set(recorded))
                ready.append(_Candidate(a, kind, stint, model, window, held))
            if ready:
                done += await _replay_bar_close(store, settings, ready, available, days, calendar, now)
                ruled = _judge_bar_close(store, settings, everyone, ready, kinds, now)
                done = [ruled.get(s.algorithm, s) for s in done]
            return SimulationRun(simulations=done, recorded_days=len(recorded))
        finally:
            store.close()


async def _replay_bar_close(
    store: PredictionStore,
    settings: Settings,
    ready: Sequence[_Candidate],
    available: Sequence[date],
    days: Sequence[date],
    calendar: MarketCalendar,
    now: datetime,
) -> list[Simulation]:
    """Replay ``days`` with the ``ready`` algorithms on their once-trained models and their kinds'
    baselines, and keep what they predicted, each with its answer."""
    db = settings.state_dir / "simulation-bar-close.sqlite3"
    for leftover in sqlite_files(db):
        leftover.unlink(missing_ok=True)
    direction = [cast(Algorithm, c.algorithm) for c in ready if c.kind is None]
    of_kinds = [cast(KindAlgorithm, c.algorithm) for c in ready if c.kind is not None]
    kinds = list({c.kind.name: c.kind for c in ready if c.kind is not None}.values())
    leaders = Leaders.of(settings)
    # The baselines' tables, refitted before each day on its days before (the candidates' trainers,
    # with their samples, are let go: a fresh one reads the answers only).
    tables_of = [
        _WalkForward(
            settings, next(c.algorithm for c in ready if c.kind is k), available, calendar, leaders, k
        )
        for k in kinds
    ]
    climate_of = _WalkForward(settings, ready[0].algorithm, available, calendar, leaders)
    models = {c.algorithm.name: c.model for c in ready}
    for day in days:
        tables: dict[str, Model] = {}
        for trainer in tables_of:
            tables.update(trainer.tables_before(day))
        climate = climate_of.climate_before(day) if settings.target_version >= 2 else None
        events = recorded_events(settings.data_dir, day, settings.symbols, leaders)
        engine = await replay_recorded(
            settings,
            day,
            events,
            db=db,
            models={**tables, **models},
            algorithms=direction,
            calendar=calendar,
            climate=climate,
            kinds=kinds,
            kind_algorithms=of_kinds,
        )
        engine.store.close()
        log.info("simulated %s with %s", day, ", ".join(models))
    run = store.add_simulation_run(now, BAR_CLOSE, days)
    replayed = PredictionStore(db)
    try:
        if direction:
            names = {a.name for a in direction} | {b.name for b in baselines_for(settings.target_version)}
            rows = [r for r in replayed.made_on(days) if r["algorithm"] in names]
            store.add_sim_predictions(run, rows, TARGET_VERSION)
        for kind in kinds:
            names = {c.algorithm.name for c in ready if c.kind is kind} | {b.name for b in kind.baselines}
            kept = [
                _sim_kind_row(r)
                for r in replayed.kind_made_on(kind.name, kind.answer_version, days)
                if r["algorithm"] in names
            ]
            store.add_sim_kind_predictions(run, kind.name, kind.answer_version, kept)
    finally:
        replayed.close()
    out: list[Simulation] = []
    for c in ready:
        training = _training_json(c)
        store.add_simulation(
            c.algorithm.name, c.stint, kind_of(c.algorithm), run, now, {"training": training}
        )
        out.append(Simulation(c.algorithm.name, kind_of(c.algorithm), list(days), training))
    return out


def _sim_kind_row(row: Mapping[str, Any]) -> SimKindPrediction:
    """A replayed prediction of a kind (``kind_made_on``'s row), with its answer, to keep."""
    answer = None
    if row.get("answer_version") is not None:
        answer = Answer(row["answer_class"], row["answer_unscorable"], row.get("answer_path") or {})
    return SimKindPrediction(
        symbol=row["symbol"],
        algorithm=row["algorithm"],
        model_version=row["model_version"],
        model_target=row.get("model_target"),
        bar_close=datetime.fromisoformat(row["bar_close"]),
        output=row.get("output"),
        answer=answer,
        special=bool(row.get("answer_special")),
        status=row["status"],
        flat_ticks=row.get("flat_ticks"),
        width_ticks=row.get("width_ticks"),
        anchor=row.get("anchor"),
        tick=row.get("tick"),
        band=row.get("band"),
    )


def _training_json(candidate: _Candidate) -> dict[str, Any]:
    """How a bar-close algorithm was trained for its simulation: once, on its ``training_days``
    before the replayed days, by where their labels came from (#165: ``recorded``, ``bars``)."""
    model, window = candidate.model, candidate.window
    days = {"recorded": candidate.recorded, "bars": len(window) - candidate.recorded}
    return {
        "cadence": BAR_CLOSE,
        "model": model.version,
        "target_version": model.target_version,
        "first_day": window[0].isoformat() if window else None,
        "last_day": window[-1].isoformat() if window else None,
        "samples": None if model.training is None else model.training.samples,
        "days": {source: n for source, n in days.items() if n},
    }


def _judge_bar_close(
    store: PredictionStore,
    settings: Settings,
    everyone: Sequence[Algorithm | KindAlgorithm],
    ready: Sequence[_Candidate],
    kinds: Sequence[Kind],
    now: datetime,
) -> dict[str, Simulation]:
    """Each simulated bar-close algorithm's review at once, applied, kind by kind (the direction
    first); a clone looked for among the earlier algorithms of its kind with a status, in the
    registries' order, on their live predictions of the simulated days."""
    out: dict[str, Simulation] = {}
    rules = Rules.of(settings)
    standings = store.statuses()
    for kind in [None, *kinds]:
        names = [c.algorithm.name for c in ready if c.kind is kind]
        if not names:
            continue
        kind_name = DIRECTION if kind is None else kind.name
        of_kind = [a.name for a in everyone if kind_of(a) == kind_name and a.name in standings]
        simulated = store.simulated(kind_name, TARGET_VERSION if kind is None else kind.answer_version)
        if kind is None:
            baselines = frozenset(b.name for b in baselines_for(settings.target_version))
            scoring = DIRECTION_SCORING
        else:
            baselines = frozenset(b.name for b in kind.baselines)
            scoring = kind_scoring(kind)
        live = _live_rows(store, kind)
        verdicts = review_at_once(
            names,
            of_kind,
            standings,
            simulated,
            live,
            min_sample=settings.min_sample,
            fee_rate=settings.fee_rate,
            rules=rules,
            baselines=baselines,
            scoring=scoring,
        )
        out.update(_apply(store, verdicts, kind_name, now))
    return out


def _live_rows(store: PredictionStore, kind: Kind | None) -> Rows:
    """The live scored predictions of ``kind`` (None: the direction's), as the reviews read them."""
    if kind is None:
        return lambda chosen, special: store.scored_predictions(chosen, None, special)
    name, version = kind.name, kind.answer_version
    return lambda chosen, special: store.kind_scored(name, version, chosen, None, special)
