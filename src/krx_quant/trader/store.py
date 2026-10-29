"""The trader's store (#394): ``state/trader.sqlite3``, written by the trader alone.

- ``runs``: each start of the trader, with its mode and settings;
- ``selections``: each pick of the cells from the cost map (#395): when, the cost map's time, the
  cells picked in their rank with what they were picked on (p*, the hit rate's lower bound, the
  Kelly fraction), every cell considered with its verdict, and, when none trades, why (no cell
  passes, or the cost map could not be read);
- ``decisions``: each prediction of a picked cell the trader read, and what it did: bought, or
  why not (``paper.py``'s reasons), under which pick;
- ``trades``: each position, from its entry to its exit: the cell and its pick (the predictor's
  role, p*, the hit rate's lower bound, the Kelly f* and the fraction bought), the prediction and
  its probability, the reference price, target, stop and time barrier, the entry and the exit
  (why it ended: take_profit, stop_loss, time), the costs (both commissions and the sell tax) and
  the P&L before and after them, in won;
- ``orders``: each order of a trade (the entry's buy, the take-profit's sell resting at the
  target, the stop's or the time exit's sell), its limit, quantity and status (open, filled,
  cancelled), and the broker's order number (none in paper);
- ``fills``: each order's fills, with the quote a paper fill was made on.

Every row carries its mode (``paper``): paper, KIS mock and live trades are told apart in the
same tables. The Engine API reads it read-only (``read_trades``, ``GET /v1/trader/trades``).
Times are ISO 8601 strings in KST, which sort in time order. The columns #395 added
(``_ADDED``) are added to a store made before it, null in its rows.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from krx_quant.feeds.base import KST

if TYPE_CHECKING:
    from krx_quant.trader.selection import Selection

DB_NAME = "trader.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (       -- each start of the trader
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    mode TEXT NOT NULL,
    detail TEXT NOT NULL               -- JSON: the cell, threshold, capital, commit
);
CREATE TABLE IF NOT EXISTS selections (  -- each pick of the cells from the cost map (#395)
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    mode TEXT NOT NULL,
    as_of TEXT,                        -- the cost map's as_of; null when it could not be read
    picks TEXT NOT NULL,               -- JSON: the cells picked, first ranked first, with p*, low, Kelly
    considered TEXT NOT NULL,          -- JSON: every (kind, role) and its verdict, or why it has none
    reason TEXT,                       -- why no cell trades: no_pass, no_cost_map; null when one does
    detail TEXT                        -- why the cost map could not be read
);
CREATE TABLE IF NOT EXISTS decisions (  -- each prediction of a picked cell read, and what was done
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL,
    symbol TEXT NOT NULL,
    kind TEXT NOT NULL,
    predictor TEXT NOT NULL,
    prediction_id INTEGER NOT NULL,    -- predictions.id (direction) or kind_predictions.id, engine DB
    bar_close TEXT NOT NULL,
    displayed_at TEXT NOT NULL,
    decided_at TEXT NOT NULL,          -- when the trader read it
    probability REAL,                  -- the rise's (up, up_first); null without a forecast
    action TEXT NOT NULL,              -- buy or skip
    reason TEXT,                       -- skip: why not; buy: why it did not fill; null once filled
    trade_id INTEGER REFERENCES trades (id),
    UNIQUE (mode, kind, predictor, prediction_id)
);
CREATE TABLE IF NOT EXISTS trades (     -- a position, from its entry to its exit
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL,
    symbol TEXT NOT NULL,
    kind TEXT NOT NULL,
    predictor TEXT NOT NULL,
    prediction_id INTEGER NOT NULL,
    bar_close TEXT NOT NULL,
    probability REAL NOT NULL,
    threshold REAL NOT NULL,
    anchor REAL NOT NULL,              -- the reference price: the bar close's mid-point
    tick REAL NOT NULL,
    width_ticks INTEGER NOT NULL,      -- w (k for the direction)
    target REAL NOT NULL,
    stop REAL NOT NULL,
    time_barrier TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    entry_at TEXT NOT NULL,
    entry_price REAL NOT NULL,
    quantity INTEGER NOT NULL,
    buy_fee REAL NOT NULL,
    exit_at TEXT,                      -- null while open
    exit_price REAL,
    exit_reason TEXT,                  -- take_profit, stop_loss, time
    sell_fee REAL,
    tax REAL,
    cost REAL,                         -- buy_fee + sell_fee + tax
    gross REAL,                        -- (exit_price - entry_price) * quantity
    net REAL                           -- gross - cost
);
CREATE INDEX IF NOT EXISTS trades_by_entry ON trades (entry_at);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    trade_id INTEGER NOT NULL REFERENCES trades (id),
    mode TEXT NOT NULL,
    side TEXT NOT NULL,                -- buy, sell
    purpose TEXT NOT NULL,             -- entry, take_profit, stop_loss, time
    price REAL NOT NULL,               -- the limit
    quantity INTEGER NOT NULL,
    placed_at TEXT NOT NULL,
    status TEXT NOT NULL,              -- open, filled, cancelled
    closed_at TEXT,
    broker_id TEXT                     -- the broker's order number; null in paper
);
CREATE INDEX IF NOT EXISTS orders_by_trade ON orders (trade_id);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders (id),
    at TEXT NOT NULL,
    price REAL NOT NULL,
    quantity INTEGER NOT NULL,
    quote_at TEXT                      -- the quote a paper fill was made on
);
CREATE INDEX IF NOT EXISTS fills_by_order ON fills (order_id);
"""

