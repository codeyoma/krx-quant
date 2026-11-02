"""The trader's policy (#399, #393 "정책과 자기 개선"): a versioned set of values the trader trades
on, fitted again every night from the stored predictions (``refit.py``).

**What a policy is.** The cells it trades, first ranked first, and for each its threshold θ (the
rise's probability that buys); with when it was made and why, the cells the cost map named (#472;
before it, those it passed) that it leaves out and why, and what it was fitted on (``fit``). No
limit is part of it: the capital, the loss limits and the risk rules (#471: a trade's loss budget,
which sizes the buy) come from the code and the server's ``.env`` alone (#393 "한도와 정지"). A policy
holds none of them, a stored one that carried one would be read without it
(``PolicyCell.from_json``), and nothing here reads or writes the trader's settings.

> **바뀜** (2026-11-03, #478): each cell had a Kelly fraction λ too (the share of the cell's f*
> bought) → none: a policy that kept one (``kelly_fraction``, versions fitted before #471) is read
> without it, its stored row as it was. 이유: spec #468 4 "크기": the loss budget sizes every buy.

**Champion and challengers.** The first policy kept becomes the champion (#399; the nightly re-fit
keeps an empty one first, #473, ``refit.py``); every later one waits for the next cohort (#473,
``cohort.py``). ``champions`` keeps each change of the champion, with why. The challengers run in
paper beside the champion (#400, ``desk.py``) as a cohort of at most ``CHALLENGERS``: formed at the
start of a review cycle from the policies waiting, the newest first (``admitted_at``: when it
entered one), and judged once, on the cycle's review night (``promotion.py``): the one crowned
(``crown``; the champion it replaces runs no more) leaves it as the champion, every other member is
dropped (``drop``, ``dropped_at``: it never runs again), as is a policy waiting that a cohort did not
take. A fit with the values of the champion, of a running challenger or of a policy waiting is not
kept (``same_as``). A challenger runs on a trading day of its cohort's cycle when it entered before
that day's pick (``PICK_AT``, 08:30: ``running_versions``), so a cohort formed on a review night
runs from the next trading day, the cycle's first.

> Spec #393 dropped the oldest of three for each new one; with a fit every night, none would have
> reached the promotion's ten days (#416, Codex's review).

> **바뀜** (2026-11-03, #481): a later policy took a free one of three running slots at once, or
> waited and was admitted when one freed, the newest first (``admit``); a challenger left its slot
> when promoted or after 20 common days unpromoted; a fit equal to a policy waiting was kept → a
> later policy waits for the next cohort, formed at a review cycle's start; every member leaves at
> the cycle's review, crowned or dropped; a fit equal to a policy waiting is not kept. 이유: spec
> #468 5 "도전 정책": challengers enter as a cohort and are judged once, on the review night.

**How the trader applies the champion** (``apply``), at each pick (its start and 08:30, #395):
the champion decides what trades. The trader trades the champion's cells, in the champion's order,
each at its θ, sized by the loss budget (``paper.size``), whatever the cost map's verdict on them
(#472: the nightly fit entered each on the lower bound of its replayed one-share net), with the cost
map's figures where it names the cell (``Selection.named``) and none where it does not
(``selection.Pick.unmapped``). A cell the champion does not have is not traded, whatever its verdict
(``NOT_IN_POLICY`` when that leaves none): it comes in through a challenger fitted on a later night,
once promoted (#400). An empty champion trades nothing. Before the first champion nothing trades
(#469, ``NO_CHAMPION``), nor while the cost map cannot be read (``NO_COST_MAP``).

> **바뀜** (2026-11-03, #480): the champion's cells traded only among those the cost map passed at a
> market order's round trip (a necessary condition), and a cell the cost map did not name that day
> did not trade (``OFF_MAP``) → the champion's cells trade, named or not. 이유: spec #468 3 "진입":
> the execution lower bound of the nightly fit replaces the cost map's pass.

**The frozen version** (#469, ``review.py``): the champion's cells, their order and θ hold from one
review to the next. A cell is its predictor's name (``Cell``: kind and predictor), never the cost
map's role: when the cost map's ``top`` of a kind becomes another algorithm, the champion's cell
stays its own (and trades, #472: never the other algorithm), and one whose predictor stopped
predicting (``GONE``, ``cells.gone``) trades no more until the next review. Each pick keeps every
cell of the policy and why it does not trade (``Selection.cells``).

> **바뀜** (2026-11-02, #476): before the first champion the trader traded the cost map's cells at
> ``KRX_TRADER_THRESHOLD`` and ``KELLY_FRACTION`` → nothing. 이유: spec #468 "챔피언 전 기본 매매는
> 없앤다". A policy's values compare by its cells' predictors' names, θ and λ, no longer their roles.

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
from krx_quant.trader import PICK_AT
from krx_quant.trader.cells import Cell
from krx_quant.trader.selection import (
    GONE,
    NO_CHAMPION,
    NO_COST_MAP,
    NOT_IN_POLICY,
    Pick,
    Selection,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS policies (   -- each policy version (#399): what the trader trades on
    id INTEGER PRIMARY KEY,             -- the version
    created_at TEXT NOT NULL,
    reason TEXT NOT NULL,               -- why it was made
    cells TEXT NOT NULL,                -- JSON: the cells it trades, first ranked first, with θ (λ to #471)
    left_out TEXT NOT NULL,             -- JSON: the cells the cost map named that it does not trade, and why
    fit TEXT NOT NULL,                  -- JSON: what it was fitted on: cost map, grid, result, run
    admitted_at TEXT,                   -- a challenger given a running slot (#400); null: waiting
    dropped_at TEXT                     -- a challenger dropped unpromoted (#400); null: not dropped
);
CREATE TABLE IF NOT EXISTS champions (  -- each change of the champion (#399; promotions, #400)
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    policy_id INTEGER NOT NULL REFERENCES policies (id),
    reason TEXT NOT NULL,
    detail TEXT                         -- JSON: a promotion's figures (#400); null for the first
);
CREATE TABLE IF NOT EXISTS cohorts (    -- each challenger cohort (#473, cohort.py), as formed
    evaluation INTEGER NOT NULL,        -- the evaluation version (#469) and its review cycle
    cycle INTEGER NOT NULL,
    formed_at TEXT NOT NULL,
    first_day TEXT NOT NULL,            -- the cycle's first trading day: its members run from it
    review_day TEXT NOT NULL,           -- the cycle's review day: judged after its close
    champion_id INTEGER,                -- the champion it is judged against, fixed when formed
    passed_over TEXT NOT NULL,          -- JSON: the policies waiting it did not take, and why
    PRIMARY KEY (evaluation, cycle)
);
CREATE TABLE IF NOT EXISTS cohort_members (  -- each cohort's challengers (#473)
    evaluation INTEGER NOT NULL,
    cycle INTEGER NOT NULL,
    policy_id INTEGER NOT NULL REFERENCES policies (id),
    rank INTEGER NOT NULL,              -- 1: the newest
    PRIMARY KEY (evaluation, cycle, policy_id)
);
CREATE TABLE IF NOT EXISTS cohort_verdicts (  -- each cohort's one judgment (#473), never changed
    evaluation INTEGER NOT NULL,
    cycle INTEGER NOT NULL,
    judged_at TEXT NOT NULL,
    champion_id INTEGER,                -- the champion it was judged against: the cohort's
    outcome TEXT NOT NULL,              -- promoted, kept, missed
    promoted_id INTEGER,                -- the member crowned; null: none
    level REAL,                         -- each member's lower bound's two-sided level (Bonferroni)
    members TEXT NOT NULL,              -- JSON: each member's common days, mean, lower bound, verdict
    PRIMARY KEY (evaluation, cycle)
);
"""
# A cohort as formed and its verdict as judged are kept as they are (#473): never updated nor deleted.
_KEPT = "".join(
    f"CREATE TRIGGER IF NOT EXISTS {table}_kept_{event} BEFORE {event} ON {table}"
    f" BEGIN SELECT RAISE(ABORT, '{table}: kept as written (#473)'); END;"
    for table in ("cohorts", "cohort_members", "cohort_verdicts")
    for event in ("update", "delete")
)
# Columns added since the tables were first made (#400): added to a store made before, null in its
# rows.
_ADDED = {
    "policies": (("admitted_at", "TEXT"), ("dropped_at", "TEXT")),
    "champions": (("detail", "TEXT"),),
}

