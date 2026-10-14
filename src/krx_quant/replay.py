"""Replay Harness: the engine on recorded (or hand-made) events and a fake clock.

The clock steps to each event's receipt time, and to each bar close and scoring time in
between, so the engine does exactly what it would have done live: the same calls in the
same order, from normalized events to the Engine API.

``krx-quant replay <date>`` runs it on a recorded day, into a separate database.
"""

from __future__ import annotations

import heapq
import logging
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import partial
from pathlib import Path

import pyarrow.parquet as pq

from krx_quant.algorithms import ALGORITHMS, KIND_ALGORITHMS, Algorithm, KindAlgorithm, Model, load_models
from krx_quant.archive import partition_files
from krx_quant.backfill import PreviousDay
from krx_quant.climate import CLIMATE, load_climate, load_tables
from krx_quant.clock import FakeClock
from krx_quant.config import Settings
from krx_quant.consensus import Timing
from krx_quant.engine import ENGINE_FEEDS, Budget, Event, Notify, PredictionEngine, Regimes, partitions
from krx_quant.features import FeatureStore, Leaders, PreviousRanges
from krx_quant.feeds.base import KST, kst_day
from krx_quant.kind_registry import KINDS
from krx_quant.kinds import Kind
from krx_quant.lifecycle import Rules
from krx_quant.macro import day_regime
from krx_quant.market_calendar import MarketCalendar, calendar_store
from krx_quant.predictions import DB_NAME, AlgorithmSummary, PredictionStore
from krx_quant.stats import DEFAULT_MIN_SAMPLE

log = logging.getLogger(__name__)

# Rows read and converted to Python at a time, per partition, while reading a recorded day.
_BATCH_ROWS = 4096


async def replay(
    events: Iterable[Event],
    *,
    db: Path,
    calendar: MarketCalendar,
    symbols: Sequence[str],
    fee_rate: float,
    until: datetime,
    start: datetime | None = None,
    display_delay: timedelta = timedelta(0),
    algorithms: Sequence[Algorithm] = ALGORITHMS,
    models: Mapping[str, Model] | None = None,
    budget: Budget | None = None,
    feature_store: FeatureStore | None = None,
    min_sample: int = DEFAULT_MIN_SAMPLE,
    notify: Notify | None = None,
    consensus_timing: Timing | None = None,
    leaders: Leaders | None = None,
    regimes: Regimes | None = None,
    admits: Callable[[Algorithm | KindAlgorithm], bool] | None = None,
    review_rules: Rules | None = None,
    target_version: int = 1,
    climate: Model | None = None,
    kinds: Sequence[Kind] = KINDS,
    kind_algorithms: Sequence[KindAlgorithm] = KIND_ALGORITHMS,
    previous: PreviousRanges | None = None,
) -> PredictionEngine:
    """Feed ``events`` (in receipt order) received by ``until`` to an engine storing into ``db``.

    The clock starts at ``start`` (default: midnight of ``until``'s day). Each bar close and
    scoring runs ``display_delay`` after its time, as a busy engine would: what it predicts
    from stays what was received by the bar close, but it is displayed later (its basis B
    outcome, still written, moves with it). The clock stands still while algorithms work; their time budgets
    count real time. The engine is returned with its clock at ``until``. It forecasts
    ``target_version``'s target, with the climate baseline's table ``climate`` (#169), and
    the ``kinds`` besides the direction with their algorithms (#179); the symbols' previous
    trading days' ranges from ``previous`` (#181; without: unknown).
    """
    clock = FakeClock(start or datetime.combine(kst_day(until), time(), KST))
    engine = PredictionEngine(
        PredictionStore(db),
        calendar,
        symbols,
        fee_rate,
        clock,
        algorithms=algorithms,
        models=models,
        budget=budget,
        feature_store=feature_store,
        min_sample=min_sample,
        notify=notify,
        consensus_timing=consensus_timing,
        leaders=leaders,
        regimes=regimes,
        admits=admits,
        review_rules=review_rules,
        target_version=target_version,
        climate=climate,
        kinds=kinds,
        kind_algorithms=kind_algorithms,
        previous=previous,
    )
    await replay_into(engine, clock, events, until=until, display_delay=display_delay)
    return engine


