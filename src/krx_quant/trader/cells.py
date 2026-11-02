"""The cells the trader trades (#394, #395), their predictions as the engine displays them, and the
special situations the engine knows of.

A cell is a (kind, predictor): the direction's ``consensus-final``, the barrier's ``breiman``.
Which cells are traded is picked from the cost map (``selection.py``). Their predictions are read
from the engine's database (``state/engine.sqlite3``), opened read-only at each read
(``mode=ro``, ``EngineReader``): the trader never writes there. The engine keeps it in
write-ahead-log mode, so a reader sees each commit as it is made.

- The direction's (``predictions``, #160): its three probabilities (up, flat, down), and from its
  outcome row, written with it, the bar close's mid-point (``mid_close``, the touch label's
  reference) and k (``flat_ticks``); the tick is that mid-point's. A final consensus the engine
  blocked (``blocks``: special, no_reference, expired, #88) carries why.
- A kind's (``kind_predictions``): its output's three probabilities in the kind's order, the
  bar close's mid-point (``anchor``) and tick (#210), and its barrier width (``width_ticks``,
  #183).
- The special situations' windows the engine kept as it learned them (``special_windows``, #236,
  ``special.py``): 005930's VIs and the whole market's circuit breakers and sidecars, each with
  its start and end (a VI's end, while its release is not known, the least it lasts).

Each prediction comes with its model version (``model_version``, #469): the nightly training goes on
within a frozen policy version, and each trade keeps the model that bought it. A cell's predictor is
gone (``gone``, #469) when the latest trading day before a pick's day that its kind has predictions
of 005930 on has none of it (any status): it was retired or taken out of the engine. Whole days
alone: the pick's own day may have only the bar closes a slower predictor has not finished yet. A
day the engine made none of the kind proves nothing, and nothing is gone then.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path

from krx_quant.feeds.base import KST
from krx_quant.kind_registry import kind_named
from krx_quant.kinds import DIRECTION, Kind
from krx_quant.pricing import tick_size
from krx_quant.special import Kind as SpecialKind
from krx_quant.special import Window

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Cell:
    kind: str
    predictor: str


@dataclass(frozen=True)
class Signal:
    """A prediction of the cell, as the trader reads it."""

    prediction_id: int  # predictions.id (the direction) or kind_predictions.id
    kind: str
    predictor: str
    symbol: str
    bar_close: datetime
    displayed_at: datetime
    status: str
    probabilities: tuple[float, float, float] | None  # in the kind's order; None unless ok
    rise: int  # the place of the kind's rise among them: up, up_first
    anchor: float | None  # the bar close's mid-point
    tick: float | None
    width: int | None  # the barriers' width in ticks: k for the direction, w for a barrier
    horizon: timedelta  # the kind's: the time barrier is the bar close plus it
    blocked: str | None = None  # why the engine blocked a final consensus (#88)
    model_version: str | None = None  # the model that made it (#469)

    @property
    def rise_probability(self) -> float | None:
        return None if self.probabilities is None else self.probabilities[self.rise]


def traded_kind(name: str) -> Kind:
    kind = kind_named(name)
    if kind is None or kind.sides is None:
        raise ValueError(f"{name!r} is not a kind with a rise")
    return kind


def open_engine_db(path: Path) -> sqlite3.Connection:
    """The engine's database, read-only: a write through it fails."""
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


