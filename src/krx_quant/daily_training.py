"""Training and pre-validation of the daily kinds' algorithms (#264), on the KRX day candles and
the histories kept on the server (``daily_features.DailyFiles``).

**Night.** After the bar-close algorithms (``nightly.train_night``), each daily algorithm is
fitted again on its latest ``training_days`` of every symbol, up to the night's day, and always
promoted, as the climate tables are: one more day changes a ten-year fit little, and its skill
is judged by the walk-forward below and the live statistics, not by three validation days (a
day is one sample a symbol). Its report says how it was trained and, beside it, the log loss
over the latest ``RECENT_DAYS`` (60) days of a fit on the days before them (a third each would
be ln 3 = 1.0986): a look, never a gate.

**Pre-validation** (``prevalidate_daily``, ``krx-quant prevalidate <name>``): a walk-forward
over the last ``WALK_YEARS`` (10) years of KRX days. The days are taken in order; from the
``WALK_MIN_TRAIN`` (750)-th on, every ``WALK_REFIT`` (20) days the algorithm is fitted on its
``training_days`` before them (never on a day it predicts) and predicts those 20; the kind's
baselines predict every day as they would have at 08:50 (from the days before). The forecasts
are scored on each day's answer and judged as a bar-close kind's pre-validation is, but for
one thing: at least ``KRX_MIN_SAMPLE`` scored predictions and pairs, a weakest lift over the
baselines above 0, and the weakest lift by trading day's lower bound above 0 on every run, at
95% on the first and 1 − 0.05/N on the N-th on the same days (#262). A bar-close kind's first
run on 20 recorded days does not look at that bound (its interval is wide); ten years of days
make it a fair test, and without it a model with no edge passes about as often as its weakest
lift comes out above 0 by luck. Also: at least
``WALK_MIN_DAYS`` (500) days predicted out of sample, no day's forecasts of every symbol over the
time budget (``KRX_ALGORITHM_SECONDS``), no other algorithm of the kind it moves with. No day is
special (circuit breakers and sidecars are not kept before 2026-10-12): the few in ten years are
in. The report (``state/prevalidations/<name>.json``, ``kind``, the kind's answer version) is
what the engine's gate reads (``prevalidation.admits``), with the walk-forward's folds, symbols
and years beside it.
"""

from __future__ import annotations

import json
import logging
import math
import time as timer
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from krx_quant.algorithms import DAILY_ALGORITHMS, Model, NothingToLearn
from krx_quant.algorithms.forecasts import LOSS_FLOOR
from krx_quant.config import Settings
from krx_quant.daily import DailyAlgorithm, DailyInputs, DailyKind, DailySample, DayPrices
from krx_quant.daily_features import DailyFiles, TradingDays, band_of, samples
from krx_quant.feeds.base import KST
from krx_quant.kind_registry import DAILY_KINDS, daily_kind_named
from krx_quant.kind_stats import entry as kind_entry
from krx_quant.kind_stats import leaderboard as kind_leaderboard
from krx_quant.kinds import KindForecast, KindScored
from krx_quant.market_calendar import MarketCalendar, calendar_store
from krx_quant.prevalidation import (
    Check,
    Prevalidation,
    _held,
    _record_run,
    code_digest,
    earlier_runs,
    report_path,
    seed_history,
    tried_level,
)
from krx_quant.stats import enough

log = logging.getLogger(__name__)

RECENT_DAYS = 60
WALK_YEARS = 10
WALK_MIN_TRAIN = 750
WALK_REFIT = 20
WALK_MIN_DAYS = 500
_PAST = 260  # the past answers a baseline is given: its year and more


def _first_day(last: date) -> date:
    return last - timedelta(days=round(365.25 * WALK_YEARS))


def _log_loss(kind: DailyKind, forecast: KindForecast, label: str) -> float:
    return -math.log(max(forecast.probabilities[kind.index(label)], LOSS_FLOOR))