# The challengers of a cohort at most (#393, #400; #473: those that enter together).
CHALLENGERS = 3

CHAMPION, CHALLENGER, FORMER, DROPPED, WAITING = "champion", "challenger", "former", "dropped", "waiting"
# Why the first policy is the champion (#399): no challenger has run against anything yet.
FIRST = "처음 맞춘 정책(#399)"
# How long a writer waits for the trader's own writes to the same store.
BUSY_SECONDS = 30.0


@dataclass(frozen=True)
class PolicyCell:
    """A cell a policy trades: its threshold θ, and what it was fitted on (``fitted``: the cost map's
    p*, lower bound and f*, the simulated trades)."""

    kind: str
    role: str  # the predictor's on the cost map when fitted: consensus, top, baseline (#469: not its key)
    predictor: str
    threshold: float
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
            "fitted": dict(self.fitted),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> PolicyCell:
        """A stored cell; its values only (any other key, a limit's say, or the Kelly fraction λ a
        policy fitted before #471 kept, is not read). ValueError when one is missing or θ is not a
        number from 0 to 1."""
        threshold = _unit(data.get("threshold"))
        if not all(isinstance(data.get(k), str) for k in ("kind", "role", "predictor")):
            raise ValueError(f"a policy cell without its kind, role or predictor: {data!r}")
        if threshold is None:
            raise ValueError(f"a policy cell's θ must be a number from 0 to 1: {data!r}")
        fitted = data.get("fitted")
        return cls(
            data["kind"],
            data["role"],
            data["predictor"],
            threshold,
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
    admitted_at: datetime | None = None  # a challenger given a running slot (#400)
    dropped_at: datetime | None = None  # a challenger dropped unpromoted (#400)


def apply(policy: Policy | None, selection: Selection, gone: Collection[Cell] = ()) -> Selection:
    """``selection`` (today's cost map) under ``policy`` (module doc): its cells, by their predictors'
    names, in its order, each at its θ, whatever the cost map's verdict (#472), with the cost map's
    figures where it names them; the cells the cost map passes that it does not have marked as not in
    it. Each of its cells kept with whether it trades and why not (``Selection.cells``, #469):
    ``GONE`` (in ``gone``: its predictor stopped predicting), ``NO_COST_MAP``. Without a policy
    nothing trades (``NO_CHAMPION``, #469), and a cost map that could not be read stays as it is."""
    if policy is None:
        if selection.reason == NO_COST_MAP:
            return selection
        unpicked = tuple(
            {**seen, "picked": False, "why": NO_CHAMPION} if seen.get("picked") else seen
            for seen in selection.considered
        )
        return replace(selection, picks=(), considered=unpicked, reason=NO_CHAMPION, named=())
    # The cost map's figures of each cell it names: its passing picks are among them.
    named = {p.cell: p for p in (*selection.picks, *selection.named)}
    picks: list[Pick] = []
    cells = []
    for cell in policy.cells:
        why = None
        if selection.reason == NO_COST_MAP:
            why = NO_COST_MAP
        elif cell.cell in gone:
            why = GONE
        else:
            found = named.get(cell.cell) or Pick.unmapped(cell.kind, cell.role, cell.predictor)
            picks.append(replace(found, threshold=cell.threshold))
        cells.append(
            {
                "kind": cell.kind,
                "role": cell.role,
                "predictor": cell.predictor,
                "threshold": cell.threshold,
                "traded": why is None,
                "why": why,
            }
        )
    kept = {p.cell for p in picks}
    mine = {c.cell for c in policy.cells}
    considered = []
    for seen in selection.considered:
        named_cell = Cell(seen["kind"], seen.get("predictor") or "")
        if named_cell in kept:  # #472: a champion's cell trades whatever its verdict
            seen = {**seen, "picked": True}
        elif seen.get("picked"):
            why = GONE if named_cell in mine else f"not in policy {policy.version}"
            seen = {**seen, "picked": False, "why": why}
        considered.append(seen)
    reason = None if picks else selection.reason or NOT_IN_POLICY
    return replace(
        selection,
        picks=tuple(picks),
        considered=tuple(considered),
        reason=reason,
        policy=policy.version,
        cells=tuple(cells),
        named=(),
    )


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(f"BEGIN;{SCHEMA}{_KEPT}COMMIT;")
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
    first_reason: str = FIRST,
) -> tuple[int, bool]:
    """Keep a new policy version; the first one kept becomes the champion (why: ``first_reason``), in
    the same transaction; a later one waits for the next cohort (#473, ``cohort.py``). Its version,
    and whether it is the champion."""
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
                "INSERT INTO champions (at, policy_id, reason) VALUES (?, ?, ?)",
                (_iso(at), version, first_reason),
            )
    return version, first