async def replay_into(
    engine: PredictionEngine,
    clock: FakeClock,
    events: Iterable[Event],
    *,
    until: datetime,
    display_delay: timedelta = timedelta(0),
) -> None:
    """Feed ``events`` (in receipt order) received by ``until`` to ``engine`` on ``clock``,
    running what falls due in between; the clock ends at ``until``."""
    for tr_id, row in events:
        received_at: datetime = row["received_at"]
        if received_at > until:
            break
        if received_at < clock.now():
            raise ValueError(f"events out of order: {received_at} is before the clock at {clock.now()}")
        await _run_until(engine, clock, received_at, display_delay)
        clock.set(received_at)
        engine.on_event(tr_id, row)
    await _run_until(engine, clock, until, display_delay)
    clock.set(until)


async def _run_until(engine: PredictionEngine, clock: FakeClock, until: datetime, delay: timedelta) -> None:
    """Run what is due, each at its own time plus ``delay``, up to and including ``until``."""
    while (due := engine.next_due()) is not None and due + delay <= until:
        clock.set(max(clock.now(), due + delay))
        await engine.run_due()


def recorded_events(
    data_dir: Path,
    day: date,
    symbols: Sequence[str],
    leaders: Leaders | None = None,
    feeds: Collection[str] | None = None,
) -> Iterator[Event]:
    """The engine's events recorded on ``day``, in receipt order; of ``feeds`` only, if given.

    Each partition is read file by file, a batch at a time, with only the columns the
    engine uses, and the partitions are merged by receipt time: memory stays at about a
    batch per partition however long the day. Raises FileNotFoundError if none of them
    were recorded that day.
    """
    read = {f: c for f, c in ENGINE_FEEDS.items() if feeds is None or f in feeds}
    streams: list[Iterator[Event]] = []
    for feed, columns in read.items():
        root = data_dir / "events" / f"date={day.isoformat()}" / f"feed={feed}"
        for key in _keys(feed, root, symbols, leaders or Leaders()):
            partition = root / f"symbol={key}"
            files = partition_files(partition) if partition.is_dir() else []
            if files:
                streams.append(_partition_rows(feed, files, columns))
    if not streams:
        raise FileNotFoundError(f"no events of {', '.join(read)} recorded on {day} in {data_dir}")
    # Stable: rows received at the same instant keep the order of the streams.
    return heapq.merge(*streams, key=lambda event: event[1]["received_at"])


def _keys(feed: str, root: Path, symbols: Sequence[str], leaders: Leaders) -> list[str]:
    """The partitions of ``feed`` the engine reads."""
    keys = partitions(feed, symbols, leaders)
    if keys is None:  # the front month, whichever it was that day
        return sorted(p.name.removeprefix("symbol=") for p in root.glob("symbol=*"))
    return keys


def _partition_rows(feed: str, files: Sequence[Path], columns: Sequence[str]) -> Iterator[Event]:
    """A partition's rows in the order written, which is the order received.

    A row stamped earlier than the one before it (the system clock stepped back) is
    taken as received with that one, so the merge stays in order. A column a file was
    recorded without (added later) is None.
    """
    last: datetime | None = None
    stepped_back = 0
    for path in files:
        parquet = pq.ParquetFile(path)
        present = [c for c in columns if c in parquet.schema_arrow.names]
        missing = dict.fromkeys(c for c in columns if c not in present)
        for batch in parquet.iter_batches(batch_size=_BATCH_ROWS, columns=present):
            for row in batch.to_pylist():
                row.update(missing)
                if last is not None and row["received_at"] < last:
                    row["received_at"] = last
                    stepped_back += 1
                last = row["received_at"]
                yield feed, row
    if stepped_back:
        log.warning(
            "%s %s: %d rows stamped before the row written before them", feed, files[0].parent, stepped_back
        )


