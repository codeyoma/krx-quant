"""The trader's store (#394): ``state/trader.sqlite3``, written by the trader, and its policy tables
by the nightly re-fit (#399, from the engine's container).

- ``runs``: each start of the trader, with its mode and settings;
- ``selections``: each pick of the cells from the cost map (#395): when, the cost map's time, the
  cells picked in their rank with what they were picked on (p*, the hit rate's lower bound, the
  Kelly fraction), every cell considered with its verdict, and, when none trades, why (no cell
  passes, or the cost map could not be read);
- ``decisions``: each prediction of a picked cell a book read (#400: the champion's and each
  challenger's), and what it did: bought, or why not (``paper.py``'s reasons), under which pick
  and policy; unique by (mode, policy, kind, predictor, prediction), a store from before made
  again so (``_key_decisions``);
- ``trades``: each position, from its entry to its exit: the cell and its pick (the predictor's
  role, p*, the hit rate's lower bound, the Kelly f* and the fraction bought), the prediction and
  its probability, the reference price, target, stop and time barrier, the entry and the exit
  (why it ended: take_profit, stop_loss, time), the costs (both commissions and the sell tax) and
  the P&L before and after them, in won;
- ``orders``: each order of a trade (the entry's buy, the take-profit's sell resting at the
  target, the stop's or the time exit's sell), its limit, quantity and status (open, filled,
  cancelled), and the broker's order number (none in paper);
- ``fills``: each order's fills, with the quote a paper fill was made on;
- ``policies`` and ``champions`` (#399, ``policy.py``): each policy version and each change of the
  champion, written by the nightly re-fit (``refit.py``) and its promotion (#400,
  ``promotion.py``); a pick and a trade carry the policy version they were made under
  (``policy_id``; null before the first champion), a trade whether the champion's book made it
  (``champion``, #400: ``CHAMPION_TRADE``);
- ``policy_days`` (#400): each trading day a policy ran in the trader (its paper record's days),
  whether it was the champion and, if so, stopped in the session;
- ``controls`` (#396): each pause, resume and re-arm asked for, by whom (``api``: the screen's
  pause through the Engine API; ``cli``: ``krx-quant trade resume|rearm`` on the server), and when
  the trader read it (null until it has: it reads them every ``CONTROL_SECONDS``);
- ``halts`` (#396): each loss limit reached: when, the day, the limit and the P&L that reached it;
  a cumulative one's re-arm (``lifted_at``);
- ``snapshot`` (#396): the trader's state now, one row it rewrites (``GET /v1/trader``);
- ``days`` (#396): each trading day's ledger by mode: the net P&L realized, the trades, the day's
  and the cumulative P&L at their lowest, the limits, the status and the limit reached.

Every row carries its mode (``paper``): paper, KIS mock and live trades are told apart in the
same tables (a pause and a resume hold whatever the mode). The trader writes it, and the nightly
re-fit its policies; the Engine API reads it read-only (``read_trades``, ``read_trader``,
``read_days``, ``policy.read_policies``) and writes a pause into it (``request_pause``), and
``krx-quant trade resume|rearm`` a resume or a re-arm: nothing else. Times are ISO 8601 strings in
KST, which sort in time order. The columns #395, #399 and #400 added (``_ADDED``) are added to a
store made before them, null in its rows.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from krx_quant.feeds.base import KST
from krx_quant.trader import policy, promotion

if TYPE_CHECKING:
    from krx_quant.trader.selection import Selection

DB_NAME = "trader.sqlite3"

# A book's decisions (#400: the champion's and each challenger's), unique by the policy too
# (``_key_decisions``): one prediction is read by every book that trades its cell.
_DECISIONS = """
CREATE TABLE IF NOT EXISTS {name} (  -- each prediction of a picked cell a book read, and what was done
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
    selection_id INTEGER REFERENCES selections (id),  -- the pick in force; null: none yet (#395)
    policy_id INTEGER REFERENCES policies (id)         -- the book's policy (#400); null: before the first
);
"""
_DECISIONS_KEY = (
    "CREATE UNIQUE INDEX IF NOT EXISTS decisions_by_prediction"
    " ON decisions (mode, coalesce(policy_id, 0), kind, predictor, prediction_id)"
)

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
{decisions}
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
CREATE TABLE IF NOT EXISTS controls (   -- each pause, resume and re-arm asked for (#396)
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,                  -- when it was asked for
    action TEXT NOT NULL,              -- pause, resume, rearm
    source TEXT NOT NULL,              -- api (the screen's pause), cli (krx-quant trade resume|rearm)
    sell_now INTEGER,                  -- a pause's: 1 sells the open position at once, 0 lets it run
    mode TEXT,                         -- a re-arm's: the mode whose cumulative limit it re-arms
    applied_at TEXT                    -- when the trader read it; null until it has
);
CREATE TABLE IF NOT EXISTS halts (      -- each loss limit reached (#396)
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    mode TEXT NOT NULL,
    day TEXT NOT NULL,                 -- the KST date it was reached on
    kind TEXT NOT NULL,                -- daily_limit, cumulative_limit
    pnl REAL NOT NULL,                 -- the P&L that reached it: realized + open, won
    realized REAL NOT NULL,
    unrealized REAL NOT NULL,          -- the open position's at the best bid, after its costs
    limit_won REAL NOT NULL,           -- the limit then, won
    lifted_at TEXT,                    -- a cumulative one's re-arm; a daily one ends with its day
    rearm_id INTEGER REFERENCES controls (id)
);
CREATE TABLE IF NOT EXISTS snapshot (   -- the trader's state now (#396): GET /v1/trader
    id INTEGER PRIMARY KEY CHECK (id = 1),
    at TEXT NOT NULL,
    body TEXT NOT NULL                 -- JSON
);
CREATE TABLE IF NOT EXISTS policy_days (  -- each trading day a policy ran in the trader (#400)
    mode TEXT NOT NULL,
    policy_id INTEGER NOT NULL REFERENCES policies (id),
    day TEXT NOT NULL,
    champion INTEGER NOT NULL,         -- 1: it was the champion (its trades the trader's); 0: a challenger
    stopped INTEGER NOT NULL,          -- 1: as the champion, paused or stopped by a limit in the session
    updated_at TEXT NOT NULL,
    PRIMARY KEY (mode, policy_id, day)
);
CREATE TABLE IF NOT EXISTS days (       -- each trading day's ledger (#396): GET /v1/trader/days
    mode TEXT NOT NULL,
    day TEXT NOT NULL,
    realized REAL NOT NULL,            -- the net P&L of the trades exited that day, won
    trades INTEGER NOT NULL,           -- the trades exited that day
    worst REAL NOT NULL,               -- the day's lowest P&L, realized + open: at most 0
    cumulative REAL NOT NULL,          -- the cumulative P&L at the day's last update
    cumulative_worst REAL NOT NULL,    -- its lowest that day: at most 0
    daily_limit REAL NOT NULL,         -- the limits that day, won
    cumulative_limit REAL NOT NULL,
    status TEXT NOT NULL,              -- the trader's at the day's last update
    halt TEXT,                         -- the limit reached that day, if any
    updated_at TEXT NOT NULL,
    PRIMARY KEY (mode, day)
);
"""

# Columns added since the tables were first made (#395, #399): added to a store made before, null
# in its rows. ``decisions`` takes its policy as it is made again (#400, ``_key_decisions``).
_ADDED = {
    "selections": (
        ("policy_id", "INTEGER REFERENCES policies (id)"),  # the policy applied (#399); null: none yet
    ),
    "decisions": (
        ("selection_id", "INTEGER REFERENCES selections (id)"),  # the pick in force; null: none yet
    ),
    "trades": (
        ("selection_id", "INTEGER REFERENCES selections (id)"),
        ("role", "TEXT"),  # the predictor's on the cost map: consensus, top, baseline
        ("p_star", "REAL"),  # the breakeven hit rate at a market order's round trip
        ("precision_low", "REAL"),  # the cell's hit rate's lower bound
        ("kelly", "REAL"),  # f*, uncut
        ("fraction", "REAL"),  # the share of the capital bought: λ × f*, cut to 0-1
        ("policy_id", "INTEGER REFERENCES policies (id)"),  # the policy it was made under (#399)
        # 1: made by the champion's book, the trader's own; 0: a challenger's paper trade (#400); null:
        # before #400, when the champion's book was the only one.
        ("champion", "INTEGER"),
    ),
}

# A trade ``t`` of the champion (#399, #400): made by the champion's book (``trades.champion``), the
# trader's own trades, before the first champion too; never a challenger's paper trade. The chart
# marks these alone (``marks.py``), the loss limits and the paper cash count these alone (#396), and
# ``GET /v1/trader/trades`` gives these unless asked for a policy's. The champion changes only after
# the market (``promotion.in_market``), so a day's trades are all of one champion.
CHAMPION_TRADE = "coalesce(t.champion, 1) = 1"

BUY, SELL = "buy", "sell"
OPEN, FILLED, CANCELLED = "open", "filled", "cancelled"
ENTRY = "entry"
# What a control asks for (#396), and who asks.
PAUSE, RESUME, REARM = "pause", "resume", "rearm"
API, CLI = "api", "cli"
# The limits a halt is of (#396): also the trader's status while it holds.
DAILY_LIMIT, CUMULATIVE_LIMIT = "daily_limit", "cumulative_limit"
# GET /v1/trader: the trader is running while its snapshot is at most this old (it rewrites it
# every ``limits.HEARTBEAT`` at least).
ALIVE_SECONDS = 15.0
# GET /v1/trader: the last controls shown.
CONTROLS_SHOWN = 20


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
    # The policy version it was made under (#399); None before the first champion.
    policy_id: int | None = None
    # Made by the champion's book (#400), the trader's own; False: a challenger's paper trade.
    champion: bool = True


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
    entry_at: datetime | None = None


@dataclass(frozen=True)
class Control:
    """A pause, resume or re-arm asked for (#396)."""

    id: int
    at: datetime
    action: str
    source: str
    sell_now: bool | None
    mode: str | None
    applied_at: datetime | None

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "at": iso(self.at),
            "action": self.action,
            "source": self.source,
            "sell_now": self.sell_now,
            "mode": self.mode,
            "applied_at": None if self.applied_at is None else iso(self.applied_at),
        }


