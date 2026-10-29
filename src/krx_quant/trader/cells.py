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
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
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
        self._rise = self._kind.index(self._kind.sides[0]) if self._kind.sides else 0
        # The latest bar close read, as the engine stores times: ISO 8601 in KST, which sorts in time.
        self._after = since.astimezone(KST).isoformat()

    def read(self, db: sqlite3.Connection) -> list[Signal]:
        signals = self._direction(db) if self._kind.name == DIRECTION else self._other(db)
        if signals:
            self._after = max(self._after, max(s.bar_close for s in signals).astimezone(KST).isoformat())
        return signals

    def _direction(self, db: sqlite3.Connection) -> list[Signal]:
        rows = db.execute(
            "SELECT p.id, p.bar_close, p.displayed_at, p.status, p.p_up, p.p_flat, p.p_down,"
            " o.mid_close, o.flat_ticks, b.reason AS blocked"
            " FROM predictions p LEFT JOIN outcomes o ON o.prediction_id = p.id"
            " LEFT JOIN blocks b ON b.prediction_id = p.id"
            " WHERE p.algorithm = ? AND p.symbol = ? AND p.bar_close > ? ORDER BY p.bar_close, p.id",
            (self.cell.predictor, self.symbol, self._after),
        ).fetchall()
        signals = []
        for r in rows:
            ok = r["status"] == "ok" and None not in (r["p_up"], r["p_flat"], r["p_down"])
            anchor = r["mid_close"]
            signals.append(
                Signal(
                    prediction_id=r["id"],
                    kind=self._kind.name,
                    predictor=self.cell.predictor,
                    symbol=self.symbol,
                    bar_close=datetime.fromisoformat(r["bar_close"]),
                    displayed_at=datetime.fromisoformat(r["displayed_at"]),
                    status=r["status"],
                    probabilities=(r["p_up"], r["p_flat"], r["p_down"]) if ok else None,
                    rise=self._rise,
                    anchor=anchor,
                    tick=None if anchor is None else float(tick_size(anchor)),
                    width=r["flat_ticks"],
                    horizon=self._kind.horizon,
                    blocked=r["blocked"],
                )
            )
        return signals

    def _other(self, db: sqlite3.Connection) -> list[Signal]:
        rows = db.execute(
            "SELECT id, bar_close, displayed_at, status, output, anchor, tick, width_ticks"
            " FROM kind_predictions WHERE kind = ? AND symbol = ? AND algorithm = ? AND bar_close > ?"
            " ORDER BY bar_close, id",
            (self._kind.name, self.symbol, self.cell.predictor, self._after),
        ).fetchall()
        signals = []
        for r in rows:
            probabilities = None
            if r["status"] == "ok" and r["output"] is not None:
                try:
                    probabilities = self._kind.probabilities(json.loads(r["output"]))
                except (ValueError, KeyError, TypeError):
                    log.warning("trader: kind prediction %s has an output it cannot read", r["id"])
            signals.append(
                Signal(
                    prediction_id=r["id"],
                    kind=self._kind.name,
                    predictor=self.cell.predictor,
                    symbol=self.symbol,
                    bar_close=datetime.fromisoformat(r["bar_close"]),
                    displayed_at=datetime.fromisoformat(r["displayed_at"]),
                    status=r["status"],
                    probabilities=probabilities,
                    rise=self._rise,
                    anchor=r["anchor"],
                    tick=None if r["tick"] is None else float(r["tick"]),
                    width=r["width_ticks"],
                    horizon=self._kind.horizon,
                )
            )
        return signals


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