# Columns added since the tables were first made (#395): added to a store made before, null in
# its rows.
_ADDED = {
    "decisions": (
        ("selection_id", "INTEGER REFERENCES selections (id)"),  # the pick in force; null: none yet
    ),
    "trades": (
        ("selection_id", "INTEGER REFERENCES selections (id)"),
        ("role", "TEXT"),  # the predictor's on the cost map: consensus, top, baseline
        ("p_star", "REAL"),  # the breakeven hit rate at a market order's round trip
        ("precision_low", "REAL"),  # the cell's hit rate's lower bound
        ("kelly", "REAL"),  # f*, uncut
        ("fraction", "REAL"),  # the share of the capital bought: KELLY_FRACTION × f*, cut to 0-1
    ),
}

BUY, SELL = "buy", "sell"
OPEN, FILLED, CANCELLED = "open", "filled", "cancelled"
ENTRY = "entry"


def iso(at: datetime) -> str:
    return at.astimezone(KST).isoformat()


@dataclass(frozen=True)
class Entry:
    """A trade as it is entered."""

    mode: str
    symbol: str
    kind: str
    predictor: str
    prediction_id: int
    bar_close: datetime
    probability: float
    threshold: float
    anchor: float
    tick: float
    width_ticks: int
    target: float
    stop: float
    time_barrier: datetime
    decided_at: datetime
    entry_at: datetime
    entry_price: float
    quantity: int
    buy_fee: float
    quote_at: datetime
    # The cell's pick (#395); None for a trade entered before it.
    selection_id: int | None = None
    role: str | None = None
    p_star: float | None = None
    low: float | None = None
    kelly: float | None = None
    fraction: float | None = None


@dataclass(frozen=True)
class Exit:
    at: datetime
    price: float
    reason: str
    sell_fee: float
    tax: float
    quote_at: datetime


@dataclass(frozen=True)
class OpenTrade:
    """A trade not exited yet, as kept: what a restarted trader goes on with."""

    id: int
    take_profit_order: int
    entry_price: float
    quantity: int
    buy_fee: float
    target: float
    stop: float
    time_barrier: datetime
    kind: str  # its cell's
    predictor: str
    bar_close: datetime


class TraderStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(f"BEGIN;{_SCHEMA}COMMIT;")
        with self._db:
            for table, columns in _ADDED.items():
                have = {r["name"] for r in self._db.execute(f"PRAGMA table_info({table})")}
                for name, spec in columns:
                    if name not in have:
                        self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")

    def close(self) -> None:
        self._db.close()

    def add_run(self, at: datetime, mode: str, detail: Mapping[str, Any]) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO runs (at, mode, detail) VALUES (?, ?, ?)",
                (iso(at), mode, json.dumps(dict(detail), ensure_ascii=False, sort_keys=True)),
            )

    def add_selection(self, mode: str, at: datetime, selection: Selection) -> int:
        with self._db:
            cursor = self._db.execute(
                "INSERT INTO selections (at, mode, as_of, picks, considered, reason, detail)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    iso(at),
                    mode,
                    selection.as_of,
                    json.dumps([p.as_json() for p in selection.picks], ensure_ascii=False),
                    json.dumps(list(selection.considered), ensure_ascii=False),
                    selection.reason,
                    selection.detail,
                ),
            )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def decided(self, mode: str, kind: str, predictor: str, prediction_id: int) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM decisions WHERE mode = ? AND kind = ? AND predictor = ? AND prediction_id = ?",
            (mode, kind, predictor, prediction_id),
        ).fetchone()
        return row is not None

    def add_decision(
        self,
        mode: str,
        symbol: str,
        kind: str,
        predictor: str,
        prediction_id: int,
        bar_close: datetime,
        displayed_at: datetime,
        decided_at: datetime,
        probability: float | None,
        action: str,
        reason: str | None,
        selection_id: int | None = None,
    ) -> int:
        with self._db:
            cursor = self._db.execute(
                "INSERT INTO decisions (mode, symbol, kind, predictor, prediction_id, bar_close,"
                " displayed_at, decided_at, probability, action, reason, selection_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    mode,
                    symbol,
                    kind,
                    predictor,
                    prediction_id,
                    iso(bar_close),
                    iso(displayed_at),
                    iso(decided_at),
                    probability,
                    action,
                    reason,
                    selection_id,
                ),
            )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def unfilled(self, decision: int, reason: str) -> None:
        """A buy decided that did not fill, and why."""
        with self._db:
            self._db.execute("UPDATE decisions SET reason = ? WHERE id = ?", (reason, decision))

    def drop_unfilled(self, mode: str, reason: str) -> int:
        """Every buy of ``mode`` decided but neither filled nor given up (the trader stopped while it
        waited for its quote), marked with ``reason``; how many."""
        with self._db:
            cursor = self._db.execute(
                "UPDATE decisions SET reason = ? WHERE mode = ? AND action = 'buy' AND reason IS NULL"
                " AND trade_id IS NULL",
                (reason, mode),
            )
        return cursor.rowcount

    def enter(self, decision: int, entry: Entry) -> OpenTrade:
        """A buy filled: its trade, the entry's order and fill, and the take-profit's sell resting at
        the target, in one transaction."""
        at = iso(entry.entry_at)
        with self._db:
            cursor = self._db.execute(
                "INSERT INTO trades (mode, symbol, kind, predictor, prediction_id, bar_close, probability,"
                " threshold, anchor, tick, width_ticks, target, stop, time_barrier, decided_at, entry_at,"
                " entry_price, quantity, buy_fee, selection_id, role, p_star, precision_low, kelly, fraction)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.mode,
                    entry.symbol,
                    entry.kind,
                    entry.predictor,
                    entry.prediction_id,
                    iso(entry.bar_close),
                    entry.probability,
                    entry.threshold,
                    entry.anchor,
                    entry.tick,
                    entry.width_ticks,
                    entry.target,
                    entry.stop,
                    iso(entry.time_barrier),
                    iso(entry.decided_at),
                    at,
                    entry.entry_price,
                    entry.quantity,
                    entry.buy_fee,
                    entry.selection_id,
                    entry.role,
                    entry.p_star,
                    entry.low,
                    entry.kelly,
                    entry.fraction,
                ),
            )
            trade = cursor.lastrowid
            assert trade is not None
            bought = self._order(trade, entry.mode, BUY, ENTRY, entry.entry_price, entry.quantity, at)
            self._fill(bought, at, entry.entry_price, entry.quantity, entry.quote_at)
            resting = self._order(
                trade, entry.mode, SELL, "take_profit", entry.target, entry.quantity, at, OPEN
            )
            self._db.execute("UPDATE decisions SET trade_id = ? WHERE id = ?", (trade, decision))
        return OpenTrade(
            trade,
            resting,
            entry.entry_price,
            entry.quantity,
            entry.buy_fee,
            entry.target,
            entry.stop,
            entry.time_barrier,
            entry.kind,
            entry.predictor,
            entry.bar_close,
        )

    def exit(self, trade: OpenTrade, out: Exit) -> None:
        """A trade's exit: the take-profit's sell filled at the target, or cancelled for the stop's
        or the time exit's sell, filled at the best bid; the trade's exit, costs and P&L."""
        at = iso(out.at)
        with self._db:
            if out.reason == "take_profit":
                sold = trade.take_profit_order
                self._db.execute(
                    "UPDATE orders SET status = ?, closed_at = ? WHERE id = ?", (FILLED, at, sold)
                )
            else:
                self._db.execute(
                    "UPDATE orders SET status = ?, closed_at = ? WHERE id = ?",
                    (CANCELLED, at, trade.take_profit_order),
                )
                sold = self._order(
                    trade.id, self._mode(trade.id), SELL, out.reason, out.price, trade.quantity, at
                )
            self._fill(sold, at, out.price, trade.quantity, out.quote_at)
            gross = (out.price - trade.entry_price) * trade.quantity
            cost = trade.buy_fee + out.sell_fee + out.tax
            self._db.execute(
                "UPDATE trades SET exit_at = ?, exit_price = ?, exit_reason = ?, sell_fee = ?, tax = ?,"
                " cost = ?, gross = ?, net = ? WHERE id = ?",
                (at, out.price, out.reason, out.sell_fee, out.tax, cost, gross, gross - cost, trade.id),
            )

    def open_trade(self, mode: str) -> OpenTrade | None:
        """``mode``'s trade not exited yet, if any (one position at a time)."""
        row = self._db.execute(
            "SELECT t.*, o.id AS resting FROM trades t JOIN orders o ON o.trade_id = t.id"
            " AND o.purpose = 'take_profit' AND o.status = 'open'"
            " WHERE t.mode = ? AND t.exit_at IS NULL ORDER BY t.id DESC LIMIT 1",
            (mode,),
        ).fetchone()
        if row is None:
            return None
        return OpenTrade(
            row["id"],
            row["resting"],
            row["entry_price"],
            row["quantity"],
            row["buy_fee"],
            row["target"],
            row["stop"],
            datetime.fromisoformat(row["time_barrier"]),
            row["kind"],
            row["predictor"],
            datetime.fromisoformat(row["bar_close"]),
        )

    def _mode(self, trade: int) -> str:
        mode: str = self._db.execute("SELECT mode FROM trades WHERE id = ?", (trade,)).fetchone()[0]
        return mode

    def _order(
        self,
        trade: int,
        mode: str,
        side: str,
        purpose: str,
        price: float,
        quantity: int,
        at: str,
        status: str = FILLED,
    ) -> int:
        cursor = self._db.execute(
            "INSERT INTO orders (trade_id, mode, side, purpose, price, quantity, placed_at, status,"
            " closed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (trade, mode, side, purpose, price, quantity, at, status, at if status == FILLED else None),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def _fill(self, order: int, at: str, price: float, quantity: int, quote_at: datetime) -> None:
        self._db.execute(
            "INSERT INTO fills (order_id, at, price, quantity, quote_at) VALUES (?, ?, ?, ?, ?)",
            (order, at, price, quantity, iso(quote_at)),
        )


@contextmanager
def _reading(path: Path) -> Iterator[sqlite3.Connection | None]:
    """The trader's store, read-only; None if there is none."""
    if not path.exists():
        yield None
        return
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


def read_trades(path: Path, before: datetime | None, limit: int) -> list[dict[str, Any]]:
    """The last ``limit`` trades entered before ``before`` (if given), newest first, each with its
    orders and their fills: ``GET /v1/trader/trades``."""
    with _reading(path) as db:
        if db is None:
            return []
        where, params = ("WHERE entry_at < ?", [iso(before)]) if before is not None else ("", [])
        rows = db.execute(
            f"SELECT * FROM trades {where} ORDER BY entry_at DESC, id DESC LIMIT ?", (*params, limit)
        ).fetchall()
        ids = [r["id"] for r in rows]
        orders: dict[int, list[dict[str, Any]]] = {}
        fills: dict[int, list[dict[str, Any]]] = {}
        if ids:
            marks = ",".join("?" * len(ids))
            for o in db.execute(f"SELECT * FROM orders WHERE trade_id IN ({marks}) ORDER BY id", ids):
                orders.setdefault(o["trade_id"], []).append(dict(o))
            order_ids = [o["id"] for os in orders.values() for o in os]
            for start in range(0, len(order_ids), 500):  # under SQLite's limit on parameters
                chunk = order_ids[start : start + 500]
                for f in db.execute(
                    f"SELECT * FROM fills WHERE order_id IN ({','.join('?' * len(chunk))}) ORDER BY id", chunk
                ):
                    fills.setdefault(f["order_id"], []).append(dict(f))
        return [_trade_json(dict(r), orders.get(r["id"], []), fills) for r in rows]


def _trade_json(
    row: dict[str, Any], orders: list[dict[str, Any]], fills: Mapping[int, list[dict[str, Any]]]
) -> dict[str, Any]:
    closed = row["exit_at"] is not None
    return {
        "id": row["id"],
        "mode": row["mode"],
        "symbol": row["symbol"],
        "kind": row["kind"],
        "predictor": row["predictor"],
        "prediction_id": row["prediction_id"],
        "bar_close": row["bar_close"],
        "role": row["role"],
        "probability": row["probability"],
        "threshold": row["threshold"],
        "p_star": row["p_star"],
        "precision_low": row["precision_low"],
        "kelly": row["kelly"],
        "fraction": row["fraction"],
        "anchor": row["anchor"],
        "tick": row["tick"],
        "width_ticks": row["width_ticks"],
        "target": row["target"],
        "stop": row["stop"],
        "time_barrier": row["time_barrier"],
        "decided_at": row["decided_at"],
        "entry": {"at": row["entry_at"], "price": row["entry_price"], "quantity": row["quantity"]},
        "exit": {"at": row["exit_at"], "price": row["exit_price"], "reason": row["exit_reason"]}
        if closed
        else None,
        "buy_fee": row["buy_fee"],
        "sell_fee": row["sell_fee"],
        "tax": row["tax"],
        "cost": row["cost"],
        "gross": row["gross"],
        "net": row["net"],
        "orders": [
            {
                "id": o["id"],
                "side": o["side"],
                "purpose": o["purpose"],
                "price": o["price"],
                "quantity": o["quantity"],
                "placed_at": o["placed_at"],
                "status": o["status"],
                "closed_at": o["closed_at"],
                "broker_id": o["broker_id"],
                "fills": [
                    {"at": f["at"], "price": f["price"], "quantity": f["quantity"], "quote_at": f["quote_at"]}
                    for f in fills.get(o["id"], [])
                ],
            }
            for o in orders
        ],
    }
