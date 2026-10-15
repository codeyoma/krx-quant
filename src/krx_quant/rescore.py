"""Past bar closes' touch labels (#160, #168) and kinds' answers (#180), from their
recorded days.

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

The kinds besides the direction (#179) are filled the same way at start, after the touch
labels (``fill_kind_answers``, #180): every bar close predicted since the recording began and
before today without the kind's answer of its answer version gets the one the Replay
Harness gives it (its class, why it has none, its path, when it was scored and its special
mark: the day's VI statuses, KOSPI index and futures are read for the mark, as the engine
marks it; a kind with barriers, #183, has their width from the day's recorded trades and the
previous trading day's backfilled bars in the data directory, as the engine fixes it). Only
the missing rows are written; a bar close recorded nowhere is ``not recorded``. The
baselines do not predict the past bar closes again: they were never shown then. The run is
recorded in ``runs`` (``kind_fill``). ``rescore`` does not compute them again: a fixed answer
rule takes a new answer version, whose rows are all missing.

Last come the special situations' windows (#236, ``fill_special_windows``): the engine keeps
each window it learns (``special_windows``), and a past day since the recording began with
predictions and not decided before (``special_days``) has its windows decided from its
recording as the engine decides them live (``special.py``): the VI status of the symbols
predicted that day, the KOSPI index and its futures, nothing else, one day at a time. The
replay replaces the windows the live engine kept that day (an engine restarted that day knew
only part of it); a day recorded nowhere keeps them. The day is then marked decided, windows
or not, so a quiet day is read once. The run is recorded in ``runs`` (``special_fill``).

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
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import partial
from pathlib import Path
from typing import IO, Any, TypeVar

from krx_quant.archive import partition_files
from krx_quant.backfill import PreviousDay
from krx_quant.config import Settings
from krx_quant.engine import Event, PredictionEngine
from krx_quant.features import Leaders, PreviousDays
from krx_quant.feeds import BOOK, BOOK_SNAPSHOT, INDEX, INDEX_REST, KOSPI200_FUTURES, TRADE, VI_STATUS
from krx_quant.feeds.base import KST, kst_day
from krx_quant.kinds import DIRECTION, Answer, Kind
from krx_quant.market_calendar import MarketCalendar, calendar_store
from krx_quant.market_view import MarketView, prediction_window
from krx_quant.object_store import ObjectStore, open_store
from krx_quant.predictions import DB_NAME, PredictionStore
from krx_quant.reference import BAR
from krx_quant.replay import recorded_events
from krx_quant.special import KOSPI, SpecialSituations, Window, touching
from krx_quant.touch import NOT_RECORDED, WINDOW, TouchResult

log = logging.getLogger(__name__)

# The first recorded day: bar closes before it have no events to be labeled from.
RECORDING_START = date(2026, 10, 12)
# The lock the running engine holds, in the state directory.
LOCK_NAME = ".engine.lock"
# The feeds a touch window follows: the integrated trades and book, and the REST book
# snapshot that replaces the book when the session limit cuts it.
TOUCH_FEEDS = (TRADE.tr_id, BOOK.tr_id, BOOK_SNAPSHOT.tr_id)
# The feeds the kinds' fill reads (#180): a touch window's, and what marks an answer special:
# each symbol's VI status, the KOSPI index (circuit breakers) and its futures (sidecars).
KIND_FEEDS = (*TOUCH_FEEDS, VI_STATUS.tr_id, INDEX.tr_id, INDEX_REST.tr_id, KOSPI200_FUTURES.tr_id)
# Of those, the market's, by the partition read: the KOSPI's, or (None) whichever was recorded
# (the futures' front month that day).
MARKET_FEEDS: dict[str, str | None] = {
    INDEX.tr_id: KOSPI,
    INDEX_REST.tr_id: KOSPI,
    KOSPI200_FUTURES.tr_id: None,
}
# The feeds the special windows' fill reads (#236): the symbols' VI status and the market's.
SPECIAL_FEEDS = (VI_STATUS.tr_id, *MARKET_FEEDS)
# How often an engine started beside a rescore looks whether it is done.
LOCK_POLL_SECONDS = 5.0
# Events between two looks at whether the fill should stop (the engine is shutting down).
_HALT_EVERY = 4096
_T = TypeVar("_T")  # what a day's events give: touch labels, or kinds' answers


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
class Answered:
    """A kind's answer of a past window (#180), as the engine would have stored it."""

    kind: str
    symbol: str
    bar_close: datetime
    answer: Answer
    special: bool  # a VI, circuit breaker or sidecar touched its four minutes


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
    labeled: list[TouchResult] = []
    _follow_day(
        events,
        calendar,
        day,
        symbols,
        fee_rate,
        (),
        lambda view, symbol, close: labeled.append(view.touch(symbol, close)),
        halt=halt,
    )
    return labeled


