"""What to do now (#672, spec #671): one card a symbol the trader trades, ``GET /v1/action``.

The user asked to see, simply, whether to buy now and when to sell (2026-11-10). The one rule that
buys and sells net of costs is the trader's, so the card follows **the champion's book** and says
what it does, in any mode (paper, mock, live): one word, ``wait``, ``buy`` or ``sell``, what to do
now, with a line of Korean and, while a trade is open or just closed, its prices and times. The
practice (#427, a cell frozen for its review cycle that does not beat its costs), the shadow (#405)
and the challengers (#400) are never read: their trades are not the champion's.

- ``buy``: the champion's trade entered at most ``SHOWN`` ago (``bought``): the price it bought at,
  its target, stop and time barrier (``plan``), and its cell's record (``record``).
- ``sell``: the champion's open trade is being sold (``selling``: a stop, its time, a pause or a loss
  limit began its sale), or it exited at most ``SHOWN`` ago (its exit's reason: ``take_profit``,
  ``stop_loss``, ``time``, ``pause``, ``daily_limit``, ``cumulative_limit``; ``exit``).
- ``wait``: anything else, and why, the first that holds: the trader is not running
  (``trader_off``); it holds the champion's trade (``holding``, with the plan); outside the hours it
  buys in, 09:05 to 15:15 (``outside_hours``); it buys nothing new (``stopped``: a pause, a loss
  limit, a failed order); its champion has no cell (``no_cell``) or none could be read
  (``no_policy``); a risk rule holds new buys (``held``: the losses in a row, the day's buy orders);
  else no signal now (``no_signal``).

The record is the nightly fit's for the cell at its θ (``PolicyCell.fitted["signals"]``, #470): the
signals of its recorded days bought alone a share, how many bought and netted above 0, and the net a
signal (the day-level mean and its 95% lower bound), in ticks and in won at the trade's tick or the
symbol's now.

Read-only: the trader's state (``store.read_trader``), its last trades (``store.read_trades``) and
the champion policy (``policy.champion``), none of which this writes.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from krx_quant.feeds.base import KST
from krx_quant.market_calendar import Session
from krx_quant.trader import ENTRY_AFTER_OPEN, EXIT_BEFORE_AUCTION, SYMBOL
from krx_quant.trader import policy as policies
from krx_quant.trader.db import iso, reading
from krx_quant.trader.limits import won
from krx_quant.trader.store import read_trader, read_trades

# The three words (spec #671 2).
WAIT, BUY, SELL = "wait", "buy", "sell"
ACTIONS = (WAIT, BUY, SELL)

# How long a buy and a sale are shown after they happen: until the next bar's decision.
SHOWN = timedelta(minutes=1)

# Why the card says what it says.
BOUGHT, SELLING = "bought", "selling"
TAKE_PROFIT, STOP_LOSS, TIME = "take_profit", "stop_loss", "time"
PAUSE, DAILY_LIMIT, CUMULATIVE_LIMIT = "pause", "daily_limit", "cumulative_limit"
TRADER_OFF, HOLDING, OUTSIDE_HOURS, STOPPED = "trader_off", "holding", "outside_hours", "stopped"
NO_CELL, NO_POLICY, HELD, NO_SIGNAL = "no_cell", "no_policy", "held", "no_signal"

# The trades read: the latest are enough, the trader holding one position at a time (a mock trade's
# paper shadow and the practice's among them).
_TRADES_READ = 20
# ``read_trades`` reads those entered before its time: one a microsecond on reads one entered at ``now``.
_AT_NOW = timedelta(microseconds=1)
# The trader's statuses that buy nothing new (``GET /v1/trader``'s ``status``), in Korean.
_STOPPED = {
    "entries_paused": "새 매수를 정지해 두었습니다(서버에서 재개합니다).",
    "paused": "정지해 두었습니다(서버에서 재개합니다).",
    "order_failed": "오늘 주문이 실패해 오늘은 새로 사지 않습니다.",
    "daily_limit": "하루 손실 한도에 닿아 오늘은 새로 사지 않습니다.",
    "cumulative_limit": "누적 손실 한도에 닿아 다시 켤 때까지 사지 않습니다.",
}
# Why a trade was sold, in Korean.
_SOLD = {
    TAKE_PROFIT: "이익 실현 가격에 닿았습니다",
    STOP_LOSS: "손절 가격에 닿았습니다",
    TIME: "팔 시각이 되었습니다",
    PAUSE: "즉시 매도 정지를 요청했습니다",
    DAILY_LIMIT: "하루 손실 한도에 닿았습니다",
    CUMULATIVE_LIMIT: "누적 손실 한도에 닿았습니다",
    SELLING: "팔고 있습니다",
}
_SAID = {
    TRADER_OFF: "자동 매매가 멈춰 있어 판단하지 않습니다.",
    OUTSIDE_HOURS: "장중(09:05–15:15)에만 판단합니다.",
    NO_CELL: "비용을 빼고도 이익이 난다고 검증된 신호가 아직 없습니다. 엔진이 밤마다 다시 찾습니다.",
    NO_POLICY: "자동 매매의 정책을 읽지 못해 사지 않습니다.",
    NO_SIGNAL: "지금은 신호가 없습니다.",
}


def read_action(
    path: Path | None,
    now: datetime,
    sessions: Callable[[date], Session | None],
    names: Mapping[str, str | None],
    ticks: Mapping[str, float | None],
) -> dict[str, Any]:
    """``GET /v1/action``: the trader's mode and whether it runs, and a card for each symbol it trades
    (module doc), named by ``names`` and its won counted at ``ticks`` (the symbol's tick now, when no
    trade gives one), at ``now`` on the session ``sessions`` gives that day. Read-only."""
    trader = read_trader(path, now, sessions)
    trades: list[dict[str, Any]] = []
    champion: policies.Policy | None = None
    unreadable = False
    if path is not None:
        # Those entered by ``now`` (a replayed store holds later ones), at ``now`` itself too.
        trades = read_trades(path, now + _AT_NOW, _TRADES_READ)
        try:
            champion = _champion(path)
        except policies.PolicyError:
            unreadable = True
    session = sessions(now.astimezone(KST).date())
    return {
        "as_of": iso(now),
        "mode": trader["mode"],
        "alive": trader["alive"],
        "symbols": [
            card(
                SYMBOL,
                names.get(SYMBOL),
                trader=trader,
                trades=trades,
                champion=champion,
                unreadable=unreadable,
                now=now,
                session=session,
                tick=ticks.get(SYMBOL),
            )
        ],
    }


def _champion(path: Path) -> policies.Policy | None:
    """The champion policy in the store at ``path``; None without a store, a policies table or a
    champion. ``PolicyError`` when it cannot be read."""
    with reading(path) as db:
        if db is None:
            return None
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not {"policies", "champions"} <= tables:
            return None
        try:
            return policies.champion(db)
        except sqlite3.Error as e:  # a store from before a column the reader asks for
            raise policies.PolicyError(str(e)) from e


def card(
    symbol: str,
    name: str | None,
    *,
    trader: Mapping[str, Any],
    trades: Sequence[Mapping[str, Any]],
    champion: policies.Policy | None,
    unreadable: bool,
    now: datetime,
    session: Session | None,
    tick: float | None,
) -> dict[str, Any]:
    """The card of ``symbol`` (module doc), from the trader's state (``GET /v1/trader``), its last
    trades newest first (``GET /v1/trader/trades``), its champion policy (None: none, or
    ``unreadable``) at ``now``, on that day's ``session`` (None: not a trading day)."""
    trade = _last_champion_trade(trades, symbol, now, trader.get("mode"))
    closed = (
        trade is not None and trade["exit"] is not None and datetime.fromisoformat(trade["exit"]["at"]) <= now
    )
    open_trade = trade if trade is not None and not closed else None
    position = trader.get("position") or {}
    if open_trade is not None and position.get("trade_id") != open_trade["id"]:
        position = {}
    out: dict[str, Any] = {
        "symbol": symbol,
        "name": name,
        "action": WAIT,
        "why": None,
        "said": None,
        "since": None,
        "until": None,
        "plan": None,
        "exit": None,
        "record": None,
    }
    cell = _cell(champion, trade) if trade is not None else _first_cell(champion)
    out["record"] = _record(cell, trade["tick"] if trade is not None else tick)
    if open_trade is not None:
        out["plan"] = _plan(open_trade, position)
        entered = datetime.fromisoformat(open_trade["entry"]["at"])
        if position.get("selling"):
            why = _selling_why(trader, open_trade, position, now)
            said = (
                "자동 매매가 팔고 있습니다."
                if why == SELLING
                else f"자동 매매가 팔고 있습니다: {_SOLD[why]}."
            )
            return out | _said(SELL, why, said)
        sell_by = datetime.fromisoformat(open_trade["time_barrier"])
        if trader["alive"] and now - entered < SHOWN:
            said = bought_said(entered, open_trade["entry"]["price"], sell_by)
            return out | _said(BUY, BOUGHT, said, since=entered, until=entered + SHOWN)
        if not trader["alive"]:
            return out | _said(WAIT, TRADER_OFF, _SAID[TRADER_OFF] + " 들고 있는 것은 아래 계획대로 팝니다.")
        return out | _said(WAIT, HOLDING, f"자동 매매가 들고 있습니다. {_sells_at(sell_by)}", since=entered)
    if trade is not None and closed:
        sold = trade["exit"]
        exited = datetime.fromisoformat(sold["at"])
        if now - exited < SHOWN:
            reason = sold["reason"] if sold["reason"] in _SOLD else SELLING
            out["plan"] = _plan(trade, {})
            out["exit"] = {"reason": sold["reason"], "price": sold["price"], "at": iso(exited)}
            said = sold_said(exited, sold["price"], reason)
            return out | _said(SELL, reason, said, since=exited, until=exited + SHOWN)
    if not trader["alive"]:
        return out | _said(WAIT, TRADER_OFF, _SAID[TRADER_OFF])
    if session is None or not _in_hours(session, now):
        return out | _said(WAIT, OUTSIDE_HOURS, _SAID[OUTSIDE_HOURS])
    status = trader.get("status")
    if status in _STOPPED:
        return out | _said(WAIT, STOPPED, _STOPPED[status])
    if unreadable:
        return out | _said(WAIT, NO_POLICY, _SAID[NO_POLICY])
    if champion is None or not champion.cells:
        return out | _said(WAIT, NO_CELL, _SAID[NO_CELL])
    held = _held(trader.get("risk"))
    if held is not None:
        return out | _said(WAIT, HELD, held)
    return out | _said(WAIT, NO_SIGNAL, _SAID[NO_SIGNAL])