def _columns(db: sqlite3.Connection) -> set[str]:
    return {r[1] for r in db.execute("PRAGMA table_info(policies)")}


def _a_challenger(db: sqlite3.Connection) -> str:
    """A policy ``p`` never the champion and not dropped (#400)."""
    dropped = "p.dropped_at IS NULL AND " if "dropped_at" in _columns(db) else ""
    return f"{dropped}p.id NOT IN (SELECT policy_id FROM champions)"


def _running(db: sqlite3.Connection) -> str:
    """A challenger ``p`` in a running slot (#400); none in a store the trader has not opened since."""
    if "admitted_at" not in _columns(db):
        return "0"
    return f"p.admitted_at IS NOT NULL AND {_a_challenger(db)}"


def drop(db: sqlite3.Connection, at: datetime, versions: Collection[int]) -> None:
    """``versions`` dropped at ``at`` for good (#400; #473: a cohort's members not crowned at its
    review, the policies waiting a cohort did not take): they never run again."""
    with db:
        _drop(db, at, versions)


def _drop(db: sqlite3.Connection, at: datetime, versions: Collection[int]) -> None:
    for version in versions:
        db.execute(
            "UPDATE policies SET dropped_at = ? WHERE id = ? AND dropped_at IS NULL", (_iso(at), version)
        )


