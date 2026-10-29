"""The trader's policy (#399, #393 "정책과 자기 개선"): a versioned set of values the trader trades
on, fitted again every night from the stored predictions (``refit.py``).

**What a policy is.** The cells it trades, first ranked first, and for each its threshold θ (the
rise's probability that buys) and its Kelly fraction λ (the share of the cell's Kelly f* bought,
cut to 0–1); with when it was made and why, the cells the cost map passed that it leaves out and
why, and what it was fitted on (``fit``). No limit is part of it: the capital and the loss limits
come from the code and the server's ``.env`` alone (#393 "한도와 정지"). A policy holds none of
them, a stored one that carried one would be read without it (``PolicyCell.from_json``), and
nothing here reads or writes the trader's settings.

**Champion and challengers.** The first policy fitted becomes the champion; every later one is
kept as a challenger (#399). ``champions`` keeps each change of the champion, with why. The
challengers run in paper beside the champion (#400, ``desk.py``), at most ``CHALLENGERS`` of them:
a new one drops the oldest still running for good (``dropped_at``, ``cap``). A challenger runs on
a trading day when it was kept before that day's pick (``PICK_AT``, 08:30: ``running_versions``), so one
fitted tonight runs from the next trading day. One that beats the champion in paper becomes the
champion after the market (``promotion.py``, ``crown``); the champion it replaces runs no more.

**How the trader applies the champion** (``apply``), at each pick (its start and 08:30, #395):
the cost map still decides what may trade (a cell passing at a market order's round trip, a
necessary condition), and the champion what does. The trader trades the champion's cells among
those passing, in the champion's order, each at its θ and with λ × the day's f* of the capital.
A cell passing that the champion does not have is not traded (``NOT_IN_POLICY`` when that leaves
none): it comes in through a challenger fitted on a later night, once promoted (#400). An empty
champion (no cell passed the night it was fitted) trades nothing. Before the first champion the
trader trades the cost map's cells at ``KRX_TRADER_THRESHOLD`` and ``KELLY_FRACTION``, as #395
did, and its trades carry no policy.

**Store** (``state/trader.sqlite3``, beside the trader's tables): ``policies`` (each version, its
id the version) and ``champions``. The nightly re-fit writes them from the engine's container
(``add``); the trader reads the champion at each pick (``champion``), and the Engine API reads
them read-only (``read_policies``, ``GET /v1/trader/policies``).
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

from krx_quant.feeds.base import KST
from krx_quant.trader import PAPER, PICK_AT
from krx_quant.trader.cells import Cell
from krx_quant.trader.selection import NOT_IN_POLICY, Selection

SCHEMA = """
CREATE TABLE IF NOT EXISTS policies (   -- each policy version (#399): what the trader trades on
    id INTEGER PRIMARY KEY,             -- the version
    created_at TEXT NOT NULL,
    reason TEXT NOT NULL,               -- why it was made
    cells TEXT NOT NULL,                -- JSON: the cells it trades, first ranked first, with θ and λ
    left_out TEXT NOT NULL,             -- JSON: the cells the cost map passed that it does not trade, and why
    fit TEXT NOT NULL,                  -- JSON: what it was fitted on: cost map, grid, result, run
    dropped_at TEXT                     -- a challenger dropped for a newer one (#400); null: not dropped
);
CREATE TABLE IF NOT EXISTS champions (  -- each change of the champion (#399; promotions, #400)
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    policy_id INTEGER NOT NULL REFERENCES policies (id),
    reason TEXT NOT NULL,
    detail TEXT                         -- JSON: a promotion's figures (#400); null for the first
);
"""
# Columns added since the tables were first made (#400): added to a store made before, null in its
# rows.
_ADDED = {"policies": (("dropped_at", "TEXT"),), "champions": (("detail", "TEXT"),)}

# The challengers run at once (#393, #400): a new one drops the oldest.
CHALLENGERS = 3

CHAMPION, CHALLENGER, FORMER, DROPPED = "champion", "challenger", "former", "dropped"
# Why the first policy is the champion (#399): no challenger has run against anything yet.
FIRST = "처음 맞춘 정책(#399)"
# How long a writer waits for the trader's own writes to the same store.
BUSY_SECONDS = 30.0


@dataclass(frozen=True)
class PolicyCell:
    """A cell a policy trades: its threshold θ and Kelly fraction λ, and what it was fitted on
    (``fitted``: the cost map's p*, lower bound and f*, the simulated trades)."""

    kind: str
    role: str  # the predictor's on the cost map: consensus, top, baseline
    predictor: str
    threshold: float
    kelly_fraction: float
    fitted: Mapping[str, Any] = field(default_factory=dict)

    @property
    def cell(self) -> Cell:
        return Cell(self.kind, self.predictor)

    def as_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "role": self.role,
            "predictor": self.predictor,
            "threshold": self.threshold,
            "kelly_fraction": self.kelly_fraction,
            "fitted": dict(self.fitted),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> PolicyCell:
        """A stored cell; its values only (any other key, a limit's say, is not read). ValueError
        when one is missing or not a number from 0 to 1 (λ above 0)."""
        threshold, fraction = _unit(data.get("threshold")), _unit(data.get("kelly_fraction"))
        if not all(isinstance(data.get(k), str) for k in ("kind", "role", "predictor")):
            raise ValueError(f"a policy cell without its kind, role or predictor: {data!r}")
        if threshold is None or fraction is None or fraction <= 0:
            raise ValueError(f"a policy cell's θ and λ must be numbers from 0 to 1: {data!r}")
        fitted = data.get("fitted")
        return cls(
            data["kind"],
            data["role"],
            data["predictor"],
            threshold,
            fraction,
            dict(fitted) if isinstance(fitted, Mapping) else {},
        )


@dataclass(frozen=True)
class Policy:
    """A policy version as kept."""

    version: int
    created_at: datetime
    reason: str
    cells: tuple[PolicyCell, ...]  # first ranked first
    left_out: tuple[dict[str, Any], ...] = ()
    fit: Mapping[str, Any] = field(default_factory=dict)
    dropped_at: datetime | None = None  # a challenger dropped for a newer one (#400)


def apply(policy: Policy | None, selection: Selection) -> Selection:
    """``selection`` (the cells the cost map passes today) under ``policy`` (module doc): its cells
    among those passing, in its order, each at its θ and with λ × the day's f*; the others marked
    as not in it. ``selection`` as it is without a policy."""
    if policy is None:
        return selection
    passing = {p.cell: p for p in selection.picks}
    picks = []
    for cell in policy.cells:
        found = passing.get(cell.cell)
        if found is not None:
            fraction = min(1.0, max(0.0, cell.kelly_fraction * found.kelly))
            picks.append(replace(found, threshold=cell.threshold, fraction=fraction))
    kept = {p.cell for p in picks}
    considered = []
    for seen in selection.considered:
        predictor = seen.get("predictor")
        if seen.get("picked") and Cell(seen["kind"], predictor or "") not in kept:
            seen = {**seen, "picked": False, "why": f"not in policy {policy.version}"}
        considered.append(seen)
    reason = selection.reason if picks or selection.reason is not None else NOT_IN_POLICY
    return replace(
        selection, picks=tuple(picks), considered=tuple(considered), reason=reason, policy=policy.version
    )


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(f"BEGIN;{SCHEMA}COMMIT;")
    with db:
        for table, columns in _ADDED.items():
            have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
            for name, spec in columns:
                if name not in have:
                    try:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")
                    except sqlite3.OperationalError as e:  # added meanwhile by the other writer
                        if "duplicate column" not in str(e):
                            raise


def connect(path: Path) -> sqlite3.Connection:
    """The trader's store for writing policies (the nightly re-fit, in the engine's container),
    write-ahead log as the trader keeps it. Every table of the store is made first if missing, the
    trader's as the trader makes them (``store.TraderStore``): a re-fit before the trader's first
    start (a collector without it) must not leave a store with the policies alone, which the
    Engine API's readers of trades and marks would take for a whole one."""
    from krx_quant.trader.store import TraderStore  # it imports this module

    TraderStore(path).close()
    db = sqlite3.connect(path, timeout=BUSY_SECONDS)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    ensure_schema(db)
    return db


def add(
    db: sqlite3.Connection,
    at: datetime,
    reason: str,
    cells: Sequence[PolicyCell],
    left_out: Sequence[Mapping[str, Any]],
    fit: Mapping[str, Any],
) -> tuple[int, bool]:
    """Keep a new policy version; the first one kept becomes the champion (``FIRST``), in the same
    transaction; a later one, a challenger, drops the oldest beyond ``CHALLENGERS`` (``cap``). Its
    version, and whether it is the champion."""
    with db:
        cursor = db.execute(
            "INSERT INTO policies (created_at, reason, cells, left_out, fit) VALUES (?, ?, ?, ?, ?)",
            (
                _iso(at),
                reason,
                _dumps([c.as_json() for c in cells]),
                _dumps([dict(x) for x in left_out]),
                _dumps(dict(fit)),
            ),
        )
        version = cursor.lastrowid
        assert version is not None
        first = db.execute("SELECT 1 FROM champions LIMIT 1").fetchone() is None
        if first:
            db.execute(
                "INSERT INTO champions (at, policy_id, reason) VALUES (?, ?, ?)", (_iso(at), version, FIRST)
            )
        else:
            _cap(db, at)
    return version, first


def _a_challenger(db: sqlite3.Connection) -> str:
    """A policy ``p`` that is a challenger (#400): never the champion, not dropped (in a store the
    trader has not opened since #400, none is)."""
    columns = {r[1] for r in db.execute("PRAGMA table_info(policies)")}
    dropped = "p.dropped_at IS NULL AND " if "dropped_at" in columns else ""
    return f"{dropped}p.id NOT IN (SELECT policy_id FROM champions)"


def cap(db: sqlite3.Connection, at: datetime) -> list[int]:
    """The challengers beyond the newest ``CHALLENGERS`` dropped at ``at`` for good (the oldest
    first goes): the versions dropped. A dropped one never runs again, even once a newer one is
    promoted (the challengers are fewer then until the next night's)."""
    with db:
        return _cap(db, at)


def _cap(db: sqlite3.Connection, at: datetime) -> list[int]:
    rows = db.execute(f"SELECT p.id FROM policies p WHERE {_a_challenger(db)} ORDER BY p.id DESC").fetchall()
    dropped = [r[0] for r in rows[CHALLENGERS:]]
    for version in dropped:
        db.execute("UPDATE policies SET dropped_at = ? WHERE id = ?", (_iso(at), version))
    return sorted(dropped)


def dropped_at(db: sqlite3.Connection, at: datetime) -> list[int]:
    """The challengers dropped at ``at`` (a night's ``add``)."""
    rows = db.execute("SELECT id FROM policies WHERE dropped_at = ? ORDER BY id", (_iso(at),))
    return [r[0] for r in rows]


def champion_version(db: sqlite3.Connection) -> int | None:
    """The champion's version now (the latest of ``champions``); None before the first."""
    row = db.execute("SELECT policy_id FROM champions ORDER BY id DESC LIMIT 1").fetchone()
    return None if row is None else int(row[0])


def challenger_versions(db: sqlite3.Connection) -> list[int]:
    """The challengers' versions, oldest first: never the champion, not dropped, the newest
    ``CHALLENGERS`` (a store from before the cap may keep more until the next night caps them)."""
    rows = db.execute(
        f"SELECT p.id FROM policies p WHERE {_a_challenger(db)} ORDER BY p.id DESC LIMIT ?", (CHALLENGERS,)
    )
    return sorted(int(r[0]) for r in rows)


def running_versions(db: sqlite3.Connection, day: date) -> list[int]:
    """The challengers that run on ``day``, oldest first: kept before its pick (``PICK_AT``, 08:30),
    so that one fitted on a night runs from the next trading day, whenever the trader reads it."""
    versions = challenger_versions(db)
    if not versions:
        return []
    marks = ", ".join("?" * len(versions))
    cutoff = datetime.combine(day, PICK_AT, KST)
    rows = db.execute(f"SELECT id, created_at FROM policies WHERE id IN ({marks})", versions)
    return sorted(int(r[0]) for r in rows if datetime.fromisoformat(r[1]) < cutoff)


def read(db: sqlite3.Connection, version: int) -> Policy:
    """Policy ``version``; ``PolicyError`` when it is not kept or cannot be read."""
    row = db.execute("SELECT * FROM policies WHERE id = ?", (version,)).fetchone()
    if row is None:
        raise PolicyError(f"policy {version}: not kept")
    try:
        return _policy(row)
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        raise PolicyError(f"policy {version}: {e}") from e


def crown(
    db: sqlite3.Connection,
    at: datetime,
    version: int,
    reason: str,
    detail: Mapping[str, Any],
    replacing: int | None = None,
) -> bool:
    """``version`` the champion from ``at`` (#400, ``promotion.promote``): a change of the champion
    kept, with why and its figures; with ``replacing``, only while that is still the champion (no
    other writer changed it meanwhile). Whether it was kept."""
    with db:
        db.execute("BEGIN IMMEDIATE")
        if replacing is not None and champion_version(db) != replacing:
            return False
        db.execute(
            "INSERT INTO champions (at, policy_id, reason, detail) VALUES (?, ?, ?, ?)",
            (_iso(at), version, reason, _dumps(dict(detail))),
        )
    return True


class PolicyError(Exception):
    """A stored policy that cannot be read."""


def champion(db: sqlite3.Connection) -> Policy | None:
    """The champion now (the latest of ``champions``); None before the first. ``PolicyError`` when
    it cannot be read: the trader then trades nothing (``selection.NO_POLICY``), never as before
    the first."""
    row = db.execute(
        "SELECT p.* FROM champions c JOIN policies p ON p.id = c.policy_id ORDER BY c.id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    try:
        return _policy(row)
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        raise PolicyError(f"policy {row['id']}: {e}") from e


def status_of(version: int, champion: int | None, once: Collection[int], dropped: bool) -> str:
    """A policy's status: ``champion`` now, ``former`` once, ``dropped`` (a challenger dropped for
    a newer one, #400), else ``challenger``."""
    if version == champion:
        return CHAMPION
    if version in once:
        return FORMER
    return DROPPED if dropped else CHALLENGER


def read_policies(path: Path, limit: int, mode: str = PAPER) -> dict[str, Any]:
    """The last ``limit`` policy versions, newest first, each with its status (``status_of``) and
    its paper record (#400, ``promotion.paper``), and every change of the champion, newest first:
    ``GET /v1/trader/policies``. Read-only; none without the store or before the first policy."""
    from krx_quant.trader import promotion  # it imports this module

    if not path.exists():
        return {"champion": None, "policies": [], "champions": []}
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        if not _has_policies(db):
            return {"champion": None, "policies": [], "champions": []}
        changes = [dict(r) for r in db.execute("SELECT * FROM champions ORDER BY id DESC")]
        rows = db.execute("SELECT * FROM policies ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        records = promotion.records(db, mode, [r["id"] for r in rows])
    finally:
        db.close()
    now = changes[0]["policy_id"] if changes else None
    once = {c["policy_id"] for c in changes}
    policies = []
    for row in rows:
        keys = row.keys()
        dropped = "dropped_at" in keys and row["dropped_at"] is not None
        policies.append(
            {
                "version": row["id"],
                "status": status_of(row["id"], now, once, dropped),
                "created_at": row["created_at"],
                "reason": row["reason"],
                "cells": json.loads(row["cells"]),
                "left_out": json.loads(row["left_out"]),
                "fit": json.loads(row["fit"]),
                "dropped_at": row["dropped_at"] if dropped else None,
                "paper": promotion.paper(records.get(row["id"])),
            }
        )
    return {
        "champion": now,
        "policies": policies,
        "champions": [
            {
                "at": c["at"],
                "version": c["policy_id"],
                "reason": c["reason"],
                "detail": json.loads(c["detail"]) if c.get("detail") else None,
            }
            for c in changes
        ],
    }


def _has_policies(db: sqlite3.Connection) -> bool:
    """Whether the store has the tables yet (a trader from before #399 made it)."""
    found = db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name IN ('policies', 'champions')"
    ).fetchone()[0]
    return bool(found == 2)


def _policy(row: sqlite3.Row) -> Policy:
    cells, left_out, fit = (json.loads(row[k]) for k in ("cells", "left_out", "fit"))
    if not isinstance(cells, list) or not isinstance(left_out, list) or not isinstance(fit, dict):
        raise ValueError("its cells, left_out and fit are not a list, a list and an object")
    dropped = row["dropped_at"] if "dropped_at" in row.keys() else None
    return Policy(
        version=row["id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        reason=row["reason"],
        cells=tuple(PolicyCell.from_json(c) for c in cells),
        left_out=tuple(dict(x) for x in left_out),
        fit=fit,
        dropped_at=None if dropped is None else datetime.fromisoformat(dropped),
    )


def _unit(value: Any) -> float | None:
    """``value`` if it is a number from 0 to 1, else None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) and 0 <= value <= 1 else None


def _iso(at: datetime) -> str:
    return at.astimezone(KST).isoformat()


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
