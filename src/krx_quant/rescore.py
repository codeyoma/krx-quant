"""Past bar closes' touch labels (#160, #168), from their recorded days.

The live engine records each bar close's touch label (``touch.py``) since #167. The bar
closes predicted before, and any an engine left without one (an older engine run after a
rollback), get theirs from the day's recorded events, replayed through the same Market View
windows: each label is the one a live run, or the Replay Harness, gives that bar close.

- **At start** (``keep_touches_filled``), once the collector has opened the recording and the
  Engine API, a worker thread fills every bar close predicted since the recording began
  (``RECORDING_START``) and before today that has no label of this target version. A label
  already stored stays. Each symbol's events of the day come from the data directory, else
  from the Object Store; its predicted bar closes are labeled ``not recorded`` (and logged)
  where neither has them. Today is left to the live engine. Then the store's version moves
  on, so the cached leaderboards and statistics are computed again. An engine rolled back
  and deployed again only fills the bar closes the older one left without.
- **By hand** (``krx-quant rescore``), with the engine stopped: every finished day's labels
  of this target version are computed again, and those that differ are replaced. A day that
  cannot be read keeps what it has. Running it again changes nothing.

Neither touches the outcomes (classes A and B, their special marks): only ``touches``. Each
filled row is stored as scored when its four minutes ended, as a replay stores it; the run
is recorded in ``runs`` (``touch_fill``, ``rescore``).

The running engine holds a lock on its state directory (``EngineLock``, ``flock``). A rescore
beside it would leave the engine's cached leaderboards and consensus weights on the labels
it replaced, so it refuses to run. An engine started while a rescore runs records, predicts
and serves as usual, and fills once the rescore is done.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import tempfile
import threading
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import IO

from krx_quant.archive import partition_files
from krx_quant.config import Settings
from krx_quant.engine import Event, PredictionEngine
from krx_quant.features import Leaders
from krx_quant.feeds import BOOK, BOOK_SNAPSHOT, TRADE
from krx_quant.feeds.base import KST, kst_day
from krx_quant.market_calendar import MarketCalendar, calendar_store
from krx_quant.market_view import MarketView, prediction_window
from krx_quant.object_store import ObjectStore, open_store
from krx_quant.predictions import DB_NAME, PredictionStore
from krx_quant.reference import BAR
from krx_quant.replay import recorded_events
from krx_quant.touch import NOT_RECORDED, WINDOW, TouchResult

log = logging.getLogger(__name__)

# The first recorded day: bar closes before it have no events to be labeled from.
RECORDING_START = date(2026, 10, 12)
# The lock the running engine holds, in the state directory.
LOCK_NAME = ".engine.lock"
# The feeds a touch window follows: the integrated trades and book, and the REST book
# snapshot that replaces the book when the session limit cuts it.
TOUCH_FEEDS = (TRADE.tr_id, BOOK.tr_id, BOOK_SNAPSHOT.tr_id)
# How often an engine started beside a rescore looks whether it is done.
LOCK_POLL_SECONDS = 5.0
# Events between two looks at whether the fill should stop (the engine is shutting down).
_HALT_EVERY = 4096


class EngineLock:
    """The running engine's hold on its state directory (``.engine.lock``, ``flock``): a
    ``rescore`` does not run beside it. The kernel lets it go when the process ends."""

    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / LOCK_NAME
        self._file: IO[str] | None = None

    def acquire(self) -> bool:
        """Take the lock unless another process holds it; whether this one holds it now."""
        if self._file is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = self.path.open("a")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            f.close()
            return False
        self._file = f
        return True

    def release(self) -> None:
        if self._file is not None:
            self._file.close()  # closing it releases the lock
            self._file = None


class EngineRunning(Exception):
    """A rescore asked for while the engine runs (it holds the engine lock)."""


class _Halted(Exception):
    """The fill was told to stop (the engine is shutting down)."""


@dataclass(frozen=True)
class Filled:
    """What a fill or a rescore did with one day."""

    day: date
    source: str | None  # where its events came from (the data directory or the Object Store)
    labeled: int  # windows labeled from its recorded events
    written: int  # rows stored: new ones, or (a rescore) ones that differed
    not_recorded: int = 0  # predicted bar closes left ``not recorded``
    failed: str | None = None  # the events could not be read: nothing written, tried again later


def label_day(
    events: Iterable[Event],
    calendar: MarketCalendar,
    day: date,
    symbols: Sequence[str],
    fee_rate: float,
    halt: threading.Event | None = None,
) -> list[TouchResult]:
    """Every touch window of ``symbols`` on ``day``'s bar closes, labeled from the day's
    events (in receipt order) through the engine's Market View, as the engine does it: each
    bar close's windows open at it, before anything received then, and each window is
    labeled when its four minutes are over."""
    session = calendar.session(day)
    if session is None:
        return []
    view = MarketView(calendar, symbols, Leaders(), datetime.combine(day, time(), KST), fee_rate)
    first, last = prediction_window(session)
    # Every minute from the first bar close to the last window's end: windows end on the
    # minute, and the engine scores at each one before it predicts the bar close there.
    minutes = deque(first + i * BAR for i in range((last + WINDOW - first) // BAR + 1))
    labeled: list[TouchResult] = []

    def reach(moment: datetime) -> None:
        while minutes and minutes[0] <= moment:
            minute = minutes.popleft()
            labeled.extend(view.touch(symbol, close) for symbol, close in view.ended_windows(minute))
            if minute <= last:
                view.at_close(minute)

    for i, (feed, row) in enumerate(events):
        if halt is not None and i % _HALT_EVERY == 0 and halt.is_set():
            raise _Halted
        reach(row["received_at"])
        view.on_event(feed, row)
    reach(last + WINDOW)
    return labeled


def fill_touches(
    settings: Settings,
    now: datetime,
    *,
    rescore: bool = False,
    halt: threading.Event | None = None,
    calendar: MarketCalendar | None = None,
) -> list[Filled]:
    """Label the past bar closes from their recorded days, on a database connection of its
    own (a worker thread may run it beside the engine).

    The days are those from ``RECORDING_START`` before ``now``'s day; a rescore takes ``now``'s
    day too once its last window is over. Without ``rescore``, the days with a predicted bar
    close lacking a label of this target version, and only the missing labels are stored;
    with it, every day with predictions or labels, and the labels that differ are replaced.
    A day that cannot be read is logged and left for the next time. ``halt`` stops it
    between batches of events, writing nothing more.
    """
    calendar = calendar or calendar_store(settings).calendar
    today = kst_day(now)
    session = calendar.session(today)
    over = session is None or now >= session.closing_auction
    before = today + timedelta(days=1) if rescore and over else today
    store = PredictionStore(settings.state_dir / DB_NAME)
    try:
        missing: dict[date, list[tuple[str, datetime]]] = {}
        for symbol, close in store.untouched(RECORDING_START, before):
            missing.setdefault(kst_day(close), []).append((symbol, close))
        if rescore:
            days = store.labeled_days(RECORDING_START, before)
        else:
            days = {day: {symbol for symbol, _ in pairs} for day, pairs in missing.items()}
        if not days:
            return []
        object_store = open_store(settings)
        filled: list[Filled] = []
        for day in sorted(days):
            if halt is not None and halt.is_set():
                break
            symbols = sorted(days[day])
            try:
                filled.append(
                    _fill_day(
                        settings,
                        store,
                        object_store,
                        calendar,
                        day,
                        symbols,
                        missing.get(day, []),
                        rescore,
                        halt,
                    )
                )
            except _Halted:
                break
        kind = "rescore" if rescore else "touch_fill"
        if rescore or any(f.written for f in filled):
            store.add_run(kind, now, None, json.dumps([_summary(f) for f in filled]))
        return filled
    finally:
        store.close()


def _fill_day(
    settings: Settings,
    store: PredictionStore,
    object_store: ObjectStore | None,
    calendar: MarketCalendar,
    day: date,
    symbols: list[str],
    missing: Sequence[tuple[str, datetime]],
    rescore: bool,
    halt: threading.Event | None,
) -> Filled:
    """Label ``day``'s windows of ``symbols`` and store them; ``missing`` are its predicted
    bar closes without a label, ``not recorded`` if its events do not label them."""
    try:
        source, labeled = _label(settings, object_store, calendar, day, symbols, halt)
    except _Halted:
        raise
    except Exception as e:  # an unreadable file, the Object Store out of reach: tried again later
        log.exception("could not read the events recorded on %s; its touch labels wait for the next try", day)
        return Filled(day, None, 0, 0, failed=f"{type(e).__name__}: {e}")
    written = store.add_touches(labeled, replace=rescore)
    # Predicted bar closes still without a label: their symbol was recorded nowhere that day
    # (or its recording holds no window for them).
    done = {(r.symbol, r.bar_close) for r in labeled}
    left = [TouchResult(s, c, None, NOT_RECORDED) for s, c in missing if (s, c) not in done]
    if left:
        where = "the data directory" + (f" nor {object_store.name}" if object_store is not None else "")
        log.warning(
            "%s: %d predicted bar closes of %s have no recorded events in %s; left unscorable (%s)",
            day,
            len(left),
            ", ".join(sorted({r.symbol for r in left})),
            where,
            NOT_RECORDED,
        )
        written += store.add_touches(left)
    if labeled:
        log.info(
            "touch labels of %s from %s: %d windows, %d rows written", day, source, len(labeled), written
        )
    return Filled(day, source, len(labeled), written, len(left))


def _label(
    settings: Settings,
    object_store: ObjectStore | None,
    calendar: MarketCalendar,
    day: date,
    symbols: Sequence[str],
    halt: threading.Event | None,
) -> tuple[str | None, list[TouchResult]]:
    """``day``'s windows of ``symbols``, each symbol's from its events in the data directory,
    else in the Object Store (copied to a temporary directory); none for a symbol neither
    has. And where they came from."""
    sources: list[str] = []
    labeled: list[TouchResult] = []
    local = [s for s in symbols if _recorded(settings.data_dir, day, s)]
    if local:
        sources.append("the data directory")
        events = recorded_events(settings.data_dir, day, local, feeds=TOUCH_FEEDS)
        labeled += label_day(events, calendar, day, local, settings.fee_rate, halt)
    rest = [s for s in symbols if s not in local]
    if rest and object_store is not None:
        with tempfile.TemporaryDirectory(prefix="krx-quant-touch-") as tmp:
            root = Path(tmp)
            for key in object_store.keys(f"events/date={day.isoformat()}/"):
                parts = key.split("/")  # events, date=…, feed=…, symbol=…, file
                if (
                    len(parts) == 5
                    and parts[2].removeprefix("feed=") in TOUCH_FEEDS
                    and parts[3].removeprefix("symbol=") in rest
                ):
                    path = root / key
                    path.parent.mkdir(parents=True, exist_ok=True)
                    object_store.get(key, path)
            stored = [s for s in rest if _recorded(root, day, s)]
            if stored:
                sources.append(object_store.name)
                events = recorded_events(root, day, stored, feeds=TOUCH_FEEDS)
                labeled += label_day(events, calendar, day, stored, settings.fee_rate, halt)
    return " and ".join(sources) or None, labeled


def _recorded(data_dir: Path, day: date, symbol: str) -> bool:
    """Whether ``symbol``'s quotes or trades were recorded on ``day`` under ``data_dir``."""
    root = data_dir / "events" / f"date={day.isoformat()}"
    return any(partition_files(root / f"feed={feed}" / f"symbol={symbol}") for feed in TOUCH_FEEDS)