class CellReader:
    """A cell's predictions of ``symbol`` displayed since the last read, oldest first."""

    def __init__(self, cell: Cell, symbol: str, since: datetime) -> None:
        self.cell = cell
        self.symbol = symbol
        self._kind = traded_kind(cell.kind)
        # The latest bar close read, as the engine stores times: ISO 8601 in KST, which sorts in time.
        self._after = since.astimezone(KST).isoformat()

    def read(self, db: sqlite3.Connection) -> list[Signal]:
        signals = self._direction(db) if self._kind.name == DIRECTION else self._other(db)
        if signals:
            self._after = max(self._after, max(s.bar_close for s in signals).astimezone(KST).isoformat())
        return signals

    def _direction(self, db: sqlite3.Connection) -> list[Signal]:
        rows = db.execute(
            f"SELECT {DIRECTION_COLUMNS} FROM {DIRECTION_FROM}"
            " WHERE p.algorithm = ? AND p.symbol = ? AND p.bar_close > ? ORDER BY p.bar_close, p.id",
            (self.cell.predictor, self.symbol, self._after),
        ).fetchall()
        return [direction_signal(r, self.cell, self.symbol) for r in rows]

    def _other(self, db: sqlite3.Connection) -> list[Signal]:
        rows = db.execute(
            f"SELECT {KIND_COLUMNS} FROM kind_predictions p"
            " WHERE p.kind = ? AND p.symbol = ? AND p.algorithm = ? AND p.bar_close > ?"
            " ORDER BY p.bar_close, p.id",
            (self._kind.name, self.symbol, self.cell.predictor, self._after),
        ).fetchall()
        return [kind_signal(r, self.cell, self.symbol) for r in rows]


# A cell's predictions as the trader reads them (``CellReader``; the re-fit's replay of the recorded
# days reads them so too, #470, ``replay.py``): the columns and the tables of a direction's (``p``,
# its outcome ``o`` written with it, its block ``b``) and a kind's (``p``), and a row read as a signal.
DIRECTION_COLUMNS = (
    "p.id, p.bar_close, p.displayed_at, p.status, p.p_up, p.p_flat, p.p_down, p.model_version,"
    " o.mid_close, o.flat_ticks, b.reason AS blocked"
)
DIRECTION_FROM = (
    "predictions p LEFT JOIN outcomes o ON o.prediction_id = p.id"
    " LEFT JOIN blocks b ON b.prediction_id = p.id"
)
KIND_COLUMNS = (
    "p.id, p.bar_close, p.displayed_at, p.status, p.output, p.anchor, p.tick, p.width_ticks, p.model_version"
)


def direction_signal(r: sqlite3.Row, cell: Cell, symbol: str) -> Signal:
    """A direction's prediction row (``DIRECTION_COLUMNS``) as the trader reads it."""
    kind = traded_kind(cell.kind)
    ok = r["status"] == "ok" and None not in (r["p_up"], r["p_flat"], r["p_down"])
    anchor = r["mid_close"]
    return Signal(
        prediction_id=r["id"],
        kind=kind.name,
        predictor=cell.predictor,
        symbol=symbol,
        bar_close=datetime.fromisoformat(r["bar_close"]),
        displayed_at=datetime.fromisoformat(r["displayed_at"]),
        status=r["status"],
        probabilities=(r["p_up"], r["p_flat"], r["p_down"]) if ok else None,
        rise=_rise(kind),
        anchor=anchor,
        tick=None if anchor is None else float(tick_size(anchor)),
        width=r["flat_ticks"],
        horizon=kind.horizon,
        blocked=r["blocked"],
        model_version=r["model_version"],
    )


def kind_signal(r: sqlite3.Row, cell: Cell, symbol: str) -> Signal:
    """A kind's prediction row (``KIND_COLUMNS``) as the trader reads it: an output it cannot read
    gives no probabilities (logged)."""
    kind = traded_kind(cell.kind)
    probabilities = None
    if r["status"] == "ok" and r["output"] is not None:
        try:
            probabilities = kind.probabilities(json.loads(r["output"]))
        except (ValueError, KeyError, TypeError):
            log.warning("trader: kind prediction %s has an output it cannot read", r["id"])
    return Signal(
        prediction_id=r["id"],
        kind=kind.name,
        predictor=cell.predictor,
        symbol=symbol,
        bar_close=datetime.fromisoformat(r["bar_close"]),
        displayed_at=datetime.fromisoformat(r["displayed_at"]),
        status=r["status"],
        probabilities=probabilities,
        rise=_rise(kind),
        anchor=r["anchor"],
        tick=None if r["tick"] is None else float(r["tick"]),
        width=r["width_ticks"],
        horizon=kind.horizon,
        model_version=r["model_version"],
    )


def _rise(kind: Kind) -> int:
    """The place of the kind's rise among its probabilities: up, up_first."""
    return kind.index(kind.sides[0]) if kind.sides else 0


