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
from typing import Any

from krx_quant.algorithms import DAILY_ALGORITHMS, Model, NothingToLearn
from krx_quant.config import Settings
from krx_quant.daily import DailyAlgorithm, DailyKind, DailySample
from krx_quant.daily_features import DailyFiles, samples
from krx_quant.daily_training import baseline_forecasts, training_first
from krx_quant.feeds.base import KST
from krx_quant.kind_registry import DAILY_KINDS
from krx_quant.kind_stats import kind_scoring
from krx_quant.kinds import Answer
from krx_quant.lifecycle import REGISTERED, AtOnce, Status, review_at_once
from krx_quant.market_calendar import MarketCalendar, calendar_store
from krx_quant.predictions import DB_NAME, PredictionStore, SimKindPrediction
from krx_quant.prevalidation import admits

log = logging.getLogger(__name__)

DAILY_DAYS = 20  # a daily kind's simulated days: its first review day (``daily.DAILY_RULES``)
DAILY, BAR_CLOSE = "daily", "bar_close"  # how a replay trains (``simulation_runs.cadence``)


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
    if not run.simulations:
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
    model = training.get("model")
    until = training.get("trained_until")
    return f"trained once ({model}, data up to {until})"
