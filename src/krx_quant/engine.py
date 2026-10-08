"""The prediction engine: events in, predictions and scores out, on the Session Clock.

Live and replay drive the same two calls. Events go to ``on_event`` as they are received;
``run_due`` handles every bar close and scoring whose time has come on the clock. At a
bar close in the prediction window, from what was received before it:

- the baselines predict every symbol, at once;
- the Feature Store computes every declared feature once per symbol, and each algorithm
  predicts every symbol from its own features, in a worker thread so recording never
  waits. Each algorithm's predictions are displayed when it finishes. One that takes
  longer than its time budget is skipped for that bar close (``timed_out``), and so is
  each later bar close until it finishes. When the algorithms' expected work exceeds the
  CPU budget, the last registered are skipped first and the skip is recorded (``runs``).

Four minutes later each prediction is scored on both bases.

The prediction window is continuous trading, from the first bar close after the open
until the last bar close whose target (t + 4 minutes) is still in continuous trading:
09:01–15:16 on a normal day.
"""

from __future__ import annotations

import asyncio
import heapq
import logging
import math
import time as timer
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from functools import partial
from typing import Any

from krx_quant.algorithms import ALGORITHMS, Algorithm, Model, register
from krx_quant.baselines import BASELINES, Forecast, Inputs
from krx_quant.clock import SessionClock
from krx_quant.features import FeatureStore, FeatureValue, FeatureVector, Snapshot
from krx_quant.feeds import BOOK, BOOK_SNAPSHOT, KRX_TRADE, TRADE
from krx_quant.feeds.base import KST
from krx_quant.market_calendar import MarketCalendar, Session
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


@dataclass(frozen=True)
class Budget:
    per_algorithm: float = 30.0  # seconds one algorithm may take for a bar close, all symbols
    cpu: float = 30.0  # seconds of expected algorithm work per bar close, all together


# How much one bar close's measured time moves an algorithm's expected time.
_COST_WEIGHT = 0.3


@dataclass(frozen=True)
class _AtClose:
    """What is fixed for a symbol at a bar close."""

    mid: float | None
    features: Mapping[str, FeatureValue]


def prediction_window(session: Session) -> tuple[datetime, datetime]:
    """The first and last bar close predicted in a session."""
    return session.open + BAR, session.closing_auction - HORIZON


