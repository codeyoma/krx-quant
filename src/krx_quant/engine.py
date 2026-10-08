"""The prediction engine: events in, predictions and scores out, on the Session Clock.

Live and replay drive the same two calls. Events go to ``on_event`` as they are received;
``run_due`` handles every bar close and scoring whose time has come on the clock. At a
bar close in the prediction window each baseline predicts each symbol from what was
received before it; four minutes later each prediction is scored on both bases.

The prediction window is continuous trading, from the first bar close after the open
until the last bar close whose target (t + 4 minutes) is still in continuous trading:
09:01–15:16 on a normal day.
"""

from __future__ import annotations

import asyncio
import heapq
import logging
from collections.abc import Sequence
from datetime import datetime, time, timedelta
from typing import Any

from krx_quant.baselines import BASELINES, Baseline, Inputs
from krx_quant.clock import SessionClock
from krx_quant.feeds import BOOK, BOOK_SNAPSHOT, KRX_TRADE, TRADE
from krx_quant.feeds.base import KST
from krx_quant.market_calendar import MarketCalendar
from krx_quant.predictions import NewPrediction, Pending, PredictionStore, Score
from krx_quant.pricing import Direction, classify, flat_ticks, tick_size
from krx_quant.reference import BAR, Bar, BarBuilder, ReferencePrice, Source

log = logging.getLogger(__name__)

Event = tuple[str, dict[str, Any]]  # (feed TR ID, normalized row)

HORIZON = timedelta(minutes=4)
# How far ahead to look for the next session (long holidays included).
_LOOKAHEAD_DAYS = 15

# The feeds the engine reads, and the columns it uses from each.
ENGINE_FEEDS: dict[str, tuple[str, ...]] = {
    # Integrated trades make the bars; their best quotes are integrated quotes too.
    TRADE.tr_id: ("symbol", "event_time", "received_at", "price", "qty", "ask1", "bid1"),
    BOOK.tr_id: ("symbol", "received_at", "ask_px_1", "bid_px_1"),
    # The REST book snapshot replaces the integrated book when it is cut by the session limit.
    BOOK_SNAPSHOT.tr_id: ("symbol", "received_at", "ask_px_1", "bid_px_1"),
    # KRX-only trades carry KRX's best quotes, for the 30 seconds before NXT opens.
    KRX_TRADE.tr_id: ("symbol", "received_at", "ask1", "bid1"),
}


