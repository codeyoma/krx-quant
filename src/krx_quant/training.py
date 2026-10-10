"""Training data: each trading day turned into samples with the live feature code.

Each bar close of a day's prediction window becomes a sample per symbol: the features
there (built by the Feature Store, as live) and what happened four minutes later,
classified with the same flat band as live scoring. A day is read one of two ways:

- Recorded: every symbol's integrated trades were recorded. The day's events go through
  the engine's own Market View. The bars, leaders and reference price (the mid-point) are
  what the engine saw. The outcome is the mid-point four minutes later, as live scoring
  decides it (basis A). Every lead group is there, indices and US daytime trades too. The
  bar closes the engine does not predict (a VI) and those live statistics leave out (a
  special situation touches them) are left out.
- Backfilled only: the backfill has bars, not quotes. The reference price is the last
  close, and the outcome the close four minutes later. A close jumps between the bid and
  the ask and back (bid-ask bounce); live scoring on mid-points never sees that. The lead
  groups are the other symbols, the futures' continuous series and the ETFs. Indices and
  US daytime trades have no backfill, so their moves are unknown.

A recorded day is read the first way even when it has backfilled bars too.
"""

from __future__ import annotations

import bisect
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Literal

import pyarrow.parquet as pq

from krx_quant.algorithms import ALGORITHMS, Model, model_path
from krx_quant.algorithms.contract import Sample
from krx_quant.archive import NIGHTLY_ARCHIVE, partition_files
from krx_quant.backfill import FUTURES_BARS_TR, MINUTE_BARS_TR, bars_key
from krx_quant.config import Settings
from krx_quant.contracts import KOSPI200, USD
from krx_quant.engine import Event
from krx_quant.features import LEAD_FEATURES, FeatureStore, FeatureVector, Leaders, Snapshot, Value
from krx_quant.feeds import TRADE
from krx_quant.feeds.base import KST
from krx_quant.macro import day_regime
from krx_quant.market_calendar import MarketCalendar, calendar_store
from krx_quant.market_view import HORIZON, MarketView, prediction_window
from krx_quant.model_shelf import Shelf
from krx_quant.pricing import classify
from krx_quant.reference import BAR, Bar
from krx_quant.replay import recorded_events
from krx_quant.special import SpecialSituations, in_vi, touching

BarDays = Mapping[tuple[str, date], Sequence[Bar]]  # (symbol, day) -> bars, oldest first
# The reference price a day's samples were made with: "mid" from recorded events, "close"
# from backfilled bars.
Reference = Literal["mid", "close"]


@dataclass(frozen=True)
class TrainingDay:
    """A day's samples, with every feature asked for, and the reference price they used."""

    reference: Reference
    samples: list[Sample]

    def of(self, features: Sequence[str]) -> list[Sample]:
        """The samples with only ``features``, as one algorithm declared them."""
        return [replace(s, features=FeatureVector(s.features, features)) for s in self.samples]


def training_day(
    data_dir: Path,
    day: date,
    calendar: MarketCalendar,
    symbols: Sequence[str],
    names: Sequence[str],
    fee_rate: float,
    leaders: Leaders,
) -> TrainingDay | None:
    """``day``'s samples: from its recorded events if every symbol's trades were recorded,
    else from its backfilled bars if every symbol has them; None otherwise. A day never
    trains on some symbols only.
    """
    regime = day_regime(data_dir, day)
    if is_recorded(data_dir, day, symbols):
        events = recorded_events(data_dir, day, symbols, leaders)
        made = recorded_samples(events, calendar, day, symbols, names, fee_rate, leaders, regime=regime)
        return TrainingDay("mid", made)
    bar_days = bar_days_on(data_dir, symbols, [day])
    if len(bar_days) < len(symbols):
        return None
    leader_days = load_leader_days(data_dir, leaders, [day]) if LEAD_FEATURES & set(names) else {}
    made = samples(
        bar_days, calendar, names, fee_rate, leader_days=leader_days, leaders=leaders, regimes={day: regime}
    )
    return TrainingDay("close", made)


def is_recorded(data_dir: Path, day: date, symbols: Sequence[str]) -> bool:
    """Whether every symbol's integrated trades were recorded on ``day``."""
    root = data_dir / "events" / f"date={day.isoformat()}" / f"feed={TRADE.tr_id}"
    return all(partition_files(root / f"symbol={s}") for s in symbols)


def days_with_data(data_dir: Path, symbols: Sequence[str]) -> list[date]:
    """The days with backfilled bars or recorded trades of every one of ``symbols``, oldest
    first.
    """
    found: set[date] = set()
    for folder in [*(data_dir / "bars").glob("date=*"), *(data_dir / "events").glob("date=*")]:
        try:
            day = date.fromisoformat(folder.name.removeprefix("date="))
        except ValueError:
            continue  # not a day's folder
        backfilled = all((data_dir / bars_key(day, MINUTE_BARS_TR, s)).exists() for s in symbols)
        if backfilled or is_recorded(data_dir, day, symbols):
            found.add(day)
    return sorted(found)


