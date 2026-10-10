"""Training data: backfilled one-minute bars turned into samples with the live feature code.

Each bar close of a day's prediction window becomes a sample: the features of the bars
finished by then (built by the Feature Store, as live) and what happened four minutes
later. The backfill has bars, not quotes, so here the reference price is the last
close, and the outcome is the close four minutes later against it, classified with the
same flat band as live scoring.

The lead groups (``features.Leaders``) come from the backfill too: the other symbols, the
futures' continuous series and the ETFs. Indices and US daytime trades have no backfill,
so their moves are unknown in training.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from krx_quant.algorithms import ALGORITHMS, Model, save_model
from krx_quant.algorithms.contract import Sample
from krx_quant.backfill import FUTURES_BARS_TR, MINUTE_BARS_TR
from krx_quant.config import Settings
from krx_quant.contracts import KOSPI200, USD
from krx_quant.engine import HORIZON, prediction_window
from krx_quant.features import LEAD_FEATURES, FeatureStore, FeatureVector, Leaders, Snapshot
from krx_quant.market_calendar import MarketCalendar, calendar_store
from krx_quant.pricing import classify
from krx_quant.reference import BAR, Bar

BarDays = Mapping[tuple[str, date], Sequence[Bar]]  # (symbol, day) -> bars, oldest first


def samples(
    bar_days: BarDays,
    calendar: MarketCalendar,
    names: Sequence[str],
    fee_rate: float,
    store: FeatureStore | None = None,
    *,
    leader_days: BarDays | None = None,
    leaders: Leaders | None = None,
) -> list[Sample]:
    """A sample per bar close in each day's prediction window, in time order.

    ``leader_days`` holds the leading series' bars by (series, day); the other symbols of
    ``bar_days`` lead too.
    """
    store = store or FeatureStore()
    leaders = leaders or Leaders()
    stocks = _by_day(bar_days)
    series = _by_day(leader_days or {})
    leading = bool(LEAD_FEATURES & set(names))  # leaders are sliced only when asked for
    out: list[Sample] = []
    for (symbol, day), bars in sorted(bar_days.items(), key=lambda item: (item[0][1], item[0][0])):
        session = calendar.session(day)
        if session is None:
            continue
        ends = [b.start + BAR for b in bars]
        closes = {b.start: b.close for b in bars}
        t, last = prediction_window(session)
        while t <= last:
            done = bars[: bisect.bisect_right(ends, t)]
            price = done[-1].close if done else None
            groups = None
            if leading:
                groups = leaders.groups(symbol, stocks[day].until(t), series.get(day, _Day({})).until(t))
            snapshot = Snapshot.at(
                symbol, t, done, price, fee_rate, session_open=session.open, leaders=groups
            )
            values = store.compute(snapshot, names)
            later = closes.get(t + HORIZON - BAR)  # the close of the bar ending at t + 4 minutes
            change = None
            outcome = None
            if later is not None and snapshot.price and snapshot.tick and snapshot.flat_ticks:
                change = (later - snapshot.price) / snapshot.tick
                outcome = classify(later - snapshot.price, snapshot.flat_ticks, snapshot.tick)
            out.append(Sample(symbol, t, FeatureVector(values, names), outcome, change))
            t += BAR
    return out


class _Day:
    """A day's bars of several series, cut at any bar close."""

    def __init__(self, bars: Mapping[str, Sequence[Bar]]) -> None:
        self._bars = bars
        self._ends = {name: [b.start + BAR for b in each] for name, each in bars.items()}

    def until(self, t: datetime) -> dict[str, Sequence[Bar]]:
        """Each series' bars finished by ``t``."""
        return {name: each[: bisect.bisect_right(self._ends[name], t)] for name, each in self._bars.items()}


def _by_day(bar_days: BarDays) -> dict[date, _Day]:
    days: dict[date, dict[str, Sequence[Bar]]] = {}
    for (name, day), bars in bar_days.items():
        days.setdefault(day, {})[name] = bars
    return {day: _Day(bars) for day, bars in days.items()}


def load_leader_days(
    data_dir: Path, leaders: Leaders, days: Iterable[date]
) -> dict[tuple[str, date], list[Bar]]:
    """The leading series' backfilled bars on ``days``: the futures and the ETFs."""
    wanted = [(FUTURES_BARS_TR, KOSPI200.series), (FUTURES_BARS_TR, USD.series)]
    wanted += [(MINUTE_BARS_TR, etf) for etf in leaders.etfs]
    out: dict[tuple[str, date], list[Bar]] = {}
    for day in days:
        for feed, name in wanted:
            path = (
                data_dir
                / "bars"
                / f"date={day.isoformat()}"
                / f"feed={feed}"
                / f"symbol={name}"
                / "bars.parquet"
            )
            if path.exists():
                out[(name, day)] = [_bar(row) for row in pq.read_table(path).to_pylist()]
    return out


def load_bar_days(data_dir: Path, symbols: Sequence[str], days: int) -> dict[tuple[str, date], list[Bar]]:
    """The backfilled bars of the latest ``days`` days that have any."""
    folders = sorted((data_dir / "bars").glob("date=*"), reverse=True)
    out: dict[tuple[str, date], list[Bar]] = {}
    taken = 0
    for folder in folders:
        if taken == days:
            break
        day = date.fromisoformat(folder.name.removeprefix("date="))
        found = False
        for symbol in symbols:
            path = folder / f"feed={MINUTE_BARS_TR}" / f"symbol={symbol}" / "bars.parquet"
            if path.exists():
                out[(symbol, day)] = [_bar(row) for row in pq.read_table(path).to_pylist()]
                found = True
        taken += found
    return out


@dataclass(frozen=True)
class Fitted:
    model: Model
    path: Path
    days: int
    samples: int


def fit_algorithm(settings: Settings, name: str, days: int | None = None) -> Fitted:
    """Train an algorithm on the latest backfilled days and save the model for the engine.

    The engine reads it when it starts.
    """
    algorithm = next((a for a in ALGORITHMS if a.name == name), None)
    if algorithm is None:
        raise ValueError(f"unknown algorithm {name!r}; registered: {', '.join(a.name for a in ALGORITHMS)}")
    bar_days = load_bar_days(settings.data_dir, settings.symbols, days or algorithm.training_days)
    if not bar_days:
        raise FileNotFoundError(f"no backfilled bars of {', '.join(settings.symbols)} in {settings.data_dir}")
    leaders = Leaders.of(settings)
    leader_days = load_leader_days(settings.data_dir, leaders, {day for _, day in bar_days})
    training = samples(
        bar_days,
        calendar_store(settings).calendar,
        algorithm.features,
        settings.fee_rate,
        leader_days=leader_days,
        leaders=leaders,
    )
    model = algorithm.fit(training)
    path = save_model(settings.state_dir, name, model)
    return Fitted(
        model, path, len({day for _, day in bar_days}), sum(s.outcome is not None for s in training)
    )


def _bar(row: dict[str, Any]) -> Bar:
    start = row["bar_time"]
    return Bar(
        start=start,
        open=row["open"],
        high=row["high"],
        low=row["low"],
        close=row["close"],
        volume=row["volume"],
        trades=0,  # the backfill has no trade counts
        first_at=start,
        last_at=start,
    )
