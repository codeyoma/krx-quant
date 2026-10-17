"""Nightly training: each algorithm retrained on the latest days, promoted only when it
beats the model the server runs on days it did not learn from.

A night trains on the latest trading day with data in the Object Store: backfilled bars
or recorded events. A recorded day trains on what the engine saw, with the mid-point as
the reference price, as live scoring has it. A day with backfilled bars only trains on
closes (``training``). For each algorithm, in registration order:

1. The last 3 trading days are kept for validation; the algorithm trains on its own
   rolling window (``training_days``) before them (walk-forward).
2. The new model (challenger) and the one the server runs (champion: ``live.json``, else
   the defaults) both forecast the validation days. The challenger is promoted only if
   its mean log loss there is lower, or whatever its loss when the champion learned
   another target than the night's (#170: ``Model.target_version``; the defaults and a
   model saved before it was recorded count as the four-minute move's): after the target
   changes, every algorithm the night trains runs a model of the new one the next morning.
3. The challenger and a report of both are stored. Besides the verdict, the report has
   what tells a real improvement from noise (#89): the losses by validation day and by
   reference price, the paired difference with its standard error over days, whether
   the champion learned the validation days, and the code that trained (``KRX_COMMIT``).
   The rule stays the mean loss alone. The report is the algorithm's
   checkpoint: a night run again (after a crash, or a reclaimed Spot instance) skips the
   algorithms it has.

Once every algorithm is done the promotion list is stored, final: the algorithms whose
challenger won, by version, and every report. The server applies it at 08:00 (``swap``).

With the touch target (``KRX_TARGET_VERSION=3``, 2 before #187; #169) the samples' outcomes are its
labels (``training``): recorded days by their quotes, the others by the one-minute bar
rule. Each report then counts the labelled samples by source (``labels``), and so does the
Telegram summary. The climate baseline's table (``climate.py``) is refitted on the latest
``CLIMATE_DAYS`` days after the algorithms, with a report of its own, and always promoted.

The algorithms of the kinds besides the direction (#179, ``kinds.py``) are trained after
those, the same way on their kind's answers (``training.TrainingDay.of_kind``): the log loss
of the kind's three classes, a champion of another answer version than the kind's replaced
whatever the scores. Their reports name the kind (``kind``) and its answer version
(``target_version``), and count the kind's labelled samples by source; the Telegram summary
has a line of those per kind. They also count the bar closes the kind's training left out, by
why (#184): a VI or a special situation on a recorded day (no sample), an answer without a
class (the bar rule's ``same minute``, an unscorable window) or no answer; the summary's line
adds their total and reasons.

A kind's baselines that predict from a table (#180, ``kinds.TableBaseline``: the range's
climate) are refitted after the direction's climate, each on the kind's answers of its
latest ``days`` days, with a report of its own (naming the kind), and always promoted, as
the direction's climate table is.

Objects:

- ``bars/...``: archived one-minute bars, and ``events/...``: recorded events of the feeds
  the engine reads, both read (copied into a local cache once; days no night needs any
  more are removed from it);
- ``models/<algorithm>/<version>.json``: each challenger, and the models the server runs;
- ``live.json``: the version each algorithm runs on the server, published by it;
- ``reports/<day>/<algorithm>.json``: one algorithm's night;
- ``promotions/<day>.json``: the night's promotion list.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import shutil
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from functools import partial
from pathlib import Path
from typing import Any, TypeVar

from krx_quant.algorithms import ALGORITHMS, KIND_ALGORITHMS, Algorithm, KindAlgorithm, Model
from krx_quant.algorithms.contract import KindSample, Sample, kind_predicted, predicted
from krx_quant.algorithms.forecasts import LOSS_FLOOR, forecasting, log_loss
from krx_quant.backfill import MINUTE_BARS_TR, bars_key
from krx_quant.climate import CLIMATE, CLIMATE_DAYS, fit_climate
from krx_quant.config import Settings, SettingsError
from krx_quant.engine import ENGINE_FEEDS, partitions
from krx_quant.features import Leaders
from krx_quant.feeds import TRADE
from krx_quant.feeds.base import KST, kst_day
from krx_quant.kind_registry import KINDS, kind_named
from krx_quant.kinds import NO_ANSWER, Kind, KindForecast, TableBaseline, table_baselines
from krx_quant.macro import regime_prefix
from krx_quant.market_calendar import MarketCalendar, calendar_store
from krx_quant.notify import send_notice
from krx_quant.object_store import ObjectStore, file_digest, get_json, open_store, put_json
from krx_quant.touch import TARGET_VERSION
from krx_quant.training import TrainingDay, leader_series, previous_days_needed, training_day

log = logging.getLogger(__name__)

VALIDATION_DAYS = 3
_S = TypeVar("_S", Sample, KindSample)  # a direction's sample or a kind's (#179)
LIVE = "live.json"


def model_key(name: str, version: str) -> str:
    return f"models/{name}/{version}.json"


def report_key(day: date, name: str) -> str:
    return f"reports/{day.isoformat()}/{name}.json"


def promotions_key(day: date) -> str:
    return f"promotions/{day.isoformat()}.json"


# What a night trained on another machine (EC2 Spot, #23) needs from the server: the
# settings training reads, as the server's environment has them, and its market calendar.
TRAINING_SETTINGS_KEY = "training/settings.json"
TRAINING_CALENDAR_KEY = "training/market_calendar.json"


def training_settings(settings: Settings) -> dict[str, str]:
    """The settings a night's training reads, as environment values (none of them secret)."""
    return {
        "KRX_SYMBOLS": ",".join(settings.symbols),
        "KRX_ETF_SYMBOLS": ",".join(settings.etf_symbols),
        "KRX_US_SYMBOLS": ",".join(settings.us_symbols),
        "KRX_INDEX_CODES": ",".join(settings.index_codes),
        "KRX_FEE_PERCENT": f"{settings.fee_rate * 100:.10g}",
        "KRX_TARGET_VERSION": str(settings.target_version),
    }