@dataclass(frozen=True)
class Halt:
    """A loss limit reached (#396)."""

    id: int
    at: datetime
    mode: str
    day: date
    kind: str
    pnl: float
    realized: float
    unrealized: float
    limit: float


class TraderStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(
            f"BEGIN;{_SCHEMA.format(decisions=_DECISIONS.format(name='decisions'))}COMMIT;"
        )
        policy.ensure_schema(self._db)
        with self._db:
            for table, columns in _ADDED.items():
                have = {r["name"] for r in self._db.execute(f"PRAGMA table_info({table})")}
                for name, spec in columns:
                    if name not in have:
                        try:
                            self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")
                        except sqlite3.OperationalError as e:  # added meanwhile by the other writer
                            if "duplicate column" not in str(e):
                                raise
        self._key_decisions()

    def _key_decisions(self) -> None:
        """``decisions`` unique by the book's policy too (#400). A store made before has the table
        unique on (mode, kind, predictor, prediction_id), which a prediction read by the champion's
        book and a challenger's breaks: it is made again with ``policy_id``, each row taking its
        pick's policy (``selections.policy_id``), in one transaction the other writers wait for."""
        db = self._db
        if _keyed_by_policy(db):
            with db:
                db.execute(_DECISIONS_KEY)
            return
        db.execute("BEGIN IMMEDIATE")
        try:
            if not _keyed_by_policy(db):  # not made again meanwhile by another writer
                columns = [
                    r["name"] for r in db.execute("PRAGMA table_info(decisions)") if r["name"] != "policy_id"
                ]
                db.execute("ALTER TABLE decisions RENAME TO decisions_before_400")
                db.execute(_DECISIONS.format(name="decisions"))
                db.execute(
                    f"INSERT INTO decisions ({', '.join(columns)}, policy_id)"
                    f" SELECT {', '.join('d.' + c for c in columns)}, s.policy_id FROM decisions_before_400 d"
                    " LEFT JOIN selections s ON s.id = d.selection_id ORDER BY d.id"
                )
                db.execute("DROP TABLE decisions_before_400")
            db.execute(_DECISIONS_KEY)
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise

    def close(self) -> None:
        self._db.close()

    def champion(self) -> policy.Policy | None:
        """The champion policy now (#399); None before the first. ``policy.PolicyError`` when it
        cannot be read."""
        return policy.champion(self._db)

    def running_challengers(self, day: date) -> list[int]:
        """The challengers that run on ``day`` (#400, ``policy.running_versions``)."""
        return policy.running_versions(self._db, day)

    def read_policy(self, version: int) -> policy.Policy:
        """Policy ``version`` (#400); ``policy.PolicyError`` when it cannot be read."""
        return policy.read(self._db, version)

    def add_run(self, at: datetime, mode: str, detail: Mapping[str, Any]) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO runs (at, mode, detail) VALUES (?, ?, ?)",
                (iso(at), mode, json.dumps(dict(detail), ensure_ascii=False, sort_keys=True)),
            )

    def add_selection(self, mode: str, at: datetime, selection: Selection) -> int:
        with self._db:
            cursor = self._db.execute(
                "INSERT INTO selections (at, mode, as_of, picks, considered, reason, detail, policy_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    iso(at),
                    mode,
                    selection.as_of,
                    json.dumps([p.as_json() for p in selection.picks], ensure_ascii=False),
                    json.dumps(list(selection.considered), ensure_ascii=False),
                    selection.reason,
                    selection.detail,
                    selection.policy,
                ),
            )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def decided(
        self, mode: str, policy_id: int | None, kind: str, predictor: str, prediction_id: int
    ) -> bool:
        """Whether the book of ``policy_id`` (None: before the first champion) decided on the
        prediction already (a restart)."""
        row = self._db.execute(
            "SELECT 1 FROM decisions WHERE mode = ? AND coalesce(policy_id, 0) = ? AND kind = ?"
            " AND predictor = ? AND prediction_id = ?",
            (mode, policy_id or 0, kind, predictor, prediction_id),
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
        policy_id: int | None = None,
    ) -> int:
        with self._db:
            cursor = self._db.execute(
                "INSERT INTO decisions (mode, symbol, kind, predictor, prediction_id, bar_close,"
                " displayed_at, decided_at, probability, action, reason, selection_id, policy_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    policy_id,
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
                " entry_price, quantity, buy_fee, selection_id, role, p_star, precision_low, kelly, fraction,"
                f" policy_id, champion) VALUES ({', '.join('?' * 27)})",
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
                    entry.policy_id,
                    int(entry.champion),
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
            entry.entry_at,
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

    def open_trade(self, mode: str, challenger: int | None = None) -> OpenTrade | None:
        """``mode``'s trade of the champion (``CHAMPION_TRADE``) not exited yet, if any (one position
        at a time); with ``challenger``, that challenger's paper trade not exited (#400)."""
        params: tuple[Any, ...]
        if challenger is None:
            whose, params = CHAMPION_TRADE, (mode,)
        else:
            whose, params = "t.policy_id = ? AND t.champion = 0", (mode, challenger)
        row = self._db.execute(
            "SELECT t.*, o.id AS resting FROM trades t JOIN orders o ON o.trade_id = t.id"
            " AND o.purpose = 'take_profit' AND o.status = 'open'"
            f" WHERE t.mode = ? AND t.exit_at IS NULL AND {whose} ORDER BY t.id DESC LIMIT 1",
            params,
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
            datetime.fromisoformat(row["entry_at"]),
        )

    def open_challengers(self, mode: str) -> list[int]:
        """The challengers with a paper trade of ``mode`` not exited (#400: the trader stopped before
        its exit), whose books go on with it."""
        rows = self._db.execute(
            "SELECT DISTINCT t.policy_id FROM trades t WHERE t.mode = ? AND t.exit_at IS NULL"
            " AND t.policy_id IS NOT NULL AND t.champion = 0 ORDER BY t.policy_id",
            (mode,),
        )
        return [int(r[0]) for r in rows]

    def put_policy_day(
        self, mode: str, policy_id: int, day: date, champion: bool, stopped: bool, at: datetime
    ) -> None:
        """A trading day ``policy_id`` ran (#400): its record's (``promotion.records``). Once the
        champion or stopped that day, it stays so."""
        with self._db:
            self._db.execute(
                "INSERT INTO policy_days (mode, policy_id, day, champion, stopped, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (mode, policy_id, day) DO UPDATE SET"
                " champion = max(champion, excluded.champion), stopped = max(stopped, excluded.stopped),"
                " updated_at = excluded.updated_at",
                (mode, policy_id, day.isoformat(), int(champion), int(stopped), iso(at)),
            )

    def realized(self, mode: str, since: datetime | None) -> tuple[float, int]:
        """The net P&L of ``mode``'s trades exited at or after ``since`` (all if None), and how many:
        the champion's alone (``CHAMPION_TRADE``), the trades the limits and the cash hold (#396); a
        challenger's paper trades (#400) are not."""
        where, params = ("AND exit_at >= ?", [iso(since)]) if since is not None else ("", [])
        row = self._db.execute(
            "SELECT coalesce(sum(net), 0), count(*) FROM trades t"
            f" WHERE mode = ? AND exit_at IS NOT NULL AND {CHAMPION_TRADE} {where}",
            (mode, *params),
        ).fetchone()
        return float(row[0]), int(row[1])

    def first_run(self, mode: str) -> datetime | None:
        """When ``mode`` was first started."""
        row = self._db.execute("SELECT min(at) FROM runs WHERE mode = ?", (mode,)).fetchone()
        return None if row[0] is None else datetime.fromisoformat(row[0])

    # Controls (#396): the engine writes a pause, ``krx-quant trade`` a resume or a re-arm, the
    # trader marks each applied as it reads it.

    def add_control(
        self, at: datetime, action: str, source: str, sell_now: bool | None = None, mode: str | None = None
    ) -> Control:
        with self._db:
            cursor = self._db.execute(
                "INSERT INTO controls (at, action, source, sell_now, mode) VALUES (?, ?, ?, ?, ?)",
                (iso(at), action, source, None if sell_now is None else int(sell_now), mode),
            )
        assert cursor.lastrowid is not None
        control = self.control(cursor.lastrowid)
        assert control is not None
        return control

    def control(self, control: int) -> Control | None:
        row = self._db.execute("SELECT * FROM controls WHERE id = ?", (control,)).fetchone()
        return None if row is None else _control(row)

    def controls(self, applied: bool | None = None) -> list[Control]:
        """The controls asked for, in order: those applied, those not yet, or all."""
        where = {None: "", True: "WHERE applied_at IS NOT NULL", False: "WHERE applied_at IS NULL"}[applied]
        return [_control(r) for r in self._db.execute(f"SELECT * FROM controls {where} ORDER BY id")]

    def applied(self, control: int, at: datetime) -> None:
        with self._db:
            self._db.execute("UPDATE controls SET applied_at = ? WHERE id = ?", (iso(at), control))

    # Halts (#396).

    def add_halt(
        self, at: datetime, mode: str, kind: str, pnl: float, realized: float, unrealized: float, limit: float
    ) -> Halt:
        day = at.astimezone(KST).date()
        with self._db:
            cursor = self._db.execute(
                "INSERT INTO halts (at, mode, day, kind, pnl, realized, unrealized, limit_won)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (iso(at), mode, day.isoformat(), kind, pnl, realized, unrealized, limit),
            )
        assert cursor.lastrowid is not None
        return Halt(cursor.lastrowid, at, mode, day, kind, pnl, realized, unrealized, limit)

    def halts(self, mode: str, day: date) -> tuple[Halt | None, Halt | None]:
        """``mode``'s halts in force on ``day``: the daily limit's of that day, the cumulative
        limit's not re-armed."""
        daily = self._db.execute(
            "SELECT * FROM halts WHERE mode = ? AND kind = ? AND day = ? ORDER BY id LIMIT 1",
            (mode, DAILY_LIMIT, day.isoformat()),
        ).fetchone()
        cumulative = self._db.execute(
            "SELECT * FROM halts WHERE mode = ? AND kind = ? AND lifted_at IS NULL ORDER BY id LIMIT 1",
            (mode, CUMULATIVE_LIMIT),
        ).fetchone()
        return (None if daily is None else _halt(daily)), (None if cumulative is None else _halt(cumulative))

    def lift(self, halt: int, at: datetime, rearm: int) -> None:
        """A cumulative halt re-armed."""
        with self._db:
            self._db.execute(
                "UPDATE halts SET lifted_at = ?, rearm_id = ? WHERE id = ?", (iso(at), rearm, halt)
            )

    def rearmed_at(self, mode: str) -> datetime | None:
        """When ``mode``'s cumulative limit was last re-armed: its count starts there."""
        row = self._db.execute("SELECT max(lifted_at) FROM halts WHERE mode = ?", (mode,)).fetchone()
        return None if row[0] is None else datetime.fromisoformat(row[0])

    # The state now and the days (#396).

    def put_snapshot(self, at: datetime, body: Mapping[str, Any]) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO snapshot (id, at, body) VALUES (1, ?, ?)"
                " ON CONFLICT (id) DO UPDATE SET at = excluded.at, body = excluded.body",
                (iso(at), json.dumps(dict(body), ensure_ascii=False)),
            )

    def put_day(self, mode: str, day: date, row: Mapping[str, Any], at: datetime) -> None:
        """``mode``'s ledger of ``day`` as it stands at ``at`` (``days``' columns)."""
        columns = (
            *("realized", "trades", "worst", "cumulative", "cumulative_worst"),
            *("daily_limit", "cumulative_limit", "status", "halt"),
        )
        values = [row[c] for c in columns]
        with self._db:
            self._db.execute(
                f"INSERT INTO days (mode, day, {', '.join(columns)}, updated_at)"
                f" VALUES (?, ?, {', '.join('?' * len(columns))}, ?)"
                " ON CONFLICT (mode, day) DO UPDATE SET "
                + ", ".join(f"{c} = excluded.{c}" for c in (*columns, "updated_at")),
                (mode, day.isoformat(), *values, iso(at)),
            )

    def day(self, mode: str, day: date) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT * FROM days WHERE mode = ? AND day = ?", (mode, day.isoformat())
        ).fetchone()
        return None if row is None else dict(row)

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