def _summary(f: Filled) -> dict[str, object]:
    return {
        "day": f.day.isoformat(),
        "source": f.source,
        "labeled": f.labeled,
        "written": f.written,
        "not_recorded": f.not_recorded,
        "failed": f.failed,
    }


async def keep_touches_filled(settings: Settings, engine: PredictionEngine, lock: EngineLock) -> None:
    """Fill the past bar closes' missing touch labels once, in a worker thread, as soon as
    this engine holds the engine lock (a rescore may hold it); then move the store's version
    on, so the cached statistics are computed again. The collector starts it after the Engine
    API: recording, predicting and serving go on meanwhile. A failure is logged; the next
    start tries again."""
    waiting = False
    while not lock.acquire():
        if not waiting:
            log.warning("%s is held (a rescore runs): past touch labels wait until it is done", lock.path)
            waiting = True
        await asyncio.sleep(LOCK_POLL_SECONDS)
    halt = threading.Event()
    # The engine's calendar as it stands, not shared with the thread.
    calendar = MarketCalendar(engine.calendar.known())
    try:
        filled = await asyncio.to_thread(
            fill_touches, settings, engine.clock.now(), halt=halt, calendar=calendar
        )
    except asyncio.CancelledError:
        halt.set()  # shutting down: the thread stops at its next batch
        raise
    except Exception:
        log.exception("could not fill the past touch labels")
        return
    if filled:
        log.info(
            "past touch labels: %d rows written over %d days",
            sum(f.written for f in filled),
            len(filled),
        )
    if any(f.written for f in filled):
        engine.store.changed()


def rescore(settings: Settings, now: datetime) -> list[Filled]:
    """``krx-quant rescore``: compute every finished day's touch labels of this target version
    again, replacing those that differ. Raises EngineRunning while the engine runs."""
    lock = EngineLock(settings.state_dir)
    if not lock.acquire():
        raise EngineRunning(
            f"the engine is running (it holds {lock.path}): stop it first (docker compose stop engine)"
        )
    try:
        return fill_touches(settings, now, rescore=True)
    finally:
        lock.release()