def samples(
    bar_days: BarDays,
    calendar: MarketCalendar,
    names: Sequence[str],
    fee_rate: float,
    store: FeatureStore | None = None,
    *,
    leader_days: BarDays | None = None,
    leaders: Leaders | None = None,
    regimes: Mapping[date, str | None] | None = None,
) -> list[Sample]:
    """A sample per bar close in each day's prediction window, in time order, from backfilled
    bars: the reference price is the last close, the outcome the close four minutes later.

    ``leader_days`` holds the leading series' bars by (series, day); the other symbols of
    ``bar_days`` lead too. ``regimes`` holds each day's macro regime (none: unknown).
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
                groups = leaders.groups(symbol, stocks[day].until(t), series.get(day, _DayBars({})).until(t))
            snapshot = Snapshot.at(
                symbol,
                t,
                done,
                price,
                fee_rate,
                session_open=session.open,
                leaders=groups,
                regime=(regimes or {}).get(day),
            )
            values = store.compute(snapshot, names)
            later = closes.get(t + HORIZON - BAR)  # the close of the bar ending at t + 4 minutes
            out.append(_sample(snapshot, values, names, later))
            t += BAR
    return out


def recorded_samples(
    events: Iterable[Event],
    calendar: MarketCalendar,
    day: date,
    symbols: Sequence[str],
    names: Sequence[str],
    fee_rate: float,
    leaders: Leaders,
    store: FeatureStore | None = None,
    *,
    regime: str | None = None,
) -> list[Sample]:
    """A sample per bar close of ``day``'s prediction window and symbol, in time order, from
    the day's events (in receipt order) through the engine's Market View.

    At each bar close the features are computed before any event received at or after it,
    as the engine does. The outcome is the mid-point four minutes later against the one at
    the bar close, as live scoring decides it on basis A. A symbol in a VI at a bar close
    has no sample there (the engine does not predict it), and neither has a bar close whose
    four minutes a VI, circuit breaker or sidecar touches (live statistics leave it out).
    """
    session = calendar.session(day)
    if session is None:
        return []
    store = store or FeatureStore()
    view = MarketView(calendar, symbols, leaders, datetime.combine(day, time(), KST))
    special = SpecialSituations(calendar)
    first, last = prediction_window(session)
    # Every minute from the first bar close to the last target: a bar close to predict, a
    # target to look the mid-point up at, or both.
    minutes = deque(first + i * BAR for i in range((last + HORIZON - first) // BAR + 1))
    # Each bar close's snapshot, features, and whether a window known then touched it.
    seen: list[tuple[Snapshot, dict[str, Value], bool]] = []
    mids: dict[tuple[str, datetime], float | None] = {}

    def reach(moment: datetime) -> None:
        """Everything due at the minutes up to ``moment``, before what is received then."""
        while minutes and minutes[0] <= moment:
            minute = minutes.popleft()
            if minute <= last:
                frozen = view.at_close(minute)
                known = special.known(before=minute)
                for symbol in symbols:
                    if in_vi(known, symbol, minute):
                        continue
                    snapshot = view.snapshot(frozen, symbol, fee_rate, regime)
                    marked = touching(known, symbol, minute, minute + HORIZON)
                    seen.append((_bare(snapshot), store.compute(snapshot, names), marked))
            if minute >= first + HORIZON:
                for symbol in symbols:
                    mids[(symbol, minute)] = view.mid(symbol, minute)

    for feed, row in events:
        reach(row["received_at"])
        view.on_event(feed, row)
        special.event(feed, row, symbols)
    reach(minutes[-1] if minutes else last)
    out = []
    for snapshot, values, marked in seen:
        # Special as the engine marks it: by the windows known when it is made, or learned since.
        t = snapshot.bar_close
        if marked or touching(special.learned_since(t), snapshot.symbol, t, t + HORIZON):
            continue
        later = mids.get((snapshot.symbol, snapshot.bar_close + HORIZON))
        out.append(_sample(snapshot, values, names, later))
    return out


def _bare(snapshot: Snapshot) -> Snapshot:
    """``snapshot`` without its bars and leaders, all a sample needs once its features are computed."""
    return Snapshot(
        snapshot.symbol,
        snapshot.bar_close,
        (),
        snapshot.price,
        snapshot.tick,
        snapshot.flat_ticks,
        snapshot.session_open,
    )


def _sample(
    snapshot: Snapshot, values: Mapping[str, Value], names: Sequence[str], later: float | None
) -> Sample:
    """A sample of ``snapshot``, with the price four minutes later classified against its own."""
    change = None
    outcome = None
    if later is not None and snapshot.price and snapshot.tick and snapshot.flat_ticks:
        change = (later - snapshot.price) / snapshot.tick
        outcome = classify(later - snapshot.price, snapshot.flat_ticks, snapshot.tick)
    return Sample(snapshot.symbol, snapshot.bar_close, FeatureVector(values, names), outcome, change)


class _DayBars:
    """A day's bars of several series, cut at any bar close."""

    def __init__(self, bars: Mapping[str, Sequence[Bar]]) -> None:
        self._bars = bars
        self._ends = {name: [b.start + BAR for b in each] for name, each in bars.items()}

    def until(self, t: datetime) -> dict[str, Sequence[Bar]]:
        """Each series' bars finished by ``t``."""
        return {name: each[: bisect.bisect_right(self._ends[name], t)] for name, each in self._bars.items()}