@dataclass
class DailyTrainer:
    """The daily algorithms' night (``nightly.train_night``), from this server's files."""

    files: DailyFiles
    symbols: Sequence[str]
    fee_rate: float
    calendar: MarketCalendar | None = None

    def train(
        self, algorithm: DailyAlgorithm, kind: DailyKind, night: date
    ) -> tuple[dict[str, Any], Model | None]:
        """The night's report of ``algorithm`` and its model (None if it learned nothing)."""
        span = timedelta(days=round(algorithm.training_days * 1.5) + 120)
        found = samples(
            kind,
            self.files,
            self.symbols,
            algorithm.features,
            self.fee_rate,
            calendar=self.calendar,
            first=night - span,
            last=night,
        )
        report: dict[str, Any] = {
            "algorithm": algorithm.name,
            "kind": kind.name,
            "cadence": "daily",
            "day": night.isoformat(),
            "training_days": [],
            "validation_days": [],
            "champion": None,
            "challenger": None,
            "promoted": False,
            "target_version": kind.answer_version,
            "labels": {"samples": {"candles": sum(s.label is not None for s in found)}, "by_day": {}},
        }
        days = sorted({s.day for s in found if s.label is not None})[-algorithm.training_days :]
        if days:
            report["training_days"] = [days[0].isoformat(), days[-1].isoformat()]
        try:
            model = algorithm.fit(found)
        except NothingToLearn as e:
            return {**report, "reason": f"not trained: {e}"}, None
        report["recent"] = self._recent(algorithm, kind, found)
        return {
            **report,
            "challenger": {"version": model.version, "trained": model.trained},
            "promoted": True,
            "reason": f"refitted on the KRX days up to {night} (installed every night): {model.trained}",
        }, model

    def _recent(
        self, algorithm: DailyAlgorithm, kind: DailyKind, found: Sequence[DailySample]
    ) -> dict[str, Any] | None:
        """The latest ``RECENT_DAYS`` days' log loss of a fit on the days before them (a look,
        not a gate)."""
        days = sorted({s.day for s in found if s.label is not None})
        if len(days) <= RECENT_DAYS:
            return None
        cut = days[-RECENT_DAYS]
        try:
            model = algorithm.fit([s for s in found if s.day < cut])
        except NothingToLearn:
            return None
        recent = [s for s in found if s.day >= cut and s.label is not None]
        losses = [_log_loss(kind, algorithm.predict(s.features, model), s.label) for s in recent]  # type: ignore[arg-type]
        return {
            "days": RECENT_DAYS,
            "first_day": cut.isoformat(),
            "samples": len(recent),
            "loss": sum(losses) / len(losses),
            "model": model.version,
        }


@dataclass(frozen=True)
class WalkForward:
    """A walk-forward's forecasts and what they were made with."""

    records: dict[str, list[KindScored]]  # by predictor: the algorithm's and the baselines'
    days: list[date]  # predicted out of sample, oldest first
    models: dict[date, str]  # each fold's first day -> the model it predicted with
    folds: int
    slowest: float  # the slowest day's forecasts of every symbol, seconds


def walk_forward(
    algorithm: DailyAlgorithm,
    kind: DailyKind,
    files: DailyFiles,
    symbols: Sequence[str],
    fee_rate: float,
    *,
    calendar: MarketCalendar | None = None,
    last: date | None = None,
) -> WalkForward:
    """The walk-forward (module doc), over the last ``WALK_YEARS`` years up to ``last``."""
    newest = last or max((d.day for s in symbols for d in files.days(s)), default=None)
    if newest is None:
        return WalkForward({}, [], {}, 0, 0.0)
    found = samples(
        kind,
        files,
        symbols,
        algorithm.features,
        fee_rate,
        calendar=calendar,
        first=_first_day(newest),
        last=newest,
    )
    by_day: dict[date, list[DailySample]] = defaultdict(list)
    for s in found:
        by_day[s.day].append(s)
    days = sorted(by_day)
    records: dict[str, list[KindScored]] = defaultdict(list)
    models: dict[date, str] = {}
    predicted: list[date] = []
    slowest = 0.0
    folds = 0
    for start in range(WALK_MIN_TRAIN, len(days), WALK_REFIT):
        window = days[max(0, start - algorithm.training_days) : start]
        training = [s for d in window for s in by_day[d]]
        try:
            model = algorithm.fit(training)
        except NothingToLearn:
            model = algorithm.default_model()
        folds += 1
        models[days[start]] = model.version
        for day in days[start : start + WALK_REFIT]:
            began = timer.monotonic()
            forecasts = [(s, algorithm.predict(s.features, model)) for s in by_day[day]]
            slowest = max(slowest, timer.monotonic() - began)
            predicted.append(day)
            for s, forecast in forecasts:
                if s.answer is not None and s.label is not None:
                    records[algorithm.name].append(_scored(kind, algorithm.name, model.version, s, forecast))
    first_predicted = predicted[0] if predicted else None
    for name, scored in _baselines(kind, files, symbols, fee_rate, first_predicted, newest).items():
        records[name] = scored
    return WalkForward(dict(records), predicted, models, folds, slowest)


