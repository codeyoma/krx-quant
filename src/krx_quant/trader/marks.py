"""The trader's buys and sells as the chart marks them (#398): ``GET
/v1/symbols/{symbol}/trader-marks``.

A trade gives two marks: its entry's buy and, once it is out, its exit's sell, each at its fill's
time and price (``trades.entry_at``, ``entry_price``, ``exit_at``, ``exit_price``) with the
trade's quantity, mode and cell. The sell carries why the trade ended and its net P&L after costs;
the buy carries neither. Each carries its trade's policy version (``policy``, #399; null for a
trade entered before the first champion). The chart shows the champion's trades: those its book
made (``store.CHAMPION_TRADE``, #400: ``trades.champion``; before the first champion too); a
challenger's paper trades (#400) are not marked. The mock book's practice trades (#427) are the
trader's own, marked with ``practice`` true (of no policy, ``policy`` null). A store the trader has not
opened since #399 has no policy yet: every trade, ``policy`` null; one not opened since #400, no
challenger's trade; one not opened since #427, no practice trade.

Read from the trader's store (``store.py``) read-only, newest first, a page at a time: the marks
before a time, at most ``limit`` of them, but never a page that leaves out some marks of the
moment it ends on, so that the next page (before that moment) misses none.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from krx_quant.trader.store import _reading, champion_of, iso, practice_of

# A trade's buy (its entry) and its sell (its exit, once out), as one table of marks, of the
# champion's trades (#399): its policy version, and which trades.
_MARKS = """
SELECT id AS trade_id, 'buy' AS side, entry_at AS at, entry_price AS price, quantity, mode, kind,
       predictor, NULL AS exit_reason, NULL AS net, {policy} AS policy, {practice} AS practice
  FROM trades t WHERE symbol = :symbol AND {champions}
UNION ALL
SELECT id, 'sell', exit_at, exit_price, quantity, mode, kind, predictor, exit_reason, net, {policy},
       {practice}
  FROM trades t WHERE symbol = :symbol AND exit_at IS NOT NULL AND {champions}
"""
_NEWEST_FIRST = "ORDER BY at DESC, trade_id DESC, side DESC"


def read_marks(path: Path, symbol: str, before: datetime | None, limit: int) -> list[dict[str, Any]]:
    """The last ``limit`` marks of ``symbol`` before ``before`` (if given), newest first, and any
    more at the moment of the last of them; none without the store."""
    with _reading(path) as db:
        if db is None:
            return []
        marks = _marks(db)
        params: dict[str, Any] = {"symbol": symbol, "limit": limit}
        where = ""
        if before is not None:
            where = "WHERE at < :before"
            params["before"] = iso(before)
        rows = db.execute(f"SELECT * FROM ({marks}) {where} {_NEWEST_FIRST} LIMIT :limit", params).fetchall()
        if len(rows) == limit:
            rows += _rest_of_moment(db, marks, symbol, rows)
        return [_mark_json(dict(r)) for r in rows]


def _marks(db: sqlite3.Connection) -> str:
    """``_MARKS`` for this store: of the champion's trades (``store.champion_of``), their policy
    null in a store from before the policies (#399)."""
    columns = {r[1] for r in db.execute("PRAGMA table_info(trades)")}
    policy = "t.policy_id" if "policy_id" in columns else "NULL"
    # The loss limits count the same; a practice trade is the trader's own (#427).
    return _MARKS.format(policy=policy, champions=champion_of(db), practice=practice_of(db))


def _rest_of_moment(
    db: sqlite3.Connection, marks: str, symbol: str, page: list[sqlite3.Row]
) -> list[sqlite3.Row]:
    """The marks at the moment ``page`` ends on that it left out (one moment's marks were more than
    the page had room for): the next page, before that moment, would never have them."""
    last = page[-1]["at"]
    kept = {(r["trade_id"], r["side"]) for r in page}
    same = db.execute(
        f"SELECT * FROM ({marks}) WHERE at = :at {_NEWEST_FIRST}", {"symbol": symbol, "at": last}
    ).fetchall()
    return [r for r in same if (r["trade_id"], r["side"]) not in kept]


def _mark_json(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "trade_id": row["trade_id"],
        "side": row["side"],
        "at": row["at"],
        "price": row["price"],
        "quantity": row["quantity"],
        "mode": row["mode"],
        "policy": row["policy"],  # its trade's policy version (#399); null before the first champion
        "kind": row["kind"],
        "predictor": row["predictor"],
        "exit_reason": row["exit_reason"],
        "net": row["net"],
        "practice": bool(row["practice"]),  # the mock book's practice trade (#427)
    }