def _by_day(bar_days: BarDays) -> dict[date, _DayBars]:
    days: dict[date, dict[str, Sequence[Bar]]] = {}
    for (name, day), bars in bar_days.items():
        days.setdefault(day, {})[name] = bars
    return {day: _DayBars(bars) for day, bars in days.items()}


def leader_series(leaders: Leaders) -> list[tuple[str, str]]:
    """The leading series with backfilled bars, as (feed, name): the futures and the ETFs."""
    return [
        (FUTURES_BARS_TR, KOSPI200.series),
        (FUTURES_BARS_TR, USD.series),
        *((MINUTE_BARS_TR, etf) for etf in leaders.etfs),
    ]


def load_leader_days(data_dir: Path, leaders: Leaders, days: Iterable[date]) -> BarDays:
    """The leading series' backfilled bars on ``days``."""
    out: dict[tuple[str, date], list[Bar]] = {}
    for day in days:
        for feed, name in leader_series(leaders):
            bars = _read_bars(data_dir, day, feed, name)
            if bars is not None:
                out[(name, day)] = bars
    return out


def bar_days_on(
    data_dir: Path, symbols: Sequence[str], days: Iterable[date]
) -> dict[tuple[str, date], list[Bar]]:
    """The backfilled bars of ``symbols`` on ``days``, where there are any."""
    out: dict[tuple[str, date], list[Bar]] = {}
    for day in days:
        for symbol in symbols:
            bars = _read_bars(data_dir, day, MINUTE_BARS_TR, symbol)
            if bars is not None:
                out[(symbol, day)] = bars
    return out


def _read_bars(data_dir: Path, day: date, feed: str, name: str) -> list[Bar] | None:
    """A day's backfilled bars of a series, if it has a file."""
    path = data_dir / bars_key(day, feed, name)
    if not path.exists():
        return None
    return [_bar(row) for row in pq.read_table(path).to_pylist()]


@dataclass(frozen=True)
class Fitted:
    model: Model
    path: Path
    days: int
    recorded: list[date]  # of the days, those trained on recorded mid-points
    samples: int


def fit_algorithm(
    settings: Settings, name: str, days: int | None = None, now: datetime | None = None
) -> Fitted:
    """Train an algorithm on the latest days with data and save the model for the engine,
    which switches to it within a minute. Today counts only once its recording is archived
    (21:00): before that it is still being recorded.
    """
    algorithm = next((a for a in ALGORITHMS if a.name == name), None)
    if algorithm is None:
        raise ValueError(f"unknown algorithm {name!r}; registered: {', '.join(a.name for a in ALGORITHMS)}")
    local = (now or datetime.now(KST)).astimezone(KST)
    finished = [
        d
        for d in days_with_data(settings.data_dir, settings.symbols)
        if d < local.date() or local.time() >= NIGHTLY_ARCHIVE
    ]
    calendar = calendar_store(settings).calendar
    leaders = Leaders.of(settings)
    loaded: dict[date, TrainingDay] = {}
    for day in finished[-(days or algorithm.training_days) :]:
        found = training_day(
            settings.data_dir, day, calendar, settings.symbols, algorithm.features, settings.fee_rate, leaders
        )
        if found is not None:
            loaded[day] = found
    if not loaded:
        raise FileNotFoundError(
            f"no backfilled bars or recorded trades of {', '.join(settings.symbols)} in {settings.data_dir}"
        )
    training = [s for found in loaded.values() for s in found.samples]
    model = algorithm.fit(training)
    Shelf(settings.state_dir).install(name, model, "fit", local)
    return Fitted(
        model,
        model_path(settings.state_dir, name),
        len(loaded),
        [day for day, found in loaded.items() if found.reference == "mid"],
        sum(s.outcome is not None for s in training),
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