def bought_said(entered: datetime, price: float, sell_by: datetime) -> str:
    """A buy of the champion's in a line of Korean: when and at what it bought, and when it sells."""
    return f"자동 매매가 {_clock(entered, seconds=True)}에 {won(price)}에 샀습니다. {_sells_at(sell_by)}"


def sold_said(exited: datetime, price: float | None, reason: str) -> str:
    """A sale of the champion's in a line of Korean: when, at what, and why (``reason``, one of the
    exit reasons; any other is said as a sale)."""
    at_price = "" if price is None else f" {won(price)}에"
    said = f"자동 매매가 {_clock(exited, seconds=True)}에{at_price} 팔았습니다"
    return f"{said}." if reason == SELLING or reason not in _SOLD else f"{said}: {_SOLD[reason]}."


def sold_reason_said(reason: str) -> str:
    """Why a sale was made, in Korean (``reason``, one of the exit reasons)."""
    return _SOLD.get(reason, _SOLD[SELLING])


def _sells_at(sell_by: datetime) -> str:
    return f"아래 가격에 닿거나 {_clock(sell_by)}이 되면 팝니다."


def _said(
    action: str, why: str, said: str, *, since: datetime | None = None, until: datetime | None = None
) -> dict[str, Any]:
    return {
        "action": action,
        "why": why,
        "said": said,
        "since": None if since is None else iso(since),
        "until": None if until is None else iso(until),
    }


