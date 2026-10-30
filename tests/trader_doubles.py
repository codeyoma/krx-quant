"""Stored predictions, picks, fake quote streams and a driver for the trader's tests (#394, #395,
#396, #400).

The engine's predictions and special windows are written into a real engine database
(``PredictionStore``), as the engine displays and learns them, and the trader reads them there
(``cells.EngineReader``); the picks are made as the cost map would pass them; the quotes are made
up, received in the order given; the pauses are written into the trader's store as the Engine API
writes them, the resumes and re-arms as ``krx-quant trade`` does. With ``Day.desk`` the trader is
the whole desk (#400: the champion's book and each challenger running, the policies kept in the
store as the nightly re-fit keeps them, ``keep``). Every price and hit rate is made up.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from krx_quant.baselines import Forecast
from krx_quant.consensus import FINAL
from krx_quant.feeds.base import KST
from krx_quant.market_calendar import MarketCalendar
from krx_quant.predictions import DB_NAME as ENGINE_DB
from krx_quant.predictions import NewKindPrediction, NewPrediction, PredictionStore
from krx_quant.special import Kind as SpecialKind
from krx_quant.special import Window
from krx_quant.trader import KELLY_FRACTION, SYMBOL
from krx_quant.trader import policy as policies
from krx_quant.trader.cells import Cell, EngineReader
from krx_quant.trader.desk import Desk
from krx_quant.trader.limits import ask
from krx_quant.trader.paper import PaperTrader, Rules
from krx_quant.trader.quotes import BestQuote
from krx_quant.trader.selection import Pick, Selection, kelly
from krx_quant.trader.store import (
    DB_NAME,
    REARM,
    RESUME,
    Control,
    TraderStore,
    read_days,
    read_trader,
    read_trades,
    request_pause,
)

DAY = (2026, 10, 14)  # a Wednesday: the calendar guesses it trades, 09:00 to 15:30
CALENDAR = MarketCalendar()
FEE = 0.00015  # 0.015% a side
BARRIER = Cell("barrier", "breiman")
DIRECTION = Cell("direction", FINAL)


def at(hms: str) -> datetime:
    """A time of the test day, ``HH:MM:SS`` with an optional fraction; of another day with its date
    before it (``2026-10-15 10:00:00``)."""
    day = DAY
    if " " in hms:
        ymd, hms = hms.split(" ")
        y, mo, d = (int(x) for x in ymd.split("-"))
        day = (y, mo, d)
    whole, _, fraction = hms.partition(".")
    h, m, s = (int(x) for x in whole.split(":"))
    micro = int((fraction + "000000")[:6]) if fraction else 0
    return datetime(*day, h, m, s, micro, tzinfo=KST)


def quote(hms: str, bid: float, ask: float) -> BestQuote:
    return BestQuote(at(hms), bid, ask, 10.0, 10.0)


def pick(
    cell: Cell,
    role: str = "top",
    low: float = 0.9,
    p_star: float = 0.6,
    fraction: float | None = None,
    width: float = 10.0,
    cost: float = 2.6,
) -> Pick:
    """A cell the cost map passes: by default its lower bound well above p*, its Kelly far above 1,
    the whole capital. ``fraction`` sets the share of the capital it buys instead."""
    f = kelly(low, width, cost, 100, 70_050)
    assert f is not None
    cut = min(1.0, max(0.0, KELLY_FRACTION * f))
    return Pick(
        kind=cell.kind,
        role=role,
        predictor=cell.predictor,
        p_star=p_star,
        low=low,
        calls=300,
        width=width,
        cost=cost,
        reference_price=70_050,
        tick=100,
        kelly=f,
        fraction=cut if fraction is None else fraction,
    )


@dataclass(frozen=True)
class Barrier:
    """A barrier prediction the engine displays: of ``kind`` (the 4, 15 or 30 minutes)."""

    bar_close: str
    p: tuple[float, float, float] = (0.7, 0.2, 0.1)  # up_first, timeout, down_first
    anchor: float | None = 70_050.0  # between 70,000 and 70,100: the mid-point
    tick: int = 100
    w: int = 2
    kind: str = "barrier"
    predictor: str = BARRIER.predictor
    shown: float = 2.0  # seconds after the bar close it is displayed
    status: str = "ok"

    def write(self, store: PredictionStore) -> None:
        close = at(self.bar_close)
        output = dict(zip(("p_up_first", "p_timeout", "p_down_first"), self.p, strict=True))
        store.add_kind_predictions(
            self.kind,
            [
                NewKindPrediction(
                    symbol=SYMBOL,
                    algorithm=self.predictor,
                    model_version="v1",
                    bar_close=close,
                    displayed_at=close + timedelta(seconds=self.shown),
                    output=output if self.status == "ok" else None,
                    status=self.status,
                    flat_ticks=2,
                    width_ticks=self.w,
                    anchor=self.anchor,
                    tick=self.tick if self.anchor is not None else None,
                )
            ],
        )


@dataclass(frozen=True)
class Direction:
    """A direction prediction the engine displays (the final consensus by default)."""

    bar_close: str
    p: tuple[float, float, float] = (0.7, 0.2, 0.1)  # up, flat, down
    mid: float | None = 70_050.0
    k: int = 2
    predictor: str = DIRECTION.predictor
    shown: float = 2.0
    blocked: str | None = None

    def write(self, store: PredictionStore) -> None:
        close = at(self.bar_close)
        store.add_all(
            [
                NewPrediction(
                    symbol=SYMBOL,
                    algorithm=self.predictor,
                    model_version="v1",
                    bar_close=close,
                    displayed_at=close + timedelta(seconds=self.shown),
                    forecast=Forecast(*self.p, expected_ticks=0.0),
                    target_at=close + timedelta(minutes=4),
                    mid_close=self.mid,
                    mid_display=self.mid,
                    flat_ticks=self.k,
                    blocked=self.blocked,
                )
            ]
        )


@dataclass(frozen=True)
class Special:
    """A special window the engine learns at its start: a VI of ``symbol`` (005930 by default); a
    circuit breaker's or a sidecar's is the whole market's."""

    start: str
    end: str
    kind: SpecialKind = SpecialKind.VI
    symbol: str = SYMBOL

    def write(self, store: PredictionStore) -> None:
        symbol = self.symbol if self.kind is SpecialKind.VI else None
        store.keep_special(Window(self.kind, symbol, at(self.start), at(self.end)), at(self.start))