def from_the_server(settings: Settings) -> Settings:
    """``settings`` with what the server published for the night's training: its
    settings, over this machine's, and its market calendar, into this state directory."""
    store = open_store(settings)
    if store is None:
        raise SettingsError("KRX_OBJECT_STORE is not set: nightly training reads and writes it")
    published = get_json(store, TRAINING_SETTINGS_KEY)
    if published is None:
        raise SettingsError(
            f"no {TRAINING_SETTINGS_KEY} in {store.name}: the server publishes it (KRX_TRAIN_ON=spot)"
        )
    if store.digest(TRAINING_CALENDAR_KEY) is not None:
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        store.get(TRAINING_CALENDAR_KEY, settings.state_dir / "market_calendar.json")
    # Over the environment and this machine's .env, as the settings were read.
    return Settings.from_env({**os.environ, **published}, require_kis=False)


@dataclass(frozen=True)
class Night:
    day: date
    finalized_at: str
    promoted: dict[str, str]  # algorithm -> the version to run from the next morning
    reports: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "finalized_at": self.finalized_at,
            "promoted": self.promoted,
            "reports": self.reports,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Night:
        return cls(date.fromisoformat(data["day"]), data["finalized_at"], data["promoted"], data["reports"])

    def summary(self) -> str:
        """The days' reference prices, the touch target's labelled samples by source (of the
        target version its reports name: 3, 2 before #187), each kind's answered samples by
        source and those it left out (#184), and one line per algorithm, for Telegram."""
        references: dict[str, str] = {}
        labelled: dict[str, int] = {}
        target = TARGET_VERSION
        kinds: dict[str, dict[str, int]] = {}  # the kinds' labelled samples by day (#179)
        dropped: dict[str, dict[str, dict[str, int]]] = {}  # the kinds' left out, by day and why (#184)
        for r in self.reports:
            references.update(r.get("reference_prices", {}))
            if "kind" in r:
                kinds.setdefault(r["kind"], {}).update(r.get("labels", {}).get("by_day", {}))
                if "dropped_by_day" in r.get("labels", {}):
                    dropped.setdefault(r["kind"], {}).update(r["labels"]["dropped_by_day"])
            else:
                labelled.update(r.get("labels", {}).get("by_day", {}))
                target = r.get("target_version", target)
        title = f"{self.day} 밤 학습"
        if references:
            recorded = sum(kind == "mid" for kind in references.values())
            title += f" (녹화 중간가 {recorded}일, 백필 종가 {len(references) - recorded}일)"
        lines = [title]
        if labelled:
            quotes = sum(n for day, n in labelled.items() if references.get(day) == "mid")
            bars = sum(labelled.values()) - quotes
            lines.append(f"정답 버전 {target} 표본: 녹화 호가 {quotes}개, 1분봉 {bars}개")
        for kind, by_day in kinds.items():
            quotes = sum(n for day, n in by_day.items() if references.get(day) == "mid")
            line = f"{kind} 정답 표본: 녹화 호가 {quotes}개, 1분봉 {sum(by_day.values()) - quotes}개"
            if kind in dropped:  # reports before #184 do not count them
                line += _dropped_text(dropped[kind])
            lines.append(line)
        for r in self.reports:
            if r["promoted"] and r.get("forced"):
                verdict = "승격(정답 바뀜, 성적 비교 없이)"
            elif r["promoted"]:
                verdict = "승격"
            elif r["challenger"] is None:
                verdict = "학습 안 함"
            else:
                verdict = "유지"
            lines.append(f"- {r['algorithm']} {verdict}: {r['reason']}")
        return "\n".join(lines)