def _keyed_by_policy(db: sqlite3.Connection) -> bool:
    """Whether ``decisions`` is unique by the policy (#400), not by the constraint it was made with
    before (an index SQLite made for a ``UNIQUE`` clause)."""
    return not any(r["origin"] == "u" for r in db.execute("PRAGMA index_list(decisions)"))


def _control(row: sqlite3.Row) -> Control:
    return Control(
        row["id"],
        datetime.fromisoformat(row["at"]),
        row["action"],
        row["source"],
        None if row["sell_now"] is None else bool(row["sell_now"]),
        row["mode"],
        None if row["applied_at"] is None else datetime.fromisoformat(row["applied_at"]),
    )


def _halt(row: sqlite3.Row) -> Halt:
    return Halt(
        row["id"],
        datetime.fromisoformat(row["at"]),
        row["mode"],
        date.fromisoformat(row["day"]),
        row["kind"],
        row["pnl"],
        row["realized"],
        row["unrealized"],
        row["limit_won"],
    )


def request_pause(path: Path, sell_now: bool, at: datetime) -> dict[str, Any]:
    """The screen's pause (``POST /v1/trader/pause``, #396), written into the trader's store for
    the trader to read: the control kept. The store is made if the trader has not made it yet; it
    reads the pause when it starts."""
    store = TraderStore(path)
    try:
        return store.add_control(at, PAUSE, API, sell_now=sell_now).as_json()
    finally:
        store.close()


