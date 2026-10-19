"""Prediction store: predictions and their outcomes in SQLite.

``predictions`` holds what each algorithm said at each bar close; ``outcomes`` holds the
mid-points and the four-minute move's classes on bases A and B, and when it was scored.
The outcome row is written with the prediction (the mid-points at the bar close and at
display time are known then) and completed at the target time, so a restarted engine can
still score what was pending. ``touches`` holds each bar close's touch label (#167), what
every prediction is scored on since #170: a prediction is scored once its four minutes are
over (``outcomes.scored_at``) and its bar close's touch label is known. The classes on
bases A and B are still written, unread, so an engine rolled back to before #170 runs on.
``model_targets`` says which target each model a prediction names was trained on (#170).

The kinds besides the direction (#179, ``kinds.py``) have tables of their own:
``kind_predictions`` holds each predictor's output at each bar close (the three
probabilities and the kind's fields, as JSON), ``kind_answers`` each window's answer, one per
(kind, symbol, bar close, answer version), what every predictor of the kind there is scored
on. Like ``touches`` they leave the schema version alone: an engine from before them still
opens the database (``_KIND_SCHEMA``). ``kind_predictions.flat_ticks`` (#200), the bar close's
k each prediction was made with, is a column added the same way (``_ADDED_COLUMNS``): an
engine from before it names the columns it writes, so it still opens and writes the
database, and its rows have no k. So is ``kind_predictions.width_ticks`` (#183), the barrier
width w of a kind with barriers at the bar close, and so are ``kind_predictions.anchor`` and
``kind_predictions.tick`` (#210), the bar close's mid-point and its tick size, the reference its
answer is measured from (a barrier's U and L before its answer is in). So is
``kind_predictions.band`` (#264): a daily kind's flat band of the day, as a change (close ÷
open − 1), known at the prediction time; a daily kind's ``bar_close`` is its prediction time
(08:50 of the day) and its answer is the day's (``daily.py``).

``special_windows`` (#236) keeps every special situation's window the engine learns (a VI,
a circuit breaker, a sidecar: ``special.py``), one row per window, its end changed when a
later version is learned (a VI's release); ``special_days`` the past days whose windows were
decided from their recordings (``rescore.fill_special_windows``). They are tables of their own,
as ``touches``: an engine from before them still opens the database (``_SPECIAL_SCHEMA``).

A simulated probation (#298, ``simulation.py``) keeps its predictions apart from the live ones,
in tables of its own (``_SIMULATION_SCHEMA``), each with the answer it was scored on:
``simulation_runs`` a replay of the latest trading days, ``simulations`` the probation each
algorithm's replay was made for, ``sim_predictions`` the direction's predictions of a replay with
their touch labels, ``sim_kind_predictions`` a kind's (a daily kind's too) with their answers.
Nothing that reads the live tables (the leaderboard, the statistics, the consensus weights, the
stream) reads them; the lifecycle does (``lifecycle.Simulated``). ``statuses.simulated`` marks a
status change whose review counted simulated days, a column added as ``kind_predictions.band``
was.

Times are ISO 8601 strings in KST, which sort in time order.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from krx_quant.baselines import Forecast
from krx_quant.consensus import Weights
from krx_quant.feeds.base import KST
from krx_quant.kinds import DIRECTION, Answer
from krx_quant.lifecycle import Simulated, Standing, Status, starts_simulation
from krx_quant.pricing import Direction
from krx_quant.special import Window
from krx_quant.touch import TARGET_VERSION, TouchResult, touch_path

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
                                      -- no_members, none_finished (consensus),
                                      -- no_previous (baseline-persist, touch target, #169)
    UNIQUE (symbol, algorithm, bar_close)
);
"""