def train_night(
    store: ObjectStore,
    cache_dir: Path,
    *,
    symbols: Sequence[str],
    fee_rate: float,
    now: Callable[[], datetime],
    algorithms: Sequence[Algorithm] = ALGORITHMS,
    leaders: Leaders | None = None,
    day: date | None = None,
    calendar: MarketCalendar | None = None,
    commit: str = "unknown",
    target_version: int = 1,
    kind_algorithms: Sequence[KindAlgorithm] = KIND_ALGORITHMS,
    kinds: Sequence[Kind] = KINDS,
) -> Night:
    """Train every algorithm for the night of ``day`` (default: the latest day with data) and
    store the promotion list. A night already finished is returned as it was. Each report
    names ``commit``, the code that trained it. The samples' outcomes are of
    ``target_version``; with the touch target (3) the climate baseline is refitted too, and
    so are the tables of the ``kinds``' baselines (#180). The algorithms of the ``kinds``
    besides the direction train last, on their kind's answers (#179).
    """
    leaders = leaders or Leaders()
    bars, trades = (
        _archived(store, MINUTE_BARS_TR, "bars", symbols),
        _archived(store, TRADE.tr_id, "events", symbols),
    )
    seen = sorted(set().union(*bars.values(), *trades.values()))
    if day is not None:
        seen = [d for d in seen if d <= day]
    if not seen or (day is not None and seen[-1] != day):
        raise FileNotFoundError(
            f"no archived bars or recorded trades of {', '.join(symbols)} for {day or 'any day'}"
        )
    night = seen[-1]
    if not (_every(bars, night) or _every(trades, night)):
        raise FileNotFoundError(
            f"for {night} neither every symbol's bars nor every symbol's trades are archived yet"
            f" (bars missing: {', '.join(s for s in symbols if night not in bars[s])};"
            f" trades missing: {', '.join(s for s in symbols if night not in trades[s])})"
        )
    # A day trains on its recording or its backfill, each of every symbol: never a mix.
    days = [d for d in seen if _every(bars, d) or _every(trades, d)]
    finished = get_json(store, promotions_key(night))
    if finished is not None:
        log.info("the night of %s is already trained", night)
        return Night.from_dict(finished)
    # Each kind algorithm with its kind (#179); one of a kind not registered is not trained.
    of_kinds = [(a, k) for a in kind_algorithms if (k := kind_named(a.kind, kinds)) is not None]
    # The kinds' baselines refitted every night, with their kind (#180).
    tables = table_baselines([k for k in kinds if k.answer is not None])
    trainer = _Trainer(
        store,
        cache_dir,
        symbols,
        fee_rate,
        leaders,
        days,
        algorithms,
        calendar,
        target_version,
        of_kinds,
        [k for k, _ in tables],
    )
    live: dict[str, str] = get_json(store, LIVE) or {}
    reports = []
    # (name, the target its report must be of, whether a kind's, the step)
    steps: list[tuple[str, int, bool, Callable[[], dict[str, Any]]]] = [
        (a.name, target_version, False, partial(trainer.train, a, live.get(a.name))) for a in algorithms
    ]
    if target_version >= 2:
        steps.append((CLIMATE, target_version, False, trainer.climate))
    for kind, table in tables:
        steps.append((table.name, kind.answer_version, True, partial(trainer.table, kind, table)))
    for a, kind in of_kinds:
        steps.append(
            (a.name, kind.answer_version, True, partial(trainer.train_kind, a, kind, live.get(a.name)))
        )
    for name, version, of_kind, step in steps:
        report = get_json(store, report_key(night, name))
        # A checkpoint an older version wrote lacks the evidence (#89), and one of another
        # target (#169; none written: the four-minute move, 1) labelled other outcomes:
        # trained again. A kind's is of its answer version (#179).
        if report is None or "commit" not in report or report.get("target_version", 1) != version:
            report = {**step(), "commit": commit}
            if target_version >= 2 and not of_kind:
                report["target_version"] = target_version
            put_json(store, report_key(night, name), report)
            log.info("%s: %s", name, report["reason"])
        else:
            log.info("%s: trained earlier tonight; skipped", name)
        reports.append(report)
    result = Night(
        night,
        now().isoformat(),
        {r["algorithm"]: r["challenger"]["version"] for r in reports if r["promoted"]},
        reports,
    )
    put_json(store, promotions_key(night), result.as_dict())
    trainer.prune()
    return result