def read_trader(path: Path | None, now: datetime) -> dict[str, Any]:
    """``GET /v1/trader`` (#396): the trader's state as it last wrote it, whether it is running
    (that state at most ``ALIVE_SECONDS`` old at ``now``), and the last controls asked for, newest
    first (those not read yet with ``applied_at`` null); the champion, the live gate on its paper
    record and the policies running with theirs (#400, ``promotion.progress``). Its fields null
    without a store (an engine without a state folder, a trader not started yet)."""
    body: dict[str, Any] = dict.fromkeys(TRADER_FIELDS)
    body |= {"updated_at": None, "alive": False, "controls": []}
    body |= {"champion": None, "gate": promotion.gate(None, None).as_json(), "policies": []}
    if path is None:
        return body
    with _reading(path) as db:
        if db is None:
            return body
        body |= promotion.progress(db)
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "snapshot" in tables:
            row = db.execute("SELECT at, body FROM snapshot WHERE id = 1").fetchone()
            if row is not None:
                updated = datetime.fromisoformat(row["at"])
                written = json.loads(row["body"])
                body |= {k: written.get(k) for k in TRADER_FIELDS}
                body["updated_at"] = iso(updated)
                body["alive"] = abs((now - updated).total_seconds()) <= ALIVE_SECONDS
        if "controls" in tables:
            rows = db.execute("SELECT * FROM controls ORDER BY id DESC LIMIT ?", (CONTROLS_SHOWN,))
            body["controls"] = [_control(r).as_json() for r in rows]
    return body