def answer_day(
    events: Iterable[Event],
    calendar: MarketCalendar,
    day: date,
    symbols: Sequence[str],
    fee_rate: float,
    kinds: Sequence[Kind],
    halt: threading.Event | None = None,
    previous: PreviousDays | None = None,
) -> list[Answered]:
    """Every window of ``symbols`` on ``day``'s bar closes answered by each of ``kinds`` (#180),
    from the day's events (in receipt order) through the engine's Market View, as the engine
    answers them; each special as the engine leaves it: a VI, circuit breaker or sidecar
    known at its bar close, or learned since in any version, touches its four minutes. A kind
    with barriers has their widths from the previous trading days in ``previous`` too (#183),
    as the engine's view has them."""
    special = SpecialSituations(calendar)
    ended: list[tuple[str, datetime, list[Answer]]] = []
    _follow_day(
        events,
        calendar,
        day,
        symbols,
        fee_rate,
        kinds,
        lambda view, symbol, close: ended.append(
            (symbol, close, [view.kind_answer(kind, symbol, close) for kind in kinds])
        ),
        halt=halt,
        also=lambda feed, row: special.event(feed, row, symbols),
        previous=previous,
    )
    out: list[Answered] = []
    for symbol, close, answers in ended:
        end = close + WINDOW
        marked = touching(special.known(before=close), symbol, close, end) or touching(
            special.learned_since(close), symbol, close, end
        )
        out += [Answered(k.name, symbol, close, a, marked) for k, a in zip(kinds, answers, strict=True)]
    return out


