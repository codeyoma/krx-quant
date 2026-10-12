"""Prediction store: predictions and their outcomes in SQLite.

``predictions`` holds what each algorithm said at each bar close; ``outcomes`` holds the
mid-points and classes it is scored by. The outcome row is written with the prediction
(the mid-points at the bar close and at display time are known then) and completed at
the target time, so a restarted engine can still score what was pending.

Times are ISO 8601 strings in KST, which sort in time order.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from krx_quant.baselines import Forecast
from krx_quant.consensus import Weights
from krx_quant.feeds.base import KST
from krx_quant.lifecycle import Standing, Status
from krx_quant.pricing import Direction

# The engine's database, in the state directory.
DB_NAME = "engine.sqlite3"

_VERSION = 4

# Probabilities are empty for a prediction that was not made (status other than ok).
_PREDICTIONS = """
CREATE TABLE IF NOT EXISTS {name} (
    id INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    algorithm TEXT NOT NULL,          -- an algorithm, a baseline, or a consensus
    model_version TEXT NOT NULL,
    bar_close TEXT NOT NULL,          -- the prediction time t
    displayed_at TEXT NOT NULL,       -- when it became visible (or was given up)
    p_up REAL,
    p_flat REAL,
    p_down REAL,
    expected_ticks REAL,
    status TEXT NOT NULL,             -- ok, timed_out, error, vi (in a VI), no_book (declined),
                                      -- no_members, none_finished (consensus)
    UNIQUE (symbol, algorithm, bar_close)
);
"""

_SCHEMA = (
    _PREDICTIONS.format(name="predictions")
    + """