def _scored(
    kind: DailyKind, name: str, version: str, sample: DailySample, forecast: KindForecast
) -> KindScored:
    assert sample.answer is not None and sample.label is not None
    return KindScored(
        algorithm=name,
        model_version=version,
        symbol=sample.symbol,
        bar_close=kind.predicted_at(sample.day),
        probabilities=forecast.probabilities,
        outcome=kind.index(sample.label),
        output=kind.output(forecast),
        path=dict(sample.answer.path),
    )


def _baselines(
    kind: DailyKind,
    files: DailyFiles,
    symbols: Sequence[str],
    fee_rate: float,
    first: date | None,
    last: date,
) -> dict[str, list[KindScored]]:
    """The kind's baselines' forecasts of every day from ``first`` to ``last``, as at 08:50."""
    out: dict[str, list[KindScored]] = {b.name: [] for b in kind.daily_baselines}
    if first is None or kind.answers is None:
        return out
    stored = {s: [d for d in files.days(s) if d.day <= last] for s in symbols}
    market = TradingDays(d.day for days in stored.values() for d in days)
    for symbol in symbols:
        days: list[DayPrices] = stored[symbol]
        answers = kind.answers(days, fee_rate)
        for i, day in enumerate(days):
            answer = answers[i]
            if day.day < first or answer.label is None:
                continue
            past = tuple((days[j].day, answers[j].label) for j in range(max(0, i - _PAST), i))
            # The market's previous trading day, as live (a symbol's missing candle stays missing).
            inputs = DailyInputs(
                symbol, day.day, kind.predicted_at(day.day), past, band_of(answer), market.previous(day.day)
            )
            for b in kind.daily_baselines:
                forecast = b.predict(inputs)
                if forecast is None:
                    continue
                sample = DailySample(symbol, day.day, _empty(), answer, kind.answer_version)
                out[b.name].append(_scored(kind, b.name, b.version, sample, forecast))
    return out


def _empty() -> Any:
    from krx_quant.features import FeatureVector

    return FeatureVector({}, ())


def _checks(
    algorithm: DailyAlgorithm,
    run: WalkForward,
    stats: Any,
    min_sample: int,
    seconds: float,
    tries: int,
    strict_low: float | None,
) -> list[Check]:
    return [
        Check(
            "days",
            len(run.days) >= WALK_MIN_DAYS,
            f"{len(run.days)} trading days predicted out of sample in {run.folds} folds"
            f" (at least {WALK_MIN_DAYS})",
        ),
        Check(
            "features", True, f"its own: {', '.join(algorithm.card.unique_features)}; no other has them first"
        ),
        Check(
            "budget",
            run.slowest <= seconds,
            f"the slowest day's forecasts of every symbol took {run.slowest:.3f}s (at most {seconds:g}s)",
        ),
        Check("correlation", True, "no other algorithm of its kind to measure against"),
        _stats_daily(stats, min_sample, tries, strict_low),
    ]


def _stats_daily(stats: Any, min_sample: int, tries: int, strict_low: float | None) -> Check:
    """Enough scored predictions and pairs, the weakest lift above 0, and its lower bound by
    trading day above 0 at ``tried_level(tries)`` (95% on a first run): module doc."""
    weakest = stats.weakest_lift
    if not enough(stats, min_sample) or weakest is None:
        pairs = min((t.lift.n for t in stats.lifts.values()), default=0)
        return Check(
            "stats",
            False,
            f"too few to judge: {stats.summary.n} scored, {pairs} pairs (at least {min_sample})",
        )
    detail = (
        f"weakest lift {weakest:.4f} over {stats.summary.n} scored predictions; run {tries} on these days,"
        f" by trading day at {tried_level(tries):.2%}: "
    )
    if strict_low is None:
        return Check("stats", False, detail + "no lift by trading day (fewer than two days paired)")
    return Check(
        "stats",
        weakest > 0 and strict_low > 0,
        detail + f"weakest lower bound {strict_low:.4f} (above 0 to pass)",
    )


def _breakdown(kind: DailyKind, records: Sequence[KindScored]) -> dict[str, Any]:
    """The algorithm's accuracy by symbol and by year."""

    def share(group: Sequence[KindScored]) -> dict[str, Any]:
        return {"n": len(group), "accuracy": sum(r.correct for r in group) / len(group) if group else None}

    by_symbol: dict[str, list[KindScored]] = defaultdict(list)
    by_year: dict[str, list[KindScored]] = defaultdict(list)
    for r in records:
        by_symbol[r.symbol].append(r)
        by_year[str(r.bar_close.year)].append(r)
    return {
        "by_symbol": {s: share(g) for s, g in sorted(by_symbol.items())},
        "by_year": {y: share(g) for y, g in sorted(by_year.items())},
    }


