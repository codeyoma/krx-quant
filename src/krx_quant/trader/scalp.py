"""The scalp policy (#705): the mock book trades a short, fixed round trip often enough to measure, the
user's decisions of 2026-11-18.

**What.** With ``KRX_TRADER_MOCK_SCALP`` on (the default in mock mode; mock mode alone), on the picks the
practice and the verdict take (a cost map and a champion read, no champion cell to trade,
``buyable.scalped``), the champion's KIS book follows the scalp's one cell too (``CELL``: the 4-minute
barrier kind, predictor ``scalp``), ranked after the verdict's horizons and before the practice cell.
The KIS book stays the one owner of the account's orders: one position at a time, so at most one open
scalp of the symbol.

**The signal** (``reader``, the one seam): the first version (``PoolReader``, ``POOL``) reads the up
first of the 4-minute barrier horizon's pool in each verdict the engine kept (``verdicts``: the newest
version of a bar close; v2 pools the night ranking's members alone, today Breiman), its displayed time
the verdict's ``made_at``. Those algorithms were trained for the 4-minute barrier, not for this round
trip, and the report says so. #706 trains an algorithm for the scalp's own event; once that is a ranking
member, ``reader`` returns a reader of it instead, and nothing else here changes. Each signal says where
it was read (``Signal.scalp``), which each trade keeps (``trades.scalp_signal``).

**The entry** (``book.plan_scalp``): the up first at ``ScalpRules.threshold`` or more
(``KRX_TRADER_MOCK_SCALP_THRESHOLD``, 0.30) buys one share (``Pick.quantity``, as the practice), at or
after 09:05, its deadline (the bar close plus 15 minutes) by 15:15, no special window touching it; no
most likely class is asked. Why 0.30: the 4-minute barrier's up first came about one bar close in five
on the recorded days (the nightly report's barrier calibration, 18% on 2026-11-17's 20 days), so 0.30
asks for about one and a half times that, low enough that the mock account trades every day. It is no
break-even rate: the pool was not trained for this target (above), and #706 sets the threshold from the
target's and the stop's payoffs once its own algorithm is ranked.

**The exits** (``paper.py``, ``book.scalp_levels``, ``book.scalp_raise``): on the fill price, the
target is the price that nets ``ScalpRules.target_percent`` (+0.2%, about +0.43% gross: commission
0.015% a side and the sell tax 0.20%), rounded up to the tick, resting as the take-profit at once after
the fill; the stop ``ScalpRules.stop_percent`` (0.3%) below the fill, gross (about −0.53% net). Once a
bid reaches the price that nets ``ScalpRules.raise_percent`` (+0.1%), the stop is raised to it (kept on
the trade, so a restart goes on with it) and sells from the next quote whose bid is at or below it
(``raised_stop``): a trade that got there leaves with about +0.1% net at least (a mock fill below the
bid aside). At the deadline neither reached sells at market (``time``). The exits are ``take_profit``,
``raised_stop``, ``stop_loss``, ``time``, and the book's own (a pause, a loss limit).

**Its own day** (``book.scalp_held``): its buy orders (``ScalpRules.daily_buys``, 20), its net losses in
a row and their cooldown (the book's ``KRX_TRADER_LOSS_STREAK`` and ``KRX_TRADER_COOLDOWN_MINUTES``),
and its net loss of the day (``ScalpRules.daily_loss``, 5,000 won), each counted on the scalp's trades
alone; the book's risk rules count none of them. The champion's loss limits count none of its P&L,
realized or open, as they count none of the practice's (#533, ``limits.py``); a limit reached holds it
all the same. Its trades are of no policy: no record, promotion or live gate counts them.

**Kept** (``store.py``): each trade with ``trades.scalp`` 1 and its signal (``scalp_signal``: the source,
the up first, the threshold, the levels). Its decisions are the decisions API's ``scalp`` book; the
nightly report gives its section (``report.py``). Telegram's ``[buy]`` and ``[sell]`` (``bought_text``,
``sold_text``), as the verdict's.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Protocol

from krx_quant.feeds.base import KST
from krx_quant.pricing import tick_size
from krx_quant.trader import SCALP, SYMBOL
from krx_quant.trader.book import Rules
from krx_quant.trader.cells import Cell, Signal
from krx_quant.trader.selection import Pick
from krx_quant.trader.store import OpenTrade

log = logging.getLogger(__name__)

# The kind whose pool the first version reads (#705): the 4-minute barrier's; the scalp's cell.
KIND = "barrier"
CELL = Cell(KIND, SCALP)
# The first version's source: the 4-minute barrier pool of the ranking's members, in the engine's verdicts.
POOL = "verdict_pool_4m"


def picks() -> tuple[Pick, ...]:
    """The scalp policy's pick (module doc): its one cell, of no cost map figure, a share at a time."""
    return (Pick(KIND, SCALP, SCALP, None, None, 0, None, None, None, None, None, quantity=1),)


class Reader(Protocol):
    """A source of scalp signals (module doc "The signal"): the cell they are of, and the new ones since
    the last read, oldest first."""

    @property
    def cell(self) -> Cell: ...

    def read(self, db: sqlite3.Connection) -> list[Signal]: ...


def reader(symbol: str, since: datetime) -> Reader:
    """The scalp's signal source (the one seam, module doc): today the verdicts' 4-minute pool. #706
    switches it to its own kind's ranking member once there is one."""
    return PoolReader(symbol, since)


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


def opened(signal: Signal, rules: Rules, entry_price: float) -> dict[str, Any]:
    """What a scalp trade keeps of its signal and rules (``trades.scalp_signal``): the source, the up
    first and the threshold, the levels on its fill price and the deadline's minutes."""
    scalp = rules.scalp
    return {
        **(signal.scalp or {}),
        "p_up": signal.rise_probability,
        "threshold": scalp.threshold,
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