class EngineReader:
    """The engine's database, read-only at each read: the followed cells' new predictions of
    ``symbol`` and the special windows kept that touch it."""

    def __init__(self, path: Path, symbol: str) -> None:
        self.path = path
        self.symbol = symbol
        self._readers: dict[Cell, CellReader] = {}
        self._missing = False

    @property
    def cells(self) -> list[Cell]:
        return list(self._readers)

    def follow(self, cells: Iterable[Cell], since: datetime) -> None:
        """Read ``cells`` from now on: one already followed goes on from where it was, a new one
        from the predictions of bar closes after ``since``; the others are no longer read."""
        self._readers = {c: self._readers.get(c) or CellReader(c, self.symbol, since) for c in cells}

    def read(self, windows_since: datetime) -> tuple[list[Window], list[Signal]] | None:
        """The special windows kept that end after ``windows_since``, and the followed cells' new
        predictions by bar close; None while there is no engine database."""
        if not self.path.exists():
            if not self._missing:
                log.warning("trader: no engine database at %s yet", self.path)
            self._missing = True
            return None
        self._missing = False
        db = open_engine_db(self.path)
        try:
            windows = special_windows(db, self.symbol, windows_since)
            signals = [s for reader in self._readers.values() for s in reader.read(db)]
        finally:
            db.close()
        signals.sort(key=lambda s: (s.bar_close, s.displayed_at))
        return windows, signals


def gone(path: Path, symbol: str, cells: Collection[Cell], at: datetime) -> set[Cell]:
    """The ``cells`` whose predictor is gone at a pick at ``at`` (#469, module doc): the latest trading
    day before ``at``'s day with predictions of the cell's kind of ``symbol`` has none of its
    predictor. None without the engine's database (read-only, at once)."""
    if not cells or not path.exists():
        return set()
    day = datetime.combine(at.astimezone(KST).date(), time(), KST).isoformat()
    db = open_engine_db(path)
    try:
        return {c for c in cells if _gone(db, c, symbol, day)}
    finally:
        db.close()


def _gone(db: sqlite3.Connection, cell: Cell, symbol: str, before: str) -> bool:
    if traded_kind(cell.kind).name == DIRECTION:
        latest = "SELECT max(bar_close) FROM predictions WHERE symbol = ? AND bar_close < ?"
        its = (
            "SELECT 1 FROM predictions WHERE algorithm = ? AND symbol = ? AND bar_close >= ?"
            " AND bar_close < ? LIMIT 1"
        )
        keys: tuple[str, ...] = (symbol,)
    else:
        latest = "SELECT max(bar_close) FROM kind_predictions WHERE kind = ? AND symbol = ? AND bar_close < ?"
        its = (
            "SELECT 1 FROM kind_predictions WHERE algorithm = ? AND kind = ? AND symbol = ?"
            " AND bar_close >= ? AND bar_close < ? LIMIT 1"
        )
        keys = (cell.kind, symbol)
    last = db.execute(latest, (*keys, before)).fetchone()[0]
    if last is None:
        return False  # no prediction of the kind at all: nothing to tell from
    day = datetime.combine(datetime.fromisoformat(last).astimezone(KST).date(), time(), KST)
    return db.execute(its, (cell.predictor, *keys, day.isoformat(), before)).fetchone() is None


def special_windows(db: sqlite3.Connection, symbol: str, since: datetime) -> list[Window]:
    """The special windows the engine kept (#236) of ``symbol`` or the whole market that end after
    ``since``, by start."""
    rows = db.execute(
        "SELECT kind, symbol, start, end, detail FROM special_windows"
        " WHERE end > ? AND (symbol IS NULL OR symbol = ?) ORDER BY start",
        (since.astimezone(KST).isoformat(), symbol),
    ).fetchall()
    windows = []
    for r in rows:
        try:
            kind = SpecialKind(r["kind"])
        except ValueError:
            log.warning("trader: a special window of a kind it does not know (%s)", r["kind"])
            continue
        start, end = datetime.fromisoformat(r["start"]), datetime.fromisoformat(r["end"])
        windows.append(Window(kind, r["symbol"], start, end, r["detail"]))
    return windows
