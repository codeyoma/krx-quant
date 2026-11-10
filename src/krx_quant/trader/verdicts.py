"""The verdict policy (#677): the mock book trades on the engine's combined verdict (#676,
``krx_quant.verdict``), the user's decision of 2026-11-10: in the mock account at once, not paper first.

**What.** With ``KRX_TRADER_MOCK_VERDICT`` on (the default in mock mode; mock mode alone), at a pick
that read the cost map and the champion (none yet counts as read) but found no champion cell to trade
(``NO_PASS``, ``NOT_IN_POLICY``, ``NO_CHAMPION``: as the practice, #427), the champion's KIS book trades
the verdict too (``buyable.verdicted``). Its picks are one a barrier horizon of the verdict (``KINDS``:
4, 15, 30 and 60 minutes), the cells (kind, ``verdict``), ranked first, before the practice cell if one
is practised. Once a pick finds a champion cell again it is picked no more: the champion trades as
before.

**The signal** (``VerdictReader``): each verdict of 005930 the engine kept (``verdicts``, the newest
version of a bar close), read as the trader reads a prediction (``cells.Signal``): its displayed time
is when the engine made it (``made_at``), its kind the verdict's horizon (a WAIT without one, the
first horizon's), its probabilities that horizon's pool, and its ``call`` the verdict
(``Call``): BUY, SELL or WAIT, its version, the target, the stop and the deadline.

**The rules** (``book.plan_verdict``, ``paper.py``): a BUY buys at or after 09:05, its target, stop
and time barrier the verdict's (the deadline: its bar close plus the horizon), by 15:15 as every
position; it is sized by the loss budget (#471, ``money.size``), not a share. A WAIT buys nothing
(``verdict_wait``), nor a SELL (``verdict_sell``). A SELL whose meaning is exit, while the book holds a
trade the verdict bought, sells it at the next quote's bid (exit reason ``verdict_sell``); a practice
trade held is sold by its own barriers alone. Every other rule is the book's: one position at a time,
the special windows, the stall, the pauses, the loss limits (a verdict trade is the trader's own and
counts in them, unlike the practice's, #533), the risk rules (the day's 20 buy orders are the book's,
the practice's and the verdict's together) and the practice's spacing, counted from the bar close of
the day's last verdict buy (#598, ``book.spaced``).

**Kept** (``store.py``): each trade it opens with its verdict's version (``trades.verdict_version``)
and the verdict that opened it (``trades.verdict``, JSON: ``Call.opened``), of no policy
(``policy_id`` null: no record, promotion or live gate counts it), so that its trades can be scored
per version. Each verdict read is a decision of the book (``decisions``, the API's ``verdict`` book).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from krx_quant.barriers import CLASSES as BARRIER_CLASSES
from krx_quant.feeds.base import KST
from krx_quant.kind_registry import KINDS as ALL_KINDS
from krx_quant.pricing import tick_size
from krx_quant.trader import VERDICT
from krx_quant.trader.cells import Cell, Signal
from krx_quant.trader.selection import Pick
from krx_quant.verdict import BUY, EXIT, SELL

log = logging.getLogger(__name__)

# The verdict's horizons (#676): the barrier kinds, shortest first; its cells, one a horizon.
KINDS = tuple(k.name for k in sorted(ALL_KINDS, key=lambda k: k.horizon) if k.classes == BARRIER_CLASSES)
CELLS = tuple(Cell(kind, VERDICT) for kind in KINDS)


def picks() -> tuple[Pick, ...]:
    """The verdict policy's picks (module doc): one a horizon, of no cost map figure, sized by the loss
    budget."""
    return tuple(Pick.unmapped(kind, VERDICT, VERDICT) for kind in KINDS)


@dataclass(frozen=True)
class Call:
    """A verdict as the trader acts on it: its rules' version, BUY, SELL or WAIT, a SELL's meaning
    (exit, avoid) and a WAIT's reason, and a BUY's target, stop and deadline; ``body`` the verdict as
    the engine kept it."""

    version: int
    action: str
    meaning: str | None
    reason: str | None
    target: float | None
    stop: float | None
    deadline: datetime | None
    body: Mapping[str, Any]

    @property
    def buys(self) -> bool:
        return self.action == BUY

    @property
    def sells(self) -> bool:
        return self.action == SELL

    @property
    def exits(self) -> bool:
        """A SELL while the trader held the symbol: get out."""
        return self.action == SELL and self.meaning == EXIT

    def opened(self) -> dict[str, Any]:
        """What a trade it opens keeps of it (``trades.verdict``): the verdict without its members and
        side panel, its horizon's pool with its expected net. No more than the trade itself shows."""
        body = self.body
        horizon = body.get("horizon") or {}
        pooled: Mapping[str, Any] = next(
            (h for h in body.get("horizons", ()) if h.get("kind") == horizon.get("kind")), {}
        )
        return {
            "version": self.version,
            "bar_close": body.get("bar_close"),
            "verdict": self.action,
            "strength": body.get("strength"),
            "margin": body.get("margin"),
            "horizon": horizon or None,
            "deadline": body.get("deadline"),
            "probabilities": pooled.get("probabilities"),
            "families": pooled.get("families"),
            "members": pooled.get("members"),
            "expected_net": pooled.get("expected_net"),
            "expected_net_ticks": pooled.get("expected_net_ticks"),
        }