def waiting_versions(db: sqlite3.Connection) -> list[int]:
    """The policies kept but not admitted, newest first (#400)."""
    if "admitted_at" not in _columns(db):
        return []
    rows = db.execute(
        f"SELECT p.id FROM policies p WHERE p.admitted_at IS NULL AND {_a_challenger(db)} ORDER BY p.id DESC"
    )
    return [int(r[0]) for r in rows]


def values(cells: Sequence[PolicyCell]) -> tuple[tuple[str, str, float], ...]:
    """A policy's values (#393: its cells in their order, each θ; no λ since #471), what was fitted
    aside; a cell by its predictor's name, not its role on the cost map (#469)."""
    return tuple((c.kind, c.predictor, c.threshold) for c in cells)


def same_as(db: sqlite3.Connection, cells: Sequence[PolicyCell]) -> int | None:
    """The champion, the running challenger or the policy waiting with the values of ``cells``, if
    any (#400: a fit that changes nothing is not kept; #473: nor one already waiting for the next
    cohort). ``PolicyError`` when one of them cannot be read."""
    found = values(cells)
    current = champion_version(db)
    for version in [*([] if current is None else [current]), *challenger_versions(db), *waiting_versions(db)]:
        if values(read(db, version).cells) == found:
            return version
    return None


def champion_version(db: sqlite3.Connection) -> int | None:
    """The champion's version now (the latest of ``champions``); None before the first."""
    row = db.execute("SELECT policy_id FROM champions ORDER BY id DESC LIMIT 1").fetchone()
    return None if row is None else int(row[0])


def challenger_versions(db: sqlite3.Connection) -> list[int]:
    """The challengers, oldest first: entered a cohort (``admitted_at``, #473), never the champion,
    not dropped (at most ``CHALLENGERS``, the cohort not judged yet)."""
    rows = db.execute(f"SELECT p.id FROM policies p WHERE {_running(db)} ORDER BY p.id")
    return [int(r[0]) for r in rows]


