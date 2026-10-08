"""Prediction store: predictions and their outcomes in SQLite.

``predictions`` holds what each algorithm said at each bar close; ``outcomes`` holds the
mid-points and classes it is scored by. The outcome row is written with the prediction
(the mid-points at the bar close and at display time are known then) and completed at
the target time, so a restarted engine can still score what was pending.

Times are ISO 8601 strings in KST, which sort in time order.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from krx_quant.baselines import Forecast
from krx_quant.feeds.base import KST
from krx_quant.pricing import Direction

# The engine's database, in the state directory.
DB_NAME = "engine.sqlite3"

_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    algorithm TEXT NOT NULL,          -- an algorithm, a baseline, or a consensus
    model_version TEXT NOT NULL,
    bar_close TEXT NOT NULL,          -- the prediction time t
    displayed_at TEXT NOT NULL,       -- when it became visible
    p_up REAL NOT NULL,
    p_flat REAL NOT NULL,
    p_down REAL NOT NULL,
    expected_ticks REAL NOT NULL,
    status TEXT NOT NULL,             -- ok
    UNIQUE (symbol, algorithm, bar_close)
);
CREATE INDEX IF NOT EXISTS predictions_by_time ON predictions (symbol, bar_close);
CREATE TABLE IF NOT EXISTS outcomes (
    prediction_id INTEGER PRIMARY KEY REFERENCES predictions (id),
    target_at TEXT NOT NULL,          -- t + 4 minutes
    mid_close REAL,                   -- basis A: the mid-point at the bar close
    mid_display REAL,                 -- basis B: the mid-point when displayed
    mid_target REAL,
    flat_ticks INTEGER,               -- k: smaller moves are flat
    class_a TEXT,                     -- up, flat or down
    class_b TEXT,
    special INTEGER NOT NULL DEFAULT 0,  -- a VI or circuit breaker fell in the window
    unscorable TEXT,                  -- why a basis could not be scored
    scored_at TEXT                    -- null while pending
);
CREATE INDEX IF NOT EXISTS outcomes_pending ON outcomes (scored_at, target_at);
"""


def iso(at: datetime) -> str:
    return at.astimezone(KST).isoformat()


@dataclass(frozen=True)
class NewPrediction:
    symbol: str
    algorithm: str
    model_version: str
    bar_close: datetime
    displayed_at: datetime
    forecast: Forecast
    target_at: datetime
    mid_close: float | None
    mid_display: float | None
    flat_ticks: int | None
    status: str = "ok"


@dataclass(frozen=True)
class Pending:
    prediction_id: int
    symbol: str
    target_at: datetime
    mid_close: float | None
    mid_display: float | None
    flat_ticks: int | None


@dataclass(frozen=True)
class AlgorithmSummary:
    """Counts on basis A."""

    algorithm: str
    predictions: int
    scored: int
    unscorable: int
    pending: int
    up: int  # scored outcomes by class
    flat: int
    down: int


@dataclass(frozen=True)
class Score:
    mid_target: float | None
    class_a: Direction | None
    class_b: Direction | None
    unscorable: str | None


class PredictionStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        # With WAL, a power loss can lose the last commits but never corrupts the file;
        # commits then do not wait for the disk, which would hold up the event loop.
        self._db.execute("PRAGMA synchronous=NORMAL")
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version > _VERSION:
            raise RuntimeError(f"{path} was written by a newer engine (schema {version})")
        with self._db:
            self._db.executescript(_SCHEMA)
            self._db.execute(f"PRAGMA user_version={_VERSION}")

    def close(self) -> None:
        self._db.close()

    def add_all(self, predictions: Sequence[NewPrediction]) -> list[Pending]:
        """Store a bar close's predictions in one transaction; they are pending until scored."""
        added: list[Pending] = []
        with self._db:
            for p in predictions:
                f = p.forecast
                cursor = self._db.execute(
                    "INSERT INTO predictions (symbol, algorithm, model_version, bar_close, displayed_at,"
                    " p_up, p_flat, p_down, expected_ticks, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        p.symbol,
                        p.algorithm,
                        p.model_version,
                        iso(p.bar_close),
                        iso(p.displayed_at),
                        f.p_up,
                        f.p_flat,
                        f.p_down,
                        f.expected_ticks,
                        p.status,
                    ),
                )
                assert cursor.lastrowid is not None
                self._db.execute(
                    "INSERT INTO outcomes (prediction_id, target_at, mid_close, mid_display, flat_ticks)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (cursor.lastrowid, iso(p.target_at), p.mid_close, p.mid_display, p.flat_ticks),
                )
                added.append(
                    Pending(cursor.lastrowid, p.symbol, p.target_at, p.mid_close, p.mid_display, p.flat_ticks)
                )
        return added

    def pending(self) -> list[Pending]:
        rows = self._db.execute(
            "SELECT o.prediction_id, p.symbol, o.target_at, o.mid_close, o.mid_display, o.flat_ticks"
            " FROM outcomes o JOIN predictions p ON p.id = o.prediction_id"
            " WHERE o.scored_at IS NULL ORDER BY o.target_at, o.prediction_id"
        ).fetchall()
        return [
            Pending(
                r["prediction_id"],
                r["symbol"],
                datetime.fromisoformat(r["target_at"]),
                r["mid_close"],
                r["mid_display"],
                r["flat_ticks"],
            )
            for r in rows
        ]

    def score_all(self, scores: Sequence[tuple[int, Score]], scored_at: datetime) -> None:
        """Complete the outcomes of (prediction ID, score) pairs in one transaction."""
        with self._db:
            self._db.executemany(
                "UPDATE outcomes SET mid_target = ?, class_a = ?, class_b = ?, unscorable = ?, scored_at = ?"
                " WHERE prediction_id = ?",
                [
                    (
                        score.mid_target,
                        score.class_a.value if score.class_a else None,
                        score.class_b.value if score.class_b else None,
                        score.unscorable,
                        iso(scored_at),
                        prediction_id,
                    )
                    for prediction_id, score in scores
                ],
            )

    def day_classes(self, symbol: str, since: datetime, until: datetime) -> Mapping[Direction, int]:
        """Classes (basis A) of the bar closes in ``[since, until]`` scored so far, once per bar."""
        rows = self._db.execute(
            "SELECT o.class_a, COUNT(DISTINCT p.bar_close) FROM outcomes o"
            " JOIN predictions p ON p.id = o.prediction_id"
            " WHERE p.symbol = ? AND p.bar_close BETWEEN ? AND ? AND o.class_a IS NOT NULL"
            " GROUP BY o.class_a",
            (symbol, iso(since), iso(until)),
        ).fetchall()
        return {Direction(r[0]): r[1] for r in rows}

    def latest(self, symbol: str) -> list[dict[str, Any]]:
        """The predictions of the symbol's latest bar close."""
        return self._select(
            "WHERE p.symbol = ? AND p.bar_close ="
            " (SELECT MAX(bar_close) FROM predictions WHERE symbol = ?) ORDER BY p.algorithm",
            (symbol, symbol),
        )

    def recent(self, symbol: str, limit: int, algorithm: str | None = None) -> list[dict[str, Any]]:
        """The symbol's predictions of its last ``limit`` bar closes, newest first; of one
        algorithm only if given.
        """
        if algorithm is None:
            return self._select(
                "WHERE p.symbol = ? AND p.bar_close IN (SELECT DISTINCT bar_close FROM predictions"
                " WHERE symbol = ? ORDER BY bar_close DESC LIMIT ?) ORDER BY p.bar_close DESC, p.algorithm",
                (symbol, symbol, limit),
            )
        return self._select(
            "WHERE p.symbol = ? AND p.algorithm = ? ORDER BY p.bar_close DESC LIMIT ?",
            (symbol, algorithm, limit),
        )

    def summary(self) -> list[AlgorithmSummary]:
        rows = self._db.execute(
            "SELECT p.algorithm, COUNT(*), COUNT(o.class_a),"
            " TOTAL(o.scored_at IS NOT NULL AND o.class_a IS NULL), TOTAL(o.scored_at IS NULL),"
            " TOTAL(o.class_a = 'up'), TOTAL(o.class_a = 'flat'), TOTAL(o.class_a = 'down')"
            " FROM predictions p JOIN outcomes o ON o.prediction_id = p.id GROUP BY p.algorithm"
            " ORDER BY p.algorithm"
        ).fetchall()
        return [AlgorithmSummary(r[0], *(int(v) for v in r[1:])) for r in rows]

    def last_bar_close(self) -> str | None:
        value: str | None = self._db.execute("SELECT MAX(bar_close) FROM predictions").fetchone()[0]
        return value

    def pending_count(self) -> int:
        count: int = self._db.execute("SELECT COUNT(*) FROM outcomes WHERE scored_at IS NULL").fetchone()[0]
        return count

    def _select(self, where: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT p.*, o.target_at, o.mid_close, o.mid_display, o.mid_target, o.flat_ticks,"
            " o.class_a, o.class_b, o.special, o.unscorable, o.scored_at"
            " FROM predictions p JOIN outcomes o ON o.prediction_id = p.id " + where,
            params,
        ).fetchall()
        return [dict(r) for r in rows]