@dataclass(frozen=True)
class Read:
    """The engine displays ``shown`` (or learns a special window); the trader reads its database
    at ``hms``."""

    hms: str
    shown: Sequence[Barrier | Direction | Special] = ()

    @property
    def time(self) -> datetime:
        return at(self.hms)


@dataclass(frozen=True)
class Tick:
    """The trader's clock at ``hms``, without a quote."""

    hms: str

    @property
    def time(self) -> datetime:
        return at(self.hms)


@dataclass(frozen=True)
class AskPause:
    """The screen's pause at ``hms`` (#396), written as the Engine API writes it; the trader reads
    it then."""

    hms: str
    sell_now: bool = False

    @property
    def time(self) -> datetime:
        return at(self.hms)


@dataclass(frozen=True)
class AskResume:
    """``krx-quant trade resume`` at ``hms``; the trader reads it then."""

    hms: str

    @property
    def time(self) -> datetime:
        return at(self.hms)


@dataclass(frozen=True)
class AskRearm:
    """``krx-quant trade rearm`` at ``hms``; the trader reads it then."""

    hms: str

    @property
    def time(self) -> datetime:
        return at(self.hms)


@dataclass(frozen=True)
class Repick:
    """The trader's pick at ``hms`` (its 08:30 one, #395): the same cells passing, under the policies
    kept then (#400)."""

    hms: str

    @property
    def time(self) -> datetime:
        return at(self.hms)


Asked = AskPause | AskResume | AskRearm
Event = Read | Tick | BestQuote | Asked | Repick


def _time(event: Event) -> datetime:
    return event.received_at if isinstance(event, BestQuote) else event.time


