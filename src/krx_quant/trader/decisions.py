"""The trader's decisions of a day (#584): ``GET /v1/trader/decisions``.

Each prediction of a picked cell a book read is a decision (``store.py``'s ``decisions``): it bought,
or why not (``paper.py``'s reasons). Each is given with when it was decided, its prediction (the cell,
the bar close, when the engine displayed it), the rise's probability, the cell's θ and p* in force
then (``_Pick``: of the book's pick it was decided under, ``selections.picks``; a pick without a θ,
the trader's ``Rules.threshold`` of its start then, ``runs``), whether it bought (``trade_id``), and
once the engine has answered its prediction the answer's class (``replay.day_answers``: the rise, the
middle or the fall) and whether the rise came (``right``: what the night report counts the calls it
traded right by, #560).

**Each book apart**, never mixed (the night report's decision sources, #560, ``report._sources``),
told from a decision's mode, its policy and its pick:

- ``champion``: the champion's book, the trader's own, of the mode the trader ran in (``paper``,
  ``mock``, ``live``): under the champion's policy, none before the first;
- ``practice`` (#427): the mock book's decisions on its practice pick (``selections.practice``),
  of no policy (``policy`` null, though the store keeps the champion's on them);
- ``shadow`` (#405): the champion policy's paper book beside a KIS book: ``paper`` decisions under the
  champion (or none before the first) while the trader ran in ``mock`` or ``live`` (``runs``, as
  ``promotion.mode_at`` reads them);
- ``challenger`` (#400): a challenger policy's paper book, each policy its own: ``paper`` decisions
  under a policy that was not the champion when the pick they were decided under was made
  (``champions``). By the pick's time, not the decision's: a book reads the champion at its picks
  alone, so the champion's book's decisions before the morning's pick are under the champion of the
  night before, and a challenger retired at a pick (promoted in the night) skips under its own.

A book is also of one mode and one policy: one of a day it ran in two (a start in another mode) is two.
The practice's paper shadow (#427, until #548's ``practice_shadow``) is none of these, and is not given.

**A page at a time**: a book's decisions newest first, those decided before ``before`` (if given), at
most ``limit``, and any more decided at the moment the page ends on, so that the next page (before that
moment) misses none; with the book's totals of the whole day, by reason (``Totals``).

**No price**: no reference price, tick, target, stop, quote or fill. A pick's figures are read for θ
and p* alone.

Read-only: the trader's store as ``db.reading`` opens it, the engine's database as
``cells.open_engine_db`` does (None: no answers). A store from before a column has none of what it tells
apart: no policy before #400 (every decision the champion's book's), no pick before #395 (no θ or p*),
no practice before #427.
"""

from __future__ import annotations

import bisect
import itertools
import json
import logging
import sqlite3
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from krx_quant.trader import KIS_MODES, MODES
from krx_quant.trader.cells import Cell, open_engine_db
from krx_quant.trader.db import iso, reading
from krx_quant.trader.replay import UP, day_answers

log = logging.getLogger(__name__)

# The books, in the order an answer gives them (module doc).
CHAMPION, PRACTICE, SHADOW, CHALLENGER = "champion", "practice", "shadow", "challenger"
BOOKS = (CHAMPION, PRACTICE, SHADOW, CHALLENGER)

_BUY = "buy"
# A skip kept without its reason (the trader always gives one), as the night report counts it.
_NO_REASON = "?"


@dataclass(frozen=True)
class _Pick:
    """A cell's figures in a book's pick that decide its rises: its θ (None: the trader's) and p*."""

    threshold: float | None
    p_star: float | None


@dataclass(frozen=True)
class _Book:
    name: str
    mode: str
    policy: int | None

    @property
    def order(self) -> tuple[int, int, int]:
        """The champion's book first, then the practice, the shadow and the challengers by version;
        a book of two modes paper first."""
        return BOOKS.index(self.name), self.policy or 0, MODES.index(self.mode) if self.mode in MODES else 0