async def run_training(settings: Settings, day: date | None = None) -> Night:
    """``krx-quant train``: the night's training from ``KRX_OBJECT_STORE``, with its summary
    sent to Telegram.
    """
    store = open_store(settings)
    if store is None:
        raise SettingsError("KRX_OBJECT_STORE is not set: nightly training reads and writes it")
    finished_before = day is not None and store.digest(promotions_key(day)) is not None
    night = await asyncio.to_thread(
        train_night,
        store,
        settings.state_dir / "training-cache",
        symbols=settings.symbols,
        fee_rate=settings.fee_rate,
        now=lambda: datetime.now(KST),
        leaders=Leaders.of(settings),
        day=day,
        calendar=calendar_store(settings).calendar,
        commit=settings.commit,
        target_version=settings.target_version,
    )
    if not finished_before:
        await send_notice(settings, night.summary())
    return night


class _Trainer:
    def __init__(
        self,
        store: ObjectStore,
        cache_dir: Path,
        symbols: Sequence[str],
        fee_rate: float,
        leaders: Leaders,
        days: Sequence[date],
        algorithms: Sequence[Algorithm],
        calendar: MarketCalendar | None = None,
        target_version: int = 1,
        of_kinds: Sequence[tuple[KindAlgorithm, Kind]] = (),
        tabled: Sequence[Kind] = (),
    ) -> None:
        self._store = store
        self._target_version = target_version
        # The kinds besides the direction its kind algorithms predict (#179) and its tables
        # are of (#180, ``tabled``), each day read with.
        self._kinds = tuple({k.name: k for k in [*(k for _, k in of_kinds), *tabled]}.values())
        self._cache = cache_dir
        self._symbols = tuple(symbols)
        self._fee_rate = fee_rate
        self._leaders = leaders
        self._days = list(days)
        # The days with data trade; the others as the server's calendar knows them (holidays
        # move the options expiry), or by weekday without one.
        self._calendar = MarketCalendar(
            {**(calendar.known() if calendar else {}), **dict.fromkeys(days, True)}
        )
        # Each day is read once, with every algorithm's features.
        everyone: list[Algorithm | KindAlgorithm] = [*algorithms, *(a for a, _ in of_kinds)]
        self._names = sorted({f for a in everyone for f in a.features})
        self._loaded: dict[date, TrainingDay | None] = {}
        self._used: set[date] = set()

    def train(self, algorithm: Algorithm, live_version: str | None) -> dict[str, Any]:
        """An algorithm's night: its challenger against its champion."""
        return self._train(
            algorithm,
            live_version,
            self._target_version,
            {},
            lambda loaded: (
                {"target_version": self._target_version, "labels": _labels(loaded)}
                if self._target_version >= 2
                else {}
            ),
            lambda days: self._samples(algorithm, days),
            algorithm.fit,
            partial(_losses, algorithm),
        )

    def train_kind(self, algorithm: KindAlgorithm, kind: Kind, live_version: str | None) -> dict[str, Any]:
        """A kind's algorithm's night (#179), on the kind's answers: the target is the kind's
        answer version."""
        return self._train(
            algorithm,
            live_version,
            kind.answer_version,
            {"kind": kind.name},
            lambda loaded: {"target_version": kind.answer_version, "labels": _kind_labels(loaded, kind)},
            lambda days: self._kind_samples(algorithm, kind, days),
            algorithm.fit,
            partial(_kind_losses, algorithm, kind),
        )

    def _train(
        self,
        algorithm: Algorithm | KindAlgorithm,
        live_version: str | None,
        target: int,
        heading: Mapping[str, Any],
        labels: Callable[[Mapping[date, TrainingDay | None]], dict[str, Any]],
        samples: Callable[[Sequence[date]], list[_S]],
        fit: Callable[[list[_S]], Model],
        losses: Callable[[Model, Sequence[_S]], list[tuple[_S, float]]],
    ) -> dict[str, Any]:
        """The challenger trained on ``samples`` of the training days, against the champion, both
        by ``losses`` on the validation days; the report's ``heading`` after its name, and its
        ``labels`` (the target and the labelled samples by source)."""
        night = self._days[-1]
        validation: list[date] = self._days[-VALIDATION_DAYS:]
        training: list[date] = self._days[:-VALIDATION_DAYS][-algorithm.training_days :]
        report: dict[str, Any] = {
            "algorithm": algorithm.name,
            **heading,
            "day": night.isoformat(),
            "training_days": [d.isoformat() for d in training],
            "validation_days": [d.isoformat() for d in validation],
            "champion": None,
            "challenger": None,
            "promoted": False,
        }
        if not training:
            return {**report, "reason": f"too few days ({len(self._days)})"}
        loaded = {day: self._day(day) for day in [*training, *validation]}
        report["reference_prices"] = {
            day.isoformat(): found.reference for day, found in loaded.items() if found is not None
        }
        report |= labels(loaded)
        try:
            challenger = fit(samples(training))
        except ValueError as e:
            return {**report, "reason": f"not trained: {e}"}
        self._put_model(algorithm.name, challenger)
        champion = self._champion(algorithm, live_version)
        # The bar closes it predicts: without the data it reads (the book), it declines,
        # whatever the model.
        challenger_losses = losses(challenger, samples(validation))
        scored = [s for s, _ in challenger_losses]
        report["samples"] = {"validation": len(scored)}
        # A champion of another target (#170) is replaced whatever the scores say.
        champion_target = champion.target_version or 1
        retarget = champion_target != target
        changed = (
            f"; {champion.version} learned target {champion_target}, the night target"
            f" {target}: promoted without comparing"
        )
        if not scored:
            if retarget:
                return {
                    **report,
                    "challenger": {
                        "version": challenger.version,
                        "loss": None,
                        "trained": challenger.trained,
                    },
                    "promoted": True,
                    "forced": True,
                    "reason": "no outcomes to validate on" + changed,
                }
            return {**report, "reason": "no outcomes to validate on"}
        champion_losses = losses(champion, scored)
        champion_loss = _mean([loss for _, loss in champion_losses])
        challenger_loss = _mean([loss for _, loss in challenger_losses])
        better = challenger_loss < champion_loss
        sign = "<" if better else "≥"
        # A champion fitted by hand on the latest days may have learned the validation days:
        # it is then judged on what it saw, and is harder to beat than it should be.
        saw = champion.trained_until is not None and champion.trained_until >= validation[0]
        return {
            **report,
            "champion": {"version": champion.version, "loss": champion_loss, "trained_on_validation": saw},
            "challenger": {
                "version": challenger.version,
                "loss": challenger_loss,
                "trained": challenger.trained,
            },
            "promoted": better or retarget,
            "forced": retarget,
            "validation": _validation(
                challenger_losses,
                champion_losses,
                {day: found.reference for day, found in loaded.items() if found is not None},
            ),
            "reason": f"{challenger_loss:.4f} {sign} {champion_loss:.4f} ({champion.version}"
            + (", trained on these days)" if saw else ")")
            + (changed if retarget else ""),
        }

    def climate(self) -> dict[str, Any]:
        """The climate baseline's table refitted on the latest ``CLIMATE_DAYS`` days (the night's
        included: it predicts the next day), stored and always promoted (#169)."""
        night = self._days[-1]
        days = self._days[-CLIMATE_DAYS:]
        loaded = {day: self._day(day) for day in days}
        report: dict[str, Any] = {
            "algorithm": CLIMATE,
            "day": night.isoformat(),
            "training_days": [d.isoformat() for d in days],
            "validation_days": [],
            "champion": None,
            "challenger": None,
            "promoted": False,
            "reference_prices": {
                day.isoformat(): found.reference for day, found in loaded.items() if found is not None
            },
            "target_version": self._target_version,
            "labels": _labels(loaded),
        }
        model = fit_climate([s for found in loaded.values() if found is not None for s in found.samples])
        if model.trained is None:
            return {**report, "reason": "not trained: no labelled samples"}
        self._put_model(CLIMATE, model)
        return {
            **report,
            "challenger": {"version": model.version, "trained": model.trained},
            "promoted": True,
            "reason": f"each symbol and 30-minute slot's most frequent touch label over {len(loaded)} days"
            " (installed every night)",
        }

    def table(self, kind: Kind, baseline: TableBaseline) -> dict[str, Any]:
        """A kind baseline's table refitted on the kind's answers of its latest ``days`` days
        (the night's included: it predicts the next day), stored and always promoted (#180)."""
        night = self._days[-1]
        days = self._days[-baseline.days :]
        loaded = {day: self._day(day) for day in days}
        report: dict[str, Any] = {
            "algorithm": baseline.name,
            "kind": kind.name,
            "day": night.isoformat(),
            "training_days": [d.isoformat() for d in days],
            "validation_days": [],
            "champion": None,
            "challenger": None,
            "promoted": False,
            "reference_prices": {
                day.isoformat(): found.reference for day, found in loaded.items() if found is not None
            },
            "target_version": kind.answer_version,
            "labels": _kind_labels(loaded, kind),
        }
        model = baseline.fit(
            [s for found in loaded.values() if found is not None for s in found.of_kind(kind, ())]
        )
        if model.trained is None:
            return {**report, "reason": "not trained: no answered samples"}
        self._put_model(baseline.name, model)
        return {
            **report,
            "challenger": {"version": model.version, "trained": model.trained},
            "promoted": True,
            "reason": f"each symbol and 30-minute slot's {kind.name} answers over {len(loaded)} days"
            " (installed every night)",
        }

    def _samples(self, algorithm: Algorithm, days: Sequence[date]) -> list[Sample]:
        return [s for d in days if (found := self._day(d)) is not None for s in found.of(algorithm.features)]

    def _kind_samples(self, algorithm: KindAlgorithm, kind: Kind, days: Sequence[date]) -> list[KindSample]:
        return [
            s
            for d in days
            if (found := self._day(d)) is not None
            for s in found.of_kind(kind, algorithm.features)
        ]

    def _day(self, day: date) -> TrainingDay | None:
        """A day's samples with every algorithm's features, read once from the cache."""
        if day not in self._loaded:
            self._fetch(day)
            self._loaded[day] = training_day(
                self._cache,
                day,
                self._calendar,
                self._symbols,
                self._names,
                self._fee_rate,
                self._leaders,
                target_version=self._target_version,
                kinds=self._kinds,
            )
        self._used.add(day)
        return self._loaded[day]

    def _champion(self, algorithm: Algorithm | KindAlgorithm, version: str | None) -> Model:
        default = algorithm.default_model()
        if version is not None and version != default.version:
            data = get_json(self._store, model_key(algorithm.name, version))
            if data is not None:
                return Model.from_dict(data)
            log.warning(
                "%s runs %s, which is not in the store; comparing with the defaults", algorithm.name, version
            )
        return default

    def _put_model(self, name: str, model: Model) -> None:
        put_json(self._store, model_key(name, model.version), model.as_dict(blob=True))

    def _fetch(self, day: date) -> None:
        """Copy a day's bars, the engine's recorded events and its macro regime from the store
        into the cache, where they are not there already; with a feature of the previous
        trading day's ranges (#181) or a barrier's width (#183), that day's bars of the symbols
        too, and with the slot returns (#265), the bars of the 20 trading days before.
        """
        series = [(MINUTE_BARS_TR, s) for s in self._symbols] + leader_series(self._leaders)
        keys = [bars_key(day, feed, name) for feed, name in series]
        before = day
        for _ in range(previous_days_needed(self._names, self._kinds)):  # #181, #183, #265
            before = self._calendar.previous_trading_day(before)
            keys += [bars_key(before, MINUTE_BARS_TR, s) for s in self._symbols]
        keys += _engine_events(
            self._store.keys(f"events/date={day.isoformat()}/"), self._symbols, self._leaders
        )
        keys += self._store.keys(regime_prefix(day))  # Markov's
        for key in keys:
            stored = self._store.digest(key)
            if stored is None:
                continue
            path = self._cache / key
            if path.exists() and file_digest(path) == stored:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.tmp")
            self._store.get(key, tmp)
            tmp.replace(path)

    def prune(self) -> None:
        """Remove the cached days this night did not use: older ones no night needs again. The
        days before the oldest used one that its samples read (the slot returns' 20, #265) are
        kept: the next night's oldest day reads them too."""
        if not self._used:
            return
        oldest = min(self._used)
        for _ in range(previous_days_needed(self._names, self._kinds)):  # its days before (#265)
            oldest = self._calendar.previous_trading_day(oldest)
        layers = ("bars", "events", "macro")
        for folder in [f for layer in layers for f in self._cache.glob(f"{layer}/date=*")]:
            try:
                day = date.fromisoformat(folder.name.removeprefix("date="))
            except ValueError:
                continue
            if day < oldest:
                shutil.rmtree(folder, ignore_errors=True)


