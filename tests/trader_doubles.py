"""Stored predictions, fake quote streams and a driver for the trader's tests (#394).

The engine's predictions are written into a real engine database (``PredictionStore``), as the
engine displays them, and the trader reads them there (``cells.CellReader``); the quotes are made
up, received in the order given. Every price is made up.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from krx_quant.baselines import Forecast
from krx_quant.consensus import FINAL
from krx_quant.feeds.base import KST
from krx_quant.market_calendar import MarketCalendar
from krx_quant.predictions import DB_NAME as ENGINE_DB
from krx_quant.predictions import NewKindPrediction, NewPrediction, PredictionStore
from krx_quant.trader import SYMBOL
from krx_quant.trader.cells import Cell, CellReader
from krx_quant.trader.paper import PaperTrader, Rules
from krx_quant.trader.quotes import BestQuote
from krx_quant.trader.store import DB_NAME, TraderStore, read_trades

DAY = (2026, 10, 14)  # a Wednesday: the calendar guesses it trades, 09:00 to 15:30
CALENDAR = MarketCalendar()
FEE = 0.00015  # 0.015% a side
BARRIER = Cell("barrier", "breiman")
DIRECTION = Cell("direction", FINAL)


def at(hms: str) -> datetime:
    """A time of the test day, ``HH:MM:SS`` with an optional fraction."""
    whole, _, fraction = hms.partition(".")
    h, m, s = (int(x) for x in whole.split(":"))
    micro = int((fraction + "000000")[:6]) if fraction else 0
    return datetime(*DAY, h, m, s, micro, tzinfo=KST)


def quote(hms: str, bid: float, ask: float) -> BestQuote:
    return BestQuote(at(hms), bid, ask, 10.0, 10.0)


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
class Read:
    """The engine displays ``shown``; the trader reads its database at ``hms``."""

    hms: str
    shown: Sequence[Barrier | Direction] = ()

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


Event = Read | Tick | BestQuote


def _time(event: Event) -> datetime:
    return event.received_at if isinstance(event, BestQuote) else event.time


@dataclass
class Day:
    """A trading day of the trader on ``state``: an engine database it reads, its own store."""

    state: Path
    cell: Cell = BARRIER
    rules: Rules = field(default_factory=lambda: Rules(fee_rate=FEE))
    started_at: datetime = field(default_factory=lambda: at("09:00:00"))

    def run(self, events: Sequence[Event]) -> list[dict[str, Any]]:
        """Each event in time order (the clock ticking at each), into a trader started at
        ``started_at``; the trades kept, newest first."""
        engine = PredictionStore(self.state / ENGINE_DB)
        store = TraderStore(self.state / DB_NAME)
        try:
            trader = PaperTrader(store, self.rules, CALENDAR.session, self.started_at)
            reader = CellReader(
                self.state / ENGINE_DB, self.cell, SYMBOL, self.started_at - timedelta(minutes=2)
            )
            for event in sorted(events, key=_time):
                trader.on_time(_time(event))
                if isinstance(event, BestQuote):
                    trader.on_quote(event)
                elif isinstance(event, Read):
                    for shown in event.shown:
                        shown.write(engine)
                    for signal in reader.new():
                        trader.on_signal(signal, event.time)
        finally:
            store.close()
            engine.close()
        return self.trades()

    def trades(self) -> list[dict[str, Any]]:
        return read_trades(self.state / DB_NAME, None, 1000)

    def decisions(self) -> list[dict[str, Any]]:
        with closing(sqlite3.connect(self.state / DB_NAME)) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute("SELECT * FROM decisions ORDER BY id")]