def _last_champion_trade(
    trades: Sequence[Mapping[str, Any]], symbol: str, now: datetime, mode: str | None
) -> Mapping[str, Any] | None:
    """The newest of ``trades`` (newest first) that is the champion's own: neither a practice trade
    nor a shadow's, of ``symbol``, of the ``mode`` the trader runs in (a trade another mode left open
    is not this book's: the trader keeps a position a mode; any mode while it never wrote its state),
    entered by ``now`` (a replayed store may hold later ones; one exited after ``now`` is open then)."""
    for t in trades:
        if t.get("practice") or t.get("shadow") or t["symbol"] != symbol:
            continue
        if mode is not None and t.get("mode") != mode:
            continue
        if datetime.fromisoformat(t["entry"]["at"]) > now:
            continue
        return t
    return None


def _plan(trade: Mapping[str, Any], position: Mapping[str, Any]) -> dict[str, Any]:
    """A trade's prices and times: what it bought at and when, its target, stop and time barrier, and
    the best bid the trader valued it at (an open trade's, else null)."""
    return {
        "entry_price": trade["entry"]["price"],
        "entry_at": trade["entry"]["at"],
        "target": trade["target"],
        "stop": trade["stop"],
        "sell_by": trade["time_barrier"],
        "bid": position.get("bid"),
        "bid_at": position.get("bid_at"),
    }