def running_versions(db: sqlite3.Connection, day: date) -> list[int]:
    """The challengers that run on ``day``, oldest first: the members of the cohort whose review
    cycle holds ``day`` (#473: from its first trading day to its review day), entered before its pick
    (``PICK_AT``, 08:30), so that a cohort formed on a night runs from the next trading day, whenever
    the trader reads it."""
    versions = challenger_versions(db)
    if not versions or not _has_table(db, "cohort_members"):
        return []
    rows = db.execute(
        "SELECT p.id, p.admitted_at FROM cohort_members m"
        " JOIN cohorts c ON c.evaluation = m.evaluation AND c.cycle = m.cycle"
        " JOIN policies p ON p.id = m.policy_id WHERE c.first_day <= :day AND c.review_day >= :day",
        {"day": day.isoformat()},
    )
    cutoff = datetime.combine(day, PICK_AT, KST)
    return sorted(
        int(r[0])
        for r in rows
        if r[0] in versions and r[1] is not None and datetime.fromisoformat(r[1]) < cutoff
    )


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
        _crown(db, at, version, reason, detail)
    return True


def _crown(
    db: sqlite3.Connection, at: datetime, version: int, reason: str, detail: Mapping[str, Any]
) -> None:
    """``version`` the champion from ``at``, in the caller's transaction."""
    db.execute(
        "INSERT INTO champions (at, policy_id, reason, detail) VALUES (?, ?, ?, ?)",
        (_iso(at), version, reason, _dumps(dict(detail))),
    )


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


def status_of(
    version: int, champion: int | None, once: Collection[int], dropped: bool, admitted: bool
) -> str:
    """A policy's status: ``champion`` now, ``former`` once, ``dropped`` (#400; #473: a cohort's
    member not crowned at its review, or a policy waiting that a cohort did not take),
    ``challenger`` (in the cohort not judged yet), else ``waiting`` (kept, waiting for the next
    cohort)."""
    if version == champion:
        return CHAMPION
    if version in once:
        return FORMER
    if dropped:
        return DROPPED
    return CHALLENGER if admitted else WAITING


def read_policies(path: Path, limit: int, mode: str | None = None) -> dict[str, Any]:
    """The last ``limit`` policy versions, newest first, each with its status (``status_of``) and
    its record (#400, ``promotion.records``: ``mode``'s, by default the mode the trader last started
    in, #405), every change of the champion, newest first, and every challenger cohort with its
    verdict, newest first (#473, ``cohort.read_cohorts``): ``GET /v1/trader/policies``. Read-only;
    none without the store or before the first policy."""
    from krx_quant.trader import cohort, promotion  # they import this module

    if not path.exists():
        return {"champion": None, "policies": [], "champions": [], "cohorts": []}
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        if not _has_policies(db):
            return {"champion": None, "policies": [], "champions": [], "cohorts": []}
        changes = [dict(r) for r in db.execute("SELECT * FROM champions ORDER BY id DESC")]
        rows = db.execute("SELECT * FROM policies ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        records = promotion.records(db, mode, [r["id"] for r in rows])
        cohorts = cohort.read_cohorts(db)
    finally:
        db.close()
    now = changes[0]["policy_id"] if changes else None
    once = {c["policy_id"] for c in changes}
    policies = []
    for row in rows:
        keys = row.keys()
        dropped = "dropped_at" in keys and row["dropped_at"] is not None
        admitted = row["admitted_at"] if "admitted_at" in keys else None
        policies.append(
            {
                "version": row["id"],
                "status": status_of(row["id"], now, once, dropped, admitted is not None),
                "created_at": row["created_at"],
                "reason": row["reason"],
                "cells": json.loads(row["cells"]),
                "left_out": json.loads(row["left_out"]),
                "fit": json.loads(row["fit"]),
                "admitted_at": admitted,
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
        "cohorts": cohorts,
    }


def _has_table(db: sqlite3.Connection, name: str) -> bool:
    found = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone()
    return found is not None


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
    admitted = row["admitted_at"] if "admitted_at" in row.keys() else None
    return Policy(
        version=row["id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        reason=row["reason"],
        cells=tuple(PolicyCell.from_json(c) for c in cells),
        left_out=tuple(dict(x) for x in left_out),
        fit=fit,
        admitted_at=None if admitted is None else datetime.fromisoformat(admitted),
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
