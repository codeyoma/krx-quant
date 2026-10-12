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
   its mean log loss there is lower.
3. The challenger and a report of both are stored. Besides the verdict, the report has
   what tells a real improvement from noise (#89): the losses by validation day and by
   reference price, the paired difference with its standard error over days, whether
   the champion learned the validation days, and the code that trained (``KRX_COMMIT``).
   The rule stays the mean loss alone. The report is the algorithm's
   checkpoint: a night run again (after a crash, or a reclaimed Spot instance) skips the
   algorithms it has.

Once every algorithm is done the promotion list is stored, final: the algorithms whose
challenger won, by version, and every report. The server applies it at 08:00 (``swap``).

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
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from krx_quant.algorithms import ALGORITHMS, Algorithm, Model
from krx_quant.algorithms.contract import Sample, predicted
from krx_quant.algorithms.forecasts import log_loss
from krx_quant.backfill import MINUTE_BARS_TR, bars_key
from krx_quant.config import Settings, SettingsError
from krx_quant.engine import ENGINE_FEEDS, partitions
from krx_quant.features import Leaders
from krx_quant.feeds import TRADE
from krx_quant.feeds.base import KST, kst_day
from krx_quant.macro import regime_prefix
from krx_quant.market_calendar import MarketCalendar, calendar_store
from krx_quant.notify import send_notice
from krx_quant.object_store import ObjectStore, file_digest, get_json, open_store, put_json
from krx_quant.training import TrainingDay, leader_series, training_day

log = logging.getLogger(__name__)

VALIDATION_DAYS = 3
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
        """The days' reference prices, and one line per algorithm, for Telegram."""
        references: dict[str, str] = {}
        for r in self.reports:
            references.update(r.get("reference_prices", {}))
        title = f"{self.day} 밤 학습"
        if references:
            recorded = sum(kind == "mid" for kind in references.values())
            title += f" (녹화 중간가 {recorded}일, 백필 종가 {len(references) - recorded}일)"
        lines = [title]
        for r in self.reports:
            if r["promoted"]:
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
) -> Night:
    """Train every algorithm for the night of ``day`` (default: the latest day with data) and
    store the promotion list. A night already finished is returned as it was. Each report
    names ``commit``, the code that trained it.
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
    trainer = _Trainer(store, cache_dir, symbols, fee_rate, leaders, days, algorithms, calendar)
    live: dict[str, str] = get_json(store, LIVE) or {}
    reports = []
    for algorithm in algorithms:
        report = get_json(store, report_key(night, algorithm.name))
        # A checkpoint an older version wrote lacks the evidence (#89): trained again.
        if report is None or "commit" not in report:
            report = {**trainer.train(algorithm, live.get(algorithm.name)), "commit": commit}
            put_json(store, report_key(night, algorithm.name), report)
            log.info("%s: %s", algorithm.name, report["reason"])
        else:
            log.info("%s: trained earlier tonight; skipped", algorithm.name)
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
    ) -> None:
        self._store = store
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
        self._names = sorted({f for a in algorithms for f in a.features})
        self._loaded: dict[date, TrainingDay | None] = {}
        self._used: set[date] = set()

    def train(self, algorithm: Algorithm, live_version: str | None) -> dict[str, Any]:
        night = self._days[-1]
        validation = self._days[-VALIDATION_DAYS:]
        training = self._days[:-VALIDATION_DAYS][-algorithm.training_days :]
        report: dict[str, Any] = {
            "algorithm": algorithm.name,
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
        try:
            challenger = algorithm.fit(self._samples(algorithm, training))
        except ValueError as e:
            return {**report, "reason": f"not trained: {e}"}
        self._put_model(algorithm.name, challenger)
        champion = self._champion(algorithm, live_version)
        # The bar closes it predicts: without the data it reads (the book), it declines,
        # whatever the model.
        challenger_losses = _losses(algorithm, challenger, self._samples(algorithm, validation))
        scored = [s for s, _ in challenger_losses]
        report["samples"] = {"validation": len(scored)}
        if not scored:
            return {**report, "reason": "no outcomes to validate on"}
        champion_losses = _losses(algorithm, champion, scored)
        champion_loss = _mean([loss for _, loss in champion_losses])
        challenger_loss = _mean([loss for _, loss in challenger_losses])
        promoted = challenger_loss < champion_loss
        sign = "<" if promoted else "≥"
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
            "promoted": promoted,
            "validation": _validation(
                challenger_losses,
                champion_losses,
                {day: found.reference for day, found in loaded.items() if found is not None},
            ),
            "reason": f"{challenger_loss:.4f} {sign} {champion_loss:.4f} ({champion.version}"
            + (", trained on these days)" if saw else ")"),
        }

    def _samples(self, algorithm: Algorithm, days: Sequence[date]) -> list[Sample]:
        return [s for d in days if (found := self._day(d)) is not None for s in found.of(algorithm.features)]

    def _day(self, day: date) -> TrainingDay | None:
        """A day's samples with every algorithm's features, read once from the cache."""
        if day not in self._loaded:
            self._fetch(day)
            self._loaded[day] = training_day(
                self._cache, day, self._calendar, self._symbols, self._names, self._fee_rate, self._leaders
            )
        self._used.add(day)
        return self._loaded[day]

    def _champion(self, algorithm: Algorithm, version: str | None) -> Model:
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
        into the cache, where they are not there already.
        """
        series = [(MINUTE_BARS_TR, s) for s in self._symbols] + leader_series(self._leaders)
        keys = [bars_key(day, feed, name) for feed, name in series]
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
        """Remove the cached days this night did not use: older ones no night needs again."""
        if not self._used:
            return
        oldest = min(self._used)
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
        if s.outcome is not None and (f := predicted(algorithm, s.features, model)) is not None:
            out.append((s, log_loss(f, s.outcome)))
    return out


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _validation(
    challenger: Sequence[tuple[Sample, float]],
    champion: Sequence[tuple[Sample, float]],
    references: Mapping[date, str],
) -> dict[str, Any]:
    """The validation's losses by day and by reference price (``mid``, ``close``), and the
    paired difference (challenger minus champion, on the samples both predicted): its mean
    over the samples, and that mean's standard error clustered by day, as the days' 4-minute
    outcomes overlap (None with fewer than two days).
    """

    def key(sample: Sample) -> tuple[str, datetime]:
        return sample.symbol, sample.bar_close

    by_champion = {key(s): loss for s, loss in champion}

    def losses(day_of: Callable[[Sample], Any]) -> dict[str, dict[str, Any]]:
        groups: dict[Any, list[tuple[Sample, float]]] = {}
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