class PredictionEngine:
    def __init__(
        self,
        store: PredictionStore,
        calendar: MarketCalendar,
        symbols: Sequence[str],
        fee_rate: float,
        clock: SessionClock,
        *,
        algorithms: Sequence[Algorithm] = ALGORITHMS,
        models: Mapping[str, Model] | None = None,
        budget: Budget | None = None,
        feature_store: FeatureStore | None = None,
    ) -> None:
        self.store = store
        self.calendar = calendar
        self.symbols = tuple(symbols)
        self.fee_rate = fee_rate
        self.clock = clock
        self.algorithms = register(algorithms)
        given = models or {}
        self.models = {a.name: given.get(a.name) or a.default_model() for a in self.algorithms}
        self._budget = budget or Budget()
        self._features = feature_store or FeatureStore()
        self._feature_names = sorted({f for a in self.algorithms for f in a.features})
        self._cost: dict[str, float] = {}  # expected seconds per bar close, by algorithm
        self._busy: set[str] = set()  # algorithms still working past their budget
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

    async def run_due(self) -> None:
        """Score and predict everything due by now, in time order."""
        now = self.clock.now()
        while (due := self.next_due()) is not None and due <= now:
            self._score_until(due)
            if due == self._next_bar_close(self._last_tick):
                self._last_tick = due
                await self._predict(due)

    def bars(self, symbol: str) -> list[Bar]:
        return self._bars.closed(symbol, self.clock.now())

    def mid(self, symbol: str) -> float | None:
        return self._reference.mid(symbol, self.clock.now())

    def _next_bar_close(self, after: datetime) -> datetime | None:
        """The first bar close in a prediction window strictly after ``after``."""
        local = after.astimezone(KST)
        day = local.date()
        for _ in range(_LOOKAHEAD_DAYS):
            session = self.calendar.session(day)
            if session is not None:
                first, last = prediction_window(session)
                candidate = max(first, local.replace(second=0, microsecond=0) + BAR)
                if candidate <= last:
                    return candidate
            day += timedelta(days=1)
            local = datetime.combine(day, time(), KST)
        return None

    async def _predict(self, bar_close: datetime) -> None:
        day_start = datetime.combine(bar_close.date(), time(), KST)
        at_close: dict[str, _AtClose] = {}
        baselines: dict[tuple[str, str], dict[str, Forecast | None]] = {}
        for symbol in self.symbols:
            mid = self._reference.mid(symbol, bar_close)
            k = flat_ticks(mid, self.fee_rate) if mid is not None else None
            tick = tick_size(mid) if mid is not None else None
            snapshot = Snapshot(symbol, bar_close, self._bars.closed(symbol, bar_close), mid, tick, k)
            at_close[symbol] = _AtClose(mid, self._features.compute(snapshot, self._feature_names))
            inputs = Inputs(
                symbol=symbol,
                bar_close=bar_close,
                mid=mid,
                mid_before=self._reference.mid(symbol, bar_close - HORIZON),
                flat_ticks=k,
                tick=tick,
                day_classes=self.store.day_classes(symbol, day_start, bar_close),
            )
            for b in BASELINES:
                baselines.setdefault((b.name, b.version), {})[symbol] = b.predict(inputs)
        self._record(
            bar_close, at_close, [(name, version, made, "ok") for (name, version), made in baselines.items()]
        )
        chosen = self._within_cpu_budget(bar_close)
        await asyncio.gather(*(self._run(a, bar_close, at_close) for a in chosen))

    def _within_cpu_budget(self, bar_close: datetime) -> list[Algorithm]:
        """The algorithms to run: the last registered drop out while the expected work is over budget."""
        chosen = list(self.algorithms)
        while chosen and sum(self._cost.get(a.name, 0.0) for a in chosen) > self._budget.cpu:
            dropped = chosen.pop()
            detail = (
                f"expected {self._cost.get(dropped.name, 0.0):.3f}s; budget {self._budget.cpu}s per bar close"
            )
            log.warning("%s skipped at %s for the CPU budget (%s)", dropped.name, bar_close, detail)
            self.store.add_run("cpu_budget", bar_close, dropped.name, detail)
        return chosen

    async def _run(self, algorithm: Algorithm, bar_close: datetime, at_close: Mapping[str, _AtClose]) -> None:
        name, model = algorithm.name, self.models[algorithm.name]
        none: dict[str, Forecast | None] = dict.fromkeys(at_close)
        if name in self._busy:
            self._record(bar_close, at_close, [(name, model.version, none, "timed_out")])
            return
        vectors = {s: FeatureVector(c.features, algorithm.features) for s, c in at_close.items()}
        started = timer.perf_counter()
        work = asyncio.ensure_future(asyncio.to_thread(_predict_symbols, algorithm, model, vectors))
        try:
            made: dict[str, Forecast | None] = dict(
                await asyncio.wait_for(asyncio.shield(work), self._budget.per_algorithm)
            )
        except TimeoutError:
            log.warning("%s took over %ss at %s; skipped", name, self._budget.per_algorithm, bar_close)
            self._busy.add(name)
            work.add_done_callback(partial(self._finished_late, name))
            self._cost[name] = max(self._cost.get(name, 0.0), self._budget.per_algorithm)
            self._record(bar_close, at_close, [(name, model.version, none, "timed_out")])
            return
        except Exception:
            log.exception("%s failed at %s", name, bar_close)
            self._record(bar_close, at_close, [(name, model.version, none, "error")])
            return
        elapsed = timer.perf_counter() - started
        previous = self._cost.get(name)
        self._cost[name] = (
            elapsed if previous is None else (1 - _COST_WEIGHT) * previous + _COST_WEIGHT * elapsed
        )
        self._record(bar_close, at_close, [(name, model.version, made, "ok")])

    def _finished_late(self, name: str, work: asyncio.Future[dict[str, Forecast]]) -> None:
        self._busy.discard(name)
        if not work.cancelled() and work.exception() is not None:
            log.error("%s failed after its budget: %r", name, work.exception())

    def _record(
        self,
        bar_close: datetime,
        at_close: Mapping[str, _AtClose],
        made: Sequence[tuple[str, str, Mapping[str, Forecast | None], str]],
    ) -> None:
        """Store (name, model version, forecasts by symbol, status) entries as displayed now."""
        displayed_at = self.clock.now()
        mid_display = {symbol: self._reference.mid(symbol, displayed_at) for symbol in at_close}
        predictions = []
        for name, version, forecasts, status in made:
            for symbol, forecast in forecasts.items():
                mid = at_close[symbol].mid
                # k comes from the bar close's mid-point, or the display's if that is all there is.
                reference = mid if mid is not None else mid_display[symbol]
                predictions.append(
                    NewPrediction(
                        symbol=symbol,
                        algorithm=name,
                        model_version=version,
                        bar_close=bar_close,
                        displayed_at=displayed_at,
                        forecast=forecast,
                        target_at=bar_close + HORIZON,
                        mid_close=mid,
                        mid_display=mid_display[symbol],
                        flat_ticks=flat_ticks(reference, self.fee_rate) if reference is not None else None,
                        status=status,
                    )
                )
        for pending in self.store.add_all(predictions):
            heapq.heappush(self._pending, (pending.target_at, pending.prediction_id, pending))

    def _score_until(self, due: datetime) -> None:
        scores: list[tuple[int, Score]] = []
        while self._pending and self._pending[0][0] <= due:
            _, _, pending = heapq.heappop(self._pending)
            mid_target = self._reference.mid(pending.symbol, pending.target_at)
            scores.append((pending.prediction_id, _score(pending, mid_target)))
        if scores:
            self.store.score_all(scores, self.clock.now())


def _predict_symbols(
    algorithm: Algorithm, model: Model, vectors: Mapping[str, FeatureVector]
) -> dict[str, Forecast]:
    """Runs in a worker thread: one algorithm on every symbol, each forecast checked."""
    forecasts = {}
    for symbol, features in vectors.items():
        f = algorithm.predict(features, model)
        probabilities = (f.p_up, f.p_flat, f.p_down)
        if not all(0 <= p <= 1 for p in probabilities) or not math.isclose(
            sum(probabilities), 1, abs_tol=1e-6
        ):
            raise ValueError(f"{algorithm.name} gave probabilities {probabilities} for {symbol}")
        forecasts[symbol] = f
    return forecasts


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
        try:
            due = engine.next_due()
            if due is None:
                await asyncio.sleep(3600)
                continue
            await clock.sleep_until(due)
            await engine.run_due()
        except Exception:
            log.exception("prediction run failed")
            await asyncio.sleep(1)  # do not spin on a failure that repeats