def read_decisions(
    path: Path,
    engine: Path | None,
    day: date,
    book: str | None = None,
    before: datetime | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Each book's decisions of ``day`` (KST, by when they were decided) in the trader's store at
    ``path``, ``book``'s alone if given (one of ``BOOKS``): a page of them each (module doc) with its
    totals of the day, its predictions answered from the engine's database at ``engine``; none without
    the store or before its first decision."""
    with reading(path) as db:
        if db is None or not _has(db, "decisions"):
            return []
        rows = _day_rows(db, day)
        picks = _picks(db, {r["selection_id"] for r in rows if r["selection_id"] is not None})
        mode_at, threshold_at = _runs(db)
        champion_at = _champions(db)
    by_book: dict[_Book, list[dict[str, Any]]] = {}
    for r in rows:
        found = _book_of(r, mode_at, champion_at)
        if found is not None and (book is None or found.name == book):
            by_book.setdefault(found, []).append(r)
    pages = {b: _page(found, before, limit) for b, found in by_book.items()}
    answers = _answers(engine, [r for page, _ in pages.values() for r in page])
    out = []
    for b in sorted(by_book, key=lambda b: b.order):
        page, more = pages[b]
        out.append(
            {
                "book": b.name,
                "mode": b.mode,
                "policy": b.policy,
                "totals": _totals(by_book[b]),
                "decisions": [_decision_json(r, picks, threshold_at, answers) for r in page],
                "more": more,
            }
        )
    return out


def _has(db: sqlite3.Connection, table: str) -> bool:
    found = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,))
    return found.fetchone() is not None


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}


def _day_rows(db: sqlite3.Connection, day: date) -> list[dict[str, Any]]:
    """The decisions of ``day`` (by when they were decided, as the night report counts them), newest
    first, each with its policy, its pick and the pick's time, and whether it is a practice pick's."""
    columns = _columns(db, "decisions")
    policy = "d.policy_id" if "policy_id" in columns else "NULL"
    selection, picked_at, practice, joined = "NULL", "NULL", "0", ""
    if "selection_id" in columns and _has(db, "selections"):
        selection, picked_at = "d.selection_id", "s.at"
        joined = " LEFT JOIN selections s ON s.id = d.selection_id"
        if "practice" in _columns(db, "selections"):
            practice = "coalesce(s.practice, 0)"
    return [
        dict(r)
        for r in db.execute(
            "SELECT d.id, d.mode, d.symbol, d.kind, d.predictor, d.prediction_id, d.bar_close,"
            " d.displayed_at, d.decided_at, d.probability, d.action, d.reason, d.trade_id,"
            f" {policy} AS policy_id, {selection} AS selection_id, {picked_at} AS picked_at,"
            f" {practice} AS practice FROM decisions d{joined}"
            " WHERE substr(d.decided_at, 1, 10) = ? ORDER BY d.decided_at DESC, d.id DESC",
            (day.isoformat(),),
        )
    ]


def _picks(db: sqlite3.Connection, selections: set[int]) -> dict[int, dict[tuple[str, str], _Pick]]:
    """Each of ``selections``' cells picked, by (kind, predictor), with its θ and p* alone; a pick whose
    cells cannot be read has none (logged)."""
    out: dict[int, dict[tuple[str, str], _Pick]] = {}
    if not selections:
        return out
    marks = ", ".join("?" * len(selections))
    for selection, text in db.execute(
        f"SELECT id, picks FROM selections WHERE id IN ({marks})", sorted(selections)
    ):
        try:
            out[int(selection)] = {
                (str(p["kind"]), str(p["predictor"])): _Pick(
                    _number(p.get("threshold")), _number(p.get("p_star"))
                )
                for p in json.loads(text)
            }
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            log.warning("trader decisions: cannot read pick %s's cells (%s)", selection, e)
            out[int(selection)] = {}
    return out


def _number(value: Any) -> float | None:
    return None if value is None else float(value)


def _runs(db: sqlite3.Connection) -> tuple[Callable[[str], str | None], Callable[[str], float | None]]:
    """The mode the trader last started in at or before a time (as ``promotion.mode_at``; None before
    its first start), and that start's θ (``Rules.threshold``, what a pick without one buys at)."""
    starts: list[tuple[str, str, float | None]] = []
    if _has(db, "runs"):
        for at, mode, detail in db.execute("SELECT at, mode, detail FROM runs ORDER BY at, id"):
            threshold = None
            try:
                threshold = _number(json.loads(detail).get("threshold"))
            except (ValueError, TypeError, AttributeError):
                log.warning("trader decisions: cannot read the start at %s", at)
            starts.append((str(at), str(mode), threshold))
    times = [at for at, _, _ in starts]

    def started(moment: str) -> tuple[str, str, float | None] | None:
        n = bisect.bisect_right(times, moment)
        return starts[n - 1] if n else None

    def mode_at(moment: str) -> str | None:
        found = started(moment)
        return None if found is None else found[1]

    def threshold_at(moment: str) -> float | None:
        found = started(moment)
        return None if found is None else found[2]

    return mode_at, threshold_at


def _champions(db: sqlite3.Connection) -> Callable[[str], int | None]:
    """The champion just before a time: the latest change of it then (as the night report reads it);
    None before the first."""
    changes: list[tuple[str, int]] = []
    if _has(db, "champions"):
        changes = [
            (str(at), int(v)) for at, v in db.execute("SELECT at, policy_id FROM champions ORDER BY at, id")
        ]
    times = [at for at, _ in changes]

    def champion_at(moment: str) -> int | None:
        n = bisect.bisect_left(times, moment)
        return changes[n - 1][1] if n else None

    return champion_at


def _book_of(
    row: Mapping[str, Any], mode_at: Callable[[str], str | None], champion_at: Callable[[str], int | None]
) -> _Book | None:
    """The book a decision is of (module doc); None for the practice's paper shadow."""
    mode, policy = str(row["mode"]), row["policy_id"]
    if mode in KIS_MODES:
        return _Book(PRACTICE, mode, None) if row["practice"] else _Book(CHAMPION, mode, policy)
    if row["practice"]:
        return None  # the practice's paper shadow (#427, none since #548)
    if policy is not None and policy != champion_at(row["picked_at"] or row["decided_at"]):
        return _Book(CHALLENGER, mode, policy)
    return _Book(SHADOW if mode_at(row["decided_at"]) in KIS_MODES else CHAMPION, mode, policy)


def _page(
    rows: Sequence[dict[str, Any]], before: datetime | None, limit: int
) -> tuple[list[dict[str, Any]], bool]:
    """A book's decisions (newest first) before ``before``, at most ``limit`` and the rest of the moment
    the page ends on; and whether earlier ones are left for the next page."""
    kept = list(rows) if before is None else [r for r in rows if r["decided_at"] < iso(before)]
    page = kept[:limit]
    if page:
        last = page[-1]["decided_at"]
        page += itertools.takewhile(lambda r: r["decided_at"] == last, kept[limit:])
    return page, len(kept) > len(page)


def _totals(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """A book's decisions of the day: how many, bought (filled), a buy waiting for its quote, the buys
    that did not fill and the skips by their reason, the commonest first."""
    bought = waiting = 0
    unfilled: Counter[str] = Counter()
    skipped: Counter[str] = Counter()
    for r in rows:
        if r["action"] != _BUY:
            skipped[r["reason"] or _NO_REASON] += 1
        elif r["trade_id"] is not None:
            bought += 1
        elif r["reason"] is None:
            waiting += 1
        else:
            unfilled[r["reason"]] += 1
    return {
        "decisions": len(rows),
        "bought": bought,
        "waiting": waiting,
        "unfilled": _commonest_first(unfilled),
        "skipped": _commonest_first(skipped),
    }


def _commonest_first(counts: Counter[str]) -> dict[str, int]:
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _answers(engine: Path | None, rows: Sequence[Mapping[str, Any]]) -> dict[tuple[str, str, int], str]:
    """The answers' classes of ``rows``' predictions by (kind, predictor, prediction): the engine's own
    (``replay.day_answers``), read-only; none when its database is not there or cannot be read
    (logged)."""
    wanted = {(Cell(r["kind"], r["predictor"]), str(r["bar_close"])[:10]) for r in rows}
    if not wanted or engine is None or not engine.exists():
        return {}
    try:
        db = open_engine_db(engine)
        try:
            return {
                (cell.kind, cell.predictor, prediction): answer.outcome
                for cell, day in wanted
                for prediction, answer in day_answers(db, cell, date.fromisoformat(day)).items()
            }
        finally:
            db.close()
    except (sqlite3.Error, ValueError) as e:  # ValueError: a kind without a rise (``cells.traded_kind``)
        log.warning("trader decisions: cannot read the calls' answers (%s)", e)
        return {}


def _decision_json(
    row: Mapping[str, Any],
    picks: Mapping[int, Mapping[tuple[str, str], _Pick]],
    threshold_at: Callable[[str], float | None],
    answers: Mapping[tuple[str, str, int], str],
) -> dict[str, Any]:
    picked = picks.get(row["selection_id"], {}).get((row["kind"], row["predictor"]))
    threshold = None
    if picked is not None:
        threshold = picked.threshold if picked.threshold is not None else threshold_at(row["decided_at"])
    answer = answers.get((row["kind"], row["predictor"], int(row["prediction_id"])))
    return {
        "id": row["id"],
        "decided_at": row["decided_at"],
        "symbol": row["symbol"],
        "kind": row["kind"],
        "predictor": row["predictor"],
        "prediction_id": row["prediction_id"],
        "bar_close": row["bar_close"],
        "displayed_at": row["displayed_at"],
        "probability": row["probability"],  # the rise's; null without a forecast
        "threshold": threshold,  # θ; null for a cell not in the pick
        "p_star": None if picked is None else picked.p_star,
        "action": row["action"],
        "reason": row["reason"],
        "trade_id": row["trade_id"],
        "answer": answer,
        "right": None if answer is None else answer == UP,
    }