def _engine_events(keys: Iterable[str], symbols: Sequence[str], leaders: Leaders) -> list[str]:
    """The recorded events among ``keys`` that the engine reads (``engine.partitions``)."""
    wanted = []
    for key in keys:
        parts = key.split("/")  # events, date=…, feed=…, symbol=…, file
        if len(parts) != 5:
            continue
        feed, symbol = parts[2].removeprefix("feed="), parts[3].removeprefix("symbol=")
        if feed not in ENGINE_FEEDS:
            continue
        read = partitions(feed, symbols, leaders)
        if read is None or symbol in read:
            wanted.append(key)
    return wanted


def _archived(store: ObjectStore, feed: str, layer: str, symbols: Sequence[str]) -> dict[str, set[date]]:
    """The days the store has ``feed`` of each of ``symbols`` for, in ``layer`` (backfilled
    ``bars``, or recorded ``events``).
    """
    days: dict[str, set[date]] = {s: set() for s in symbols}
    for key in store.keys(f"{layer}/"):
        parts = key.split("/")  # layer, date=…, feed=…, symbol=…, file
        if len(parts) != 5 or parts[2] != f"feed={feed}":
            continue
        symbol = parts[3].removeprefix("symbol=")
        if symbol in days:
            days[symbol].add(date.fromisoformat(parts[1].removeprefix("date=")))
    return days