@dataclass
class Day:
    """A trading day of the trader on ``state``: an engine database it reads, its own store. It
    trades ``picks``, in that rank (by default ``cell``'s alone, passing well)."""

    state: Path
    cell: Cell = BARRIER
    rules: Rules = field(default_factory=lambda: Rules(fee_rate=FEE))
    started_at: datetime = field(default_factory=lambda: at("09:00:00"))
    picks: Sequence[Pick] | None = None
    selection: Selection | None = None  # the picks as chosen from a cost map, ``picks`` unless given
    notices: list[str] = field(default_factory=list)  # sent to Telegram (#396)
    # False: a quote reaches the trader before its clock moves to the quote's time (a process
    # suspended overnight wakes to a quote first).
    clock_first: bool = True
    asked: list[Control | str] = field(default_factory=list)  # each resume and re-arm, or why none
    # The whole desk (#400): the champion's book and the challengers' under the policies kept in the
    # store, ``picks`` being the cells the cost map passes; else the champion's book alone, trading
    # ``selection`` or ``picks`` as given.
    desk: bool = False
    # The desk's watch of the quotes (#407): off unless a test sets it, since the made-up quotes come
    # seconds or minutes apart, where 005930's stream brings several a second.
    stall_after: timedelta | None = None

    def run(self, events: Sequence[Event]) -> list[dict[str, Any]]:
        """Each event in time order (the clock ticking at each), into a trader started at
        ``started_at`` with the picks, its state written after the last; the champion's trades kept,
        newest first."""
        engine = PredictionStore(self.state / ENGINE_DB)
        store = TraderStore(self.state / DB_NAME)
        try:
            trader: Desk | PaperTrader
            if self.desk:
                trader = Desk(
                    store,
                    self.rules,
                    CALENDAR.session,
                    self.started_at,
                    notify=self.notices.append,
                    stall_after=self.stall_after,
                )
            else:
                trader = PaperTrader(
                    store, self.rules, CALENDAR.session, self.started_at, notify=self.notices.append
                )
            picks = tuple(self.picks) if self.picks is not None else (pick(self.cell),)
            chosen = self.selection or Selection(picks)
            trader.select(chosen, self.started_at)
            reader = EngineReader(self.state / ENGINE_DB, SYMBOL)
            reader.follow(trader.cells, self.started_at - timedelta(minutes=2))
            for event in sorted(events, key=_time):
                if self.clock_first or not isinstance(event, BestQuote):
                    trader.on_time(_time(event))
                if isinstance(event, Repick):
                    trader.select(chosen, event.time)
                    reader.follow(trader.cells, event.time - timedelta(minutes=2))
                elif isinstance(event, BestQuote):
                    trader.on_quote(event)
                elif isinstance(event, Read):
                    for shown in event.shown:
                        shown.write(engine)
                    read = reader.read(at("00:00:00"))
                    assert read is not None
                    windows, signals = read
                    trader.on_special(windows)
                    for signal in signals:
                        trader.on_signal(signal, event.time)
                elif isinstance(event, AskPause):
                    request_pause(self.state / DB_NAME, event.sell_now, event.time)
                    trader.on_controls(event.time)
                elif isinstance(event, AskResume | AskRearm):
                    server = TraderStore(self.state / DB_NAME)  # as krx-quant trade does
                    try:
                        action = RESUME if isinstance(event, AskResume) else REARM
                        self.asked.append(ask(server, action, trader.mode, event.time))
                    finally:
                        server.close()
                    trader.on_controls(event.time)
            if events:
                trader.write(max(_time(e) for e in events))
        finally:
            store.close()
            engine.close()
        return self.trades()

    def state_now(self, hms: str) -> dict[str, Any]:
        """``GET /v1/trader`` at ``hms``."""
        return read_trader(self.state / DB_NAME, at(hms))

    def days(self) -> list[dict[str, Any]]:
        """``GET /v1/trader/days`` of the test month."""
        return read_days(self.state / DB_NAME, date(2026, 10, 1), date(2026, 10, 31))

    def trades(self, policy: int | None = None) -> list[dict[str, Any]]:
        """The champion's trades, or ``policy``'s (#400), newest first."""
        return read_trades(self.state / DB_NAME, None, 1000, policy)

    def decisions(self) -> list[dict[str, Any]]:
        return self._rows("decisions")

    def selections(self) -> list[dict[str, Any]]:
        return self._rows("selections")

    def _rows(self, table: str) -> list[dict[str, Any]]:
        with closing(sqlite3.connect(self.state / DB_NAME)) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY id")]