CREATE INDEX IF NOT EXISTS predictions_by_time ON predictions (symbol, bar_close);
CREATE INDEX IF NOT EXISTS predictions_by_close ON predictions (bar_close);
CREATE TABLE IF NOT EXISTS outcomes (
    prediction_id INTEGER PRIMARY KEY REFERENCES predictions (id),
    target_at TEXT NOT NULL,          -- t + 4 minutes
    mid_close REAL,                   -- basis A: the mid-point at the bar close
    mid_display REAL,                 -- basis B: the mid-point when displayed
    mid_target REAL,
    flat_ticks INTEGER,               -- k: smaller moves are flat
    class_a TEXT,                     -- up, flat or down
    class_b TEXT,
    special INTEGER NOT NULL DEFAULT 0,  -- a VI, circuit breaker or sidecar touched the window
    unscorable TEXT,                  -- why a basis could not be scored
    scored_at TEXT                    -- null while pending
);
CREATE INDEX IF NOT EXISTS outcomes_pending ON outcomes (scored_at, target_at);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,               -- cpu_budget: an algorithm skipped for the CPU budget;
                                      -- review: the daily algorithm review ran;
                                      -- retire_candidate: benched too long, alerted;
                                      -- signal_sent, signal_blocked: a Telegram signal (#80)
    at TEXT NOT NULL,
    algorithm TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS statuses (  -- each algorithm's status changes, oldest first (#17)
    id INTEGER PRIMARY KEY,
    algorithm TEXT NOT NULL,
    at TEXT NOT NULL,
    previous TEXT,                    -- null for the first
    status TEXT NOT NULL,             -- probation, active, bench, retired, clone
    reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS statuses_by_algorithm ON statuses (algorithm, id);
CREATE TABLE IF NOT EXISTS consensus_fits (  -- the consensus weights refitted after each close (#18)
    version TEXT PRIMARY KEY,
    computed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consensus_weights (
    version TEXT NOT NULL REFERENCES consensus_fits (version),
    symbol TEXT NOT NULL,
    slot TEXT NOT NULL,               -- "09:00": the bar closes 09:01-09:30
    algorithm TEXT NOT NULL,
    weight REAL NOT NULL,
    PRIMARY KEY (version, symbol, slot, algorithm)
);
"""
)

# Version 1 (#9) required probabilities; rebuild the table without that.
_FROM_V1 = (
    _PREDICTIONS.format(name="predictions_v2")
    + """
INSERT INTO predictions_v2 SELECT * FROM predictions;
DROP TABLE predictions;
ALTER TABLE predictions_v2 RENAME TO predictions;
"""
)


# A prediction scored on either basis (outcomes ``o`` joined to predictions ``p``).
SCORED = "p.status = 'ok' AND o.scored_at IS NOT NULL AND (o.class_a IS NOT NULL OR o.class_b IS NOT NULL)"


def iso(at: datetime) -> str:
    return at.astimezone(KST).isoformat()


@dataclass(frozen=True)
class Run:
    kind: str
    at: datetime
    algorithm: str | None
    detail: str


@dataclass(frozen=True)
class NewPrediction:
    symbol: str
    algorithm: str
    model_version: str
    bar_close: datetime
    displayed_at: datetime
    forecast: Forecast | None  # None unless the status is ok
    target_at: datetime
    mid_close: float | None
    mid_display: float | None
    flat_ticks: int | None
    status: str = "ok"
    special: bool = False  # a special situation known to touch its window when it was made


@dataclass(frozen=True)
class Pending:
    prediction_id: int
    symbol: str
    target_at: datetime
    mid_close: float | None
    mid_display: float | None
    flat_ticks: int | None


@dataclass(frozen=True)
class Added:
    ids: dict[str, list[int]]  # the IDs stored, by predictor
    pending: list[Pending]  # those made, to score


@dataclass(frozen=True)
class AlgorithmSummary:
    """Counts on basis A."""

    algorithm: str
    predictions: int
    skipped: int  # not made: over the time budget, failed, or in a VI
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
        self._path = path
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        # With WAL, a power loss can lose the last commits but never corrupts the file;
        # commits then do not wait for the disk, which would hold up the event loop.
        self._db.execute("PRAGMA synchronous=NORMAL")
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version > _VERSION:
            raise RuntimeError(f"{path} was written by a newer engine (schema {version})")
        script = (_FROM_V1 if version == 1 else "") + _SCHEMA + f"PRAGMA user_version={_VERSION};"
        self._db.executescript(f"BEGIN;{script}COMMIT;")
        # Bumped by every write, so derived results (statistics) know when to recompute.
        self.version = 0
        self._algorithms = {r[0] for r in self._db.execute("SELECT DISTINCT algorithm FROM predictions")}

    def close(self) -> None:
        self._db.close()

    def add_all(self, predictions: Sequence[NewPrediction]) -> Added:
        """Store predictions in one transaction; those made are pending until scored."""
        self.version += 1
        self._algorithms.update(p.algorithm for p in predictions)
        ids: dict[str, list[int]] = {}
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
                        f and f.p_up,
                        f and f.p_flat,
                        f and f.p_down,
                        f and f.expected_ticks,
                        p.status,
                    ),
                )
                assert cursor.lastrowid is not None
                ids.setdefault(p.algorithm, []).append(cursor.lastrowid)
                if f is None:
                    continue  # nothing to score
                self._db.execute(
                    "INSERT INTO outcomes"
                    " (prediction_id, target_at, mid_close, mid_display, flat_ticks, special)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        cursor.lastrowid,
                        iso(p.target_at),
                        p.mid_close,
                        p.mid_display,
                        p.flat_ticks,
                        int(p.special),
                    ),
                )
                added.append(
                    Pending(cursor.lastrowid, p.symbol, p.target_at, p.mid_close, p.mid_display, p.flat_ticks)
                )
        return Added(ids, added)

    def add_status(
        self, algorithm: str, at: datetime, previous: Status | None, status: Status, reason: str
    ) -> None:
        self.version += 1
        with self._db:
            self._db.execute(
                "INSERT INTO statuses (algorithm, at, previous, status, reason) VALUES (?, ?, ?, ?, ?)",
                (algorithm, iso(at), previous.value if previous else None, status.value, reason),
            )

    def change_status(
        self, algorithm: str, at: datetime, previous: Status, status: Status, reason: str
    ) -> bool:
        """Change ``algorithm``'s status if it is still ``previous``, in one statement (another
        process may be changing it too); whether it changed.
        """
        self.version += 1
        with self._db:
            cursor = self._db.execute(
                "INSERT INTO statuses (algorithm, at, previous, status, reason) SELECT ?, ?, ?, ?, ?"
                " WHERE (SELECT status FROM statuses WHERE algorithm = ? ORDER BY id DESC LIMIT 1) = ?",
                (algorithm, iso(at), previous.value, status.value, reason, algorithm, previous.value),
            )
        return cursor.rowcount == 1

    def save_weights(self, weights: Weights) -> None:
        """Keep ``weights`` (replacing a fit of the same version: a review run again)."""
        assert weights.computed_at is not None
        self.version += 1
        with self._db:
            self._db.execute("DELETE FROM consensus_weights WHERE version = ?", (weights.version,))
            self._db.execute(
                "INSERT OR REPLACE INTO consensus_fits (version, computed_at) VALUES (?, ?)",
                (weights.version, iso(weights.computed_at)),
            )
            self._db.executemany(
                "INSERT INTO consensus_weights (version, symbol, slot, algorithm, weight)"
                " VALUES (?, ?, ?, ?, ?)",
                [
                    (weights.version, symbol, slot, name, w)
                    for (symbol, slot), cell in weights.cells.items()
                    for name, w in cell.items()
                ],
            )

    def latest_weights(self) -> Weights | None:
        """The last weights fitted, if any."""
        latest = self._db.execute(
            "SELECT version, computed_at FROM consensus_fits ORDER BY computed_at DESC LIMIT 1"
        ).fetchone()
        if latest is None:
            return None
        cells: dict[tuple[str, str], dict[str, float]] = {}
        for r in self._db.execute(
            "SELECT symbol, slot, algorithm, weight FROM consensus_weights WHERE version = ?",
            (latest["version"],),
        ):
            cells.setdefault((r["symbol"], r["slot"]), {})[r["algorithm"]] = r["weight"]
        return Weights(latest["version"], datetime.fromisoformat(latest["computed_at"]), cells)

    def statuses(self) -> dict[str, Standing]:
        """Each algorithm's current status and since when."""
        rows = self._db.execute(
            "SELECT algorithm, status, at FROM statuses"
            " WHERE id IN (SELECT MAX(id) FROM statuses GROUP BY algorithm)"
        ).fetchall()
        return {r["algorithm"]: Standing(Status(r["status"]), datetime.fromisoformat(r["at"])) for r in rows}

    def status_history(self, algorithm: str) -> list[dict[str, Any]]:
        """The algorithm's status changes, oldest first."""
        rows = self._db.execute(
            "SELECT at, previous, status, reason FROM statuses WHERE algorithm = ? ORDER BY id", (algorithm,)
        ).fetchall()
        return [dict(r) for r in rows]

    def ran_since(self, kind: str, algorithm: str, since: datetime) -> bool:
        """Whether a ``kind`` run of ``algorithm`` was recorded at or after ``since``."""
        row = self._db.execute(
            "SELECT 1 FROM runs WHERE kind = ? AND algorithm = ? AND at >= ? LIMIT 1",
            (kind, algorithm, iso(since)),
        ).fetchone()
        return row is not None

    def count_runs(
        self, kind: str, since: datetime, algorithm: str | None = None, until: datetime | None = None
    ) -> int:
        """How many ``kind`` runs were recorded at or after ``since`` (and before ``until`` if
        given); of one algorithm if given."""
        sql, params = "SELECT COUNT(*) FROM runs WHERE kind = ? AND at >= ?", [kind, iso(since)]
        if until is not None:
            sql += " AND at < ?"
            params.append(iso(until))
        if algorithm is not None:
            sql += " AND algorithm = ?"
            params.append(algorithm)
        count: int = self._db.execute(sql, params).fetchone()[0]
        return count

    def runs_by_algorithm(self, kind: str, since: datetime, until: datetime) -> dict[str, int]:
        """How many ``kind`` runs each algorithm had from ``since`` until before ``until``."""
        rows = self._db.execute(
            "SELECT algorithm, COUNT(*) FROM runs"
            " WHERE kind = ? AND at >= ? AND at < ? AND algorithm IS NOT NULL"
            " GROUP BY algorithm ORDER BY algorithm",
            (kind, iso(since), iso(until)),
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def timeouts_by_algorithm(self, since: datetime, until: datetime) -> dict[str, int]:
        """How many bar closes from ``since`` until before ``until`` each algorithm timed out at."""
        rows = self._db.execute(
            "SELECT algorithm, COUNT(DISTINCT bar_close) FROM predictions"
            " WHERE status = 'timed_out' AND bar_close >= ? AND bar_close < ?"
            " GROUP BY algorithm ORDER BY algorithm",
            (iso(since), iso(until)),
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def last_run(self, kind: str) -> datetime | None:
        """When the latest ``kind`` run was recorded, if one was."""
        row = self._db.execute("SELECT MAX(at) FROM runs WHERE kind = ?", (kind,)).fetchone()
        return None if row[0] is None else datetime.fromisoformat(row[0])

    def runs(self, kind: str, since: datetime) -> list[Run]:
        """The ``kind`` runs recorded at or after ``since``, oldest first."""
        rows = self._db.execute(
            "SELECT kind, at, algorithm, detail FROM runs WHERE kind = ? AND at >= ? ORDER BY at, id",
            (kind, iso(since)),
        ).fetchall()
        return [Run(r[0], datetime.fromisoformat(r[1]), r[2], r[3] or "") for r in rows]

    def add_run(self, kind: str, at: datetime, algorithm: str | None, detail: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO runs (kind, at, algorithm, detail) VALUES (?, ?, ?, ?)",
                (kind, iso(at), algorithm, detail),
            )

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
        self.version += 1
        with self._db:
            self._db.executemany(
                "UPDATE outcomes SET mid_target = ?, class_a = ?, class_b = ?, unscorable = ?,"
                " scored_at = ? WHERE prediction_id = ?",
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

    def mark_special(self, symbol: str | None, start: datetime, end: datetime, horizon: timedelta) -> None:
        """Mark special every prediction (of ``symbol``, or of every symbol if None) whose
        window, ``horizon`` long, meets the open span from ``start`` to ``end``, scored or not.
        """
        self.version += 1
        where = "p.bar_close > ? AND p.bar_close < ? AND o.target_at > ?"
        params: list[str] = [iso(start - horizon), iso(end), iso(start)]
        if symbol is not None:
            where += " AND p.symbol = ?"
            params.append(symbol)
        with self._db:
            self._db.execute(
                "UPDATE outcomes SET special = 1 WHERE prediction_id IN"
                f" (SELECT o.prediction_id FROM outcomes o JOIN predictions p ON p.id = o.prediction_id"
                f" WHERE {where})",
                params,
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

    def rows(self, ids: Sequence[int]) -> list[dict[str, Any]]:
        """The predictions with these IDs, with their outcomes, by symbol and predictor."""
        if not ids:
            return []
        marks = ", ".join("?" * len(ids))
        return self._select(f"WHERE p.id IN ({marks}) ORDER BY p.symbol, p.algorithm", tuple(ids))

    def status_changes(self, after: int) -> list[dict[str, Any]]:
        """The status changes recorded after the one with ID ``after``, oldest first."""
        rows = self._db.execute(
            "SELECT id, algorithm, at, previous, status, reason FROM statuses WHERE id > ? ORDER BY id",
            (after,),
        ).fetchall()
        return [dict(r) for r in rows]

    def last_status_id(self) -> int:
        last: int | None = self._db.execute("SELECT MAX(id) FROM statuses").fetchone()[0]
        return last or 0

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
            "SELECT p.algorithm, COUNT(*), TOTAL(p.status != 'ok'), COUNT(o.class_a),"
            " TOTAL(o.scored_at IS NOT NULL AND o.class_a IS NULL),"
            " TOTAL(o.prediction_id IS NOT NULL AND o.scored_at IS NULL),"
            " TOTAL(o.class_a = 'up'), TOTAL(o.class_a = 'flat'), TOTAL(o.class_a = 'down')"
            " FROM predictions p LEFT JOIN outcomes o ON o.prediction_id = p.id GROUP BY p.algorithm"
            " ORDER BY p.algorithm"
        ).fetchall()
        return [AlgorithmSummary(r[0], *(int(v) for v in r[1:])) for r in rows]

    def status_counts(self, algorithm: str) -> dict[str, int]:
        """How many of the algorithm's predictions have each status (ok, timed_out, ...)."""
        rows = self._db.execute(
            "SELECT status, COUNT(*) FROM predictions WHERE algorithm = ? GROUP BY status", (algorithm,)
        )
        return {status: count for status, count in rows}

    def algorithms(self) -> set[str]:
        """Every algorithm (and baseline) that ever predicted."""
        return set(self._algorithms)

    def prediction_days(self, until: date, limit: int) -> list[date]:
        """The latest ``limit`` days with predictions, up to ``until``, latest first.

        Walks back a day at a time on the bar-close index rather than reading every row.
        """
        days: list[date] = []
        before = iso(datetime.combine(until + timedelta(days=1), time(), KST))
        while len(days) < limit:
            latest = self._db.execute("SELECT MAX(bar_close) FROM predictions WHERE bar_close < ?", (before,))
            value = latest.fetchone()[0]
            if value is None:
                break
            day = date.fromisoformat(value[:10])
            days.append(day)
            before = iso(datetime.combine(day, time(), KST))
        return days

    def scored_predictions(
        self, days: Sequence[date], symbol: str | None, include_special: bool
    ) -> list[dict[str, Any]]:
        """Predictions made on ``days`` and scored on either basis, of one symbol or all.

        Read on a read-only connection of its own, so a worker thread can run it while
        the engine keeps writing (WAL lets them work at once).
        """
        if not days:
            return []
        marks = ", ".join("?" for _ in days)
        # The time range lets SQLite use the bar-close index; the day list keeps out the
        # days in between that were not asked for.
        where = (
            "WHERE p.bar_close >= ? AND p.bar_close < ?"
            f" AND substr(p.bar_close, 1, 10) IN ({marks}) AND {SCORED}"
        )
        params: list[Any] = [
            iso(datetime.combine(min(days), time(), KST)),
            iso(datetime.combine(max(days) + timedelta(days=1), time(), KST)),
            *(d.isoformat() for d in days),
        ]
        if symbol is not None:
            where += " AND p.symbol = ?"
            params.append(symbol)
        if not include_special:
            where += " AND o.special = 0"
        with contextlib.closing(sqlite3.connect(self._path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            return self._select(where, tuple(params), db)

    def last_bar_close(self) -> str | None:
        value: str | None = self._db.execute("SELECT MAX(bar_close) FROM predictions").fetchone()[0]
        return value

    def pending_count(self) -> int:
        count: int = self._db.execute("SELECT COUNT(*) FROM outcomes WHERE scored_at IS NULL").fetchone()[0]
        return count

    def _select(
        self, where: str, params: tuple[Any, ...], db: sqlite3.Connection | None = None
    ) -> list[dict[str, Any]]:
        rows = (
            (db or self._db)
            .execute(
                "SELECT p.*, o.target_at, o.mid_close, o.mid_display, o.mid_target, o.flat_ticks,"
                " o.class_a, o.class_b, o.special, o.unscorable, o.scored_at"
                " FROM predictions p LEFT JOIN outcomes o ON o.prediction_id = p.id " + where,
                params,
            )
            .fetchall()
        )
        return [dict(r) for r in rows]