def _every(days_of: dict[str, set[date]], day: date) -> bool:
    """Whether every symbol has ``day``."""
    return all(day in days for days in days_of.values())


def _losses(algorithm: Algorithm, model: Model, samples: Sequence[Sample]) -> list[tuple[Sample, float]]:
    """``model``'s log loss on each sample with an outcome that it predicts."""
    out = []
    for s in samples:
        if s.outcome is None:
            continue
        with forecasting(s.features):  # of the target the sample was labelled with (#169)
            f = predicted(algorithm, s.features, model)
        if f is not None:
            out.append((s, log_loss(f, s.outcome)))
    return out


def _labels(loaded: Mapping[date, TrainingDay | None]) -> dict[str, Any]:
    """The labelled samples of the days ``loaded``, by day and by where their labels came
    from (#169): ``recorded`` days by their quotes, ``bars`` days by the one-minute rule."""
    by_day = {
        day.isoformat(): sum(s.outcome is not None for s in found.samples)
        for day, found in loaded.items()
        if found is not None
    }
    recorded = sum(
        by_day[day.isoformat()]
        for day, found in loaded.items()
        if found is not None and found.reference == "mid"
    )
    return {"samples": {"recorded": recorded, "bars": sum(by_day.values()) - recorded}, "by_day": by_day}


