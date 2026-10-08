"""Replay Harness: the engine on recorded (or hand-made) events and a fake clock.

The clock steps to each event's receipt time, and to each bar close and scoring time in
between, so the engine does exactly what it would have done live: the same calls in the
same order, from normalized events to the Engine API.

``krx-quant replay <date>`` runs it on a recorded day, into a separate database.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterable, Iterator, Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from krx_quant.archive import partition_files
from krx_quant.clock import FakeClock
from krx_quant.config import Settings
from krx_quant.engine import ENGINE_FEEDS, Event, PredictionEngine
from krx_quant.feeds.base import KST, kst_day
from krx_quant.market_calendar import MarketCalendar, calendar_store
from krx_quant.predictions import DB_NAME, AlgorithmSummary, PredictionStore

# Rows converted to Python at a time while reading a recorded day.
_BATCH_ROWS = 10_000


def replay(
    events: Iterable[Event],
    *,
    db: Path,
    calendar: MarketCalendar,
    symbols: Sequence[str],
    fee_rate: float,
    until: datetime,
    start: datetime | None = None,
    display_delay: timedelta = timedelta(0),
) -> PredictionEngine:
    """Feed ``events`` (in receipt order) received by ``until`` to an engine storing into ``db``.

    The clock starts at ``start`` (default: midnight of ``until``'s day). Each bar close and
    scoring runs ``display_delay`` after its time, as a busy engine would: what it predicts
    from stays what was received by the bar close, but it is displayed (and scored on
    basis B) later. The engine is returned with its clock at ``until``.
    """
    clock = FakeClock(start or datetime.combine(kst_day(until), time(), KST))
    engine = PredictionEngine(PredictionStore(db), calendar, symbols, fee_rate, clock)
    for feed, row in events:
        received_at: datetime = row["received_at"]
        if received_at > until:
            break
        if received_at < clock.now():
            raise ValueError(f"events out of order: {received_at} after {clock.now()}")
        _run_until(engine, clock, received_at, display_delay)
        clock.set(received_at)
        engine.on_event(feed, row)
    _run_until(engine, clock, until, display_delay)
    clock.set(until)
    return engine


def _run_until(engine: PredictionEngine, clock: FakeClock, until: datetime, delay: timedelta) -> None:
    """Run what is due, each at its own time plus ``delay``, up to and including ``until``."""
    while (due := engine.next_due()) is not None and due + delay <= until:
        clock.set(max(clock.now(), due + delay))
        engine.run_due()


def recorded_events(data_dir: Path, day: date, symbols: Sequence[str]) -> Iterator[Event]:
    """The engine's events recorded on ``day``, in receipt order.

    Only the columns the engine reads are loaded, and rows become Python objects a batch
    at a time, so a whole day fits in little memory. Raises FileNotFoundError if none of
    them were recorded that day.
    """
    streams: list[Iterator[Event]] = []
    for feed, columns in ENGINE_FEEDS.items():
        for symbol in symbols:
            partition = data_dir / "events" / f"date={day.isoformat()}" / f"feed={feed}" / f"symbol={symbol}"
            files = partition_files(partition) if partition.is_dir() else []
            if files:
                tables = [
                    pq.read_table(f, columns=list(columns)).replace_schema_metadata(None) for f in files
                ]
                streams.append(_rows(feed, pa.concat_tables(tables).sort_by("received_at")))
    if not streams:
        raise FileNotFoundError(f"no events of {', '.join(ENGINE_FEEDS)} recorded on {day} in {data_dir}")
    # Stable: rows received at the same instant keep the order of the streams.
    return heapq.merge(*streams, key=lambda event: event[1]["received_at"])


def _rows(feed: str, table: pa.Table) -> Iterator[Event]:
    for batch in table.to_batches(max_chunksize=_BATCH_ROWS):
        for row in batch.to_pylist():
            yield feed, row


def replay_day(settings: Settings, day: date, db: Path | None = None) -> tuple[Path, list[AlgorithmSummary]]:
    """Replay a recorded day into a fresh database (default ``state/replay-<day>.sqlite3``).

    Refuses the live engine's database, and deletes nothing if the day was not recorded.
    """
    db = db or settings.state_dir / f"replay-{day.isoformat()}.sqlite3"
    if db.resolve() == (settings.state_dir / DB_NAME).resolve():
        raise ValueError(f"{db} is the live engine's database; replay into another file")
    events = recorded_events(settings.data_dir, day, settings.symbols)
    for leftover in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        leftover.unlink(missing_ok=True)
    start = datetime.combine(day, time(), KST)
    engine = replay(
        events,
        db=db,
        calendar=calendar_store(settings).calendar,
        symbols=settings.symbols,
        fee_rate=settings.fee_rate,
        start=start,
        until=start + timedelta(days=1) - timedelta(microseconds=1),
    )
    try:
        return db, engine.store.summary()
    finally:
        engine.store.close()