class PredictionEngine:
    def __init__(
        self,
        store: PredictionStore,
        calendar: MarketCalendar,
        symbols: Sequence[str],
        fee_rate: float,
        clock: SessionClock,
        baselines: Sequence[Baseline] = BASELINES,
    ) -> None:
        self.store = store
        self.calendar = calendar
        self.symbols = tuple(symbols)
        self.fee_rate = fee_rate
        self.clock = clock
        self._baselines = tuple(baselines)
        self._reference = ReferencePrice(calendar)
        self._bars = BarBuilder()
        self._last_tick = clock.now()
        # Outcomes waiting for their target time: (target_at, prediction_id, pending).
        self._pending: list[tuple[datetime, int, Pending]] = []
        for p in store.pending():
            heapq.heappush(self._pending, (p.target_at, p.prediction_id, p))

    def on_event(self, feed: str, row: dict[str, Any]) -> None:
        symbol = row["symbol"]
        if symbol not in self.symbols or feed not in ENGINE_FEEDS:
            return
        received_at = row["received_at"]
        if feed == TRADE.tr_id:
            self._bars.trade(symbol, row["event_time"], row["price"], row["qty"])
            self._reference.quote(symbol, Source.INTEGRATED, received_at, row["bid1"], row["ask1"])
        elif feed == KRX_TRADE.tr_id:
            self._reference.quote(symbol, Source.KRX, received_at, row["bid1"], row["ask1"])
        else:
            self._reference.quote(symbol, Source.INTEGRATED, received_at, row["bid_px_1"], row["ask_px_1"])

    def next_due(self) -> datetime | None:
        """When ``run_due`` next has work: a bar close to predict or an outcome to score."""
        candidates = [self._next_bar_close(self._last_tick)]
        if self._pending:
            candidates.append(self._pending[0][0])
        due = [c for c in candidates if c is not None]
        return min(due) if due else None

    def run_due(self) -> None:
        """Score and predict everything due by now, in time order."""
        now = self.clock.now()
        while (due := self.next_due()) is not None and due <= now:
            self._score_until(due)
            if due == self._next_bar_close(self._last_tick):
                self._last_tick = due
                self._predict(due)

    def bars(self, symbol: str) -> list[Bar]:
        return self._bars.closed(symbol, self.clock.now())

    def mid(self, symbol: str) -> float | None:
        return self._reference.mid(symbol, self.clock.now())

    def flat_ticks(self, price: float) -> int:
        return flat_ticks(price, self.fee_rate)

    def _next_bar_close(self, after: datetime) -> datetime | None:
        """The first bar close in a prediction window strictly after ``after``."""
        local = after.astimezone(KST)
        day = local.date()
        for _ in range(_LOOKAHEAD_DAYS):
            session = self.calendar.session(day)
            if session is not None:
                first = session.open + BAR
                last = session.closing_auction - HORIZON
                candidate = max(first, local.replace(second=0, microsecond=0) + BAR)
                if candidate <= last:
                    return candidate
            day += timedelta(days=1)
            local = datetime.combine(day, time(), KST)
        return None

    def _predict(self, bar_close: datetime) -> None:
        day_start = datetime.combine(bar_close.date(), time(), KST)
        for symbol in self.symbols:
            mid = self._reference.mid(symbol, bar_close)
            k = flat_ticks(mid, self.fee_rate) if mid is not None else None
            inputs = Inputs(
                symbol=symbol,
                bar_close=bar_close,
                mid=mid,
                mid_before=self._reference.mid(symbol, bar_close - HORIZON),
                flat_ticks=k,
                tick=tick_size(mid) if mid is not None else None,
                day_classes=self.store.day_classes(symbol, day_start, bar_close),
            )
            for baseline in self._baselines:
                forecast = baseline.predict(inputs)
                displayed_at = self.clock.now()
                mid_display = self._reference.mid(symbol, displayed_at)
                if k is None and mid_display is not None:
                    k = flat_ticks(mid_display, self.fee_rate)
                pending = self.store.add(
                    NewPrediction(
                        symbol=symbol,
                        algorithm=baseline.name,
                        model_version=baseline.version,
                        bar_close=bar_close,
                        displayed_at=displayed_at,
                        forecast=forecast,
                        target_at=bar_close + HORIZON,
                        mid_close=mid,
                        mid_display=mid_display,
                        flat_ticks=k,
                    )
                )
                heapq.heappush(self._pending, (pending.target_at, pending.prediction_id, pending))

    def _score_until(self, due: datetime) -> None:
        while self._pending and self._pending[0][0] <= due:
            _, _, pending = heapq.heappop(self._pending)
            mid_target = self._reference.mid(pending.symbol, pending.target_at)
            self.store.score(pending.prediction_id, _score(pending, mid_target), self.clock.now())


def _score(p: Pending, mid_target: float | None) -> Score:
    """Classes on both bases; a missing mid-point leaves its basis unscored, with the reason."""
    missing = [
        name
        for name, value in (("close", p.mid_close), ("display", p.mid_display), ("target", mid_target))
        if value is None
    ]
    reference = p.mid_close if p.mid_close is not None else p.mid_display

    def direction(start: float | None) -> Direction | None:
        if start is None or mid_target is None or reference is None or p.flat_ticks is None:
            return None
        return classify(mid_target - start, p.flat_ticks, tick_size(reference))

    return Score(
        mid_target=mid_target,
        class_a=direction(p.mid_close),
        class_b=direction(p.mid_display),
        unscorable=f"missing mid: {', '.join(missing)}" if missing else None,
    )


async def run_live(engine: PredictionEngine, clock: SessionClock) -> None:
    """Predict and score on the system clock until cancelled; a failure is logged, not fatal."""
    while True:
        due = engine.next_due()
        if due is None:
            await asyncio.sleep(3600)
            continue
        await clock.sleep_until(due)
        try:
            engine.run_due()
        except Exception:
            log.exception("prediction run failed")