@dataclass(frozen=True)
class ReplayedDay:
    db: Path
    summary: list[AlgorithmSummary]
    # Saved models trained on data up to the replayed day or later: they would be scored
    # on what they learned from, so their defaults were used instead. One line each.
    set_aside: list[str]


async def replay_day(settings: Settings, day: date, db: Path | None = None) -> ReplayedDay:
    """Replay a recorded day into a fresh database (default ``state/replay-<day>.sqlite3``).

    Refuses the live engine's database and its WAL and shared-memory files, and deletes
    nothing if the day was not recorded. Saved models are used only if trained on days
    before ``day``.
    """
    db = db or settings.state_dir / f"replay-{day.isoformat()}.sqlite3"
    if db.resolve() in {p.resolve() for p in sqlite_files(settings.state_dir / DB_NAME)}:
        raise ValueError(f"{db} is the live engine's database; replay into another file")
    leaders = Leaders.of(settings)
    events = recorded_events(settings.data_dir, day, settings.symbols, leaders)
    for leftover in sqlite_files(db):
        leftover.unlink(missing_ok=True)
    models: dict[str, Model] = {}
    set_aside: list[str] = []
    saved = load_models(settings.state_dir, [*ALGORITHMS, *KIND_ALGORITHMS])
    if settings.target_version >= 2:
        saved[CLIMATE] = load_climate(settings.state_dir)
    saved.update(load_tables(settings.state_dir, KINDS))  # the kinds' baselines' tables (#180)
    for name, model in saved.items():
        if model.trained_until is not None and model.trained_until >= day:
            set_aside.append(
                f"{model.version} was trained on data up to {model.trained_until}; using {name}'s default"
            )
        else:
            models[name] = model
    climate = models.pop(CLIMATE, None)
    engine = await replay_recorded(settings, day, events, db=db, models=models, climate=climate)
    try:
        return ReplayedDay(db, engine.store.summary(), set_aside)
    finally:
        engine.store.close()


async def replay_recorded(
    settings: Settings,
    day: date,
    events: Iterable[Event],
    *,
    db: Path,
    models: Mapping[str, Model],
    algorithms: Sequence[Algorithm] = ALGORITHMS,
    calendar: MarketCalendar | None = None,
    climate: Model | None = None,
    kinds: Sequence[Kind] = KINDS,
    kind_algorithms: Sequence[KindAlgorithm] = KIND_ALGORITHMS,
) -> PredictionEngine:
    """``day``'s recorded ``events``, all day, through an engine set up as the server's (its
    settings, calendar, macro regimes, the previous trading days' backfilled bars and target
    version) storing into ``db``; with the touch target, the climate baseline's table
    ``climate`` (its defaults without); the ``kinds`` besides the direction with their
    algorithms (#179)."""
    start = datetime.combine(day, time(), KST)
    calendar = calendar or calendar_store(settings).calendar
    return await replay(
        events,
        db=db,
        calendar=calendar,
        symbols=settings.symbols,
        fee_rate=settings.fee_rate,
        start=start,
        until=start + timedelta(days=1) - timedelta(microseconds=1),
        algorithms=algorithms,
        models=models,
        budget=Budget.of(settings),
        min_sample=settings.min_sample,
        leaders=Leaders.of(settings),
        regimes=partial(day_regime, settings.data_dir),
        review_rules=Rules.of(settings),
        target_version=settings.target_version,
        climate=climate,
        kinds=kinds,
        kind_algorithms=kind_algorithms,
        previous=PreviousDay(settings.data_dir, calendar),  # range_prev_day (#181)
    )


def sqlite_files(db: Path) -> list[Path]:
    """A SQLite database and the files WAL mode keeps beside it."""
    return [db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")]