def daily_algorithm(name: str) -> tuple[DailyAlgorithm, DailyKind] | None:
    """A registered daily algorithm and its kind, by name."""
    found = next((a for a in DAILY_ALGORITHMS if a.name == name), None)
    kind = None if found is None else daily_kind_named(found.kind)
    return None if found is None or kind is None else (found, kind)


def prevalidate_daily(
    settings: Settings,
    name: str,
    *,
    algorithms: Sequence[DailyAlgorithm] = DAILY_ALGORITHMS,
    kinds: Sequence[DailyKind] = DAILY_KINDS,
    now: datetime | None = None,
) -> Prevalidation:
    """Walk ``name`` forward over the KRX days and save its report (module doc). Raises
    ValueError for a name not registered, FileNotFoundError without KRX day candles,
    ``PrevalidationRunning`` while another runs."""
    algorithm = next((a for a in algorithms if a.name == name), None)
    kind = None if algorithm is None else next((k for k in kinds if k.name == algorithm.kind), None)
    if algorithm is None or kind is None:
        raise ValueError(
            f"unknown daily algorithm {name!r}; registered: {', '.join(a.name for a in algorithms)}"
        )
    with _held(settings.state_dir, name):
        now = now or datetime.now(KST)
        files = DailyFiles(settings.data_dir, settings.state_dir)
        if not any(files.days(s) for s in settings.symbols):
            raise FileNotFoundError(
                f"no KRX day candles of {', '.join(settings.symbols)} in {settings.data_dir}"
            )
        calendar = calendar_store(settings).calendar
        run = walk_forward(algorithm, kind, files, settings.symbols, settings.fee_rate, calendar=calendar)
        mine = run.records.get(name, [])
        baselines = {b.name: run.records.get(b.name, []) for b in kind.daily_baselines}
        seed_history(settings.state_dir, name)
        before, on_days = earlier_runs(settings.state_dir, name, run.days)
        tries = on_days + 1
        board = kind_leaderboard({name: mine}, baselines, settings.min_sample, kind)
        stats = next(e for e in [*board.ranked, *board.unranked] if e.algorithm == name)
        strict = kind_entry(name, mine, baselines, kind, tried_level(tries)).day_low
        result = Prevalidation(
            algorithm=name,
            code=code_digest(algorithm),
            prevalidated_at=now,
            days=list(run.days),
            models=dict(run.models),
            checks=_checks(
                algorithm, run, stats, settings.min_sample, settings.algorithm_seconds, tries, strict
            ),
            stats=stats,
            correlations={},
            statuses={"ok": len(mine)},
            skipped_for_cpu=0,
            db=None,
            target_version=kind.answer_version,
            kind=kind.name,
            walk_forward={
                "folds": run.folds,
                "refit_every": WALK_REFIT,
                "min_train_days": WALK_MIN_TRAIN,
                "training_days": algorithm.training_days,
                "years": WALK_YEARS,
                **_breakdown(kind, mine),
            },
        )
        result = replace(result, earlier_runs=before, earlier_on_these_days=on_days)
        path = report_path(settings.state_dir, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
        _record_run(settings.state_dir, result)
        tmp.replace(path)
        return result


@dataclass(frozen=True)
class FittedDaily:
    model: Model
    path: Path
    samples: int


def fit_daily(settings: Settings, name: str) -> FittedDaily:
    """``krx-quant fit <name>`` of a daily algorithm: fitted on its latest days up to the last
    stored KRX day, saved for the engine (which switches to it within a minute)."""
    from krx_quant.algorithms import save_model

    found = daily_algorithm(name)
    if found is None:
        raise ValueError(f"unknown daily algorithm {name!r}")
    algorithm, kind = found
    files = DailyFiles(settings.data_dir, settings.state_dir)
    newest = max((d.day for s in settings.symbols for d in files.days(s)), default=None)
    if newest is None:
        raise FileNotFoundError(f"no KRX day candles in {settings.data_dir}")
    trainer = DailyTrainer(files, settings.symbols, settings.fee_rate, calendar_store(settings).calendar)
    report, model = trainer.train(algorithm, kind, newest)
    if model is None:
        raise ValueError(report["reason"])
    return FittedDaily(
        model, save_model(settings.state_dir, name, model), report["labels"]["samples"]["candles"]
    )


def recent_json(report: Mapping[str, Any]) -> str:
    """The night report's recent look in a line, for the Telegram summary."""
    recent = report.get("recent")
    if not recent:
        return ""
    return f" · 최근 {recent['days']}일 표본 밖 log loss {recent['loss']:.4f}"
