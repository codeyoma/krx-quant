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
  cancelled), and the broker's order, its number and branch (#405: KIS's, in mock; none in paper).
  A mock sale may take several sells at the best bid (``mock.py``), each its own order;
- ``fills``: each order's fills, with the quote a paper fill was made on (none in mock: a mock
  fill is KIS's, at its average price ``avg_prvs``, as the day's order inquiry shows it);
- ``unfilled_buys`` (#405): each mock buy KIS took of which nothing filled (no trade opened), with
  KIS's order, so that the ledger holds every order KIS has; since #406 each mock buy from before
  it is sent (``sent``: its answer not had yet, or lost), so that one whose answer was lost is
  looked up on KIS and adopted with its decision (``mock.py``);
- ``failures`` (#405): each KIS order of mock mode that failed (refused, its outcome unknown, or
  an error before it was sent), which stops new entries for the rest of that day (``limits.py``);
- ``reconciles`` (#406): each check of the KIS account against the ledger at a mock start: what
  the account holds or has open outside the ledger, whether it was as the ledger has it (``clean``)
  or not (``held``: new entries paused until ``krx-quant trade resume``, which accepts what it
  found, ``resumed_at``);
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
  and the cumulative P&L at their lowest, the limits, the status and the limit reached;
- ``stalls`` (#407, ``stall.py``): each stall of the collector's quotes: when the trader saw it
  (more than five seconds with no quote in the session), the last quote before it, when a quote
  came again (or the trader started again instead), and the REST quotes the mock book read
  meanwhile; ``trade_stalls``: each stall a trade was open through, and what it did to the exit
  (``effect``: ``waited``, the exit made on the first quote after it; ``rest``, the sale decided on
  a REST quote during it; null, none);
- ``gate_checks`` (#408, ``promotion.check_gate``): each checkpoint of the live gate, evaluated once
  by the nightly job: its number, its day, the champion then and its record's figures, its α and
  level, whether it passed, and with one extra adverse tick a leg;
- ``order_checks`` (#408): each KIS trading day's orders in the ledger against KIS's day order
  inquiry, compared by the KIS book after that day's session (``mock.MockTrader``,
  ``mock_check.Check``): the verdict (``same``, ``different``, ``no_orders``) and its counts;
- ``live_probes`` (#408, ``live_probe.py``): each read of the live account by inquiry calls alone,
  checked against the fields the broker reads: whether it passed, and each call's code, rows and
  fields missing or not as read (never a value);
- ``ramp`` (#408, ``live.py``): the live mode's first period's end: the cap lifted to the capital
  (``lift``), or a check breached (``breach``, its pause of new entries, and the resume that ended
  it, ``resumed_at``).

**Practice** (#427, ``KRX_TRADER_MOCK_PRACTICE``): on a day no champion cell trades, the mock book
practises one cell a share at a time (``desk.Desk``). Its pick is kept with ``selections.practice``
1, its trades with ``trades.practice`` 1 (``PRACTICE_TRADE``) and the buys of which nothing filled
with ``unfilled_buys.practice`` 1 (and in their ``entry``, which a restart adopts or opens a trade
from); a trade's orders and fills are its trade's. A practice trade of the mock book is the trader's
own (``champion`` 1: the loss limits, the cash, the KIS checks, the reconcile and the chart count
it), of no policy (``policy_id`` null: no record, promotion or live gate counts it); its paper
shadow's (``shadow`` 1) is of no policy either, and the paper account leaves it out
(``paper_account``).

Every row carries its mode (``paper``, ``mock``, ``live``): paper, KIS mock and live trades are told
apart in the same tables (a pause and a resume hold whatever the mode). The records (``policy_days`` and the
policies' trades' P&L) are paper's whatever the mode (``promotion.records_of``, #405: in mock mode
the champion's paper shadow keeps the champion's, ``shadow`` 1). The trader writes it, and the
nightly re-fit its policies; the Engine API reads it read-only (``read_trades``,
``read_trader``, ``read_days``, ``policy.read_policies``) and writes a pause into it
(``request_pause``), and ``krx-quant trade resume|rearm`` a resume or a re-arm: nothing else. Times
are ISO 8601 strings in KST, which sort in time order. The columns #395, #399, #400 and #405 added
(``_ADDED``) are added to a store made before them, null in its rows.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from krx_quant.feeds.base import KST
from krx_quant.pricing import SELL_TAX
from krx_quant.trader import MOCK, PAPER, policy, promotion
from krx_quant.trader.gap import day_gap

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
CREATE TABLE IF NOT EXISTS unfilled_buys (  -- each mock buy KIS took of which nothing filled (#405)
    id INTEGER PRIMARY KEY,
    decision_id INTEGER NOT NULL REFERENCES decisions (id),
    mode TEXT NOT NULL,
    price REAL NOT NULL,               -- the limit
    quantity INTEGER NOT NULL,
    placed_at TEXT NOT NULL,           -- when it was sent (#406: kept before it is)
    -- cancelled; open when its cancel did not take; filled once it did. #406: sent (its answer not
    -- had: lost, or the trader stopped), refused, unsent (no token), absent (looked up: KIS has none)
    status TEXT NOT NULL,
    broker_id TEXT NOT NULL,           -- KIS's order number; '' while sent (#406)
    broker_branch TEXT,
    entry TEXT                         -- JSON: the trade it would open (its fill to come), while open
);
CREATE TABLE IF NOT EXISTS failures (   -- each KIS order of mock mode that failed (#405)
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    mode TEXT NOT NULL,
    day TEXT NOT NULL,                 -- the KST date: new entries stop for the rest of it
    trade_id INTEGER REFERENCES trades (id),  -- the trade it was of; null for a buy that opened none
    what TEXT NOT NULL,                -- buy, take_profit, cancel, sell, orderable
    outcome TEXT NOT NULL,             -- refused, unknown (no answer: whether it went in is not known), error
    code TEXT,                         -- KIS's msg_cd, when it answered
    detail TEXT NOT NULL               -- why, as KIS said it (never an account or order number)
);
CREATE TABLE IF NOT EXISTS reconciles (  -- each check of the KIS account at a mock start (#406)
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,              -- clean: as the ledger has it; held: not, new entries paused
    -- JSON: what the account held outside the ledger: shares by symbol ("holdings") and the day's KIS
    -- order numbers with something left ("orders")
    outside TEXT NOT NULL,
    detail TEXT NOT NULL,              -- JSON: what differed (``reconcile.Finding.detail``)
    control_id INTEGER REFERENCES controls (id),  -- the pause it asked for (held)
    resumed_at TEXT                    -- held: when a resume lifted its pause, accepting what it found
);
CREATE TABLE IF NOT EXISTS controls (   -- each pause, resume and re-arm asked for (#396)
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,                  -- when it was asked for
    action TEXT NOT NULL,              -- pause, resume, rearm
    -- api (the screen's pause), cli (krx-quant trade resume|rearm), reconcile (a mock start's account
    -- check that found the account unlike the ledger, #406)
    source TEXT NOT NULL,
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
CREATE TABLE IF NOT EXISTS gate_checks (  -- each checkpoint of the live gate (#408)
    k INTEGER PRIMARY KEY,             -- 1, 2, …: the trader's checkpoints, never a champion's
    day TEXT NOT NULL,                 -- the trader's k × 20th paper day
    policy_id INTEGER,                 -- the champion that day; null: none
    days INTEGER NOT NULL,             -- its paper record's days up to it
    mean REAL,                         -- their daily net P&L's mean, won
    low REAL,                          -- its lower bound at ``level``
    stressed_low REAL,                 -- the same with one extra adverse tick a leg
    alpha REAL NOT NULL,               -- 0.025 × 6 ÷ (π² k²), one-sided
    level REAL NOT NULL,               -- 1 − 2α, two-sided
    passed INTEGER NOT NULL,
    stressed_passed INTEGER NOT NULL,
    at TEXT NOT NULL                   -- when the nightly job evaluated it
);
CREATE TABLE IF NOT EXISTS order_checks (  -- each KIS trading day's orders against KIS (#408)
    mode TEXT NOT NULL,                -- mock, live
    day TEXT NOT NULL,
    verdict TEXT NOT NULL,             -- same, different, no_orders
    at TEXT NOT NULL,                  -- when it was compared
    detail TEXT NOT NULL,              -- JSON: the counts (orders, compared, missing, extra, failed)
    PRIMARY KEY (mode, day)
);
CREATE TABLE IF NOT EXISTS live_probes (  -- each read of the live account, checked (#408)
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    passed INTEGER NOT NULL,           -- 1: every answer as the broker reads it
    detail TEXT NOT NULL               -- JSON: each call's code, rows, fields missing or not as read
);
CREATE TABLE IF NOT EXISTS ramp (      -- the live mode's first period's end (#408)
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    mode TEXT NOT NULL,                -- live
    event TEXT NOT NULL,               -- lift: the cap is the capital from now; breach: a check breached
    detail TEXT NOT NULL,              -- JSON: the period and its checks
    control_id INTEGER REFERENCES controls (id),  -- a breach's pause of new entries
    resumed_at TEXT                    -- a breach: when a resume lifted its pause, a new period beginning
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
        ("practice", "INTEGER"),  # 1: its pick is the mock book's practice cell (#427); null or 0: not
    ),
    "decisions": (
        ("selection_id", "INTEGER REFERENCES selections (id)"),  # the pick in force; null: none yet
    ),
    "orders": (
        ("broker_branch", "TEXT"),  # the broker order's branch (#405: KRX_FWDG_ORD_ORGNO); null in paper
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
        # 1: the champion's paper shadow's (#405: in mock mode, the champion policy's trade filled in
        # paper beside its KIS orders, ``champion`` 0); null or 0 otherwise.
        ("shadow", "INTEGER"),
        # Why a mock trade's sale began (#405: stop_loss, time, pause, a limit), kept when it is
        # decided so that a restart goes on selling; null until then.
        ("selling", "TEXT"),
        # 1: a practice trade (#427): the mock book's on its practice cell (``champion`` 1), or its paper
        # shadow's (``shadow`` 1); of no policy. Null or 0 otherwise.
        ("practice", "INTEGER"),
    ),
    "unfilled_buys": (
        ("practice", "INTEGER"),  # 1: a practice buy (#427); null or 0 otherwise
    ),
}

# The stalls of the collector's quotes (#407, ``stall.py``), and the trades open through them.
_STALLS = """
CREATE TABLE IF NOT EXISTS stalls (     -- each stall of the collector's quote stream (#407)
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL,
    began_at TEXT NOT NULL,            -- when the trader saw it: more than 5 s with no quote
    last_quote_at TEXT,                -- the last quote received before it; null: none since the start
    ended_at TEXT,                     -- null while it lasts
    ended_by TEXT,                     -- quote: a quote came again; restart: the trader started again
    rest_reads INTEGER NOT NULL DEFAULT 0  -- KIS's best quotes read over REST meanwhile (mock)
);
CREATE TABLE IF NOT EXISTS trade_stalls (  -- each stall a trade was open through (#407)
    trade_id INTEGER NOT NULL REFERENCES trades (id),
    stall_id INTEGER NOT NULL REFERENCES stalls (id),
    effect TEXT,                       -- waited, rest; null: the trade went on unaffected
    PRIMARY KEY (trade_id, stall_id)
);
"""
# What a stall did to a trade's exit (``trade_stalls.effect``): its exit was made on the first quote
# after the stall (paper waits for the quotes), or its sale decided on a REST quote during it (mock).
WAITED, REST = "waited", "rest"
# How a stall ended (``stalls.ended_by``): a quote came again, or the trader started again.
QUOTE, RESTART = "quote", "restart"

# A trade ``t`` of the champion (#399, #400): made by the champion's book (``trades.champion``), the
# trader's own trades, before the first champion too; never a challenger's paper trade. The chart
# marks these alone (``marks.py``), the loss limits and the paper cash count these alone (#396), and
# ``GET /v1/trader/trades`` gives these unless asked for a policy's. The champion changes only after
# the market (``promotion.in_market``), so a day's trades are all of one champion.
CHAMPION_TRADE = "coalesce(t.champion, 1) = 1"
# A trade ``t`` of the champion's paper shadow (#405): in mock mode, the champion policy's paper trade
# beside the KIS orders. Not the trader's (``champion`` 0): no limit, chart or default trade list
# counts it; its policy's paper record does (``promotion.records``).
SHADOW_TRADE = "coalesce(t.shadow, 0) = 1"
# A practice trade ``t`` (#427, module doc): the mock book's on its practice cell (``champion`` 1, the
# trader's own: the limits, the cash and the KIS checks count it) or its paper shadow's (``shadow``
# 1). Of no policy: no record counts it.
PRACTICE_TRADE = "coalesce(t.practice, 0) = 1"

BUY, SELL = "buy", "sell"
OPEN, FILLED, CANCELLED = "open", "filled", "cancelled"
# A mock buy's (``unfilled_buys``, #406): sent, its answer not had (lost, or the trader stopped
# before it came); refused; not sent (no token); looked up and not on KIS.
SENT, BUY_REFUSED, UNSENT, ABSENT = "sent", "refused", "unsent", "absent"
ENTRY = "entry"
# What a control asks for (#396), and who asks: the reconcile's pause (#406) is a mock start's; the
# ramp's (#408) the live mode's first period's breach.
PAUSE, RESUME, REARM = "pause", "resume", "rearm"
API, CLI, RECONCILE, RAMP = "api", "cli", "reconcile", "ramp"
# The live mode's first period's end (#408, ``ramp``).
LIFT, BREACH = "lift", "breach"
# A KIS day's orders against KIS (#408, ``order_checks``): as ``mock_check.Check.code`` finds them.
SAME, DIFFERENT, NO_ORDERS = "same", "different", "no_orders"
# A reconcile's (#406): the account as the ledger has it, or not (new entries paused).
CLEAN, HELD = "clean", "held"
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
    # Made by the champion's book (#400), the trader's own; False: a challenger's paper trade, or the
    # shadow's (``shadow``).
    champion: bool = True
    # The champion's paper shadow's (#405): the champion policy's paper trade in mock mode.
    shadow: bool = False
    # A practice trade (#427): the mock book's on its practice cell, or its paper shadow's.
    practice: bool = False


@dataclass(frozen=True)
class Exit:
    at: datetime
    price: float
    reason: str
    sell_fee: float
    tax: float
    quote_at: datetime


@dataclass(frozen=True)
class Placed:
    """An order as the broker took it (#405): its limit and quantity, and KIS's order (its branch and
    number; None when KIS named none). ``at`` is when it was sent (#406): its day's order inquiry
    shows it, though its fills were read later (the trader stopped between), on another day."""

    price: float
    quantity: int
    branch: str | None = None
    number: str | None = None
    at: datetime | None = None


@dataclass(frozen=True)
class Resting:
    """A trade's sell order open on KIS (#405): the take-profit's resting at the target, or a sale's
    whose cancel did not take; its row, KIS's order, what of it was kept filled."""

    order_id: int
    price: float
    quantity: int
    branch: str
    number: str
    filled: int
    amount: float  # the fills kept of it, in won
    purpose: str = "take_profit"  # take_profit, or a sale's (stop_loss, time, ...)
    day: date | None = None  # the day it was placed: the day order inquiry that shows it


@dataclass(frozen=True)
class Failure:
    """A KIS order that failed (#405)."""

    id: int
    at: datetime
    mode: str
    day: date
    what: str
    outcome: str
    code: str | None
    detail: str


@dataclass(frozen=True)
class Stall:
    """A stall of the collector's quotes (#407): when the trader saw it, and the last quote received
    before it (None: none since the trader started)."""

    id: int
    mode: str
    began_at: datetime
    last_quote_at: datetime | None


@dataclass(frozen=True)
class OpenTrade:
    """A trade not exited yet, as kept: what a restarted trader goes on with."""

    id: int
    take_profit_order: int | None  # None: a mock trade whose take-profit sell KIS did not take (#405)
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
    practice: bool = False  # a practice trade (#427)


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
            f"BEGIN;{_SCHEMA.format(decisions=_DECISIONS.format(name='decisions'))}{_STALLS}COMMIT;"
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

    @property
    def db(self) -> sqlite3.Connection:
        """The connection, for the readers of several tables at once (``live.py``, ``mock_check``)."""
        return self._db

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
                "INSERT INTO selections (at, mode, as_of, picks, considered, reason, detail, policy_id,"
                " practice) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    iso(at),
                    mode,
                    selection.as_of,
                    json.dumps([p.as_json() for p in selection.picks], ensure_ascii=False),
                    json.dumps(list(selection.considered), ensure_ascii=False),
                    selection.reason,
                    selection.detail,
                    selection.policy,
                    int(selection.practising),
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

    def unfilled(self, decision: int, reason: str | None) -> None:
        """A buy decided that did not fill, and why (None: not given up, #405)."""
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
            trade = self._insert_trade(entry)
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
            entry.practice,
        )

    def exit(self, trade: OpenTrade, out: Exit) -> None:
        """A trade's exit: the take-profit's sell filled at the target, or cancelled for the stop's
        or the time exit's sell, filled at the best bid; the trade's exit, costs and P&L."""
        at = iso(out.at)
        with self._db:
            if out.reason == "take_profit":
                assert trade.take_profit_order is not None, "a paper trade's take-profit rests from its entry"
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

    # Mock trades (#405): each order as KIS took it, each fill as its order inquiry showed it.

    def open_position(self, decision: int, entry: Entry, buy: Placed, status: str | None = None) -> OpenTrade:
        """A mock buy filled (``entry``: the fills' average price and quantity): its trade, the buy's
        order as KIS took it (filled whole, or cancelled for the rest; ``status`` open when its cancel
        did not take, followed until it settles: ``grow_position``, ``close_entry``) and its fill, in
        one transaction. Its take-profit sell is added once KIS takes it (``add_order``). The order is
        placed when it was sent (``buy.at``, #406), the fill and the trade's entry when the fills
        were read: a buy whose fills a restart recovered is on its own day's inquiry."""
        at = iso(entry.entry_at)
        placed = iso(buy.at) if buy.at is not None else at
        status = status or (FILLED if entry.quantity >= buy.quantity else CANCELLED)
        with self._db:
            trade = self._insert_trade(entry)
            bought = self._order(trade, entry.mode, BUY, ENTRY, buy.price, buy.quantity, placed, status)
            self._db.execute(
                "UPDATE orders SET closed_at = ?, broker_id = ?, broker_branch = ? WHERE id = ?",
                (None if status == OPEN else at, buy.number, buy.branch, bought),
            )
            self._db.execute(
                "INSERT INTO fills (order_id, at, price, quantity) VALUES (?, ?, ?, ?)",
                (bought, at, entry.entry_price, entry.quantity),
            )
            self._db.execute("UPDATE decisions SET trade_id = ? WHERE id = ?", (trade, decision))
        return OpenTrade(
            trade,
            None,
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
            entry.practice,
        )

    def add_order(
        self, trade: int, side: str, purpose: str, placed: Placed, at: datetime, status: str = OPEN
    ) -> int:
        """An order of ``trade`` as KIS took it (#405); a closed one is closed at ``at``."""
        when = iso(at)
        with self._db:
            order = self._order(
                trade, self._mode(trade), side, purpose, placed.price, placed.quantity, when, status
            )
            self._db.execute(
                "UPDATE orders SET closed_at = ?, broker_id = ?, broker_branch = ? WHERE id = ?",
                (None if status == OPEN else when, placed.number, placed.branch, order),
            )
        return order

    def add_fill(self, order: int, at: datetime, price: float, quantity: int) -> None:
        """A fill of ``order`` as KIS's order inquiry showed it (#405): its average price."""
        with self._db:
            self._db.execute(
                "INSERT INTO fills (order_id, at, price, quantity) VALUES (?, ?, ?, ?)",
                (order, iso(at), price, quantity),
            )

    def close_order(self, order: int, status: str, at: datetime) -> None:
        with self._db:
            self._db.execute(
                "UPDATE orders SET status = ?, closed_at = ? WHERE id = ?", (status, iso(at), order)
            )

    def open_sells(self, trade: int) -> list[Resting]:
        """``trade``'s sell orders still open on KIS (#405), oldest first: its take-profit sell resting
        at the target, and a sale's sell whose cancel did not take; each with what of it was kept
        filled."""
        rows = self._db.execute(
            "SELECT o.*, coalesce(sum(f.quantity), 0) AS kept,"
            " coalesce(sum(f.price * f.quantity), 0) AS amount"
            " FROM orders o LEFT JOIN fills f ON f.order_id = o.id WHERE o.trade_id = ? AND o.side = ?"
            " AND o.status = ? AND o.broker_id IS NOT NULL GROUP BY o.id ORDER BY o.id",
            (trade, SELL, OPEN),
        )
        return [
            Resting(
                r["id"],
                r["price"],
                r["quantity"],
                r["broker_branch"] or "",
                r["broker_id"],
                int(r["kept"]),
                float(r["amount"]),
                r["purpose"],
                date.fromisoformat(r["placed_at"][:10]),
            )
            for r in rows
        ]

    def begin_sale(self, trade: int, reason: str) -> None:
        """Why ``trade``'s sale began (#405), kept when it is decided: a restart goes on selling it
        though none of its sells is open (each cancelled, or refused)."""
        with self._db:
            self._db.execute(
                "UPDATE trades SET selling = ? WHERE id = ? AND selling IS NULL", (reason, trade)
            )

    def sale_begun(self, trade: int) -> str | None:
        """Why ``trade``'s sale began (``begin_sale``), if it did."""
        row = self._db.execute("SELECT selling FROM trades WHERE id = ?", (trade,)).fetchone()
        return None if row is None or row[0] is None else str(row[0])

    def resting(self, trade: int) -> Resting | None:
        """``trade``'s take-profit sell resting on KIS (#405), if one is."""
        return next((o for o in self.open_sells(trade) if o.purpose == "take_profit"), None)

    def sells(self, trade: int) -> tuple[int, float]:
        """The shares of ``trade`` sold so far and what they brought, in won: its sell orders' fills."""
        row = self._db.execute(
            "SELECT coalesce(sum(f.quantity), 0), coalesce(sum(f.price * f.quantity), 0) FROM fills f"
            " JOIN orders o ON o.id = f.order_id WHERE o.trade_id = ? AND o.side = ?",
            (trade, SELL),
        ).fetchone()
        return int(row[0]), float(row[1])

    def sold(self, trade: int) -> int:
        """The shares of ``trade`` sold so far."""
        return self.sells(trade)[0]

    def add_unfilled_buy(
        self, decision: int, mode: str, placed: Placed, at: datetime, status: str, entry: Entry | None = None
    ) -> int:
        """A mock buy of which nothing filled (#405): no trade, but the ledger keeps every order KIS
        has (``mock_check``). ``status`` is cancelled, or open when its cancel did not take
        (followed: ``close_unfilled_buy``), kept with the trade it would open (``entry``) for a
        restart to follow it (``open_unfilled_buy``). Since #406 it is kept before it is sent
        (``sent``, no order number yet: ``accept_buy`` names it once KIS takes it), so that a buy
        whose answer was lost is looked up and adopted with its decision (``sent_buys``). Its row."""
        with self._db:
            cursor = self._db.execute(
                "INSERT INTO unfilled_buys (decision_id, mode, price, quantity, placed_at, status, broker_id,"
                " broker_branch, entry, practice) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (decision, mode, placed.price, placed.quantity, iso(at), status, placed.number or "")
                + (placed.branch, None if entry is None else _entry_json(entry))
                + (int(entry is not None and entry.practice),),
            )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def accept_buy(self, row: int, branch: str | None, number: str, status: str = OPEN) -> None:
        """A buy kept before it was sent (#406), as KIS took it: its order (``branch`` None for one
        adopted off the inquiry, which names it by number alone), and open while it is followed."""
        with self._db:
            self._db.execute(
                "UPDATE unfilled_buys SET broker_id = ?, broker_branch = ?, status = ? WHERE id = ?",
                (number, branch, status, row),
            )

    def open_unfilled_buy(self, mode: str) -> tuple[int, int, Placed, date, Entry] | None:
        """``mode``'s buy of which nothing filled, still open on KIS (#405): its row, its decision, its
        order (sent at ``Placed.at``), its day, the trade it would open. None without one."""
        row = self._db.execute(
            "SELECT * FROM unfilled_buys WHERE mode = ? AND status = ? AND entry IS NOT NULL"
            " ORDER BY id DESC LIMIT 1",
            (mode, OPEN),
        ).fetchone()
        if row is None:
            return None
        return row["id"], row["decision_id"], _placed(row), _day_of(row), _entry_of(row["entry"])

    def sent_buys(self, mode: str) -> list[tuple[int, int, Placed, date, Entry]]:
        """``mode``'s buys sent whose answer was not had (#406: lost, or the trader stopped before it
        came), oldest first: each row, decision, order (no number: ``Placed.at`` when it was sent),
        day and the trade it would open, to be looked up on KIS."""
        rows = self._db.execute(
            "SELECT * FROM unfilled_buys WHERE mode = ? AND status = ? AND entry IS NOT NULL ORDER BY id",
            (mode, SENT),
        )
        return [(r["id"], r["decision_id"], _placed(r), _day_of(r), _entry_of(r["entry"])) for r in rows]

    def broker_numbers(self, mode: str) -> set[str]:
        """Every KIS order number ``mode``'s ledger keeps (#406): its trades' orders' and its buys'
        that opened none."""
        rows = self._db.execute(
            "SELECT broker_id FROM orders WHERE mode = ? AND broker_id IS NOT NULL AND broker_id != ''"
            " UNION SELECT broker_id FROM unfilled_buys WHERE mode = ? AND broker_id != ''",
            (mode, mode),
        )
        return {str(r[0]) for r in rows}

    def close_unfilled_buy(self, row: int, status: str) -> None:
        """An unfilled buy followed (#405) settled: cancelled with nothing filled, or ``filled`` once
        something did (its trade then holds the order: ``open_position``)."""
        with self._db:
            self._db.execute("UPDATE unfilled_buys SET status = ? WHERE id = ?", (status, row))

    def grow_position(
        self, trade: int, quantity: int, price: float, buy_fee: float, fill: tuple[datetime, float, int]
    ) -> None:
        """More of a mock trade's buy filled after its cancel failed (#405): the trade's shares, entry
        price and commission the buy's fills now, and the new fill (when, its average, how many) kept
        on its entry order."""
        at, fill_price, fill_quantity = fill
        with self._db:
            self._db.execute(
                "UPDATE trades SET quantity = ?, entry_price = ?, buy_fee = ? WHERE id = ?",
                (quantity, price, buy_fee, trade),
            )
            self._db.execute(
                "INSERT INTO fills (order_id, at, price, quantity) SELECT id, ?, ?, ? FROM orders"
                " WHERE trade_id = ? AND purpose = ? ORDER BY id LIMIT 1",
                (iso(at), fill_price, fill_quantity, trade, ENTRY),
            )

    def close_entry(self, trade: int, status: str, at: datetime) -> None:
        """A mock trade's buy settled (#405): filled whole, or cancelled for the rest."""
        with self._db:
            self._db.execute(
                "UPDATE orders SET status = ?, closed_at = ? WHERE trade_id = ? AND purpose = ?",
                (status, iso(at), trade, ENTRY),
            )

    def open_entry(self, trade: int) -> tuple[Placed, int, float, date] | None:
        """A mock trade's buy still open on KIS (#405): its order, what of it filled and for how much,
        its day; None once it settled."""
        row = self._db.execute(
            "SELECT o.*, coalesce(sum(f.quantity), 0) AS kept,"
            " coalesce(sum(f.price * f.quantity), 0) AS amount"
            " FROM orders o LEFT JOIN fills f ON f.order_id = o.id WHERE o.trade_id = ? AND o.purpose = ?"
            " AND o.status = ? AND o.broker_id IS NOT NULL GROUP BY o.id",
            (trade, ENTRY, OPEN),
        ).fetchone()
        if row is None or row["id"] is None:
            return None
        return _placed(row), int(row["kept"]), float(row["amount"]), _day_of(row)

    def exit_on_fills(self, trade: OpenTrade, at: datetime, reason: str, fee_rate: float) -> float:
        """A mock trade's exit (#405), once its sells' fills hold every share: its price their average,
        its costs on their notional (the commission ``fee_rate`` a side and the sell tax), its P&L. The
        net, in won."""
        row = self._db.execute(
            "SELECT coalesce(sum(f.quantity), 0), coalesce(sum(f.price * f.quantity), 0) FROM fills f"
            " JOIN orders o ON o.id = f.order_id WHERE o.trade_id = ? AND o.side = ?",
            (trade.id, SELL),
        ).fetchone()
        quantity, amount = int(row[0]), float(row[1])
        if quantity != trade.quantity:
            raise ValueError(f"trade {trade.id}: {quantity} of its {trade.quantity} shares sold")
        price = amount / quantity
        sell_fee, tax = amount * fee_rate, amount * SELL_TAX
        gross = amount - trade.entry_price * trade.quantity
        cost = trade.buy_fee + sell_fee + tax
        with self._db:
            self._db.execute(
                "UPDATE trades SET exit_at = ?, exit_price = ?, exit_reason = ?, sell_fee = ?, tax = ?,"
                " cost = ?, gross = ?, net = ? WHERE id = ?",
                (iso(at), price, reason, sell_fee, tax, cost, gross, gross - cost, trade.id),
            )
        return gross - cost

    def add_failure(
        self,
        at: datetime,
        mode: str,
        what: str,
        outcome: str,
        detail: str,
        code: str | None = None,
        trade: int | None = None,
    ) -> Failure:
        day = at.astimezone(KST).date()
        with self._db:
            cursor = self._db.execute(
                "INSERT INTO failures (at, mode, day, trade_id, what, outcome, code, detail)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (iso(at), mode, day.isoformat(), trade, what, outcome, code, detail),
            )
        assert cursor.lastrowid is not None
        return Failure(cursor.lastrowid, at, mode, day, what, outcome, code, detail)

    def failure(self, mode: str, day: date) -> Failure | None:
        """``mode``'s first failed order of ``day`` (#405): new entries stop for the rest of it."""
        row = self._db.execute(
            "SELECT * FROM failures WHERE mode = ? AND day = ? ORDER BY id LIMIT 1", (mode, day.isoformat())
        ).fetchone()
        return None if row is None else _failure(row)

    def open_trade(
        self, mode: str, challenger: int | None = None, shadow: bool = False, practice: bool = False
    ) -> OpenTrade | None:
        """``mode``'s trade of the champion (``CHAMPION_TRADE``, a practice one of the mock book's too,
        #427) not exited yet, if any (one position at a time); with ``challenger``, that challenger's
        paper trade not exited (#400); with ``shadow``, the champion's paper shadow's (#405,
        ``SHADOW_TRADE``), or with ``practice`` too the practice shadow's (#427)."""
        params: tuple[Any, ...]
        if shadow:
            which = PRACTICE_TRADE if practice else f"NOT {PRACTICE_TRADE}"
            whose, params = f"{SHADOW_TRADE} AND {which}", (mode,)
        elif challenger is None:
            whose, params = CHAMPION_TRADE, (mode,)
        else:
            whose = f"t.policy_id = ? AND t.champion = 0 AND NOT {SHADOW_TRADE}"
            params = (mode, challenger)
        row = self._db.execute(
            "SELECT t.*, o.id AS resting FROM trades t LEFT JOIN orders o ON o.trade_id = t.id"
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
            bool(row["practice"]),
        )

    def open_challengers(self, mode: str) -> list[int]:
        """The challengers with a paper trade of ``mode`` not exited (#400: the trader stopped before
        its exit), whose books go on with it."""
        rows = self._db.execute(
            "SELECT DISTINCT t.policy_id FROM trades t WHERE t.mode = ? AND t.exit_at IS NULL"
            f" AND t.policy_id IS NOT NULL AND t.champion = 0 AND NOT {SHADOW_TRADE} ORDER BY t.policy_id",
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

    def paper_account(self) -> float:
        """The net P&L of the champion's paper book (#405): its paper trades exited, the champion's
        book's in paper mode and its shadow's in mock mode, never the practice shadow's (#427). What
        the shadows and the challengers size on in mock mode, never the KIS fills."""
        row = self._db.execute(
            "SELECT coalesce(sum(net), 0) FROM trades t WHERE mode = ? AND exit_at IS NOT NULL"
            f" AND ({CHAMPION_TRADE} OR {SHADOW_TRADE}) AND NOT {PRACTICE_TRADE}",
            (PAPER,),
        ).fetchone()
        return float(row[0])

    def realized(self, mode: str, since: datetime | None) -> tuple[float, int]:
        """The net P&L of ``mode``'s trades exited at or after ``since`` (all if None), and how many:
        the champion's alone (``CHAMPION_TRADE``), the trades the limits and the cash hold (#396), the
        mock book's practice trades with them (#427: its KIS fills, the same account); a challenger's
        paper trades (#400) are not, nor a shadow's."""
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

    # Reconciles (#406): each mock start's check of the KIS account against the ledger.

    def add_reconcile(
        self,
        at: datetime,
        mode: str,
        status: str,
        outside: Mapping[str, Any],
        detail: Mapping[str, Any],
        control: int | None = None,
    ) -> int:
        with self._db:
            cursor = self._db.execute(
                "INSERT INTO reconciles (at, mode, status, outside, detail, control_id)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (iso(at), mode, status, _json(outside), _json(detail), control),
            )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def accepted_outside(self, mode: str) -> dict[str, Any]:
        """What ``mode``'s account held outside the ledger as last accepted (#406): the last reconcile
        that found it as the ledger has it, or whose pause a resume lifted. Empty before any."""
        row = self._db.execute(
            "SELECT outside FROM reconciles WHERE mode = ? AND (status = ? OR resumed_at IS NOT NULL)"
            " ORDER BY id DESC LIMIT 1",
            (mode, CLEAN),
        ).fetchone()
        return {} if row is None else dict(json.loads(row[0]))

    def held_reconcile(self, mode: str) -> dict[str, Any] | None:
        """``mode``'s last reconcile, if it found the account unlike the ledger and no resume has
        lifted its pause yet (#406): its row."""
        row = self._db.execute(
            "SELECT * FROM reconciles WHERE mode = ? ORDER BY id DESC LIMIT 1", (mode,)
        ).fetchone()
        if row is None or row["status"] != HELD or row["resumed_at"] is not None:
            return None
        return {**dict(row), "outside": json.loads(row["outside"]), "detail": json.loads(row["detail"])}

    # The KIS days' orders against KIS (#408).

    def add_order_check(
        self, mode: str, day: date, verdict: str, at: datetime, detail: Mapping[str, Any]
    ) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO order_checks (mode, day, verdict, at, detail) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (mode, day) DO UPDATE SET verdict = excluded.verdict, at = excluded.at,"
                " detail = excluded.detail",
                (mode, day.isoformat(), verdict, iso(at), _json(detail)),
            )

    def order_checks(self, mode: str) -> dict[date, str]:
        """``mode``'s days compared, each its verdict."""
        rows = self._db.execute("SELECT day, verdict FROM order_checks WHERE mode = ?", (mode,))
        return {date.fromisoformat(r[0]): str(r[1]) for r in rows}

    # The live account's read (#408, ``live_probe.py``).

    def add_live_probe(self, at: datetime, passed: bool, detail: Mapping[str, Any]) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO live_probes (at, passed, detail) VALUES (?, ?, ?)",
                (iso(at), int(passed), _json(detail)),
            )

    # The live mode's first period (#408, ``live.py``).

    def add_ramp(
        self, at: datetime, mode: str, event: str, detail: Mapping[str, Any], control: int | None = None
    ) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO ramp (at, mode, event, detail, control_id) VALUES (?, ?, ?, ?, ?)",
                (iso(at), mode, event, _json(detail), control),
            )

    def resume_ramp(self, at: datetime) -> int:
        """A resume lifting the pauses (#396): the live first period's breaches not resumed yet end at
        ``at``, and a new period begins (``live.ramp``). Whatever mode the resume is read in: a pause
        holds every mode, so a breach's is lifted by a resume a paper trader read too. How many."""
        with self._db:
            cursor = self._db.execute(
                "UPDATE ramp SET resumed_at = ? WHERE event = ? AND resumed_at IS NULL", (iso(at), BREACH)
            )
        return cursor.rowcount

    def accept_reconciles(self, mode: str, at: datetime) -> int:
        """A resume at ``at`` (#406): the reconciles of ``mode`` that paused, accepted (what they found
        outside the ledger is the account's from then); how many."""
        with self._db:
            cursor = self._db.execute(
                "UPDATE reconciles SET resumed_at = ? WHERE mode = ? AND status = ? AND resumed_at IS NULL",
                (iso(at), mode, HELD),
            )
        return cursor.rowcount

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

    # The stalls of the collector's quotes (#407, ``stall.py``).

    def add_stall(self, mode: str, began_at: datetime, last_quote_at: datetime | None) -> Stall:
        with self._db:
            cursor = self._db.execute(
                "INSERT INTO stalls (mode, began_at, last_quote_at) VALUES (?, ?, ?)",
                (mode, iso(began_at), None if last_quote_at is None else iso(last_quote_at)),
            )
        assert cursor.lastrowid is not None
        return Stall(cursor.lastrowid, mode, began_at, last_quote_at)

    def end_stall(self, stall: int, at: datetime, how: str) -> None:
        with self._db:
            self._db.execute(
                "UPDATE stalls SET ended_at = ?, ended_by = ? WHERE id = ? AND ended_at IS NULL",
                (iso(at), how, stall),
            )

    def open_stall(self) -> Stall | None:
        """The last stall not ended (the trader stopped during it), if any."""
        row = self._db.execute(
            "SELECT * FROM stalls WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        last = row["last_quote_at"]
        return Stall(
            row["id"],
            row["mode"],
            datetime.fromisoformat(row["began_at"]),
            None if last is None else datetime.fromisoformat(last),
        )

    def read_rest(self, stall: int) -> None:
        """One more REST quote read during ``stall`` (the mock book's, #407)."""
        with self._db:
            self._db.execute("UPDATE stalls SET rest_reads = rest_reads + 1 WHERE id = ?", (stall,))

    def add_trade_stall(self, trade: int, stall: int) -> None:
        """``trade`` open through ``stall``; kept once."""
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO trade_stalls (trade_id, stall_id) VALUES (?, ?)", (trade, stall)
            )

    def stall_effect(self, trade: int, stall: int, effect: str) -> None:
        """What ``stall`` did to ``trade``'s exit (``WAITED``, ``REST``)."""
        with self._db:
            self._db.execute(
                "INSERT INTO trade_stalls (trade_id, stall_id, effect) VALUES (?, ?, ?)"
                " ON CONFLICT (trade_id, stall_id) DO UPDATE SET effect = excluded.effect",
                (trade, stall, effect),
            )

    def _insert_trade(self, entry: Entry) -> int:
        """``entry``'s row in ``trades``, inside the caller's transaction."""
        cursor = self._db.execute(
            "INSERT INTO trades (mode, symbol, kind, predictor, prediction_id, bar_close, probability,"
            " threshold, anchor, tick, width_ticks, target, stop, time_barrier, decided_at, entry_at,"
            " entry_price, quantity, buy_fee, selection_id, role, p_star, precision_low, kelly, fraction,"
            f" policy_id, champion, shadow, practice) VALUES ({', '.join('?' * 29)})",
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
                iso(entry.entry_at),
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
                int(entry.shadow),
                int(entry.practice),
            ),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

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


# An Entry's times, as ISO strings in its JSON (``_entry_json``).
_ENTRY_TIMES = ("bar_close", "time_barrier", "decided_at", "entry_at", "quote_at")


def _entry_json(entry: Entry) -> str:
    fields = asdict(entry)
    return json.dumps({k: iso(v) if k in _ENTRY_TIMES else v for k, v in fields.items()})


def _entry_of(text: str) -> Entry:
    fields: dict[str, Any] = json.loads(text)
    times: dict[str, Any] = {k: datetime.fromisoformat(fields[k]) for k in _ENTRY_TIMES}
    return Entry(**(fields | times))


def _placed(row: sqlite3.Row) -> Placed:
    """An order's row (``orders``, ``unfilled_buys``) as KIS took it, sent at its ``placed_at``."""
    number = row["broker_id"] or None
    return Placed(
        row["price"], row["quantity"], row["broker_branch"], number, datetime.fromisoformat(row["placed_at"])
    )


def _day_of(row: sqlite3.Row) -> date:
    """The day an order's row was placed on: the day order inquiry that shows it."""
    return date.fromisoformat(row["placed_at"][:10])


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True)


def _failure(row: sqlite3.Row) -> Failure:
    return Failure(
        row["id"],
        datetime.fromisoformat(row["at"]),
        row["mode"],
        date.fromisoformat(row["day"]),
        row["what"],
        row["outcome"],
        row["code"],
        row["detail"],
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
    first (those not read yet with ``applied_at`` null); the champion, the live gate on its record
    and the policies running with theirs (#400, ``promotion.progress``), in the mode it runs in
    (#405). Its fields null without a store (an engine without a state folder, a trader not started
    yet)."""
    body: dict[str, Any] = dict.fromkeys(TRADER_FIELDS)
    body |= {"updated_at": None, "alive": False, "controls": []}
    body |= {"champion": None, "gate": promotion.no_gate().as_json(), "policies": []}
    if path is None:
        return body
    with _reading(path) as db:
        if db is None:
            return body
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "snapshot" in tables:
            row = db.execute("SELECT at, body FROM snapshot WHERE id = 1").fetchone()
            if row is not None:
                updated = datetime.fromisoformat(row["at"])
                written = json.loads(row["body"])
                body |= {k: written.get(k) for k in TRADER_FIELDS}
                body["updated_at"] = iso(updated)
                body["alive"] = abs((now - updated).total_seconds()) <= ALIVE_SECONDS
        # The records of the mode it runs in (#405): its state's, else the mode it last started in.
        body |= promotion.progress(db, body["mode"])
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
    "live",
    "practice",
)


def read_days(path: Path, since: date, to: date) -> list[dict[str, Any]]:
    """``GET /v1/trader/days`` (#396): each day's ledger from ``since`` to ``to`` (both included),
    oldest first, each mode's; a mock day's with its gap to the champion's paper shadow (#405,
    ``gap.day_gap``), null on the others."""
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
        return [
            _day_json(dict(r))
            | {"gap": day_gap(db, date.fromisoformat(r["day"])) if r["mode"] == MOCK else None}
            for r in rows
        ]


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


def practice_of(db: sqlite3.Connection) -> str:
    """``PRACTICE_TRADE`` for this store: none is a practice trade in a store the trader has not
    opened since #427 (no ``practice`` column yet)."""
    columns = {r[1] for r in db.execute("PRAGMA table_info(trades)")}
    return PRACTICE_TRADE if "practice" in columns else "0"


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
    trades the trader makes, which the limits hold and the chart marks; the mock book's practice
    trades among them, each ``practice``, #427), or with ``policy_id`` that policy's (#400: in paper
    as a challenger, and as the champion; a practice trade is of no policy)."""
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
        stalls: dict[int, list[dict[str, Any]]] = {}
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if ids and "trade_stalls" in tables:  # a store the trader has not opened since #407: none
            marks = ",".join("?" * len(ids))
            for s in db.execute(
                "SELECT ts.trade_id, ts.effect, s.began_at, s.last_quote_at, s.ended_at"
                " FROM trade_stalls ts JOIN stalls s ON s.id = ts.stall_id"
                f" WHERE ts.trade_id IN ({marks}) ORDER BY s.began_at, s.id",
                ids,
            ):
                stalls.setdefault(s["trade_id"], []).append(
                    {k: s[k] for k in ("began_at", "last_quote_at", "ended_at", "effect")}
                )
        return [_trade_json(dict(r), orders.get(r["id"], []), fills, stalls.get(r["id"], [])) for r in rows]


def _trade_json(
    row: dict[str, Any],
    orders: list[dict[str, Any]],
    fills: Mapping[int, list[dict[str, Any]]],
    stalls: list[dict[str, Any]],
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
        "shadow": bool(row.get("shadow")),  # the champion's paper shadow's (#405, mock mode)
        "practice": bool(row.get("practice")),  # the mock book's practice trade, or its shadow's (#427)
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
        # The stalls of the collector's quotes the trade was open through (#407), and what each did
        # to its exit.
        "stalls": stalls,
    }