_SCHEMA = (
    _PREDICTIONS.format(name="predictions")
    + """
CREATE INDEX IF NOT EXISTS predictions_by_time ON predictions (symbol, bar_close);
CREATE TABLE IF NOT EXISTS blocks (  -- why a final consensus is not to be acted on (#88); a table of
    prediction_id INTEGER PRIMARY KEY REFERENCES predictions (id),  -- its own, so an engine
    reason TEXT NOT NULL              -- rolled back to before it still reads the DB:
);                                    -- special, no_reference or expired
CREATE TABLE IF NOT EXISTS votes (   -- how a consensus's members leaned (#106): a table of its own,
    prediction_id INTEGER PRIMARY KEY REFERENCES predictions (id),  -- as blocks
    up INTEGER NOT NULL,             -- members whose most likely class was up
    flat INTEGER NOT NULL,
    down INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS pooled (  -- the members a consensus pooled (#107), in registration order
    prediction_id INTEGER NOT NULL REFERENCES predictions (id),
    position INTEGER NOT NULL,
    algorithm TEXT NOT NULL,
    PRIMARY KEY (prediction_id, position)
);
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
CREATE TABLE IF NOT EXISTS touches (  -- the touch target of a bar close (#160, #167): a table of its
    symbol TEXT NOT NULL,             -- own, so an engine rolled back to before it still reads the DB
    bar_close TEXT NOT NULL,
    target_version INTEGER NOT NULL,  -- touch.TARGET_VERSION
    class TEXT,                       -- up, flat or down; null when it could not be scored
    unscorable TEXT,                  -- why not: missing mid, rest quotes, silent quotes, restarted
    path TEXT NOT NULL,               -- JSON: the targets, where they were reached, the extremes
    scored_at TEXT NOT NULL,
    PRIMARY KEY (symbol, bar_close, target_version)
);
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
    reason TEXT NOT NULL,
    simulated INTEGER NOT NULL DEFAULT 0  -- its review counted simulated days (#298)
);
CREATE INDEX IF NOT EXISTS statuses_by_algorithm ON statuses (algorithm, id);
CREATE TABLE IF NOT EXISTS model_targets (  -- the target each model run was trained on (#170): a table
    algorithm TEXT NOT NULL,                 -- of its own, as touches
    version TEXT NOT NULL,                   -- a prediction's model_version
    target_version INTEGER,                  -- null: the defaults, or saved before it was recorded
    PRIMARY KEY (algorithm, version)
);
CREATE INDEX IF NOT EXISTS predictions_by_algorithm ON predictions (algorithm, bar_close);
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

# The kinds besides the direction (#179): tables of their own, as touches, so an engine from
# before them still opens the database; the schema version stays.
_KIND_SCHEMA = """
CREATE TABLE IF NOT EXISTS kind_predictions (  -- a prediction of a kind besides the direction
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,               -- kinds.Kind.name
    symbol TEXT NOT NULL,
    algorithm TEXT NOT NULL,          -- an algorithm or a baseline of the kind
    model_version TEXT NOT NULL,
    bar_close TEXT NOT NULL,          -- the prediction time t
    displayed_at TEXT NOT NULL,
    status TEXT NOT NULL,             -- as predictions.status: ok, timed_out, error, vi, no_book, no_previous
    output TEXT,                      -- JSON: p_<class> of its three classes, then its fields; null unless ok
    flat_ticks INTEGER,               -- k at the bar close (#200): what its predictors were given; null
                                      -- without a mid-point there, and in rows stored before #200
    width_ticks INTEGER,              -- a kind's barrier width w at the bar close (#183, the barrier's):
                                      -- what its answer is made with; null for a kind without barriers,
                                      -- without a mid-point there, and in rows stored before #183
    anchor REAL,                      -- the mid-point at the bar close (#210): its answer's reference;
                                      -- null without one there, and in rows stored before #210
    tick INTEGER,                     -- the tick size at that mid-point (#210); null likewise
    band REAL,                        -- a daily kind's flat band of the day (#264), a change;
                                      -- null for the bar-close kinds and without one
    inputs TEXT,                      -- JSON (#304): a daily algorithm's inputs at its time and how
                                      -- they moved its forecast; null for the others and before #304
    UNIQUE (kind, symbol, algorithm, bar_close)
);
CREATE INDEX IF NOT EXISTS kind_predictions_by_close ON kind_predictions (kind, bar_close);
CREATE INDEX IF NOT EXISTS kind_predictions_by_time ON kind_predictions (kind, symbol, bar_close);
CREATE INDEX IF NOT EXISTS kind_predictions_by_algorithm ON kind_predictions (algorithm, bar_close);
CREATE TABLE IF NOT EXISTS kind_answers (  -- a kind's answer of a bar close's four minutes
    kind TEXT NOT NULL,
    symbol TEXT NOT NULL,
    bar_close TEXT NOT NULL,
    answer_version INTEGER NOT NULL,  -- the kind's (kinds.Kind.answer_version)
    class TEXT,                       -- one of the kind's classes; null when it could not be scored
    unscorable TEXT,                  -- why not: missing mid, rest quotes, silent quotes, restarted, ...
    special INTEGER NOT NULL DEFAULT 0,  -- a VI, circuit breaker or sidecar touched the window
    path TEXT NOT NULL,               -- JSON: the kind's summary of the window
    scored_at TEXT NOT NULL,
    PRIMARY KEY (kind, symbol, bar_close, answer_version)
);
"""

# Special situations' windows (#236): tables of their own, as touches, so an engine from before
# them still opens the database; the schema version stays.
_SPECIAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS special_windows (  -- a special situation's window, as last learned
    kind TEXT NOT NULL,               -- vi, circuit_breaker, sidecar (special.Kind)
    symbol TEXT,                      -- a VI's symbol; null for the whole market
    start TEXT NOT NULL,
    end TEXT NOT NULL,                -- the release, or the least it lasts while that is not known
    detail TEXT,                      -- a breaker's stage (1단계), a VI's type (static); else null
    learned_at TEXT NOT NULL          -- when this version of it was received
);
CREATE UNIQUE INDEX IF NOT EXISTS special_windows_key ON special_windows (kind, ifnull(symbol, ''), start);
CREATE INDEX IF NOT EXISTS special_windows_by_start ON special_windows (start);
CREATE TABLE IF NOT EXISTS special_days (  -- a past day whose windows were decided from its recording
    day TEXT PRIMARY KEY,             -- YYYY-MM-DD (KST)
    source TEXT,                      -- where its events came from; null: recorded nowhere
    windows INTEGER NOT NULL,         -- the windows decided
    decided_at TEXT NOT NULL
);
"""

# Simulated probations (#298): tables of their own, as touches, so an engine from before them
# still opens the database; the schema version stays.
_SIMULATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS simulation_runs (  -- a replay of the latest trading days (#298)
    id INTEGER PRIMARY KEY,
    made_at TEXT NOT NULL,
    cadence TEXT NOT NULL,            -- daily: refitted before each day; bar_close: trained once
    days TEXT NOT NULL                -- JSON: the days replayed, oldest first
);
CREATE TABLE IF NOT EXISTS simulations (  -- the probation an algorithm's replay was made for
    algorithm TEXT NOT NULL,
    stint TEXT NOT NULL,              -- when that probation began (its statuses.at)
    kind TEXT NOT NULL,               -- the kind it predicts: direction, a kind, a daily kind
    run INTEGER REFERENCES simulation_runs (id),  -- null: not simulated, detail says why
    made_at TEXT NOT NULL,
    detail TEXT NOT NULL,             -- JSON: how it was trained, its review at once
    PRIMARY KEY (algorithm, stint)
);
CREATE TABLE IF NOT EXISTS sim_kind_predictions (  -- a replay's predictions of a kind besides the
    run INTEGER NOT NULL REFERENCES simulation_runs (id),  -- direction (a daily kind's too), each with
    kind TEXT NOT NULL,               -- the answer it was scored on
    symbol TEXT NOT NULL,
    algorithm TEXT NOT NULL,
    model_version TEXT NOT NULL,
    model_target INTEGER,             -- the answer version its model learned; null: the defaults
    bar_close TEXT NOT NULL,
    status TEXT NOT NULL,             -- as kind_predictions.status
    output TEXT,                      -- as kind_predictions.output
    flat_ticks INTEGER,
    width_ticks INTEGER,
    anchor REAL,
    tick INTEGER,
    band REAL,
    answer_version INTEGER NOT NULL,  -- the kind's when it was replayed
    answer_class TEXT,                -- its answer's class; null when it could not be scored
    answer_unscorable TEXT,
    answer_special INTEGER NOT NULL DEFAULT 0,
    answer_path TEXT,                 -- JSON, as kind_answers.path
    PRIMARY KEY (run, kind, symbol, algorithm, bar_close)
);
CREATE INDEX IF NOT EXISTS sim_kind_predictions_by_close ON sim_kind_predictions (run, kind, bar_close);
CREATE TABLE IF NOT EXISTS sim_predictions (  -- a replay's direction predictions, each with the touch
    run INTEGER NOT NULL REFERENCES simulation_runs (id),  -- label it was scored on (#298)
    symbol TEXT NOT NULL,
    algorithm TEXT NOT NULL,          -- a simulated algorithm or a baseline
    model_version TEXT NOT NULL,
    model_target INTEGER,             -- the target its model learned; null: the defaults
    bar_close TEXT NOT NULL,
    status TEXT NOT NULL,             -- as predictions.status
    p_up REAL,
    p_flat REAL,
    p_down REAL,
    expected_ticks REAL,
    special INTEGER NOT NULL DEFAULT 0,  -- as outcomes.special
    touch_version INTEGER NOT NULL,   -- the touch target's when it was replayed
    touch_class TEXT,                 -- as touches.class; null when it could not be scored
    touch_unscorable TEXT,
    touch_path TEXT,                  -- as touches.path
    PRIMARY KEY (run, symbol, algorithm, bar_close)
);
CREATE INDEX IF NOT EXISTS sim_predictions_by_close ON sim_predictions (run, bar_close);
"""

# Columns added to a table after it was first made, without a schema version (#200): a
# database made before has the table without them, and gets them when an engine opens it.
# (table, column, type); new tables have them already (``_KIND_SCHEMA``).
_ADDED_COLUMNS = (
    ("kind_predictions", "flat_ticks", "INTEGER"),
    ("kind_predictions", "width_ticks", "INTEGER"),
    ("kind_predictions", "anchor", "REAL"),
    ("kind_predictions", "tick", "INTEGER"),
    ("kind_predictions", "band", "REAL"),
    ("kind_predictions", "inputs", "TEXT"),
    ("statuses", "simulated", "INTEGER NOT NULL DEFAULT 0"),
)


def _added_columns(db: sqlite3.Connection) -> str:
    """The statements adding ``_ADDED_COLUMNS`` a table already in ``db`` lacks."""
    script = ""
    for table, column, declared in _ADDED_COLUMNS:
        have = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if have and column not in have:  # none: the table is not there yet, and is made with it
            script += f"ALTER TABLE {table} ADD COLUMN {column} {declared};"
    return script


def _target_detail(target_version: int, kind: str | None) -> str:
    """A ``target`` run's detail: the direction's target version, or a kind's name and answer
    version (#187)."""
    return str(target_version) if kind is None else f"{kind} {target_version}"


# A kind's prediction ``p`` and its bar close's answer ``a`` of the version given (#179).
KIND_ANSWER_JOIN = (
    "LEFT JOIN kind_answers a ON a.kind = p.kind AND a.symbol = p.symbol AND a.bar_close = p.bar_close"
    " AND a.answer_version = ?"
)
# A kind's prediction scored: made, and its bar close's answer has a class.
KIND_SCORED = "p.status = 'ok' AND a.class IS NOT NULL"

# Version 1 (#9) required probabilities; rebuild the table without that.
_FROM_V1 = (
    _PREDICTIONS.format(name="predictions_v2")
    + """
INSERT INTO predictions_v2 SELECT * FROM predictions;
DROP TABLE predictions;
ALTER TABLE predictions_v2 RENAME TO predictions;
"""
)


# A prediction ``p``'s bar close's touch label ``t`` (#167): one per bar close, every predictor's.
TOUCH_JOIN = (
    "LEFT JOIN touches t ON t.symbol = p.symbol AND t.bar_close = p.bar_close"
    f" AND t.target_version = {TARGET_VERSION}"
)
# A prediction scored (#170): made, its four minutes over (outcomes ``o``), and its bar close's
# touch label (``t``, ``TOUCH_JOIN``) known.
SCORED = "p.status = 'ok' AND o.scored_at IS NOT NULL AND t.class IS NOT NULL"


def _made_on(days: Sequence[date], symbol: str | None) -> tuple[str, list[Any]]:
    """A condition on predictions ``p`` made on ``days`` (of ``symbol`` if given), and its
    parameters. The time range lets SQLite use the bar-close index; the day list keeps out
    the days in between that were not asked for."""
    marks = ", ".join("?" for _ in days)
    where = f"p.bar_close >= ? AND p.bar_close < ? AND substr(p.bar_close, 1, 10) IN ({marks})"
    params: list[Any] = [
        iso(datetime.combine(min(days), time(), KST)),
        iso(datetime.combine(max(days) + timedelta(days=1), time(), KST)),
        *(d.isoformat() for d in days),
    ]
    if symbol is not None:
        where += " AND p.symbol = ?"
        params.append(symbol)
    return where, params


def _excluded(days: Sequence[date], symbol: str | None, db: sqlite3.Connection) -> dict[str, Excluded]:
    """What the stats leave out of the predictions made on ``days``, by predictor: those over
    without a touch label (#170), and the special ones."""
    on, params = _made_on(days, symbol)
    out: dict[str, Excluded] = {}
    for name, special, unscorable in db.execute(
        "SELECT p.algorithm, SUM(t.class IS NOT NULL AND o.special = 1), SUM(t.class IS NULL)"
        f" FROM predictions p JOIN outcomes o ON o.prediction_id = p.id {TOUCH_JOIN}"
        f" WHERE {on} AND p.status = 'ok' AND o.scored_at IS NOT NULL GROUP BY p.algorithm",
        params,
    ):
        out[name] = Excluded(special, unscorable)
    for name, status, n in db.execute(
        f"SELECT p.algorithm, p.status, COUNT(*) FROM predictions p WHERE {on} AND p.status != 'ok'"
        " GROUP BY p.algorithm, p.status ORDER BY p.status",
        params,
    ):
        out.setdefault(name, Excluded()).status[status] = n
    return out


@contextlib.contextmanager
def read_only(db: Path) -> Iterator[sqlite3.Connection | None]:
    """The engine DB, read without writing (``diagnose``, ``signal-review``): while the engine
    runs (its write-ahead log is there) as a reader beside it, else as a file nothing changes
    (no ``-wal`` or ``-shm`` is made); None if there is no DB."""
    if not db.exists():
        yield None
        return
    live = db.with_name(db.name + "-wal").exists()
    conn = sqlite3.connect(db.resolve().as_uri() + ("?mode=ro" if live else "?mode=ro&immutable=1"), uri=True)
    try:
        yield conn
    finally:
        conn.close()


def select_predictions(db: sqlite3.Connection, where: str, params: Sequence[Any]) -> list[dict[str, Any]]:
    """The predictions ``where`` selects (on ``p``), each with its outcome, block, votes, touch
    label, model target and members (``pooled``), as the store and the API read them; ``db``'s
    rows are ``sqlite3.Row``."""
    rows = db.execute(
        "SELECT p.*, o.target_at, o.mid_close, o.mid_display, o.mid_target, o.flat_ticks,"
        " o.class_a, o.class_b, o.special, o.unscorable, o.scored_at, b.reason AS blocked,"
        " v.up AS votes_up, v.flat AS votes_flat, v.down AS votes_down,"
        " t.class AS touch_class, t.unscorable AS touch_unscorable, t.path AS touch_path,"
        " m.target_version AS model_target"
        " FROM predictions p LEFT JOIN outcomes o ON o.prediction_id = p.id"
        " LEFT JOIN blocks b ON b.prediction_id = p.id"
        " LEFT JOIN votes v ON v.prediction_id = p.id"
        f" {TOUCH_JOIN}"
        " LEFT JOIN model_targets m ON m.algorithm = p.algorithm AND m.version = p.model_version " + where,
        tuple(params),
    ).fetchall()
    out = [dict(r) for r in rows]
    # A consensus's members, as a list on its row (#107); None where none were stored.
    ids = [r["id"] for r in out]
    pooled: dict[int, list[str]] = {}
    for start in range(0, len(ids), 500):  # under SQLite's limit on parameters
        chunk = ids[start : start + 500]
        marks = ",".join("?" * len(chunk))
        for r in db.execute(
            f"SELECT prediction_id, algorithm FROM pooled WHERE prediction_id IN ({marks})"
            " ORDER BY prediction_id, position",
            chunk,
        ):
            pooled.setdefault(r[0], []).append(r[1])
    for r in out:
        r["pooled"] = pooled.get(r["id"])
    return out


def scored_rows(
    db: sqlite3.Connection, days: Sequence[date], symbol: str | None, include_special: bool
) -> list[dict[str, Any]]:
    """Predictions made on ``days`` (of ``symbol`` if given) and scored on their touch label
    (``SCORED``), special ones only if ``include_special``: what the statistics are made of."""
    if not days:
        return []
    on, params = _made_on(days, symbol)
    where = f"WHERE {on} AND {SCORED}"
    if not include_special:
        where += " AND o.special = 0"
    return select_predictions(db, where, params)


def days_with_predictions(db: sqlite3.Connection, until: date, limit: int) -> list[date]:
    """The latest ``limit`` days with predictions, up to ``until``, latest first.

    Walks back a day at a time on the bar-close index rather than reading every row.
    """
    days: list[date] = []
    before = iso(datetime.combine(until + timedelta(days=1), time(), KST))
    while len(days) < limit:
        latest = db.execute("SELECT MAX(bar_close) FROM predictions WHERE bar_close < ?", (before,))
        value = latest.fetchone()[0]
        if value is None:
            break
        day = date.fromisoformat(value[:10])
        days.append(day)
        before = iso(datetime.combine(day, time(), KST))
    return days


def iso(at: datetime) -> str:
    return at.astimezone(KST).isoformat()


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time(), KST)


@dataclass(frozen=True)
class Excluded:
    """What a query's stats leave out of a predictor's predictions (#83)."""

    special: int = 0  # scored, touched by a special situation
    unscorable: int = 0  # made and over, but without a touch label (#170)
    status: dict[str, int] = field(default_factory=dict)  # not made, by status (vi, no_book, ...)


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
    blocked: str | None = None  # a final consensus's reason not to be acted on (#88)
    votes: tuple[int, int, int] | None = None  # a consensus's members by direction: up, flat, down (#106)
    pooled: tuple[str, ...] | None = None  # a consensus's members, in registration order (#107)


@dataclass(frozen=True)
class NewKindPrediction:
    """A prediction of a kind besides the direction (#179), to store."""

    symbol: str
    algorithm: str
    model_version: str
    bar_close: datetime
    displayed_at: datetime
    output: Mapping[str, float] | None  # the probabilities and the kind's fields; None unless ok
    status: str = "ok"
    flat_ticks: int | None = None  # k at the bar close (#200); None without a mid-point there
    width_ticks: int | None = None  # the kind's barrier width there (#183); None for a kind without
    anchor: float | None = None  # the mid-point at the bar close (#210); None without one there
    tick: int | None = None  # the tick size at that mid-point (#210); None likewise
    band: float | None = None  # a daily kind's flat band of the day (#264); None for the others
    # A daily algorithm's inputs and their effects (#304, ``daily.inputs_json``); None for the others.
    inputs: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class SimKindPrediction:
    """A simulated prediction of a kind (#298), with the answer it was scored on, to store."""

    symbol: str
    algorithm: str
    model_version: str
    model_target: int | None  # the answer version its model learned; None for the defaults
    bar_close: datetime
    output: Mapping[str, float] | None  # None unless ok
    answer: Answer | None  # None: no answer
    special: bool = False
    status: str = "ok"
    flat_ticks: int | None = None
    width_ticks: int | None = None
    anchor: float | None = None
    tick: int | None = None
    band: float | None = None


@dataclass(frozen=True)
class SimulationRecord:
    """An algorithm's latest simulated probation (#298), as stored."""

    algorithm: str
    stint: datetime  # when the probation it was made for began
    kind: str
    run: int | None  # None: not simulated (``detail["why"]``)
    made_at: datetime
    days: tuple[date, ...]  # oldest first; none when not simulated
    cadence: str | None
    detail: Mapping[str, Any]


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
    """Counts on the touch label (#170)."""

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
        script = (
            (_FROM_V1 if version == 1 else "")
            + _SCHEMA
            + _KIND_SCHEMA
            + _SPECIAL_SCHEMA
            + _SIMULATION_SCHEMA
            + _added_columns(self._db)
            + f"PRAGMA user_version={_VERSION};"
        )
        self._db.executescript(f"BEGIN;{script}COMMIT;")
        # Bumped by every write, so derived results (statistics) know when to recompute.
        self.version = 0
        self._algorithms = {r[0] for r in self._db.execute("SELECT DISTINCT algorithm FROM predictions")}
        # Every predictor of a kind besides the direction that ever predicted, and its kind (#179).
        self._kind_algorithms: dict[str, str] = dict(
            self._db.execute("SELECT DISTINCT algorithm, kind FROM kind_predictions").fetchall()
        )

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
                if p.blocked is not None:
                    self._db.execute(
                        "INSERT INTO blocks (prediction_id, reason) VALUES (?, ?)",
                        (cursor.lastrowid, p.blocked),
                    )
                if p.votes is not None:
                    self._db.execute(
                        "INSERT INTO votes (prediction_id, up, flat, down) VALUES (?, ?, ?, ?)",
                        (cursor.lastrowid, *p.votes),
                    )
                if p.pooled is not None:
                    self._db.executemany(
                        "INSERT INTO pooled (prediction_id, position, algorithm) VALUES (?, ?, ?)",
                        [(cursor.lastrowid, i, name) for i, name in enumerate(p.pooled)],
                    )
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
        self,
        algorithm: str,
        at: datetime,
        previous: Status,
        status: Status,
        reason: str,
        *,
        simulated: bool = False,
    ) -> bool:
        """Change ``algorithm``'s status if it is still ``previous``, in one statement (another
        process may be changing it too); whether it changed. ``simulated``: the review counted
        simulated days (#298).
        """
        self.version += 1
        with self._db:
            cursor = self._db.execute(
                "INSERT INTO statuses (algorithm, at, previous, status, reason, simulated)"
                " SELECT ?, ?, ?, ?, ?, ?"
                " WHERE (SELECT status FROM statuses WHERE algorithm = ? ORDER BY id DESC LIMIT 1) = ?",
                (
                    algorithm,
                    iso(at),
                    previous.value,
                    status.value,
                    reason,
                    int(simulated),
                    algorithm,
                    previous.value,
                ),
            )
        return cursor.rowcount == 1

    def record_models(self, models: Sequence[tuple[str, str, int | None]]) -> None:
        """Keep which target each model the engine runs was trained on (#170): (algorithm,
        version, target version) entries; a model already known keeps its entry."""
        with self._db:
            self._db.executemany(
                "INSERT OR IGNORE INTO model_targets (algorithm, version, target_version) VALUES (?, ?, ?)",
                models,
            )

    def first_on_target(self, algorithm: str, since: datetime, target_version: int) -> datetime | None:
        """The first bar close at or after ``since`` at which ``algorithm`` predicted (made or
        not) with a model trained on ``target_version`` (#170); None before any."""
        row = self._db.execute(
            "SELECT p.bar_close FROM predictions p JOIN model_targets m"
            " ON m.algorithm = p.algorithm AND m.version = p.model_version"
            " WHERE p.algorithm = ? AND p.bar_close >= ? AND m.target_version = ?"
            " ORDER BY p.bar_close LIMIT 1",
            (algorithm, iso(since), target_version),
        ).fetchone()
        return None if row is None else datetime.fromisoformat(row[0])

    def target_marked(self, target_version: int, kind: str | None = None) -> bool:
        """Whether the statuses were restarted for ``target_version`` (#170, ``mark_target``):
        the direction's, or (``kind``) a kind's answer version (#187)."""
        row = self._db.execute(
            "SELECT 1 FROM runs WHERE kind = 'target' AND detail = ? LIMIT 1",
            (_target_detail(target_version, kind),),
        ).fetchone()
        return row is not None

    def mark_target(self, target_version: int, at: datetime, kind: str | None = None) -> None:
        """The statuses go by ``target_version``'s models from ``at`` (#170): a ``target`` run,
        its detail the version (the direction's) or ``<kind> <version>`` (a kind's, #187)."""
        self.add_run("target", at, None, _target_detail(target_version, kind))

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
        """Each algorithm's current status, since when and why."""
        rows = self._db.execute(
            "SELECT algorithm, status, at, reason FROM statuses"
            " WHERE id IN (SELECT MAX(id) FROM statuses GROUP BY algorithm)"
        ).fetchall()
        return {
            r["algorithm"]: Standing(Status(r["status"]), datetime.fromisoformat(r["at"]), r["reason"])
            for r in rows
        }

    def status_history(self, algorithm: str) -> list[dict[str, Any]]:
        """The algorithm's status changes, oldest first, each with whether its review counted
        simulated days (#298)."""
        rows = self._db.execute(
            "SELECT at, previous, status, reason, simulated FROM statuses WHERE algorithm = ? ORDER BY id",
            (algorithm,),
        ).fetchall()
        return [{**dict(r), "simulated": bool(r["simulated"])} for r in rows]

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

    def add_touches(
        self, results: Sequence[TouchResult], scored_at: datetime | None = None, *, replace: bool = False
    ) -> int:
        """Store bar closes' touch labels (#167), scored at ``scored_at`` (default: when each
        one's four minutes ended, as a replay scores it). One already stored for a bar close
        stays; with ``replace`` (#168, a rescore) one whose label or path differs is replaced.
        How many rows were written."""
        rows = [
            (
                r.symbol,
                iso(r.bar_close),
                TARGET_VERSION,
                r.direction.value if r.direction else None,
                r.unscorable,
                json.dumps(touch_path(r), separators=(",", ":")),
                iso(scored_at or r.end),
            )
            for r in results
        ]
        if not rows:
            return 0
        with self._db:
            before = self._db.total_changes
            if replace:
                stored = self._stored_touches(min(r[1] for r in rows), max(r[1] for r in rows))
                rows = [r for r in rows if stored.get((r[0], r[1])) != r[3:6]]
            self._db.executemany(
                f"INSERT OR {'REPLACE' if replace else 'IGNORE'} INTO touches"
                " (symbol, bar_close, target_version, class, unscorable, path, scored_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            written = self._db.total_changes - before
        if written:
            self.version += 1  # only a change moves what is derived from it on
        return written

    def _stored_touches(self, first: str, last: str) -> dict[tuple[str, str], tuple[Any, ...]]:
        """The labels of this target version stored for the bar closes ``first`` to ``last``:
        (class, unscorable, path) by (symbol, bar close)."""
        rows = self._db.execute(
            "SELECT symbol, bar_close, class, unscorable, path FROM touches"
            " WHERE target_version = ? AND bar_close BETWEEN ? AND ?",
            (TARGET_VERSION, first, last),
        )
        return {(r[0], r[1]): (r[2], r[3], r[4]) for r in rows}

    def untouched(self, since: date, before: date) -> list[tuple[str, datetime]]:
        """The (symbol, bar close) pairs predicted on the days from ``since`` to before
        ``before`` without a touch label of this target version (#168), in time order."""
        rows = self._db.execute(
            "SELECT DISTINCT p.symbol, p.bar_close FROM predictions p"
            " WHERE p.bar_close >= ? AND p.bar_close < ? AND NOT EXISTS (SELECT 1 FROM touches t"
            " WHERE t.symbol = p.symbol AND t.bar_close = p.bar_close AND t.target_version = ?)"
            " ORDER BY p.bar_close, p.symbol",
            (iso(_midnight(since)), iso(_midnight(before)), TARGET_VERSION),
        )
        return [(r[0], datetime.fromisoformat(r[1])) for r in rows]

    def labeled_days(self, since: date, before: date) -> dict[date, set[str]]:
        """The days from ``since`` to before ``before`` with predictions or touch labels of
        this target version, and their symbols (#168)."""
        params = (iso(_midnight(since)), iso(_midnight(before)))
        rows = self._db.execute(
            "SELECT DISTINCT substr(bar_close, 1, 10), symbol FROM predictions"
            " WHERE bar_close >= ? AND bar_close < ?"
            " UNION SELECT DISTINCT substr(bar_close, 1, 10), symbol FROM touches"
            " WHERE bar_close >= ? AND bar_close < ? AND target_version = ?",
            (*params, *params, TARGET_VERSION),
        )
        days: dict[date, set[str]] = {}
        for day, symbol in rows:
            days.setdefault(date.fromisoformat(day), set()).add(symbol)
        return days

    def changed(self) -> None:
        """Another connection wrote to the database (the touch fill, #168): what is derived
        from it (the cached statistics) is computed again."""
        self.version += 1

    def touches(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """The touch labels of this target version stored, oldest first, with their path
        parsed (diagnosis, tests)."""
        where, params = ("AND symbol = ?", (symbol,)) if symbol else ("", ())
        rows = self._db.execute(
            "SELECT symbol, bar_close, class, unscorable, path, scored_at FROM touches"
            f" WHERE target_version = {TARGET_VERSION} {where}"
            " ORDER BY bar_close, symbol",
            params,
        ).fetchall()
        return [{**dict(r), "path": json.loads(r["path"])} for r in rows]

    def mark_special(
        self,
        symbol: str | None,
        start: datetime,
        end: datetime,
        horizon: timedelta,
        kind_horizons: Mapping[str, timedelta] | None = None,
    ) -> None:
        """Mark special every prediction (of ``symbol``, or of every symbol if None) whose
        window, ``horizon`` long, meets the open span from ``start`` to ``end``, scored or not;
        and every kind's answer of such a window (#179), a kind's window its own horizon long
        where ``kind_horizons`` names it (#263), ``horizon`` otherwise.
        """
        self.version += 1
        where = "p.bar_close > ? AND p.bar_close < ? AND o.target_at > ?"
        params: list[str] = [iso(start - horizon), iso(end), iso(start)]
        if symbol is not None:
            where += " AND p.symbol = ?"
            params.append(symbol)
        own = dict(kind_horizons or {})
        marks = [
            ("kind NOT IN ({})".format(", ".join("?" * len(own))) if own else "1 = 1", list(own), horizon)
        ]
        marks += [("kind = ?", [name], h) for name, h in own.items()]
        with self._db:
            self._db.execute(
                "UPDATE outcomes SET special = 1 WHERE prediction_id IN"
                f" (SELECT o.prediction_id FROM outcomes o JOIN predictions p ON p.id = o.prediction_id"
                f" WHERE {where})",
                params,
            )
            for which, names, length in marks:
                # Its end, bar close + its horizon, after start.
                answers = f"{which} AND bar_close > ? AND bar_close < ?"
                answered: list[str] = [*names, iso(start - length), iso(end)]
                if symbol is not None:
                    answers += " AND symbol = ?"
                    answered.append(symbol)
                self._db.execute(f"UPDATE kind_answers SET special = 1 WHERE {answers}", answered)

    # Special situations' windows (#236).

    def keep_special(self, window: Window, learned_at: datetime) -> None:
        """Keep a special window the engine learned at ``learned_at``: a new one, or a later
        version of one kept (the same kind, symbol and start: a VI's release)."""
        with self._db:
            self._keep_special(window, learned_at)

    def _keep_special(self, window: Window, learned_at: datetime) -> int:
        """Store ``window`` in the transaction open; the rows written."""
        key = (window.kind.value, window.symbol or "", iso(window.start))
        values = (iso(window.end), window.detail, iso(learned_at))
        cursor = self._db.execute(
            "UPDATE special_windows SET end = ?, detail = ?, learned_at = ?"
            " WHERE kind = ? AND ifnull(symbol, '') = ? AND start = ?",
            (*values, *key),
        )
        if cursor.rowcount:
            return cursor.rowcount
        self._db.execute(
            "INSERT INTO special_windows (kind, symbol, start, end, detail, learned_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (window.kind.value, window.symbol, iso(window.start), *values),
        )
        return 1

    def special_windows(self, symbol: str | None, since: datetime, until: datetime) -> list[dict[str, Any]]:
        """The windows kept that start in ``[since, until)``: ``symbol``'s VIs and the whole
        market's (every window if ``symbol`` is None), in time order."""
        where = "start >= ? AND start < ?"
        params: list[Any] = [iso(since), iso(until)]
        if symbol is not None:
            where += " AND (symbol IS NULL OR symbol = ?)"
            params.append(symbol)
        rows = self._db.execute(
            "SELECT kind, symbol, start, end, detail, learned_at FROM special_windows"
            f" WHERE {where} ORDER BY start, kind, ifnull(symbol, '')",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def special_undecided(self, since: date, before: date) -> dict[date, list[str]]:
        """The days from ``since`` to before ``before`` with predictions, not yet decided from
        their recording (#236), with the symbols predicted on each. Windows the live engine kept
        do not make a day decided: an engine restarted that day learned only part of it (its
        special situations live in memory), so only a whole day's replay does."""
        decided = {
            r[0]
            for r in self._db.execute(
                "SELECT day FROM special_days WHERE day >= ? AND day < ?",
                (since.isoformat(), before.isoformat()),
            )
        }
        days: dict[date, list[str]] = {}
        day = since
        while day < before:
            if day.isoformat() not in decided:
                rows = self._db.execute(
                    "SELECT DISTINCT symbol FROM predictions WHERE bar_close >= ? AND bar_close < ?"
                    " ORDER BY symbol",
                    (iso(_midnight(day)), iso(_midnight(day + timedelta(days=1)))),
                ).fetchall()
                if rows:
                    days[day] = [r[0] for r in rows]
            day += timedelta(days=1)
        return days

    def decide_special_day(
        self, day: date, windows: Sequence[tuple[datetime, Window]], source: str | None, decided_at: datetime
    ) -> int:
        """Keep the windows of a past ``day`` decided from its recording (read from ``source``;
        None: recorded nowhere), each with when it was learned, and mark the day decided, in one
        transaction; nothing if the day was decided already. The replay is the whole day, so it
        replaces the windows the live engine kept that day (part of it, if it restarted); a day
        recorded nowhere keeps them. The rows written."""
        first, last = iso(_midnight(day)), iso(_midnight(day + timedelta(days=1)))
        with self._db:
            taken = self._db.execute(
                "SELECT 1 FROM special_days WHERE day = ?", (day.isoformat(),)
            ).fetchone()
            if taken is not None:
                return 0
            if source is not None:
                self._db.execute("DELETE FROM special_windows WHERE start >= ? AND start < ?", (first, last))
            written = sum(self._keep_special(w, learned_at) for learned_at, w in windows)
            self._db.execute(
                "INSERT INTO special_days (day, source, windows, decided_at) VALUES (?, ?, ?, ?)",
                (day.isoformat(), source, len(windows), iso(decided_at)),
            )
        return written

    # The kinds besides the direction (#179): their predictions and answers.

    def add_kind_predictions(
        self, kind: str, predictions: Sequence[NewKindPrediction]
    ) -> dict[str, list[int]]:
        """Store a kind's predictions in one transaction; their IDs, by predictor."""
        self.version += 1
        ids: dict[str, list[int]] = {}
        with self._db:
            for p in predictions:
                cursor = self._db.execute(
                    "INSERT INTO kind_predictions (kind, symbol, algorithm, model_version, bar_close,"
                    " displayed_at, status, output, flat_ticks, width_ticks, anchor, tick, band, inputs)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        kind,
                        p.symbol,
                        p.algorithm,
                        p.model_version,
                        iso(p.bar_close),
                        iso(p.displayed_at),
                        p.status,
                        None if p.output is None else json.dumps(dict(p.output), separators=(",", ":")),
                        p.flat_ticks,
                        p.width_ticks,
                        p.anchor,
                        p.tick,
                        p.band,
                        None if p.inputs is None else json.dumps(dict(p.inputs), separators=(",", ":")),
                    ),
                )
                assert cursor.lastrowid is not None
                ids.setdefault(p.algorithm, []).append(cursor.lastrowid)
                self._kind_algorithms.setdefault(p.algorithm, kind)
        return ids

    def add_kind_answers(
        self,
        kind: str,
        answer_version: int,
        answers: Sequence[tuple[str, datetime, Answer, bool]],
        scored_at: datetime | None = None,
        horizon: timedelta = timedelta(minutes=4),
    ) -> list[tuple[str, datetime]]:
        """Store windows' answers of ``kind``: (symbol, bar close, answer, special) entries,
        scored at ``scored_at`` (default: when each one's window, ``horizon`` long, ended, as a
        replay or a fill scores it, #263). One stored already stays. The (symbol, bar close) of
        those written."""
        written: list[tuple[str, datetime]] = []
        with self._db:
            for symbol, bar_close, answer, special in answers:
                cursor = self._db.execute(
                    "INSERT OR IGNORE INTO kind_answers (kind, symbol, bar_close, answer_version, class,"
                    " unscorable, special, path, scored_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        kind,
                        symbol,
                        iso(bar_close),
                        answer_version,
                        answer.label,
                        answer.unscorable,
                        int(special),
                        json.dumps(dict(answer.path), separators=(",", ":")),
                        iso(scored_at or bar_close + horizon),
                    ),
                )
                if cursor.rowcount:
                    written.append((symbol, bar_close))
        if written:
            self.version += 1
        return written

    def kind_unanswered(
        self, versions: Mapping[str, int], since: datetime
    ) -> list[tuple[str, str, datetime]]:
        """The (kind, symbol, bar close) of the kinds' predictions made from ``since`` without an
        answer of the kind's version (``versions``, by kind): made before a restart, their
        windows not followed through. In time order."""
        out: list[tuple[str, str, datetime]] = []
        for kind, version in versions.items():
            rows = self._db.execute(
                "SELECT DISTINCT p.symbol, p.bar_close FROM kind_predictions p"
                " WHERE p.kind = ? AND p.bar_close >= ? AND p.status = 'ok' AND NOT EXISTS"
                " (SELECT 1 FROM kind_answers a WHERE a.kind = p.kind AND a.symbol = p.symbol"
                " AND a.bar_close = p.bar_close AND a.answer_version = ?)",
                (kind, iso(since), version),
            )
            out += [(kind, r[0], datetime.fromisoformat(r[1])) for r in rows]
        return sorted(out, key=lambda k: (k[2], k[0], k[1]))

    def unanswered(
        self, kind: str, answer_version: int, since: date, before: date
    ) -> list[tuple[str, datetime]]:
        """The (symbol, bar close) pairs predicted (the direction's predictions: every bar close
        the engine ran) on the days from ``since`` to before ``before`` without ``kind``'s answer
        of ``answer_version`` (#180, the kinds' fill), in time order."""
        rows = self._db.execute(
            "SELECT DISTINCT p.symbol, p.bar_close FROM predictions p"
            " WHERE p.bar_close >= ? AND p.bar_close < ? AND NOT EXISTS (SELECT 1 FROM kind_answers a"
            " WHERE a.kind = ? AND a.symbol = p.symbol AND a.bar_close = p.bar_close"
            " AND a.answer_version = ?) ORDER BY p.bar_close, p.symbol",
            (iso(_midnight(since)), iso(_midnight(before)), kind, answer_version),
        )
        return [(r[0], datetime.fromisoformat(r[1])) for r in rows]

    def daily_unanswered(self, kind: str, answer_version: int, since: date) -> list[tuple[str, date]]:
        """The (symbol, day) a daily kind predicted from ``since`` on without an answer of
        ``answer_version`` (#264), in time order: its answers wait for the day's candle."""
        rows = self._db.execute(
            "SELECT DISTINCT p.symbol, p.bar_close FROM kind_predictions p"
            " WHERE p.kind = ? AND p.bar_close >= ? AND NOT EXISTS (SELECT 1 FROM kind_answers a"
            " WHERE a.kind = p.kind AND a.symbol = p.symbol AND a.bar_close = p.bar_close"
            " AND a.answer_version = ?) ORDER BY p.bar_close, p.symbol",
            (kind, iso(_midnight(since)), answer_version),
        )
        return [(r[0], date.fromisoformat(r[1][:10])) for r in rows]

    def kind_answered_days(self, kind: str, answer_version: int, until: date, limit: int) -> list[date]:
        """The latest ``limit`` days up to ``until`` with an answer of a daily kind's predictions,
        latest first (#264): the days its lifecycle counts, each a day scored."""
        rows = self._db.execute(
            "SELECT DISTINCT substr(a.bar_close, 1, 10) AS day FROM kind_answers a WHERE a.kind = ?"
            " AND a.answer_version = ? AND a.bar_close < ? AND EXISTS (SELECT 1 FROM kind_predictions p"
            " WHERE p.kind = a.kind AND p.symbol = a.symbol AND p.bar_close = a.bar_close)"
            " ORDER BY day DESC LIMIT ?",
            (kind, answer_version, iso(_midnight(until + timedelta(days=1))), limit),
        )
        return [date.fromisoformat(r[0]) for r in rows]

    def day_kind_classes(
        self, kind: str, answer_version: int, symbol: str, since: datetime, until: datetime
    ) -> dict[str, int]:
        """A kind's answers of the symbol's bar closes in ``[since, until]`` stored so far, by
        class, predicted or not (a kind baseline's ``day_classes``)."""
        rows = self._db.execute(
            "SELECT class, COUNT(*) FROM kind_answers WHERE kind = ? AND answer_version = ? AND symbol = ?"
            " AND bar_close BETWEEN ? AND ? AND class IS NOT NULL GROUP BY class",
            (kind, answer_version, symbol, iso(since), iso(until)),
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def first_kind_on_target(self, algorithm: str, since: datetime, answer_version: int) -> datetime | None:
        """``first_on_target`` of a kind's algorithm: its first bar close at or after ``since``
        predicted with a model trained on its kind's ``answer_version``."""
        row = self._db.execute(
            "SELECT p.bar_close FROM kind_predictions p JOIN model_targets m"
            " ON m.algorithm = p.algorithm AND m.version = p.model_version"
            " WHERE p.algorithm = ? AND p.bar_close >= ? AND m.target_version = ?"
            " ORDER BY p.bar_close LIMIT 1",
            (algorithm, iso(since), answer_version),
        ).fetchone()
        return None if row is None else datetime.fromisoformat(row[0])

    def kind_prediction_days(self, kind: str, until: date, limit: int) -> list[date]:
        """``prediction_days`` of a kind: the latest ``limit`` days with its predictions, up to
        ``until``, latest first."""
        days: list[date] = []
        before = iso(datetime.combine(until + timedelta(days=1), time(), KST))
        while len(days) < limit:
            value = self._db.execute(
                "SELECT MAX(bar_close) FROM kind_predictions WHERE kind = ? AND bar_close < ?", (kind, before)
            ).fetchone()[0]
            if value is None:
                break
            day = date.fromisoformat(value[:10])
            days.append(day)
            before = iso(datetime.combine(day, time(), KST))
        return days

    def kind_algorithms(self) -> dict[str, str]:
        """Every predictor of a kind besides the direction that ever predicted, and its kind."""
        return dict(self._kind_algorithms)

    def kind_status_counts(self, algorithm: str) -> dict[str, int]:
        """``status_counts`` of a kind's predictor."""
        rows = self._db.execute(
            "SELECT status, COUNT(*) FROM kind_predictions WHERE algorithm = ? GROUP BY status", (algorithm,)
        )
        return {status: count for status, count in rows}

    def kind_timeouts_by_algorithm(self, since: datetime, until: datetime) -> dict[str, int]:
        """``timeouts_by_algorithm`` of the kinds' algorithms."""
        rows = self._db.execute(
            "SELECT algorithm, COUNT(DISTINCT bar_close) FROM kind_predictions"
            " WHERE status = 'timed_out' AND bar_close >= ? AND bar_close < ?"
            " GROUP BY algorithm ORDER BY algorithm",
            (iso(since), iso(until)),
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def kind_rows(self, kind: str, answer_version: int, ids: Sequence[int]) -> list[dict[str, Any]]:
        """A kind's predictions with these IDs, with their answers, by symbol and predictor."""
        if not ids:
            return []
        marks = ", ".join("?" * len(ids))
        return self._kind_select(
            answer_version,
            f"WHERE p.kind = ? AND p.id IN ({marks}) ORDER BY p.symbol, p.algorithm",
            (kind, *ids),
        )

    def kind_latest(self, kind: str, answer_version: int, symbol: str) -> list[dict[str, Any]]:
        """A kind's predictions of the symbol's latest bar close."""
        return self._kind_select(
            answer_version,
            "WHERE p.kind = ? AND p.symbol = ? AND p.bar_close = (SELECT MAX(bar_close) FROM kind_predictions"
            " WHERE kind = ? AND symbol = ?) ORDER BY p.algorithm",
            (kind, symbol, kind, symbol),
        )

    def kind_at_close(
        self, kind: str, answer_version: int, symbol: str, bar_close: datetime, algorithm: str | None = None
    ) -> list[dict[str, Any]]:
        """A kind's predictions of the symbol at one bar close; of one predictor only if given."""
        where, params = (
            "WHERE p.kind = ? AND p.symbol = ? AND p.bar_close = ?",
            [kind, symbol, iso(bar_close)],
        )
        if algorithm is not None:
            where += " AND p.algorithm = ?"
            params.append(algorithm)
        return self._kind_select(answer_version, where + " ORDER BY p.algorithm", tuple(params))

    def kind_recent(
        self,
        kind: str,
        answer_version: int,
        symbol: str,
        limit: int,
        algorithm: str | None = None,
        before: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """``recent`` of a kind: the predictions of the symbol's last ``limit`` bar closes of the
        kind (before ``before`` if given), newest first; of one predictor only if given."""
        until = iso(before) if before is not None else "9999"
        if algorithm is None:
            return self._kind_select(
                answer_version,
                "WHERE p.kind = ? AND p.symbol = ? AND p.bar_close IN (SELECT DISTINCT bar_close"
                " FROM kind_predictions WHERE kind = ? AND symbol = ? AND bar_close < ?"
                " ORDER BY bar_close DESC LIMIT ?) ORDER BY p.bar_close DESC, p.algorithm",
                (kind, symbol, kind, symbol, until, limit),
            )
        return self._kind_select(
            answer_version,
            "WHERE p.kind = ? AND p.symbol = ? AND p.algorithm = ? AND p.bar_close < ?"
            " ORDER BY p.bar_close DESC LIMIT ?",
            (kind, symbol, algorithm, until, limit),
        )

    def kind_answers_at(
        self, kind: str, answer_version: int, keys: Sequence[tuple[str, datetime]]
    ) -> list[dict[str, Any]]:
        """A kind's answers of these (symbol, bar close), with their path parsed, in time order."""
        out: list[dict[str, Any]] = []
        for symbol, bar_close in keys:
            row = self._db.execute(
                "SELECT symbol, bar_close, answer_version, class, unscorable, special, path, scored_at"
                " FROM kind_answers WHERE kind = ? AND answer_version = ? AND symbol = ? AND bar_close = ?",
                (kind, answer_version, symbol, iso(bar_close)),
            ).fetchone()
            if row is not None:
                out.append({**dict(row), "special": bool(row["special"]), "path": json.loads(row["path"])})
        return sorted(out, key=lambda r: (r["bar_close"], r["symbol"]))

    def kind_answers(self, kind: str, symbol: str | None = None) -> list[dict[str, Any]]:
        """Every answer of ``kind`` stored, oldest first, with its path parsed (tests, diagnosis)."""
        where, params = ("AND symbol = ?", (kind, symbol)) if symbol else ("", (kind,))
        rows = self._db.execute(
            "SELECT symbol, bar_close, answer_version, class, unscorable, special, path, scored_at"
            f" FROM kind_answers WHERE kind = ? {where} ORDER BY bar_close, symbol",
            params,
        ).fetchall()
        return [{**dict(r), "special": bool(r["special"]), "path": json.loads(r["path"])} for r in rows]

    def kind_scored(
        self, kind: str, answer_version: int, days: Sequence[date], symbol: str | None, include_special: bool
    ) -> list[dict[str, Any]]:
        """A kind's predictions made on ``days`` and scored on their answer (``KIND_SCORED``), of
        one symbol or all, read on a connection of its own (``_reading``)."""
        if not days:
            return []
        with self._reading() as db:
            db.row_factory = sqlite3.Row
            return self._kind_scored(kind, answer_version, days, symbol, include_special, db)

    def _kind_scored(
        self,
        kind: str,
        answer_version: int,
        days: Sequence[date],
        symbol: str | None,
        include_special: bool,
        db: sqlite3.Connection,
    ) -> list[dict[str, Any]]:
        on, params = _made_on(days, symbol)
        where = f"WHERE p.kind = ? AND {on} AND {KIND_SCORED}"
        if not include_special:
            where += " AND a.special = 0"
        return self._kind_select(answer_version, where, (kind, *params), db)

    def kind_scored_and_excluded(
        self, kind: str, answer_version: int, days: Sequence[date], symbol: str | None, include_special: bool
    ) -> tuple[list[dict[str, Any]], dict[str, Excluded]]:
        """``kind_scored`` and what the stats leave out of the same predictions, by predictor,
        read in one transaction so they agree: made ones whose answer is special or has no
        class (``unscorable``), and those not made, by status."""
        if not days:
            return [], {}
        with self._reading() as db:
            db.execute("BEGIN")
            db.row_factory = sqlite3.Row
            rows = self._kind_scored(kind, answer_version, days, symbol, include_special, db)
            db.row_factory = None
            on, params = _made_on(days, symbol)
            excluded: dict[str, Excluded] = {}
            for name, special, unscorable in db.execute(
                "SELECT p.algorithm, SUM(a.class IS NOT NULL AND a.special = 1), SUM(a.class IS NULL)"
                f" FROM kind_predictions p JOIN kind_answers a ON a.kind = p.kind AND a.symbol = p.symbol"
                f" AND a.bar_close = p.bar_close AND a.answer_version = ?"
                f" WHERE p.kind = ? AND {on} AND p.status = 'ok' GROUP BY p.algorithm",
                (answer_version, kind, *params),
            ):
                excluded[name] = Excluded(special, unscorable)
            for name, status, n in db.execute(
                f"SELECT p.algorithm, p.status, COUNT(*) FROM kind_predictions p WHERE p.kind = ? AND {on}"
                " AND p.status != 'ok' GROUP BY p.algorithm, p.status ORDER BY p.status",
                (kind, *params),
            ):
                excluded.setdefault(name, Excluded()).status[status] = n
            db.execute("COMMIT")
        return rows, excluded

    def _kind_select(
        self, answer_version: int, where: str, params: tuple[Any, ...], db: sqlite3.Connection | None = None
    ) -> list[dict[str, Any]]:
        """A kind's predictions ``p`` with their bar close's answer ``a`` of ``answer_version``,
        their output and the answer's path parsed."""
        rows = (
            (db or self._db)
            .execute(
                "SELECT p.*, a.answer_version AS answer_version, a.class AS answer_class,"
                " a.unscorable AS answer_unscorable, a.special AS answer_special, a.path AS answer_path,"
                " a.scored_at AS answer_scored_at, m.target_version AS model_target"
                f" FROM kind_predictions p {KIND_ANSWER_JOIN}"
                " LEFT JOIN model_targets m ON m.algorithm = p.algorithm AND m.version = p.model_version "
                + where,
                (answer_version, *params),
            )
            .fetchall()
        )
        out = []
        for r in rows:
            row = dict(r)
            row["output"] = None if row["output"] is None else json.loads(row["output"])
            row["answer_path"] = None if row["answer_path"] is None else json.loads(row["answer_path"])
            row["inputs"] = None if row.get("inputs") is None else json.loads(row["inputs"])
            out.append(row)
        return out

    # Simulated probations (#298): their replays' predictions, apart from the live ones.

    def add_simulation_run(self, made_at: datetime, cadence: str, days: Sequence[date]) -> int:
        """A new replay of ``days`` (oldest first), by ``cadence`` (``daily``: refitted before each
        day; ``bar_close``: trained once before them); its ID."""
        with self._db:
            cursor = self._db.execute(
                "INSERT INTO simulation_runs (made_at, cadence, days) VALUES (?, ?, ?)",
                (iso(made_at), cadence, json.dumps([d.isoformat() for d in sorted(days)])),
            )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def add_sim_kind_predictions(
        self, run: int, kind: str, answer_version: int, predictions: Sequence[SimKindPrediction]
    ) -> None:
        """Keep a replay's predictions of ``kind`` with their answers (of ``answer_version``), in
        one transaction."""
        with self._db:
            self._db.executemany(
                "INSERT OR REPLACE INTO sim_kind_predictions (run, kind, symbol, algorithm, model_version,"
                " model_target, bar_close, status, output, flat_ticks, width_ticks, anchor, tick, band,"
                " answer_version, answer_class, answer_unscorable, answer_special, answer_path)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        run,
                        kind,
                        p.symbol,
                        p.algorithm,
                        p.model_version,
                        p.model_target,
                        iso(p.bar_close),
                        p.status,
                        None if p.output is None else json.dumps(dict(p.output), separators=(",", ":")),
                        p.flat_ticks,
                        p.width_ticks,
                        p.anchor,
                        p.tick,
                        p.band,
                        answer_version,
                        None if p.answer is None else p.answer.label,
                        None if p.answer is None else p.answer.unscorable,
                        int(p.special),
                        None if p.answer is None else json.dumps(dict(p.answer.path), separators=(",", ":")),
                    )
                    for p in predictions
                ],
            )

    def add_sim_predictions(self, run: int, rows: Sequence[Mapping[str, Any]], touch_version: int) -> None:
        """Keep a replay's direction predictions (``select_predictions``' rows: each with its
        outcome's ``special`` and its bar close's touch label of ``touch_version``), in one
        transaction."""
        with self._db:
            self._db.executemany(
                "INSERT OR REPLACE INTO sim_predictions (run, symbol, algorithm, model_version,"
                " model_target, bar_close, status, p_up, p_flat, p_down, expected_ticks, special,"
                " touch_version, touch_class, touch_unscorable, touch_path)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        run,
                        r["symbol"],
                        r["algorithm"],
                        r["model_version"],
                        r.get("model_target"),
                        r["bar_close"],
                        r["status"],
                        r.get("p_up"),
                        r.get("p_flat"),
                        r.get("p_down"),
                        r.get("expected_ticks"),
                        int(bool(r.get("special"))),
                        touch_version,
                        r.get("touch_class"),
                        r.get("touch_unscorable"),
                        r.get("touch_path"),
                    )
                    for r in rows
                ],
            )

    def sim_scored(
        self, run: int, days: Sequence[date], symbol: str | None, include_special: bool
    ) -> list[dict[str, Any]]:
        """``scored_predictions`` of a replay: its direction predictions made on ``days`` and
        scored on their touch label of this version (``touch.TARGET_VERSION``), as the lifecycle
        reads them (the rows' keys are ``scored_predictions``'), read on a connection of its own."""
        if not days:
            return []
        on, params = _made_on(days, symbol)
        where = f"p.run = ? AND {on} AND p.status = 'ok' AND p.touch_class IS NOT NULL"
        where += f" AND p.touch_version = {TARGET_VERSION}"
        if not include_special:
            where += " AND p.special = 0"
        with self._reading() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(f"SELECT p.* FROM sim_predictions p WHERE {where}", (run, *params)).fetchall()
        return [dict(r) for r in rows]

    def made_on(self, days: Sequence[date]) -> list[dict[str, Any]]:
        """Every direction prediction made on ``days``, made or not, with its outcome and touch
        label (``select_predictions``): what a simulation keeps of its replay (#298)."""
        if not days:
            return []
        on, params = _made_on(days, None)
        return self._select(f"WHERE {on} ORDER BY p.bar_close, p.symbol, p.algorithm", tuple(params))

    def kind_made_on(self, kind: str, answer_version: int, days: Sequence[date]) -> list[dict[str, Any]]:
        """Every prediction of ``kind`` made on ``days``, made or not, with its answer of
        ``answer_version`` (``_kind_select``): what a simulation keeps of its replay (#298)."""
        if not days:
            return []
        on, params = _made_on(days, None)
        return self._kind_select(answer_version, f"WHERE p.kind = ? AND {on}", (kind, *params))

    def add_simulation(
        self,
        algorithm: str,
        stint: datetime,
        kind: str,
        run: int | None,
        made_at: datetime,
        detail: Mapping[str, Any],
    ) -> None:
        """Keep ``algorithm``'s simulated probation begun at ``stint``: its replay (``run``; None
        when it was not simulated, ``detail["why"]`` saying why), replacing one kept before."""
        self.version += 1
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO simulations (algorithm, stint, kind, run, made_at, detail)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    algorithm,
                    iso(stint),
                    kind,
                    run,
                    iso(made_at),
                    json.dumps(dict(detail), ensure_ascii=False),
                ),
            )

    def simulations(self) -> dict[str, SimulationRecord]:
        """Each algorithm's latest simulated probation (the probation begun last it was made for)."""
        rows = self._db.execute(
            "SELECT s.algorithm, s.stint, s.kind, s.run, s.made_at, s.detail, r.cadence, r.days"
            " FROM simulations s LEFT JOIN simulation_runs r ON r.id = s.run ORDER BY s.stint, s.made_at"
        ).fetchall()
        out: dict[str, SimulationRecord] = {}
        for r in rows:
            out[r["algorithm"]] = SimulationRecord(
                algorithm=r["algorithm"],
                stint=datetime.fromisoformat(r["stint"]),
                kind=r["kind"],
                run=r["run"],
                made_at=datetime.fromisoformat(r["made_at"]),
                days=tuple(date.fromisoformat(d) for d in json.loads(r["days"] or "[]")),
                cadence=r["cadence"],
                detail=json.loads(r["detail"]),
            )
        return out

    def simulated_stints(self) -> set[tuple[str, datetime]]:
        """The (algorithm, start) of every probation a simulation was kept for, simulated or not."""
        rows = self._db.execute("SELECT algorithm, stint FROM simulations").fetchall()
        return {(r[0], datetime.fromisoformat(r[1])) for r in rows}

    def simulation_mark(self) -> tuple[int, str | None]:
        """What changes whenever a simulation is kept: what the stages are cached for."""
        row = self._db.execute("SELECT COUNT(*), MAX(made_at) FROM simulations").fetchone()
        return int(row[0]), row[1]

    def to_simulate(self, names: Sequence[str]) -> list[str]:
        """Of ``names``, in order, the algorithms on a probation that waits for its simulation
        (#298): begun for a reason that is simulated (``lifecycle.starts_simulation``: a new
        algorithm's, a new target's), no simulation kept for it yet."""
        standings, done = self.statuses(), self.simulated_stints()
        return [
            n
            for n in names
            if (s := standings.get(n)) is not None
            and s.status is Status.PROBATION
            and starts_simulation(s.reason)
            and (n, s.since) not in done
        ]

    def simulated(self, kind: str, answer_version: int) -> dict[str, Simulated]:
        """The algorithms of ``kind`` with a simulated probation (#298), their latest replay as the
        lifecycle counts it (``lifecycle.Simulated``): its days, the probation it was made for, its
        scored predictions of the kind's ``answer_version``, read from their own table."""
        out: dict[str, Simulated] = {}
        for name, record in self.simulations().items():
            if record.kind != kind or record.run is None or not record.days:
                continue
            run = record.run

            def rows(chosen: Sequence[date], special: bool, run: int = run) -> list[dict[str, Any]]:
                if kind == DIRECTION:
                    return self.sim_scored(run, chosen, None, special)
                return self.sim_kind_scored(run, kind, answer_version, chosen, None, special)

            days = tuple(sorted(record.days, reverse=True))
            out[name] = Simulated(days, record.stint, rows, self.sim_predictors(run, kind))
        return out

    def sim_predictors(self, run: int, kind: str) -> frozenset[str]:
        """The predictors a replay has predictions of, of ``kind`` (the direction's too)."""
        if kind == DIRECTION:
            rows = self._db.execute("SELECT DISTINCT algorithm FROM sim_predictions WHERE run = ?", (run,))
        else:
            rows = self._db.execute(
                "SELECT DISTINCT algorithm FROM sim_kind_predictions WHERE run = ? AND kind = ?", (run, kind)
            )
        return frozenset(r[0] for r in rows.fetchall())

    def sim_kind_scored(
        self,
        run: int,
        kind: str,
        answer_version: int,
        days: Sequence[date],
        symbol: str | None,
        include_special: bool,
    ) -> list[dict[str, Any]]:
        """``kind_scored`` of a replay: its predictions of ``kind`` made on ``days`` and scored on
        their answer of ``answer_version``, as the lifecycle reads them (the rows' keys are
        ``kind_scored``'s), read on a connection of its own (``_reading``: the reviews read in a
        worker thread)."""
        if not days:
            return []
        on, params = _made_on(days, symbol)
        where = f"p.run = ? AND p.kind = ? AND p.answer_version = ? AND {on} AND p.status = 'ok'"
        where += " AND p.answer_class IS NOT NULL"
        if not include_special:
            where += " AND p.answer_special = 0"
        with self._reading() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                f"SELECT p.* FROM sim_kind_predictions p WHERE {where}", (run, kind, answer_version, *params)
            ).fetchall()
        out = []
        for r in rows:
            row = dict(r)
            row["output"] = None if row["output"] is None else json.loads(row["output"])
            row["answer_path"] = None if row["answer_path"] is None else json.loads(row["answer_path"])
            out.append(row)
        return out

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

    def day_touches(self, symbol: str, since: datetime, until: datetime) -> Mapping[Direction, int]:
        """Touch labels (this target version) of the bar closes in ``[since, until]`` stored so
        far, predicted or not (#169)."""
        rows = self._db.execute(
            "SELECT class, COUNT(*) FROM touches WHERE symbol = ? AND bar_close BETWEEN ? AND ?"
            f" AND target_version = {TARGET_VERSION} AND class IS NOT NULL GROUP BY class",
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
            "SELECT id, algorithm, at, previous, status, reason, simulated FROM statuses WHERE id > ?"
            " ORDER BY id",
            (after,),
        ).fetchall()
        return [{**dict(r), "simulated": bool(r["simulated"])} for r in rows]

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

    def at_close(
        self, symbol: str, bar_close: datetime, algorithm: str | None = None
    ) -> list[dict[str, Any]]:
        """Every prediction of the symbol at one bar close (#107); of one algorithm only if given."""
        if algorithm is None:
            return self._select(
                "WHERE p.symbol = ? AND p.bar_close = ? ORDER BY p.algorithm", (symbol, iso(bar_close))
            )
        return self._select(
            "WHERE p.symbol = ? AND p.bar_close = ? AND p.algorithm = ?", (symbol, iso(bar_close), algorithm)
        )

    def recent(
        self, symbol: str, limit: int, algorithm: str | None = None, before: datetime | None = None
    ) -> list[dict[str, Any]]:
        """The symbol's predictions of its last ``limit`` bar closes, newest first; of one
        algorithm only if given; of bar closes before ``before`` only if given (#108).
        """
        # Bar closes are stored as ISO 8601 in KST, so they compare as text.
        until = iso(before) if before is not None else "9999"
        if algorithm is None:
            return self._select(
                "WHERE p.symbol = ? AND p.bar_close IN (SELECT DISTINCT bar_close FROM predictions"
                " WHERE symbol = ? AND bar_close < ? ORDER BY bar_close DESC LIMIT ?)"
                " ORDER BY p.bar_close DESC, p.algorithm",
                (symbol, symbol, until, limit),
            )
        return self._select(
            "WHERE p.symbol = ? AND p.algorithm = ? AND p.bar_close < ? ORDER BY p.bar_close DESC LIMIT ?",
            (symbol, algorithm, until, limit),
        )

    def summary(self) -> list[AlgorithmSummary]:
        """Each predictor's counts, scored on the touch label (#170)."""
        over = "o.scored_at IS NOT NULL"
        rows = self._db.execute(
            f"SELECT p.algorithm, COUNT(*), TOTAL(p.status != 'ok'), TOTAL({over} AND t.class IS NOT NULL),"
            f" TOTAL({over} AND t.class IS NULL),"
            " TOTAL(o.prediction_id IS NOT NULL AND o.scored_at IS NULL),"
            f" TOTAL({over} AND t.class = 'up'), TOTAL({over} AND t.class = 'flat'),"
            f" TOTAL({over} AND t.class = 'down')"
            f" FROM predictions p LEFT JOIN outcomes o ON o.prediction_id = p.id {TOUCH_JOIN}"
            " GROUP BY p.algorithm ORDER BY p.algorithm"
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
        """The latest ``limit`` days with predictions, up to ``until``, latest first
        (``days_with_predictions``)."""
        return days_with_predictions(self._db, until, limit)

    def scored_predictions(
        self, days: Sequence[date], symbol: str | None, include_special: bool
    ) -> list[dict[str, Any]]:
        """Predictions made on ``days`` and scored on their touch label (``SCORED``), of one
        symbol or all, read on a connection of its own (``_reading``)."""
        if not days:
            return []
        with self._reading() as db:
            db.row_factory = sqlite3.Row
            return self._scored(days, symbol, include_special, db)

    def _scored(
        self, days: Sequence[date], symbol: str | None, include_special: bool, db: sqlite3.Connection
    ) -> list[dict[str, Any]]:
        return scored_rows(db, days, symbol, include_special)

    def scored_and_excluded(
        self, days: Sequence[date], symbol: str | None, include_special: bool
    ) -> tuple[list[dict[str, Any]], dict[str, Excluded]]:
        """``scored_predictions`` and what the stats leave out of the same predictions, by
        predictor, read in one transaction so they agree."""
        if not days:
            return [], {}
        with self._reading() as db:
            db.execute("BEGIN")
            db.row_factory = sqlite3.Row
            rows = self._scored(days, symbol, include_special, db)
            db.row_factory = None
            excluded = _excluded(days, symbol, db)
            db.execute("COMMIT")
        return rows, excluded

    def barrier_widths(self, days: Sequence[date], symbol: str | None) -> dict[tuple[str, str], float]:
        """The barrier width w the barrier kind stored at each bar close of ``days`` (of
        ``symbol`` if given), in basis points of the bar close's mid-point (w ticks × the tick
        ÷ the mid-point, #183, #210), by (symbol, bar close): a volatility known at the bar
        close (1-minute changes' σ × √4, no narrower than k), what the direction's stats split
        their calls by (#262). In basis points, not ticks: a tick changes size at band edges.
        Rows without the mid-point (stored before #210) are left out."""
        if not days:
            return {}
        where, params = _made_on(days, symbol)
        with self._reading() as db:
            rows = db.execute(
                "SELECT p.symbol, p.bar_close, MAX(p.width_ticks * p.tick * 1e4 / p.anchor)"
                " FROM kind_predictions p WHERE p.kind = 'barrier' AND p.width_ticks IS NOT NULL"
                f" AND p.tick IS NOT NULL AND p.anchor > 0 AND {where}"
                " GROUP BY p.symbol, p.bar_close",
                params,
            ).fetchall()
        return {(s, b): float(w) for s, b, w in rows}

    def _reading(self) -> contextlib.closing[sqlite3.Connection]:
        """A read-only connection of its own: a worker thread can read while the engine
        keeps writing (WAL lets them work at once)."""
        return contextlib.closing(sqlite3.connect(self._path.resolve().as_uri() + "?mode=ro", uri=True))

    def last_reference(self, symbol: str) -> tuple[float, datetime] | None:
        """The mid-point at the symbol's latest bar close with one (its touch path's ``anchor``)
        and that bar close (#307: the cost map's price out of continuous trading); None without."""
        row = self._db.execute(
            "SELECT bar_close, json_extract(path, '$.anchor') FROM touches"
            " WHERE symbol = ? AND json_extract(path, '$.anchor') IS NOT NULL"
            " ORDER BY bar_close DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        return None if row is None else (float(row[1]), datetime.fromisoformat(row[0]))

    def last_bar_close(self) -> str | None:
        value: str | None = self._db.execute("SELECT MAX(bar_close) FROM predictions").fetchone()[0]
        return value

    def pending_count(self) -> int:
        count: int = self._db.execute("SELECT COUNT(*) FROM outcomes WHERE scored_at IS NULL").fetchone()[0]
        return count

    def _select(self, where: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        return select_predictions(self._db, where, params)