def _kind_losses(
    algorithm: KindAlgorithm, kind: Kind, model: Model, samples: Sequence[KindSample]
) -> list[tuple[KindSample, float]]:
    """``model``'s log loss of ``kind``'s three classes on each sample with an answer that it
    predicts (#179)."""
    out = []
    for s in samples:
        if s.label is None:
            continue
        f = kind_predicted(algorithm, s.features, model)
        if f is not None:
            out.append((s, kind_log_loss(kind, f, s.label)))
    return out


def kind_log_loss(kind: Kind, forecast: KindForecast, label: str) -> float:
    """The log loss of a forecast of ``kind`` whose answer was ``label`` (``forecasts.log_loss``)."""
    return -math.log(max(forecast.probabilities[kind.index(label)], LOSS_FLOOR))


def _kind_labels(loaded: Mapping[date, TrainingDay | None], kind: Kind) -> dict[str, Any]:
    """``_labels`` of a kind's answers (#179), and the bar closes (and symbols) of the days its
    training leaves out, by why (#184), in all (``dropped``) and by day (``dropped_by_day``):
    those without a sample (a VI, a special situation: ``TrainingDay.left_out``, recorded days),
    those whose answer has no class, by its reason (the bar rule's ``same minute``, an
    unscorable window's), and those without an answer (``no answer``). With the answered ones,
    they are every bar close and symbol of the days."""
    by_day: dict[str, int] = {}
    dropped_by_day: dict[str, dict[str, int]] = {}
    for day, found in loaded.items():
        if found is None:
            continue
        answered = found.of_kind(kind, ())
        by_day[day.isoformat()] = sum(s.label is not None for s in answered)
        why = Counter({reason: n for reason, n in found.left_out.items() if n})
        why.update(
            NO_ANSWER if s.answer is None else s.answer.unscorable or NO_ANSWER
            for s in answered
            if s.label is None
        )
        dropped_by_day[day.isoformat()] = dict(sorted(why.items()))
    recorded = sum(
        by_day[day.isoformat()]
        for day, found in loaded.items()
        if found is not None and found.reference == "mid"
    )
    dropped: Counter[str] = Counter()
    for reasons in dropped_by_day.values():
        dropped.update(reasons)
    return {
        "samples": {"recorded": recorded, "bars": sum(by_day.values()) - recorded},
        "by_day": by_day,
        "dropped": dict(sorted(dropped.items())),
        "dropped_by_day": dropped_by_day,
    }


