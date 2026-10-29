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
kept as a challenger only (#399). Running the challengers in paper beside the champion and
promoting one is #400's. ``champions`` keeps each change of the champion, with why.

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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from krx_quant.feeds.base import KST
from krx_quant.trader.cells import Cell
from krx_quant.trader.selection import NOT_IN_POLICY, Selection

SCHEMA = """
CREATE TABLE IF NOT EXISTS policies (   -- each policy version (#399): what the trader trades on
    id INTEGER PRIMARY KEY,             -- the version
    created_at TEXT NOT NULL,
    reason TEXT NOT NULL,               -- why it was made
    cells TEXT NOT NULL,                -- JSON: the cells it trades, first ranked first, with θ and λ
    left_out TEXT NOT NULL,             -- JSON: the cells the cost map passed that it does not trade, and why
    fit TEXT NOT NULL                   -- JSON: what it was fitted on: cost map, grid, result, run
);
CREATE TABLE IF NOT EXISTS champions (  -- each change of the champion (#399; promotions, #400)
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    policy_id INTEGER NOT NULL REFERENCES policies (id),
    reason TEXT NOT NULL
);
"""

CHAMPION, CHALLENGER, FORMER = "champion", "challenger", "former"
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


def connect(path: Path) -> sqlite3.Connection:
    """The trader's store for writing policies (the nightly re-fit, in the engine's container): the
    tables made if missing, write-ahead log as the trader keeps it."""
    path.parent.mkdir(parents=True, exist_ok=True)
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
    transaction. Its version, and whether it is the champion."""
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
    return version, first


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


def read_policies(path: Path, limit: int) -> dict[str, Any]:
    """The last ``limit`` policy versions, newest first, each with its status (``champion`` now,
    ``former`` once, else ``challenger``), and every change of the champion, newest first:
    ``GET /v1/trader/policies``. Read-only; none without the store or before the first policy."""
    if not path.exists():
        return {"champion": None, "policies": [], "champions": []}
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        if not _has_policies(db):
            return {"champion": None, "policies": [], "champions": []}
        changes = [dict(r) for r in db.execute("SELECT * FROM champions ORDER BY id DESC")]
        rows = db.execute("SELECT * FROM policies ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        db.close()
    now = changes[0]["policy_id"] if changes else None
    once = {c["policy_id"] for c in changes}
    policies = []
    for row in rows:
        status = CHAMPION if row["id"] == now else FORMER if row["id"] in once else CHALLENGER
        policies.append(
            {
                "version": row["id"],
                "status": status,
                "created_at": row["created_at"],
                "reason": row["reason"],
                "cells": json.loads(row["cells"]),
                "left_out": json.loads(row["left_out"]),
                "fit": json.loads(row["fit"]),
            }
        )
    return {
        "champion": now,
        "policies": policies,
        "champions": [{"at": c["at"], "version": c["policy_id"], "reason": c["reason"]} for c in changes],
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
    return Policy(
        version=row["id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        reason=row["reason"],
        cells=tuple(PolicyCell.from_json(c) for c in cells),
        left_out=tuple(dict(x) for x in left_out),
        fit=fit,
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