def _follow_day(
    events: Iterable[Event],
    calendar: MarketCalendar,
    day: date,
    symbols: Sequence[str],
    fee_rate: float,
    kinds: Sequence[Kind],
    ended: Callable[[MarketView, str, datetime], object],
    *,
    halt: threading.Event | None = None,
    also: Callable[[str, dict[str, Any]], object] | None = None,
    previous: PreviousDays | None = None,
) -> None:
    """Follow ``day``'s windows of ``symbols`` through the engine's Market View (with the
    ``kinds``' quotes, and the previous trading days in ``previous``), giving each to ``ended``
    when its four minutes are over, before anything received then; each event goes to
    ``also`` too."""
    session = calendar.session(day)
    if session is None:
        return
    start = datetime.combine(day, time(), KST)
    view = MarketView(calendar, symbols, Leaders(), start, fee_rate, kinds=kinds, previous=previous)
    first, last = prediction_window(session)
    # Every minute from the first bar close to the last window's end: windows end on the
    # minute, and the engine scores at each one before it predicts the bar close there.
    minutes = deque(first + i * BAR for i in range((last + WINDOW - first) // BAR + 1))

    def reach(moment: datetime) -> None:
        while minutes and minutes[0] <= moment:
            minute = minutes.popleft()
            for symbol, close in view.ended_windows(minute):
                ended(view, symbol, close)
            if minute <= last:
                view.at_close(minute)

    for i, (feed, row) in enumerate(events):
        if halt is not None and i % _HALT_EVERY == 0 and halt.is_set():
            raise _Halted
        reach(row["received_at"])
        view.on_event(feed, row)
        if also is not None:
            also(feed, row)
    reach(last + WINDOW)


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
        source, labeled = _read_day(
            settings,
            object_store,
            day,
            symbols,
            TOUCH_FEEDS,
            {},
            lambda events, chosen: label_day(events, calendar, day, chosen, settings.fee_rate, halt),
        )
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


def _read_day(
    settings: Settings,
    object_store: ObjectStore | None,
    day: date,
    symbols: Sequence[str],
    feeds: Sequence[str],
    market: Mapping[str, str | None],
    run: Callable[[Iterable[Event], Sequence[str]], list[_T]],
) -> tuple[str | None, list[_T]]:
    """``run`` on ``day``'s events of ``feeds`` (the ``market`` ones' partitions besides the
    symbols': the one named, or any if None), each symbol's from the data directory, else
    from the Object Store (copied to a temporary directory); not for a symbol neither has.
    What it gave, and where the events came from."""
    sources: list[str] = []
    out: list[_T] = []
    local = [s for s in symbols if _recorded(settings.data_dir, day, s)]
    if local:
        sources.append("the data directory")
        out += run(recorded_events(settings.data_dir, day, local, feeds=feeds), local)
    rest = [s for s in symbols if s not in local]
    if rest and object_store is not None:
        with tempfile.TemporaryDirectory(prefix="krx-quant-touch-") as tmp:
            root = Path(tmp)
            for key in object_store.keys(f"events/date={day.isoformat()}/"):
                parts = key.split("/")  # events, date=…, feed=…, symbol=…, file
                if len(parts) != 5:
                    continue
                feed, symbol = parts[2].removeprefix("feed="), parts[3].removeprefix("symbol=")
                wanted = (
                    market[feed] in (None, symbol) if feed in market else feed in feeds and symbol in rest
                )
                if wanted:
                    path = root / key
                    path.parent.mkdir(parents=True, exist_ok=True)
                    object_store.get(key, path)
            stored = [s for s in rest if _recorded(root, day, s)]
            if stored:
                sources.append(object_store.name)
                out += run(recorded_events(root, day, stored, feeds=feeds), stored)
    return " and ".join(sources) or None, out


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


def fill_kind_answers(
    settings: Settings,
    now: datetime,
    *,
    kinds: Sequence[Kind],
    halt: threading.Event | None = None,
    calendar: MarketCalendar | None = None,
) -> list[Filled]:
    """The past bar closes' answers of the ``kinds`` besides the direction (#180), from their
    recorded days, on a database connection of its own (a worker thread may run it beside
    the engine).

    The days are those from ``RECORDING_START`` before ``now``'s day with a predicted bar
    close lacking a kind's answer of its answer version. Every window of their symbols that
    day is answered, as a fresh replay answers it, and only the missing rows are stored; a
    predicted bar close its day's recording does not answer is ``not recorded``. A day that
    cannot be read is logged and left for the next time. ``halt`` stops it between batches of
    events, writing nothing more.
    """
    answered = [k for k in kinds if k.name != DIRECTION and k.answer is not None]
    if not answered:
        return []
    calendar = calendar or calendar_store(settings).calendar
    today = kst_day(now)
    store = PredictionStore(settings.state_dir / DB_NAME)
    try:
        # By day: each predicted bar close lacking an answer, and the kinds it lacks.
        missing: dict[date, dict[tuple[str, datetime], set[str]]] = {}
        for kind in answered:
            for symbol, close in store.unanswered(kind.name, kind.answer_version, RECORDING_START, today):
                missing.setdefault(kst_day(close), {}).setdefault((symbol, close), set()).add(kind.name)
        if not missing:
            return []
        object_store = open_store(settings)
        filled: list[Filled] = []
        for day in sorted(missing):
            if halt is not None and halt.is_set():
                break
            try:
                filled.append(
                    _answer_day(settings, store, object_store, calendar, day, answered, missing[day], halt)
                )
            except _Halted:
                break
        if any(f.written for f in filled):
            store.add_run("kind_fill", now, None, json.dumps([_summary(f) for f in filled]))
        return filled
    finally:
        store.close()


def _answer_day(
    settings: Settings,
    store: PredictionStore,
    object_store: ObjectStore | None,
    calendar: MarketCalendar,
    day: date,
    kinds: Sequence[Kind],
    missing: Mapping[tuple[str, datetime], set[str]],
    halt: threading.Event | None,
) -> Filled:
    """Answer ``day``'s windows of the symbols ``missing`` names by ``kinds`` and store the
    missing ones; a predicted bar close its recording does not answer is ``not recorded``."""
    symbols = sorted({symbol for symbol, _ in missing})
    try:
        source, answers = _read_day(
            settings,
            object_store,
            day,
            symbols,
            KIND_FEEDS,
            MARKET_FEEDS,
            lambda events, chosen: answer_day(
                events,
                calendar,
                day,
                chosen,
                settings.fee_rate,
                kinds,
                halt,
                PreviousDay(settings.data_dir, calendar),
            ),
        )
    except _Halted:
        raise
    except Exception as e:  # an unreadable file, the Object Store out of reach: tried again later
        log.exception(
            "could not read the events recorded on %s; its kinds' answers wait for the next try", day
        )
        return Filled(day, None, 0, 0, failed=f"{type(e).__name__}: {e}")
    rows: dict[str, list[tuple[str, datetime, Answer, bool]]] = {k.name: [] for k in kinds}
    for a in answers:
        rows[a.kind].append((a.symbol, a.bar_close, a.answer, a.special))
    done = {(a.kind, a.symbol, a.bar_close) for a in answers}
    left = sorted(
        (symbol, close, name)
        for (symbol, close), names in missing.items()
        for name in names
        if (name, symbol, close) not in done
    )
    for symbol, close, name in left:
        rows[name].append((symbol, close, Answer(None, NOT_RECORDED), False))
    if left:
        where = "the data directory" + (f" nor {object_store.name}" if object_store is not None else "")
        log.warning(
            "%s: %d predicted bar closes of %s have no recorded events in %s; their kinds' answers"
            " left unscorable (%s)",
            day,
            len({(symbol, close) for symbol, close, _ in left}),
            ", ".join(sorted({symbol for symbol, _, _ in left})),
            where,
            NOT_RECORDED,
        )
    written = sum(len(store.add_kind_answers(k.name, k.answer_version, rows[k.name])) for k in kinds)
    windows = len({(a.symbol, a.bar_close) for a in answers})
    if answers:
        log.info("kinds' answers of %s from %s: %d windows, %d rows written", day, source, windows, written)
    return Filled(day, source, windows, written, len({(symbol, close) for symbol, close, _ in left}))


def decide_day(
    events: Iterable[Event],
    calendar: MarketCalendar,
    symbols: Sequence[str],
    halt: threading.Event | None = None,
) -> list[tuple[datetime, Window]]:
    """A day's special windows decided from its events (in receipt order) as the engine
    decides them (#236): the VIs of ``symbols``, the circuit breakers and the sidecars, the
    latest version of each with when it was received, as the engine keeps them."""
    special = SpecialSituations(calendar)
    for i, (feed, row) in enumerate(events):
        if halt is not None and i % _HALT_EVERY == 0 and halt.is_set():
            raise _Halted
        special.event(feed, row, symbols)
    return special.latest()


def fill_special_windows(
    settings: Settings,
    now: datetime,
    *,
    halt: threading.Event | None = None,
    calendar: MarketCalendar | None = None,
) -> list[Filled]:
    """The special windows of past days (#236), decided from their recordings, on a database
    connection of its own (a worker thread may run it beside the engine).

    The days are those from ``RECORDING_START`` before ``now``'s day with predictions and not
    decided before (``special_days``). Each is decided as the engine decides it live
    (``decide_day``), its VIs those of the symbols predicted that day, and stored with the day
    marked decided, in place of the windows the live engine kept that day: a quiet day is read
    once. A day recorded nowhere is marked decided
    without windows, and logged; a day that cannot be read is logged and left for the next
    time. ``halt`` stops it between batches of events, writing nothing more.
    """
    calendar = calendar or calendar_store(settings).calendar
    store = PredictionStore(settings.state_dir / DB_NAME)
    try:
        days = store.special_undecided(RECORDING_START, kst_day(now))
        if not days:
            return []
        object_store = open_store(settings)
        filled: list[Filled] = []
        for day in sorted(days):
            if halt is not None and halt.is_set():
                break
            try:
                filled.append(
                    _decide_special_day(settings, store, object_store, calendar, day, days[day], now, halt)
                )
            except _Halted:
                break
        if any(f.failed is None for f in filled):
            store.add_run("special_fill", now, None, json.dumps([_summary(f) for f in filled]))
        return filled
    finally:
        store.close()


def _decide_special_day(
    settings: Settings,
    store: PredictionStore,
    object_store: ObjectStore | None,
    calendar: MarketCalendar,
    day: date,
    symbols: Sequence[str],
    now: datetime,
    halt: threading.Event | None,
) -> Filled:
    """Decide ``day``'s special windows (VIs of ``symbols``) from its recording and keep them."""
    try:
        source, windows = _read_special_day(
            settings, object_store, day, symbols, lambda events: decide_day(events, calendar, symbols, halt)
        )
    except _Halted:
        raise
    except Exception as e:  # an unreadable file, the Object Store out of reach: tried again later
        log.exception(
            "could not read the events recorded on %s; its special windows wait for the next try", day
        )
        return Filled(day, None, 0, 0, failed=f"{type(e).__name__}: {e}")
    if source is None:
        where = "the data directory" + (f" nor {object_store.name}" if object_store is not None else "")
        log.warning(
            "%s: no VI status, KOSPI index nor KOSPI200 futures recorded in %s; no special windows kept",
            day,
            where,
        )
    written = store.decide_special_day(day, windows, source, now)
    log.info("special windows of %s from %s: %d windows, %d rows written", day, source, len(windows), written)
    return Filled(day, source, len(windows), written)


def _read_special_day(
    settings: Settings,
    object_store: ObjectStore | None,
    day: date,
    symbols: Sequence[str],
    run: Callable[[Iterable[Event]], list[_T]],
) -> tuple[str | None, list[_T]]:
    """``run`` on ``day``'s events of ``SPECIAL_FEEDS`` (the VI status of ``symbols``, the
    market's partitions): from the data directory if it has any of them, else from the Object
    Store (copied to a temporary directory). What it gave and where the events came from;
    None and nothing where neither has them."""
    if _special_recorded(settings.data_dir, day, symbols):
        events = recorded_events(settings.data_dir, day, symbols, feeds=SPECIAL_FEEDS)
        return "the data directory", run(events)
    if object_store is None:
        return None, []
    with tempfile.TemporaryDirectory(prefix="krx-quant-special-") as tmp:
        root = Path(tmp)
        for key in object_store.keys(f"events/date={day.isoformat()}/"):
            parts = key.split("/")  # events, date=…, feed=…, symbol=…, file
            if len(parts) != 5:
                continue
            feed, symbol = parts[2].removeprefix("feed="), parts[3].removeprefix("symbol=")
            if feed in MARKET_FEEDS:
                wanted = MARKET_FEEDS[feed] in (None, symbol)
            else:
                wanted = feed == VI_STATUS.tr_id and symbol in symbols
            if wanted:
                path = root / key
                path.parent.mkdir(parents=True, exist_ok=True)
                object_store.get(key, path)
        if _special_recorded(root, day, symbols):
            return object_store.name, run(recorded_events(root, day, symbols, feeds=SPECIAL_FEEDS))
    return None, []


def _special_recorded(data_dir: Path, day: date, symbols: Sequence[str]) -> bool:
    """Whether any of ``SPECIAL_FEEDS`` was recorded on ``day`` under ``data_dir``: the VI
    status of one of ``symbols``, the KOSPI index or its futures."""
    root = data_dir / "events" / f"date={day.isoformat()}"
    partitions = [root / f"feed={VI_STATUS.tr_id}" / f"symbol={s}" for s in symbols]
    for feed, key in MARKET_FEEDS.items():
        if key is None:  # whichever was recorded (the futures' front month)
            partitions += sorted((root / f"feed={feed}").glob("symbol=*"))
        else:
            partitions.append(root / f"feed={feed}" / f"symbol={key}")
    return any(p.is_dir() and partition_files(p) for p in partitions)


async def keep_touches_filled(settings: Settings, engine: PredictionEngine, lock: EngineLock) -> None:
    """Fill the past bar closes' missing touch labels once, then the missing answers of the
    engine's kinds besides the direction (#180), then the past days' special windows (#236), in
    a worker thread, as soon as this engine holds the engine lock (a rescore may hold it); after
    each that wrote, move the store's version on, so the cached statistics are computed again.
    The collector starts it after the Engine API: recording, predicting and serving go on
    meanwhile. A failure is logged; the next start tries again."""
    waiting = False
    while not lock.acquire():
        if not waiting:
            log.warning("%s is held (a rescore runs): past touch labels wait until it is done", lock.path)
            waiting = True
        await asyncio.sleep(LOCK_POLL_SECONDS)
    halt = threading.Event()
    # The engine's calendar as it stands, not shared with the thread.
    calendar = MarketCalendar(engine.calendar.known())
    now = engine.clock.now()
    fills: list[tuple[str, Callable[[], list[Filled]]]] = [
        ("touch labels", partial(fill_touches, settings, now, halt=halt, calendar=calendar)),
        (
            "kinds' answers",
            partial(fill_kind_answers, settings, now, kinds=engine.kinds, halt=halt, calendar=calendar),
        ),
        ("special windows", partial(fill_special_windows, settings, now, halt=halt, calendar=calendar)),
    ]
    for what, fill in fills:
        try:
            filled = await asyncio.to_thread(fill)
        except asyncio.CancelledError:
            halt.set()  # shutting down: the thread stops at its next batch
            raise
        except Exception:
            log.exception("could not fill the past %s", what)
            continue
        if filled:
            log.info(
                "past %s: %d rows written over %d days", what, sum(f.written for f in filled), len(filled)
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
