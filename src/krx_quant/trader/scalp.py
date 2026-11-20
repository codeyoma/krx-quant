"""The scalp policy (#705): the mock book trades a short, fixed round trip often enough to measure, the
user's decisions of 2026-11-18.

**What.** With ``KRX_TRADER_MOCK_SCALP`` on (the default in mock mode; mock mode alone), on the picks the
practice and the verdict take (a cost map and a champion read, no champion cell to trade,
``buyable.scalped``), the champion's KIS book follows the scalp's one cell too (predictor ``scalp``, of
the kind its signal is read from: below), ranked after the verdict's horizons and before the practice
cell.
The KIS book stays the one owner of the account's orders: one position at a time, so at most one open
scalp of the symbol.

**The signal** (``source``, ``reader``): two versions, one at a time, chosen at each pick (``source``,
``desk.Desk.select``) and read by the reader of its cell (``reader``):

- **The pool** (``POOL``, ``PoolReader``, cell ``CELL``: kind ``barrier``, the first version, #705): the up
  first of the 4-minute barrier horizon's pool in each verdict the engine kept (``verdicts``: the newest
  version of a bar close; v2 pools the night ranking's members alone, today Breiman), its displayed time
  the verdict's ``made_at``. Those algorithms were trained for the 4-minute barrier, not for this round
  trip, and the report says so.
- **The member** (``MEMBER``, ``MemberReader``, cell ``MEMBER_CELL``: kind ``scalp``, #706): the
  P(target first) of the scalp kind's ranking member (``krx_quant/scalp.py``: the event this round trip
  is, trained on its own answers; ``ranking.py``), its prediction as the engine displayed it
  (``kind_predictions``). Of several members, the one ranked highest in the kind's latest ranking (its
  skill's lower bound, ``rank`` 1 first; by name on a tie, ``ranked_member``), looked up again at each
  read.

The pick reads the member when the engine's latest ranking of the scalp kind (``rankings``, version
``ranking.RANKING_VERSION``) has one and the policy's levels are the kind's (``differs``: the target's
net, the stop's gross, the deadline and the commission; the kind's are fixed in its answer version 1);
else the pool, and a member passed over for different levels is logged with why. Each signal says where
it was read (``Signal.scalp``: its ``source``, and the member), which each trade keeps
(``trades.scalp_signal``) and the decisions API gives (``scalp_signal``). The two cells' kinds differ,
so a decision of one is never taken for the other's (``store.decided``).

**The entry** (``book.plan_scalp``, ``book.scalp_threshold``): the rise's probability at its threshold or
more buys one share (``Pick.quantity``, as the practice), at or after 09:05, its deadline (the bar close
plus 15 minutes) by 15:15, no special window touching it; no most likely class is asked.

- The pool's up first at ``ScalpRules.threshold`` (``KRX_TRADER_MOCK_SCALP_THRESHOLD``, 0.30). Why 0.30:
  the 4-minute barrier's up first came about one bar close in five on the recorded days (the nightly
  report's barrier calibration, 18% on 2026-11-17's 20 days), so 0.30 asks for about one and a half
  times that, low enough that the mock account trades every day. It is no break-even rate: the pool was
  not trained for this target.
- The member's P(target first) at the break-even rate (``ScalpRules.break_even``) on the bar close's
  mid-point: p* = L / (G + L), G the net gain of a share sold at the target (rounded up to the tick), L
  the net loss of one sold at the stop, each after both commissions and the sell tax (``money.net_of``).
  The setting is not read then. At the defaults L is 0.529% and G +0.2% and what the tick rounding adds,
  so p* is 0.726 at most (no rounding) and lower as the rounding adds: 0.663 at 70,050 won (target
  70,400, +0.269%), 0.609 at 70,000 (+0.340%), 0.627 at 55,000 (55,300). The raised stop and a sale at
  the deadline are left out, as the kind's answer has neither.

**The exits** (``paper.py``, ``book.scalp_levels``, ``book.scalp_raise``): on the fill price, the
target is the price that nets ``ScalpRules.target_percent`` (+0.2%, about +0.43% gross: commission
0.015% a side and the sell tax 0.20%), rounded up to the tick, resting as the take-profit at once after
the fill; the stop ``ScalpRules.stop_percent`` (0.3%) below the fill, gross (about −0.53% net). Once a
bid reaches the price that nets ``ScalpRules.raise_percent`` (+0.1%), the stop is raised to it (kept on
the trade, so a restart goes on with it) and sells from the next quote whose bid is at or below it
(``raised_stop``): a trade that got there leaves with about +0.1% net at least (a mock fill below the
bid aside). At the deadline neither reached sells at market (``time``). The exits are ``take_profit``,
``raised_stop``, ``stop_loss``, ``time``, and the book's own (a pause, a loss limit).

**Its own day** (``book.scalp_held``): its buy orders (``ScalpRules.daily_buys``, 100; 0 for no cap),
counted on the scalp's alone; the book's risk rules count none of them. Since #710 it takes no cooldown
after net losses in a row and no daily loss cap (it had 5,000 won), and its cap may go above 20: the user
wants it to trade all day so that its results can be measured (2026-11-20). The account still holds one
position at a time. The champion's loss limits count none of its P&L,
realized or open, as they count none of the practice's (#533, ``limits.py``); a limit reached holds it
all the same. Its trades are of no policy: no record, promotion or live gate counts them.

**Kept** (``store.py``): each trade with ``trades.scalp`` 1 and its signal (``scalp_signal``: the source
and the member, the rise's probability ``p_up``: the pool's up first or the member's target first, the
threshold it bought at, the levels). Its decisions are the decisions API's ``scalp`` book; the
nightly report gives its section (``report.py``). Telegram's ``[buy]`` and ``[sell]`` (``bought_text``,
``sold_text``), as the verdict's.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from krx_quant import scalp as event
from krx_quant.feeds.base import KST
from krx_quant.pricing import tick_size
from krx_quant.trader import SCALP, SYMBOL
from krx_quant.trader.book import SCALP_MEMBER, Rules, scalp_threshold
from krx_quant.trader.cells import KIND_COLUMNS, Cell, Signal, kind_signal, open_engine_db
from krx_quant.trader.selection import Pick
from krx_quant.trader.store import OpenTrade

log = logging.getLogger(__name__)

# The kind whose pool the first version reads (#705): the 4-minute barrier's; its cell.
KIND = "barrier"
CELL = Cell(KIND, SCALP)
# The first version's source: the 4-minute barrier pool of the ranking's members, in the engine's verdicts.
POOL = "verdict_pool_4m"
# The second's (#706): the scalp kind's ranking member, its P(target first); its cell.
MEMBER = SCALP_MEMBER
MEMBER_CELL = Cell(event.NAME, SCALP)
# The rankings it reads: ``ranking.RANKING_VERSION``'s (a test holds them equal). Not imported: the
# ranking loads every algorithm, seconds the trader has no use for.
RANKING_VERSION = 1


@dataclass(frozen=True)
class Source:
    """Where a pick's scalp signal is read (module doc "The signal"): ``POOL`` or ``MEMBER``, the member
    found in the ranking (None: none), and why the pool when a member was found but not read."""

    name: str
    member: str | None = None
    why: str | None = None

    @property
    def cell(self) -> Cell:
        return MEMBER_CELL if self.name == MEMBER else CELL


def picks(source: Source | None = None) -> tuple[Pick, ...]:
    """The scalp policy's pick (module doc): its one cell, of ``source``'s kind (None: the pool's), of no
    cost map figure, a share at a time."""
    kind = (source or Source(POOL)).cell.kind
    return (Pick(kind, SCALP, SCALP, None, None, 0, None, None, None, None, None, quantity=1),)


def differs(rules: Rules) -> str | None:
    """Why the policy's levels are not the scalp kind's (module doc; the kind's answer version 1): its
    target's net, its stop's gross, its deadline or its commission; None when they are the same."""
    scalp, found = rules.scalp, []
    if not math.isclose(scalp.target_percent, event.TARGET_NET * 100, abs_tol=1e-9):
        found.append(f"target {scalp.target_percent:g}% net, the kind's {event.TARGET_NET * 100:g}%")
    if not math.isclose(scalp.stop_percent, event.STOP_GROSS * 100, abs_tol=1e-9):
        found.append(f"stop {scalp.stop_percent:g}% gross, the kind's {event.STOP_GROSS * 100:g}%")
    if scalp.deadline != timedelta(minutes=event.MINUTES):
        minutes = scalp.deadline.total_seconds() / 60
        found.append(f"deadline {minutes:g} minutes, the kind's {event.MINUTES}")
    if not math.isclose(rules.fee_rate, event.FEE_RATE, abs_tol=1e-12):
        found.append(f"commission {rules.fee_rate:g} a side, the kind's {event.FEE_RATE:g}")
    return "; ".join(found) or None


def ranked_member(db: sqlite3.Connection) -> str | None:
    """The scalp kind's member the policy reads (module doc): of the engine's latest ranking of the kind,
    the member ranked highest (``rank`` 1 first, by name on a tie); None without a member, a ranking or
    the table. A ranking it cannot read has none (logged)."""
    kept = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'rankings'")
    if kept.fetchone() is None:
        return None
    row = db.execute(
        "SELECT night, body FROM rankings WHERE kind = ? AND version = ? ORDER BY night DESC LIMIT 1",
        (event.NAME, RANKING_VERSION),
    ).fetchone()
    if row is None:
        return None
    try:
        members = [r for r in json.loads(row[1])["rows"] if r.get("member")]
        ordered = sorted(members, key=lambda r: (r.get("rank") is None, r.get("rank") or 0, r["algorithm"]))
    except (ValueError, KeyError, TypeError, AttributeError):
        log.warning("trader: scalp: the scalp ranking of %s has a body it cannot read", row[0])
        return None
    return str(ordered[0]["algorithm"]) if ordered else None


def source(engine: Path | None, rules: Rules) -> Source:
    """Where the pick at hand reads the scalp signal (module doc "The signal"): the scalp kind's ranking
    member in the engine's database at ``engine`` (read-only, at once) when there is one and ``rules``'
    levels are the kind's, else the pool; a member passed over for its levels is logged with why."""
    if engine is None or not engine.exists():
        return Source(POOL)
    try:
        db = open_engine_db(engine)
        try:
            member = ranked_member(db)
        finally:
            db.close()
    except sqlite3.Error as e:
        log.warning("trader: scalp: cannot read the scalp ranking (%s): the 4-minute pool's signal", e)
        return Source(POOL)
    if member is None:
        return Source(POOL)
    why = differs(rules)
    if why is not None:
        log.warning(
            "trader: scalp: %s is the scalp kind's ranking member, but the policy's levels are not the"
            " kind's (%s): the 4-minute pool's signal stays",
            member,
            why,
        )
        return Source(POOL, member, why)
    log.info("trader: scalp: reads the scalp kind's ranking member %s, at the break-even rate", member)
    return Source(MEMBER, member)


class Reader(Protocol):
    """A source of scalp signals (module doc "The signal"): the cell they are of, and the new ones since
    the last read, oldest first."""

    @property
    def cell(self) -> Cell: ...

    def read(self, db: sqlite3.Connection) -> list[Signal]: ...


def reader(symbol: str, since: datetime, cell: Cell = CELL) -> Reader:
    """The reader of the scalp's ``cell`` (module doc "The signal"): the member's (``MEMBER_CELL``) or
    the verdicts' 4-minute pool's."""
    return MemberReader(symbol, since) if cell == MEMBER_CELL else PoolReader(symbol, since)


def pool_signal(row: sqlite3.Row, symbol: str) -> Signal:
    """A ``verdicts`` row (``rowid AS id``, ``bar_close``, ``version``, ``made_at``, ``body``) as a scalp
    signal: its 4-minute barrier pool's probabilities (up first, timeout, down first), its anchor the
    pool's barriers' middle (the bar close's mid-point), its tick and width the pool's."""
    body: dict[str, Any] = json.loads(row["body"])
    pooled: Mapping[str, Any] = next((h for h in body.get("horizons", ()) if h.get("kind") == KIND), {})
    p = pooled.get("probabilities")
    target, stop, width = pooled.get("target"), pooled.get("stop"), pooled.get("width_ticks")
    anchor = None if target is None or stop is None else (target + stop) / 2
    return Signal(
        prediction_id=row["id"],
        kind=KIND,
        predictor=SCALP,
        symbol=symbol,
        bar_close=datetime.fromisoformat(row["bar_close"]),
        displayed_at=datetime.fromisoformat(row["made_at"]),
        status="ok" if p is not None else "no_members",
        probabilities=None if p is None else (p["p_up_first"], p["p_timeout"], p["p_down_first"]),
        rise=0,
        anchor=anchor,
        tick=None if anchor is None else float(tick_size(anchor)),
        width=width,
        horizon=timedelta(minutes=pooled.get("minutes") or 4),
        scalp={
            "source": POOL,
            "kind": KIND,
            "verdict_version": int(row["version"]),
            "members": pooled.get("members"),
            "families": pooled.get("families"),
        },
    )


class PoolReader:
    """The verdicts of ``symbol`` kept since the last read, oldest first, each bar close's newest version,
    as scalp signals (``pool_signal``). A database without the table has none."""

    cell = CELL

    def __init__(self, symbol: str, since: datetime) -> None:
        self.symbol = symbol
        self._after = since.astimezone(KST).isoformat()

    def read(self, db: sqlite3.Connection) -> list[Signal]:
        kept = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'verdicts'")
        if kept.fetchone() is None:
            return []
        rows = db.execute(
            "SELECT rowid AS id, bar_close, version, made_at, body FROM verdicts"
            " WHERE symbol = ? AND bar_close > ? ORDER BY bar_close, version",
            (self.symbol, self._after),
        ).fetchall()
        newest: dict[str, sqlite3.Row] = {r["bar_close"]: r for r in rows}
        signals: list[Signal] = []
        for row in newest.values():
            try:
                signals.append(pool_signal(row, self.symbol))
            except (ValueError, KeyError, TypeError):
                log.warning("trader: scalp: verdict %s has a body it cannot read", row["id"])
        if rows:
            self._after = max(self._after, max(r["bar_close"] for r in rows))
        return signals


def member_signal(row: sqlite3.Row, member: str, symbol: str) -> Signal:
    """A scalp kind's prediction row of ``member`` (``cells.KIND_COLUMNS``) as a scalp signal of
    ``MEMBER_CELL``: its three probabilities (target first, timeout, stop first), its anchor and tick the
    bar close's, its width the target's distance from the anchor in ticks (the kind has no barrier width;
    the book asks for one)."""
    found = kind_signal(row, MEMBER_CELL, symbol)
    anchor, tick = found.anchor, found.tick
    width = None
    if anchor and tick:
        width = max(1, round((event.target_price(anchor) - anchor) / tick))
    return replace(
        found,
        width=width,
        scalp={
            "source": MEMBER,
            "kind": event.NAME,
            "member": member,
            "answer_version": event.ANSWER_VERSION,
        },
    )


class MemberReader:
    """The scalp kind's ranking member's predictions of ``symbol`` displayed since the last read, oldest
    first, as scalp signals (``member_signal``); the member looked up at each read (``ranked_member``).
    None while the ranking has no member (logged once)."""

    cell = MEMBER_CELL

    def __init__(self, symbol: str, since: datetime) -> None:
        self.symbol = symbol
        self._after = since.astimezone(KST).isoformat()
        self._missing = False

    def read(self, db: sqlite3.Connection) -> list[Signal]:
        member = ranked_member(db)
        if member is None:
            if not self._missing:
                log.warning("trader: scalp: the scalp ranking has no member now: no scalp signal")
            self._missing = True
            return []
        self._missing = False
        rows = db.execute(
            f"SELECT {KIND_COLUMNS} FROM kind_predictions p"
            " WHERE p.kind = ? AND p.symbol = ? AND p.algorithm = ? AND p.bar_close > ?"
            " ORDER BY p.bar_close, p.id",
            (event.NAME, self.symbol, member, self._after),
        ).fetchall()
        signals = [member_signal(r, member, self.symbol) for r in rows]
        if rows:
            self._after = max(self._after, max(r["bar_close"] for r in rows))
        return signals


def opened(signal: Signal, rules: Rules, entry_price: float) -> dict[str, Any]:
    """What a scalp trade keeps of its signal and rules (``trades.scalp_signal``): the source (and the
    member), the rise's probability and the threshold it bought at (``book.scalp_threshold``), the levels
    on its fill price and the deadline's minutes."""
    scalp = rules.scalp
    return {
        **(signal.scalp or {}),
        "p_up": signal.rise_probability,
        "threshold": scalp_threshold(signal, rules),
        "target_percent": scalp.target_percent,
        "raise_percent": scalp.raise_percent,
        "stop_percent": scalp.stop_percent,
        "raise_at": scalp.raise_at(entry_price, rules.fee_rate),
        "deadline_minutes": scalp.deadline.total_seconds() / 60,
    }


# Telegram's and the report's words for a scalp trade's exit.
NAMED = f"삼성전자({SYMBOL})"
EXITS = {
    "take_profit": "목표가 도달",
    "raised_stop": "올린 손절가 도달",
    "stop_loss": "손절가 도달",
    "time": "마감 시한",
    "daily_limit": "하루 손실 한도",
    "cumulative_limit": "누적 손실 한도",
    "pause": "정지(즉시 매도)",
}


def _won(value: float) -> str:
    return f"{value:,.0f}원"


def bought_text(mode: str, trade: OpenTrade) -> str:
    """The ``[buy]`` message of a scalp trade bought: the symbol, the shares, the average price, the
    target, the stop and the deadline. No account or order number."""
    return (
        f"[buy] 스캘프 매수({mode}): {NAMED} {trade.quantity}주 {_won(trade.entry_price)} · "
        f"목표가 {_won(trade.target)} · 손절가 {_won(trade.stop)} · "
        f"마감 {trade.time_barrier.astimezone(KST):%H:%M}"
    )


def sold_text(mode: str, trade: OpenTrade, reason: str, net: float) -> str:
    """The ``[sell]`` message of a scalp trade sold whole: the symbol, the shares, why and its net after
    costs. No account or order number."""
    return (
        f"[sell] 스캘프 매도({mode}): {NAMED} {trade.quantity}주 · {EXITS.get(reason, reason)} · "
        f"순손익 {net:+,.0f}원"
    )