def _selling_why(
    trader: Mapping[str, Any], trade: Mapping[str, Any], position: Mapping[str, Any], now: datetime
) -> str:
    """Why an open trade is being sold, as far as the state tells: a loss limit or a pause in force,
    else its time barrier passed, else its stop reached by the bid; ``selling`` when none tells."""
    status = trader.get("status")
    if status in (DAILY_LIMIT, CUMULATIVE_LIMIT):
        return str(status)
    if status == "paused":
        return PAUSE
    if now >= datetime.fromisoformat(trade["time_barrier"]):
        return TIME
    bid = position.get("bid")
    if isinstance(bid, int | float) and bid <= trade["stop"]:
        return STOP_LOSS
    return SELLING


def _in_hours(session: Session, now: datetime) -> bool:
    """Whether ``now`` is in the hours the trader buys in: from five minutes after the open to five
    minutes before the closing auction (09:05 to 15:15)."""
    return session.open + ENTRY_AFTER_OPEN <= now < session.closing_auction - EXIT_BEFORE_AUCTION


def _held(risk: Any) -> str | None:
    """What holds new buys by the risk rules (#471), in Korean; None when nothing does."""
    if not isinstance(risk, Mapping):
        return None
    held = risk.get("held")
    if held == "cooldown":
        until = risk.get("cooldown_until")
        if isinstance(until, str):
            return f"손실이 연달아 {_clock(datetime.fromisoformat(until))}까지 쉽니다."
        return "손실이 연달아 잠시 쉽니다."
    if held == "buy_limit":
        return "오늘 살 수 있는 횟수를 다 썼습니다."
    return None


def _cell(champion: policies.Policy | None, trade: Mapping[str, Any]) -> policies.PolicyCell | None:
    """The champion's cell a trade was of; None when the champion has none of its (kind, predictor)."""
    if champion is None:
        return None
    for c in champion.cells:
        if (c.kind, c.predictor) == (trade["kind"], trade["predictor"]):
            return c
    return None


def _first_cell(champion: policies.Policy | None) -> policies.PolicyCell | None:
    if champion is None or not champion.cells:
        return None
    return champion.cells[0]


def _record(cell: policies.PolicyCell | None, tick: float | None) -> dict[str, Any] | None:
    """A cell's record at its θ from the nightly fit (module doc); None without one."""
    if cell is None:
        return None
    signals = cell.fitted.get("signals")
    if not isinstance(signals, Mapping):
        return None
    at = signals.get(f"{cell.threshold:g}")
    if not isinstance(at, Mapping):
        return None
    interval = at.get("per_signal") if isinstance(at.get("per_signal"), Mapping) else None
    value = None if interval is None else interval.get("value")
    low = None if interval is None else interval.get("low")
    return {
        "kind": cell.kind,
        "predictor": cell.predictor,
        "threshold": cell.threshold,
        "days": None if interval is None else interval.get("days"),
        "signals": at.get("signals"),
        "bought": at.get("bought"),
        "wins": at.get("wins"),
        "per_signal_ticks": value,
        "low_ticks": low,
        "tick": tick,
        "per_signal_won": _won(value, tick),
        "low_won": _won(low, tick),
    }


def _won(ticks: Any, tick: float | None) -> float | None:
    if not isinstance(ticks, int | float) or isinstance(ticks, bool) or tick is None:
        return None
    return round(ticks * tick, 1)


def _clock(at: datetime, *, seconds: bool = False) -> str:
    return at.astimezone(KST).strftime("%H:%M:%S" if seconds else "%H:%M")