def keep(state: Path, made: datetime, *cells: policies.PolicyCell, reason: str = "밤 맞추기") -> int:
    """A policy version kept at ``made`` as the nightly re-fit keeps it (``policy.add``): the first
    the champion, a later one a challenger (#400: it runs from the trading day after ``made``'s
    pick). Its version."""
    db = policies.connect(state / DB_NAME)
    try:
        version, _ = policies.add(db, made, reason, cells, [], {"grid": {}})
    finally:
        db.close()
    return version


def policy_cell(
    of: Cell, threshold: float = 0.6, kelly_fraction: float = 0.5, role: str = "top"
) -> policies.PolicyCell:
    """A cell of a policy at ``threshold`` and ``kelly_fraction``, fitted as the re-fit keeps it."""
    fitted = {"p_star": 0.63, "low": 0.7, "kelly": 10.5, "fraction": 1.0, "days": [], "read": {}}
    fitted |= {"trades": 24, "wins": 12, "net": 0.0, "growth": 0.0}
    return policies.PolicyCell(of.kind, role, of.predictor, threshold, kelly_fraction, fitted)


# The cost map (``GET /v1/cost-map``, #307) as the Engine API gives it: made-up rows within the /v1
# schema (``cost_map``), each verdict as given.

HORIZONS = {"direction": 4, "barrier": 4, "barrier_15m": 15, "barrier_30m": 30, "barrier_60m": 60}


def predictor(
    algorithm: str, low: float, verdict: str | dict[str, str] | None, calls: int = 300, rank: int | None = 1
) -> dict[str, Any]:
    """A predictor on a row: its hit rate's lower bound ``low``; ``verdict`` both costs' or each."""
    verdicts = {"limit": verdict, "market": verdict} if isinstance(verdict, str) else verdict
    return {
        "algorithm": algorithm,
        "rank": rank,
        "calls": calls,
        "precision": {"value": min(1.0, low + 0.05), "low": low, "high": min(1.0, low + 0.1), "n": calls},
        "edge": 0.3,
        "pnl": None,
        "pnl_inverse": None,
        "verdict": verdicts,
    }


def cost_row(
    kind: str,
    *,
    width: float = 10.0,
    cost: float = 2.6,
    price: float = 70_050.0,
    tick: int = 100,
    consensus: dict[str, Any] | None = None,
    top: dict[str, Any] | None = None,
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A kind's row: W and the market cost C in ticks at ``price`` (the limit one a tick less)."""
    limit = cost - 1
    daily = kind not in HORIZONS
    breakeven = {
        "limit": {"c_over_w": limit / width, "p_star": (width + limit) / (2 * width)},
        "market": {"c_over_w": cost / width, "p_star": (width + cost) / (2 * width)},
    }
    return {
        "kind": kind,
        "title": kind,
        "horizon_minutes": HORIZONS.get(kind),
        "rule": "made up",
        "days": ["2026-10-13", "2026-10-14"],
        "unit": "bp" if daily else "ticks",
        "bar_closes": 380,
        "width": width,
        "reference_price": price,
        "tick": None if daily else tick,
        "cost": {"limit": limit, "market": cost},
        "breakeven": None if daily else breakeven,
        "breakeven_missing": "대칭 장벽이 아님: 보합 폭은 거래를 닫는 자리가 아니다" if daily else None,
        "consensus": consensus,
        "top": top,
        "baseline": baseline,
    }


def cost_map(rows: Sequence[dict[str, Any]], symbol: str = SYMBOL, period: str = "20d") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "period": period,
        "special": "exclude",
        "as_of": "2026-10-14T08:30:00+09:00",
        "min_sample": 200,
        "fee_rate": FEE,
        "spread_ticks": 1,
        "symbols": [],
        "rows": list(rows),
        "regimes": None,
        "regimes_missing": "not computed yet",
    }