# The fields of the trader's state it writes (``PaperTrader.state``) and GET /v1/trader gives.
TRADER_FIELDS = (
    "mode",
    "status",
    "started_at",
    "capital",
    "cash",
    "position",
    "today",
    "cumulative",
    "limits",
    "pause",
)


def read_days(path: Path, since: date, to: date) -> list[dict[str, Any]]:
    """``GET /v1/trader/days`` (#396): each day's ledger from ``since`` to ``to`` (both included),
    oldest first, each mode's."""
    with _reading(path) as db:
        if db is None:
            return []
        if (
            db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'days'").fetchone()
            is None
        ):
            return []
        rows = db.execute(
            "SELECT * FROM days WHERE day >= ? AND day <= ? ORDER BY day, mode",
            (since.isoformat(), to.isoformat()),
        ).fetchall()
    return [_day_json(dict(r)) for r in rows]


def _used(worst: float, limit: float) -> float:
    """How much of ``limit`` a P&L of ``worst`` used: 0 for none, 1 for all of it."""
    return max(0.0, -worst) / limit if limit > 0 else 0.0


def _day_json(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "day": row["day"],
        "mode": row["mode"],
        "net": row["realized"],
        "trades": row["trades"],
        "daily": {
            "limit": row["daily_limit"],
            "worst": row["worst"],
            "used": _used(row["worst"], row["daily_limit"]),
        },
        "cumulative": {
            "limit": row["cumulative_limit"],
            "net": row["cumulative"],
            "worst": row["cumulative_worst"],
            "used": _used(row["cumulative_worst"], row["cumulative_limit"]),
        },
        "status": row["status"],
        "halt": row["halt"],
        "updated_at": row["updated_at"],
    }


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


