"""Nightly training: each algorithm retrained on the latest days, promoted only when it
beats the model the server runs on days it did not learn from.

A night trains on the latest trading day with bars in the Object Store. For each
algorithm, in registration order:

1. The last 3 trading days are kept for validation; the algorithm trains on its own
   rolling window (``training_days``) before them (walk-forward).
2. The new model (challenger) and the one the server runs (champion: ``live.json``, else
   the defaults) both forecast the validation days. The challenger is promoted only if
   its mean log loss there is lower.
3. The challenger and a report of both are stored. The report is the algorithm's
   checkpoint: a night run again (after a crash, or a reclaimed Spot instance) skips the
   algorithms it has.

Once every algorithm is done the promotion list is stored, final: the algorithms whose
challenger won, by version, and every report. The server applies it at 08:00 (``swap``).

Objects:

- ``bars/...``: archived one-minute bars, read (copied into a local cache once);
- ``models/<algorithm>/<version>.json``: each challenger, and the models the server runs;
- ``live.json``: the version each algorithm runs on the server, published by it;
- ``reports/<day>/<algorithm>.json``: one algorithm's night;
- ``promotions/<day>.json``: the night's promotion list.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from krx_quant.algorithms import ALGORITHMS, Algorithm, Model
from krx_quant.algorithms.contract import Sample
from krx_quant.algorithms.forecasts import log_loss
from krx_quant.backfill import MINUTE_BARS_TR, bars_key
from krx_quant.config import Settings, SettingsError
from krx_quant.features import Leaders
from krx_quant.feeds.base import KST
from krx_quant.market_calendar import MarketCalendar
from krx_quant.notify import send_notice
from krx_quant.object_store import ObjectStore, file_digest, get_json, open_store, put_json
from krx_quant.training import bar_days_on, leader_series, load_leader_days, samples

log = logging.getLogger(__name__)

VALIDATION_DAYS = 3
LIVE = "live.json"


def model_key(name: str, version: str) -> str:
    return f"models/{name}/{version}.json"


def report_key(day: date, name: str) -> str:
    return f"reports/{day.isoformat()}/{name}.json"


def promotions_key(day: date) -> str:
    return f"promotions/{day.isoformat()}.json"


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
        """One line per algorithm, for Telegram."""
        lines = [f"{self.day} 밤 학습"]
        for r in self.reports:
            if r["promoted"]:
                verdict = "승격"
            elif r["challenger"] is None:
                verdict = "학습 안 함"
            else:
                verdict = "유지"
            lines.append(f"- {r['algorithm']}: {verdict} ({r['reason']})")
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
) -> Night:
    """Train every algorithm for the night of ``day`` (default: the latest day with bars) and
    store the promotion list. A night already finished is returned as it was.
    """
    leaders = leaders or Leaders()
    days_of = _days_with_bars(store, symbols)
    days = sorted(set().union(*days_of.values()))
    if day is not None:
        days = [d for d in days if d <= day]
    if not days or (day is not None and days[-1] != day):
        raise FileNotFoundError(f"no archived bars of {', '.join(symbols)} for {day or 'any day'}")
    night = days[-1]
    missing = [s for s in symbols if night not in days_of[s]]
    if missing:
        raise FileNotFoundError(f"bars of {', '.join(missing)} for {night} are not archived yet")
    finished = get_json(store, promotions_key(night))
    if finished is not None:
        log.info("the night of %s is already trained", night)
        return Night.from_dict(finished)
    trainer = _Trainer(store, cache_dir, symbols, fee_rate, leaders, days)
    live: dict[str, str] = get_json(store, LIVE) or {}
    reports = []
    for algorithm in algorithms:
        report = get_json(store, report_key(night, algorithm.name))
        if report is None:
            report = trainer.train(algorithm, live.get(algorithm.name))
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
    ) -> None:
        self._store = store
        self._cache = cache_dir
        self._symbols = tuple(symbols)
        self._fee_rate = fee_rate
        self._leaders = leaders
        self._days = list(days)
        self._calendar = MarketCalendar(dict.fromkeys(days, True))

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
        self._fetch([*training, *validation])
        try:
            challenger = algorithm.fit(self._samples(algorithm, training))
        except ValueError as e:
            return {**report, "reason": f"not trained: {e}"}
        self._put_model(algorithm.name, challenger)
        champion = self._champion(algorithm, live_version)
        scored = [s for s in self._samples(algorithm, validation) if s.outcome is not None]
        report["samples"] = {"validation": len(scored)}
        if not scored:
            return {**report, "reason": "no outcomes to validate on"}
        champion_loss = _mean_loss(algorithm, champion, scored)
        challenger_loss = _mean_loss(algorithm, challenger, scored)
        promoted = challenger_loss < champion_loss
        sign = "<" if promoted else "≥"
        # A champion fitted by hand on the latest days may have learned the validation days:
        # it is then judged on what it saw, and is harder to beat than it should be.
        saw = champion.trained_until is not None and champion.trained_until >= validation[0]
        return {
            **report,
            "champion": {"version": champion.version, "loss": champion_loss},
            "challenger": {
                "version": challenger.version,
                "loss": challenger_loss,
                "trained": challenger.trained,
            },
            "promoted": promoted,
            "reason": f"{challenger_loss:.4f} {sign} {champion_loss:.4f} ({champion.version}"
            + (", trained on these days)" if saw else ")"),
        }

    def _samples(self, algorithm: Algorithm, days: Sequence[date]) -> list[Sample]:
        return samples(
            bar_days_on(self._cache, self._symbols, days),
            self._calendar,
            algorithm.features,
            self._fee_rate,
            leader_days=load_leader_days(self._cache, self._leaders, days),
            leaders=self._leaders,
        )

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
        put_json(self._store, model_key(name, model.version), model.as_dict())

    def _fetch(self, days: Sequence[date]) -> None:
        """Copy the days' bars from the store into the cache, where they are not there already."""
        series = [(MINUTE_BARS_TR, s) for s in self._symbols] + leader_series(self._leaders)
        for day in days:
            for feed, name in series:
                key = bars_key(day, feed, name)
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


def _days_with_bars(store: ObjectStore, symbols: Sequence[str]) -> dict[str, set[date]]:
    """The days the store has bars of each of ``symbols`` for."""
    days: dict[str, set[date]] = {s: set() for s in symbols}
    for key in store.keys("bars/"):
        parts = key.split("/")  # bars, date=…, feed=…, symbol=…, bars.parquet
        if len(parts) != 5 or parts[2] != f"feed={MINUTE_BARS_TR}":
            continue
        symbol = parts[3].removeprefix("symbol=")
        if symbol in days:
            days[symbol].add(date.fromisoformat(parts[1].removeprefix("date=")))
    return days


def _mean_loss(algorithm: Algorithm, model: Model, scored: Sequence[Sample]) -> float:
    return sum(log_loss(algorithm.predict(s.features, model), s.outcome) for s in scored if s.outcome) / len(
        scored
    )