def _time(text: str | None) -> datetime | None:
    return None if text is None else datetime.fromisoformat(text)


def verdict_signal(row: sqlite3.Row, symbol: str) -> Signal:
    """A ``verdicts`` row (``rowid AS id``, ``bar_close``, ``version``, ``made_at``, ``body``) as the
    trader reads it (module doc)."""
    body: dict[str, Any] = json.loads(row["body"])
    horizon = body.get("horizon") or {}
    kind = horizon.get("kind") or KINDS[0]
    pooled: Mapping[str, Any] = next((h for h in body.get("horizons", ()) if h.get("kind") == kind), {})
    p = pooled.get("probabilities")
    target, stop, width = pooled.get("target"), pooled.get("stop"), pooled.get("width_ticks")
    anchor = None if target is None or stop is None else (target + stop) / 2
    action = body["verdict"]
    return Signal(
        prediction_id=row["id"],
        kind=kind,
        predictor=VERDICT,
        symbol=symbol,
        bar_close=datetime.fromisoformat(row["bar_close"]),
        displayed_at=datetime.fromisoformat(row["made_at"]),
        status="ok",
        probabilities=None if p is None else (p["p_up_first"], p["p_timeout"], p["p_down_first"]),
        rise=0,  # the pool's order: up first, timeout, down first
        anchor=anchor,
        tick=None if anchor is None else float(tick_size(anchor)),
        width=width,
        horizon=timedelta(minutes=horizon.get("minutes") or 0),
        call=Call(
            version=int(row["version"]),
            action=action,
            meaning=body.get("meaning"),
            reason=body.get("reason"),
            target=body.get("target") if action == BUY else None,
            stop=body.get("stop") if action == BUY else None,
            deadline=_time(body.get("deadline")),
            body=body,
        ),
    )


class VerdictReader:
    """The engine's verdicts of ``symbol`` kept since the last read, oldest first: of each bar close the
    newest version's. A database without the table (an engine from before #676) has none."""

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
        newest: dict[str, sqlite3.Row] = {r["bar_close"]: r for r in rows}  # the last version wins
        signals: list[Signal] = []
        for row in newest.values():
            try:
                signals.append(verdict_signal(row, self.symbol))
            except (ValueError, KeyError, TypeError):
                log.warning("trader: verdict %s has a body it cannot read", row["id"])
        if rows:
            self._after = max(self._after, max(r["bar_close"] for r in rows))
        return signals