def champion_of(db: sqlite3.Connection) -> str:
    """``CHAMPION_TRADE`` for this store: every trade is the champion's in a store from before #400
    (the trader has not opened it since, so its trades have no ``champion``: the champion's book was
    the only one)."""
    columns = {r[1] for r in db.execute("PRAGMA table_info(trades)")}
    return CHAMPION_TRADE if "champion" in columns else "1"


def read_trades(
    path: Path, before: datetime | None, limit: int, policy_id: int | None = None
) -> list[dict[str, Any]]:
    """The last ``limit`` trades entered before ``before`` (if given), newest first, each with its
    orders and their fills: ``GET /v1/trader/trades``. The champion's (``CHAMPION_TRADE``: the
    trades the trader makes, which the limits hold and the chart marks), or with ``policy_id`` that
    policy's (#400: in paper as a challenger, and as the champion)."""
    with _reading(path) as db:
        if db is None:
            return []
        champion = champion_of(db)
        params: list[Any]
        if policy_id is None:
            conditions, params = [champion], []
        elif "policy_id" not in {r[1] for r in db.execute("PRAGMA table_info(trades)")}:
            return []  # a store from before #399: no trade carries a policy
        else:
            conditions, params = ["t.policy_id = ?"], [policy_id]
        if before is not None:
            conditions.append("t.entry_at < ?")
            params.append(iso(before))
        rows = db.execute(
            # Its own name: ``t.*`` holds ``champion`` already, null in a row from before #400, and a
            # row reads the first column of a name.
            f"SELECT t.*, {champion} AS by_champion FROM trades t WHERE {' AND '.join(conditions)}"
            " ORDER BY t.entry_at DESC, t.id DESC LIMIT ?",
            (*params, limit),
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
        "policy": row.get("policy_id"),  # a store the trader has not opened since #399: none
        "champion": bool(row["by_champion"]),  # the champion's (#400): the trader's, which the limits hold
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