def _dropped_text(by_day: Mapping[str, Mapping[str, int]]) -> str:
    """A kind's left-out samples as the summary's line ends (#184): their total, then each
    reason's count, the most first: `` · 뺀 표본 42개 (same minute 30, special 12)``."""
    reasons: Counter[str] = Counter()
    for why in by_day.values():
        reasons.update(why)
    listed = ", ".join(f"{reason} {n}" for reason, n in sorted(reasons.items(), key=lambda r: (-r[1], r[0])))
    return f" · 뺀 표본 {sum(reasons.values())}개" + (f" ({listed})" if listed else "")


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _validation(
    challenger: Sequence[tuple[Sample | KindSample, float]],
    champion: Sequence[tuple[Sample | KindSample, float]],
    references: Mapping[date, str],
) -> dict[str, Any]:
    """The validation's losses by day and by reference price (``mid``, ``close``), and the
    paired difference (challenger minus champion, on the samples both predicted): its mean
    over the samples, and that mean's standard error clustered by day, as the days' 4-minute
    outcomes overlap (None with fewer than two days).
    """

    def key(sample: Sample | KindSample) -> tuple[str, datetime]:
        return sample.symbol, sample.bar_close

    by_champion = {key(s): loss for s, loss in champion}

    def losses(day_of: Callable[[Sample | KindSample], Any]) -> dict[str, dict[str, Any]]:
        groups: dict[Any, list[tuple[Sample | KindSample, float]]] = {}
        for sample, loss in challenger:
            groups.setdefault(day_of(sample), []).append((sample, loss))
        out = {}
        for group, rows in sorted(groups.items()):
            theirs = [by_champion[key(s)] for s, _ in rows if key(s) in by_champion]
            out[str(group)] = {
                "samples": len(rows),
                "challenger": _mean([loss for _, loss in rows]),
                "champion": _mean(theirs) if theirs else None,
            }
        return out

    paired: dict[date, list[float]] = {}
    for sample, loss in challenger:
        if key(sample) in by_champion:
            paired.setdefault(kst_day(sample.bar_close), []).append(loss - by_champion[key(sample)])
    differences = [d for day in paired.values() for d in day]
    mean = _mean(differences) if differences else None
    error = None
    if mean is not None and len(paired) > 1:
        # Each day's residuals summed, then the days' sums as independent: with days of equal
        # size, the standard error of the days' means.
        sums = [sum(d - mean for d in day) for day in paired.values()]
        error = math.sqrt(len(sums) / (len(sums) - 1) * sum(s * s for s in sums)) / len(differences)
    return {
        "by_day": losses(lambda s: kst_day(s.bar_close)),
        "by_reference": losses(lambda s: references.get(kst_day(s.bar_close))),
        "paired": {"samples": len(differences), "mean_difference": mean, "day_standard_error": error},
    }
