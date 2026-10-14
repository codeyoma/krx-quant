"""The prediction engine: events in, predictions and scores out, on the Session Clock.

Live and replay drive the same two calls. Events go to ``on_event`` as they are received;
``run_due`` handles every bar close and scoring whose time has come on the clock. At a
bar close in the prediction window, from what was received before it:

- the baselines predict every symbol, at once;
- the Feature Store computes every declared feature once per symbol, and each algorithm
  predicts every symbol from its own features, in a worker thread so recording never
  waits. Each algorithm's predictions are displayed when it finishes. One that takes
  longer than its time budget is skipped for that bar close (``timed_out``), and so is
  each later bar close until it finishes. A symbol an algorithm declines for missing data
  (``NotPredicted``) is stored with the reason as its status (``no_book``), unscored.
  When the algorithms' expected work exceeds the CPU budget, the last registered are
  skipped first and the skip is recorded (``runs``); each skip halves the skipped one's
  expected time, so it is soon run (and timed) again.

Four minutes later each prediction is scored on both bases.

Two consensus predictions follow (``consensus.py``): the fast one from the active
algorithms done 2 seconds after they started, the final one once they are all done.

After each day's close (+10 minutes) the algorithms' statuses are reviewed by the
lifecycle rules (``lifecycle.py``), the day's predictions lost to the time and CPU budgets
are told once, a retired algorithm is no longer run, and the
consensus weights are refitted. Where each algorithm on probation or on the bench stands
toward its next review (``stages``, #165) is worked out by the same rules, as of that review.

Live, a new algorithm runs only once ``admits`` lets it (its pre-validation passed,
``prevalidation.py``): from the next bar close it starts on probation. Until then it is
not run, listed or reviewed; the first review after the engine starts alerts it once.
An algorithm the engine has already enrolled always runs.

The symbols' bars and reference prices, and the bars of the series that may lead them
(``features.Leaders``: futures, indices, US daytime trades, ETFs), come from the Market
View (``market_view.py``) as they stood at the bar close. Training on a recorded day goes
through the same view.

Special situations (``special.py``): a symbol in a VI at a bar close is not predicted
(``vi``), and a prediction whose four minutes a VI, circuit breaker or sidecar touches is
special, marked when it is made or as soon as the situation is learned.

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
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from functools import partial
from typing import Any

from krx_quant.algorithms import ALGORITHMS, Abstention, Algorithm, Model, NotPredicted, register
from krx_quant.baselines import BASELINES, Forecast, Inputs
from krx_quant.clock import SessionClock
from krx_quant.config import MAX_REVIEW_DAY, Settings
from krx_quant.consensus import FAST, FINAL, NAMES, NO_WEIGHTS, SPANS, Timing, Weights, pool
from krx_quant.consensus import fit as fit_weights
from krx_quant.features import REGIME_CODES, FeatureStore, FeatureVector, Leaders, Value
from krx_quant.feeds import (
    BOOK,
    BOOK_SNAPSHOT,
    FOREIGN_ESTIMATE,
    INDEX,
    INDEX_REST,
    KOSPI200_FUTURES,
    KRX_TRADE,
    MEMBER,
    MEMBER_REST,
    NXT_TRADE,
    OPTION,
    PROGRAM,
    TRADE,
    US_TRADE,
    USD_FUTURES,
    VI_STATUS,
)
from krx_quant.feeds.base import KST, LEVELS
from krx_quant.lifecycle import Rules, Stage, Standing, Status, review
from krx_quant.lifecycle import stages as stages_now
from krx_quant.market_calendar import MarketCalendar
from krx_quant.market_view import (
    FUTURES_SERIES,
    HORIZON,
    INDEX_FEEDS,
    LOOKAHEAD_DAYS,
    MarketView,
    next_bar_close,
)
from krx_quant.predictions import NewPrediction, Pending, PredictionStore, Score
from krx_quant.pricing import Direction, classify, flat_ticks, most_likely, tick_size
from krx_quant.reference import Bar
from krx_quant.signals import SENT, Blocked, Judged, Signals
from krx_quant.special import KOSPI, Kind, SpecialSituations, Window, in_vi, touching
from krx_quant.stats import DEFAULT_MIN_SAMPLE

log = logging.getLogger(__name__)

Event = tuple[str, dict[str, Any]]  # (feed TR ID, normalized row)
Notify = Callable[[str], Awaitable[bool]]  # send an alert; whether it went


@dataclass(frozen=True)
class PredictionsStored:
    """Predictions just stored: displayed now."""

    bar_close: datetime
    ids: Mapping[str, list[int]]  # by predictor


@dataclass(frozen=True)
class OutcomesScored:
    """Outcomes just scored."""

    ids: list[int]  # their predictions'


Listener = Callable[[PredictionsStored | OutcomesScored], None]  # told of each, as it happens (the stream)
# A day's macro regime as decided before a time (the day's open); None unknown.
Regimes = Callable[[date, datetime], str | None]

# Each day's algorithm review runs this long after the close, when everything is scored.
REVIEW_AFTER_CLOSE = timedelta(minutes=10)
# Days with predictions a review reads back over: the longest rule counts 60 on the bench,
# and a status can be older than that.
_REVIEW_DAYS = MAX_REVIEW_DAY  # prediction days a review reads back

_BOOK_COLUMNS = (
    "symbol",
    "received_at",
    "ask_px_1",
    "bid_px_1",
    *(f"{side}_qty_{i}" for side in ("ask", "bid") for i in LEVELS),
)
# The feeds the engine reads, and the columns it uses from each.
ENGINE_FEEDS: dict[str, tuple[str, ...]] = {
    # Integrated trades make the bars; their best quotes are integrated quotes too, and their
    # aggressor side counts (Shannon's).
    TRADE.tr_id: ("symbol", "event_time", "received_at", "price", "qty", "ask1", "bid1", "side"),
    # The book's sizes make the microprice (Kalman's), the order flow imbalance and the
    # depth imbalance (Shannon's).
    BOOK.tr_id: _BOOK_COLUMNS,
    # The REST book snapshot replaces the integrated book when it is cut by the session limit.
    BOOK_SNAPSHOT.tr_id: _BOOK_COLUMNS,
    # KRX-only trades carry KRX's best quotes, for the 30 seconds before NXT opens; the
    # ETFs' make their bars.
    KRX_TRADE.tr_id: ("symbol", "event_time", "received_at", "price", "qty", "ask1", "bid1"),
    # NXT's trades, for its share of the volume (Shannon's).
    NXT_TRADE.tr_id: ("symbol", "received_at", "qty"),
    # A symbol's VIs: a bar close inside one is not predicted; a window touching one is special.
    VI_STATUS.tr_id: ("symbol", "event_time", "received_at", "end_time", "active"),
    # The KOSPI index and KOSPI200 futures, for circuit breakers and sidecars; with the other
    # indices, USD futures and US daytime trades, they lead.
    INDEX.tr_id: ("symbol", "event_time", "received_at", "value", "change_pct"),
    INDEX_REST.tr_id: ("symbol", "event_time", "received_at", "value", "change_pct"),
    KOSPI200_FUTURES.tr_id: ("symbol", "event_time", "received_at", "price", "qty", "change_pct"),
    USD_FUTURES.tr_id: ("symbol", "event_time", "received_at", "price", "qty"),
    US_TRADE.tr_id: ("symbol", "event_time", "received_at", "price", "qty"),
    # The symbols' flows, for Turing and Shannon: program trading, foreign member firms
    # (live or by REST), and brokers' estimates of investor buying.
    PROGRAM.tr_id: ("symbol", "received_at", "net_value"),
    MEMBER.tr_id: ("symbol", "received_at", "foreign_net_qty"),
    MEMBER_REST.tr_id: ("symbol", "received_at", "foreign_net_qty"),
    FOREIGN_ESTIMATE.tr_id: ("symbol", "received_at", "slot", "foreign_net_qty", "institution_net_qty"),
    # The options near the money, for their at-the-money implied volatility (Markov's).
    OPTION.tr_id: ("symbol", "received_at", "implied_vol", "delta"),
}


def partitions(feed: str, symbols: Sequence[str], leaders: Leaders) -> list[str] | None:
    """The partitions of ``feed`` the engine reads; None for every one (a futures feed's
    single front month, whichever it was that day, and the options near the money that day).
    """
    if feed in FUTURES_SERIES or feed == OPTION.tr_id:
        return None
    if feed in INDEX_FEEDS:
        return sorted({KOSPI, *leaders.indices})
    if feed == US_TRADE.tr_id:
        return list(leaders.us)
    if feed == KRX_TRADE.tr_id:
        return [*symbols, *leaders.etfs]
    return list(symbols)


@dataclass(frozen=True)
class Budget:
    per_algorithm: float = 10.0  # seconds one algorithm may take for a bar close, all symbols
    cpu: float = 30.0  # seconds of expected algorithm work per bar close, all together

    @classmethod
    def of(cls, settings: Settings) -> Budget:
        return cls(settings.algorithm_seconds, settings.cpu_budget_seconds)


def _start_of(day: date) -> datetime:
    """Midnight starting ``day``, KST."""
    return datetime.combine(day, time(), KST)


# How much one bar close's measured time moves an algorithm's expected time.
_COST_WEIGHT = 0.3


@dataclass(frozen=True)
class CpuUse:
    """The CPU budget now, for the status."""

    budget: Budget
    expected: Mapping[str, float]  # seconds per bar close of each algorithm not retired, once timed
    skipped_today: int  # algorithm runs skipped for the budget today

    @property
    def expected_seconds(self) -> float:
        return sum(self.expected.values())

    @property
    def usage(self) -> float | None:
        """The expected work as a share of the budget; over 1, the last registered are skipped."""
        return self.expected_seconds / self.budget.cpu if self.budget.cpu > 0 else None


@dataclass(frozen=True)
class _AtClose:
    """What is fixed for a symbol at a bar close."""

    mid: float | None
    features: Mapping[str, Value]


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
        min_sample: int = DEFAULT_MIN_SAMPLE,
        notify: Notify | None = None,
        consensus_timing: Timing | None = None,
        leaders: Leaders | None = None,
        signals: Signals | None = None,
        regimes: Regimes | None = None,
        admits: Callable[[Algorithm], bool] | None = None,
        commit: str = "unknown",
        review_rules: Rules | None = None,
    ) -> None:
        self.store = store
        self.commit = commit  # the code it runs (the deploy's KRX_COMMIT)
        self.started_at = clock.now()
        self.review_rules = review_rules or Rules()  # when statuses are judged (#87)
        self.calendar = calendar
        self.symbols = tuple(symbols)
        self.fee_rate = fee_rate
        self.clock = clock
        self.registered = register(algorithms)
        given = models or {}
        self.models = {a.name: given.get(a.name) or a.default_model() for a in self.registered}
        self._admits = admits  # without: every registered algorithm runs (replay, tests)
        self.algorithms: tuple[Algorithm, ...] = ()  # the registered ones that run, in order
        self._feature_names: list[str] = []
        self._told_waiting: set[str] = set()  # waiting algorithms already alerted
        self._budget = budget or Budget()
        self._features = feature_store or FeatureStore()
        self._cost: dict[str, float] = {}  # expected seconds per bar close, by algorithm
        self._busy: set[str] = set()  # algorithms still working past their budget
        self._special = SpecialSituations(calendar)
        self.leaders = leaders or Leaders()
        self._last_tick = clock.now()
        self._market = MarketView(calendar, symbols, self.leaders, self._last_tick, self.fee_rate)
        # Outcomes waiting for their target time: (target_at, prediction_id, pending).
        self._pending: list[tuple[datetime, int, Pending]] = []
        for p in store.pending():
            heapq.heappush(self._pending, (p.target_at, p.prediction_id, p))
        self.min_sample = min_sample
        self.consensus_timing = consensus_timing or Timing()
        self.weights: Weights = store.latest_weights() or NO_WEIGHTS
        self.notify = notify  # where alerts go (Telegram, live)
        self.training: dict[str, Any] | None = None  # the last night's training, as the swap saw it
        # Each algorithm's report of that night, as stored (``nightly``), by algorithm (#165).
        self.night_reports: dict[str, dict[str, Any]] = {}
        # The statuses' stages and what they were computed for (``stages``).
        self._stages: tuple[Any, dict[str, Stage]] | None = None
        self.listeners: list[Listener] = []
        self.signals = signals  # Telegram signal alerts, off without
        self._signals_resumed = False  # from the signals sent before a restart, at the first final
        self._regimes = regimes  # without: no macro regime, and nothing to say about it
        self._regime_days: dict[date, str | None] = {}
        # Reviews pick up where the last one left off: one missed while down runs at start.
        self._reviewed_through = store.last_run("review") or self._last_tick
        self._admit()

    def _admit(self) -> None:
        """Run the algorithms enrolled before and the new ones admitted; a new one starts on
        probation."""
        known = self.store.statuses()
        admitted = [a for a in self.registered if a.name in known or self._admits is None or self._admits(a)]
        if tuple(admitted) == self.algorithms:
            return
        for a in admitted:
            if a.name not in known:
                self.store.add_status(a.name, self.clock.now(), None, Status.PROBATION, "registered")
        self.algorithms = tuple(admitted)
        self._feature_names = sorted({f for a in self.algorithms for f in a.features})

    def waiting(self) -> list[str]:
        """The registered algorithms not run yet: their pre-validation has not passed."""
        return [a.name for a in self.registered if a not in self.algorithms]

    def on_event(self, feed: str, row: dict[str, Any]) -> None:
        self._market.on_event(feed, row)
        self._mark_special(self._special.event(feed, row, self.symbols))

    def _mark_special(self, window: Window | None) -> None:
        """A special window just learned: the predictions it touches are special, scored or not."""
        if window is not None:
            log.info("special situation: %s", window)
            self.store.mark_special(window.symbol, window.start, window.end, HORIZON)

    def next_due(self) -> datetime | None:
        """When ``run_due`` next has work: a bar close to predict, an outcome to score, or a review."""
        candidates = [
            next_bar_close(self.calendar, self._last_tick),
            self._next_review(self._reviewed_through),
        ]
        if self._pending:
            candidates.append(self._pending[0][0])
        due = [c for c in candidates if c is not None]
        return min(due) if due else None

    async def run_due(self) -> None:
        """Score, predict and review everything due by now, in time order."""
        now = self.clock.now()
        while (due := self.next_due()) is not None and due <= now:
            self._score_until(due)
            if due == next_bar_close(self.calendar, self._last_tick):
                self._last_tick = due
                await self._predict(due)
            if due == self._next_review(self._reviewed_through):
                self._reviewed_through = due
                await self._review(due)

    def bars(self, symbol: str) -> list[Bar]:
        return self._market.bars(symbol, self.clock.now())

    def last_bar(self, symbol: str) -> Bar | None:
        """The symbol's latest bar today, the one being made if its minute goes on (#110)."""
        return self._market.last_bar(symbol, self.clock.now())

    def mid(self, symbol: str) -> float | None:
        return self._market.mid(symbol, self.clock.now())

    def latest_lead(self, series: str) -> tuple[float, datetime] | None:
        """A leading series' latest trade today (e.g. the USD futures): price and time."""
        return self._market.latest_lead(series, self.clock.now())

    def switch_models(self, models: Mapping[str, Model]) -> bool:
        """Run ``models`` from the next bar close (a swap, a fit, a rollback); whether any changed."""
        changed = False
        for name, model in models.items():
            if name in self.models and self.models[name] != model:
                log.info("%s now runs %s", name, model.version)
                self.models[name] = model
                changed = True
        return changed

    def members(self) -> list[str]:
        """The consensus members now: the active algorithms, in registration order."""
        active = _with_status(self.store.statuses(), Status.ACTIVE)
        return [a.name for a in self.algorithms if a.name in active]

    def cpu_use(self) -> CpuUse:
        """The CPU budget now: the algorithms that run, as timed so far, and today's skips."""
        retired = _with_status(self.store.statuses(), Status.RETIRED)
        expected = {
            a.name: self._cost[a.name]
            for a in self.algorithms
            if a.name in self._cost and a.name not in retired
        }
        midnight = _start_of(self.clock.now().astimezone(KST).date())
        return CpuUse(self._budget, expected, self.store.count_runs("cpu_budget", midnight))

    def special_today(self) -> list[Window]:
        """The special situations known now that touch today, in time order."""
        now = self.clock.now()
        today = now.astimezone(KST).date()
        windows = [w for w in self._special.known(before=now) if w.end.astimezone(KST).date() >= today]
        return sorted(windows, key=lambda w: (w.start, w.kind.value, w.symbol or ""))

    def _next_review(self, after: datetime) -> datetime | None:
        """The first daily review strictly after ``after``."""
        day = after.astimezone(KST).date()
        for _ in range(LOOKAHEAD_DAYS):
            session = self.calendar.session(day)
            if session is not None and session.close + REVIEW_AFTER_CLOSE > after:
                return session.close + REVIEW_AFTER_CLOSE
            day += timedelta(days=1)
        return None

    async def _review(self, at: datetime) -> None:
        """Change statuses by the lifecycle rules, alert retirement candidates, and tell the
        predictions the budgets cost that day."""
        standings = self.store.statuses()
        alerted = {
            name
            for name, s in standings.items()
            if s.status in (Status.PROBATION, Status.BENCH)
            and self.store.ran_since("retire_candidate", name, s.since)
        }
        result = await asyncio.to_thread(
            review,
            [a.name for a in self.algorithms],
            standings,
            {name: self._first_counted(s.since) for name, s in standings.items()},
            self.store.prediction_days(at.astimezone(KST).date(), _REVIEW_DAYS),
            lambda chosen, special: self.store.scored_predictions(chosen, None, special),
            min_sample=self.min_sample,
            fee_rate=self.fee_rate,
            alerted=alerted,
            rules=self.review_rules,
        )
        for c in result.changes:
            # Only from the status reviewed: a retire command may have landed meanwhile.
            if self.store.change_status(c.algorithm, at, c.previous, c.status, c.reason):
                log.info("%s: %s -> %s (%s)", c.algorithm, c.previous.value, c.status.value, c.reason)
            else:
                log.info("%s: %s not applied, its status changed meanwhile", c.algorithm, c.status.value)
        for candidate in result.candidates:
            name = candidate.algorithm
            stuck = (
                f"수습 {candidate.days}거래일 동안 정식이 되지 못했습니다"
                if candidate.status is Status.PROBATION
                else f"벤치에서 {candidate.days}거래일 동안 회복하지 못했습니다"
            )
            text = f"{name} 은퇴 후보: {stuck}. 은퇴시키려면 서버에서 krx-quant retire {name}"
            if await self._deliver(text):  # else asked again at the next review
                self.store.add_run("retire_candidate", at, name, text)
        untold = [name for name in self.waiting() if name not in self._told_waiting]
        if untold:
            names = ", ".join(untold)
            text = (
                f"사전 검증 대기: {names}. 검증을 통과해야 돌기 시작합니다. "
                f"서버에서 장 시간 밖에 krx-quant prevalidate <이름>"
            )
            if await self._deliver(text):
                self._told_waiting.update(untold)
        await self._report_budgets(at)
        await self._refit_weights(at)
        # Last: a review that failed part way is run again after a restart.
        self.store.add_run("review", at, None, f"{len(result.changes)} status changes")

    async def _report_budgets(self, at: datetime) -> None:
        """Tell the reviewed day's predictions the time and CPU budgets cost, if any: an
        algorithm too slow for its limit, or the algorithms together too much for the
        server. Once a day, a review run again after a restart included (``budget_report``)."""
        day = at.astimezone(KST).date()
        midnight, next_day = _start_of(day), _start_of(day + timedelta(days=1))
        if self.store.count_runs("budget_report", midnight, until=next_day):
            return
        timed_out = self.store.timeouts_by_algorithm(midnight, next_day)
        skipped = self.store.runs_by_algorithm("cpu_budget", midnight, next_day)
        if not timed_out and not skipped:
            return

        def counts(by_algorithm: Mapping[str, int]) -> str:
            return ", ".join(f"{name} {n}회" for name, n in by_algorithm.items())

        lines = [f"{day} 예산 때문에 예측이 빠진 알고리즘이 있습니다."]
        if timed_out:
            lines.append(
                f"시간 초과(봉 마감 한 번에 {self._budget.per_algorithm:g}초 넘김): {counts(timed_out)}."
            )
        if skipped:
            lines.append(f"CPU 예산({self._budget.cpu:g}초)으로 건너뜀: {counts(skipped)}.")
        lines.append("알고리즘을 가볍게 하거나 서버를 늘리세요. 걸리는 시간은 /v1/status의 cpu에 있습니다.")
        text = " ".join(lines)
        if await self._deliver(text):
            self.store.add_run("budget_report", at, None, text)

    async def _refit_weights(self, at: datetime) -> None:
        """The consensus weights of the members active after the review."""
        members = self.members()
        days = self.store.prediction_days(at.astimezone(KST).date(), max(SPANS))
        rows = await asyncio.to_thread(self.store.scored_predictions, days, None, False)
        self.weights = await asyncio.to_thread(fit_weights, rows, members, days, at)
        self.store.save_weights(self.weights)
        log.info("consensus weights %s for %s", self.weights.version, ", ".join(members) or "no member")

    def _first_counted(self, since: datetime) -> date:
        """The first day a status that began at ``since`` counts for: its day, unless it
        began after that day's review.
        """
        day = since.astimezone(KST).date()
        session = self.calendar.session(day)
        if session is not None and since >= session.close + REVIEW_AFTER_CLOSE:
            return day + timedelta(days=1)
        return day

    async def stages(self) -> dict[str, Stage]:
        """Where each algorithm on probation or on the bench stands toward its next review
        (#165, ``lifecycle.stages``), as of the latest review: the days counted as the review
        counts them, the weakest lift by trading day it judges by, and the day the next review
        falls on if the engine predicts every trading day. Read off the event loop, and again
        only once a review has run or a status changed: the days reviewed do not change.
        """
        standings = self.store.statuses()
        upcoming = self._next_review(self._reviewed_through)
        next_day = None if upcoming is None else upcoming.astimezone(KST).date()
        key = (sorted((name, s.status.value, s.since) for name, s in standings.items()), next_day)
        if self._stages is not None and self._stages[0] == key:
            return self._stages[1]
        today = self.clock.now().astimezone(KST).date()
        days = self.store.prediction_days(today, _REVIEW_DAYS)
        reviewed = [d for d in days if next_day is None or d < next_day]
        found = await asyncio.to_thread(
            stages_now,
            [a.name for a in self.algorithms],
            standings,
            {name: self._first_counted(s.since) for name, s in standings.items()},
            reviewed,
            lambda chosen, special: self.store.scored_predictions(chosen, None, special),
            min_sample=self.min_sample,
            fee_rate=self.fee_rate,
            rules=self.review_rules,
        )
        dated = {name: replace(s, next_review_on=self._review_on(next_day, s)) for name, s in found.items()}
        self._stages = (key, dated)
        return dated

    def _review_on(self, next_day: date | None, stage: Stage) -> date | None:
        """The trading day ``stage``'s next review falls on, counted from ``next_day``, the next
        daily review's, which counts one day more than the latest; None without either."""
        if next_day is None or stage.next_review is None:
            return None
        day = next_day
        for _ in range(stage.next_review - stage.days - 1):
            day += timedelta(days=1)
            while not self.calendar.is_trading_day(day):
                day += timedelta(days=1)
        return day

    async def _deliver(self, text: str) -> bool:
        """Send an alert; whether it went (or there is nowhere to send it)."""
        if self.notify is None:
            log.warning("alert: %s", text)
            return True
        return await self.notify(text)

    async def _predict(self, bar_close: datetime) -> None:
        self._admit()
        day_start = datetime.combine(bar_close.date(), time(), KST)
        at_close: dict[str, _AtClose] = {}
        frozen = self._market.at_close(bar_close)
        baselines: dict[tuple[str, str], dict[str, Forecast | None]] = {}
        standings = self.store.statuses()  # read each time: retiring is a command
        retired, active = _with_status(standings, Status.RETIRED), _with_status(standings, Status.ACTIVE)
        running = [a for a in self.algorithms if a.name not in retired]
        members = [a.name for a in running if a.name in active]
        known = self._special.known(before=bar_close)
        regime = self._regime(bar_close)
        stopped = [s for s in self.symbols if in_vi(known, s, bar_close)]
        if stopped:
            self._record_not_predicted(bar_close, stopped, "vi", running)
        for symbol in self.symbols:
            if symbol in stopped:
                continue
            snapshot = self._market.snapshot(frozen, symbol, self.fee_rate, regime)
            mid = snapshot.price
            at_close[symbol] = _AtClose(mid, self._features.compute(snapshot, self._feature_names))
            inputs = Inputs(
                symbol=symbol,
                bar_close=bar_close,
                mid=mid,
                mid_before=self._market.mid(symbol, bar_close - HORIZON),
                flat_ticks=snapshot.flat_ticks,
                tick=snapshot.tick,
                day_classes=self.store.day_classes(symbol, day_start, bar_close),
            )
            for b in BASELINES:
                baselines.setdefault((b.name, b.version), {})[symbol] = b.predict(inputs)
        self._record(
            bar_close, at_close, [(name, version, made, "ok") for (name, version), made in baselines.items()]
        )
        if not at_close:
            return
        chosen = self._within_cpu_budget(bar_close, running)
        runs = {a.name: asyncio.ensure_future(self._run(a, bar_close, at_close)) for a in chosen}
        mine = [runs[m] for m in members if m in runs]
        try:
            # The fast consensus: the members done within a moment of starting.
            if mine:
                await asyncio.wait(mine, timeout=self.consensus_timing.fast_seconds)
            self._record_consensus(FAST, bar_close, at_close, members, _finished(mine))
            # The final one: every member done (each has its time budget), not the others.
            if mine:
                await asyncio.wait(mine)
            self._record_consensus(FINAL, bar_close, at_close, members, _finished(mine))
            await asyncio.gather(*runs.values(), return_exceptions=True)
        except BaseException:
            for task in runs.values():  # shutting down: none may write after the store closes
                task.cancel()
            raise

    def _regime(self, bar_close: datetime) -> str | None:
        """The day's macro regime, read once a day. A day without one, or with one this
        engine does not know, is recorded (``runs``) when an algorithm reads it."""
        day = bar_close.astimezone(KST).date()
        if self._regimes is None:
            return None
        if day not in self._regime_days:
            session = self.calendar.session(day)
            opened = session.open if session is not None else bar_close
            read = self._regimes(day, opened)
            regime = self._regime_days[day] = read if read in REGIME_CODES else None
            midnight = datetime.combine(day, time(), KST)
            if regime is None and "macro_regime" in self._feature_names:
                if not self.store.count_runs("no_regime", midnight):  # once, restarts too
                    detail = f"no macro regime for {day}" + (f" (unknown: {read})" if read else "")
                    log.warning(detail)
                    self.store.add_run("no_regime", bar_close, None, detail)
        return self._regime_days[day]

    def _record_consensus(
        self,
        name: str,
        bar_close: datetime,
        at_close: Mapping[str, _AtClose],
        members: Sequence[str],
        finished: Mapping[str, Mapping[str, Forecast | None]],
    ) -> None:
        """The consensus of the members' forecasts finished so far; empty where there are none."""
        pooled: dict[str, Forecast] = {}
        empty: dict[str, Forecast | None] = {}
        pooled_from: dict[str, dict[str, Forecast]] = {}
        why = "none_finished" if members else "no_members"
        for symbol in at_close:
            present = {m: f for m in members if (f := finished.get(m, {}).get(symbol)) is not None}
            if present:
                pooled[symbol] = pool(present, self.weights.of(symbol, bar_close, list(present)))
                pooled_from[symbol] = present
            else:
                empty[symbol] = None
        version = self.weights.version
        made: list[tuple[str, str, Mapping[str, Forecast | None], str]] = [
            (name, version, forecasts, status)
            for forecasts, status in ((pooled, "ok"), (empty, why))
            if forecasts
        ]
        # A final consensus not to be acted on says why, stored with it for every client (#88).
        blocked = self._blocked(bar_close, at_close, pooled) if name == FINAL else {}
        self._record(
            bar_close,
            at_close,
            made,
            blocked,
            {s: _votes(f) for s, f in pooled_from.items()},
            {s: tuple(f) for s, f in pooled_from.items()},
        )
        if name == FINAL and self.signals is not None:
            try:
                self._signal(self.signals, bar_close, pooled, pooled_from, blocked)
            except Exception:  # an alert never stops predicting
                log.exception("signal alert failed")

    def _blocked(
        self, bar_close: datetime, at_close: Mapping[str, _AtClose], pooled: Mapping[str, Forecast]
    ) -> dict[str, Blocked]:
        """Why each symbol's final consensus is not to be acted on, by what is known now: a VI
        or circuit breaker touching its four minutes, no reference price at its bar close, or
        its target time past."""
        now = self.clock.now()
        target_at = bar_close + HORIZON
        known = [w for w in self._special.known(before=now) if w.kind in (Kind.VI, Kind.CIRCUIT_BREAKER)]
        blocked: dict[str, Blocked] = {}
        for symbol in pooled:
            if touching(known, symbol, bar_close, target_at):
                blocked[symbol] = Blocked.SPECIAL
            elif at_close[symbol].mid is None:
                blocked[symbol] = Blocked.NO_REFERENCE
            elif now >= target_at:
                blocked[symbol] = Blocked.EXPIRED
        return blocked

    def _signal(
        self,
        signals: Signals,
        bar_close: datetime,
        pooled: Mapping[str, Forecast],
        pooled_from: Mapping[str, Mapping[str, Forecast]],
        blocked: Mapping[str, Blocked],
    ) -> None:
        """Judge a final consensus for signals, block what should not be acted on, and keep
        each signal judged (``runs``)."""
        now = self.clock.now()
        if not self._signals_resumed:
            self._signals_resumed = True  # once, even if reading fails
            midnight = datetime.combine(now.astimezone(KST).date(), time(), KST)
            signals.resume(r.detail for r in self.store.runs(SENT, midnight))
        target_at = bar_close + HORIZON

        def keep(judged: Judged) -> None:
            self.store.add_run(judged.kind, now, None, judged.detail(signals.rule))
            if judged.blocked is not None:
                log.info("signal blocked (%s): %s", judged.blocked.value, judged.signal.text())

        signals.final(bar_close, target_at, pooled, pooled_from, blocked, keep)

    def _record_not_predicted(
        self, bar_close: datetime, symbols: Sequence[str], status: str, running: Sequence[Algorithm]
    ) -> None:
        """Every predictor's empty prediction for ``symbols``, recorded with why."""
        at_close = {s: _AtClose(self._market.mid(s, bar_close), {}) for s in symbols}
        nothing = dict.fromkeys(symbols)
        made = [(b.name, b.version, nothing, status) for b in BASELINES]
        made += [(a.name, self.models[a.name].version, nothing, status) for a in running]
        made += [(name, self.weights.version, nothing, status) for name in NAMES]
        self._record(bar_close, at_close, made)

    def _within_cpu_budget(self, bar_close: datetime, running: Sequence[Algorithm]) -> list[Algorithm]:
        """The algorithms to run: the last registered drop out while the expected work is over budget."""
        chosen = list(running)
        while chosen and sum(self._cost.get(a.name, 0.0) for a in chosen) > self._budget.cpu:
            dropped = chosen.pop()
            expected = self._cost.get(dropped.name, 0.0)
            detail = f"expected {expected:.3f}s; budget {self._budget.cpu}s per bar close"
            log.warning("%s skipped at %s for the CPU budget (%s)", dropped.name, bar_close, detail)
            self.store.add_run("cpu_budget", bar_close, dropped.name, detail)
            # Never run, it would never be timed again: expect less, so it is tried soon.
            self._cost[dropped.name] = expected / 2
        return chosen

    async def _run(
        self, algorithm: Algorithm, bar_close: datetime, at_close: Mapping[str, _AtClose]
    ) -> tuple[str, Mapping[str, Forecast | None]]:
        """Run and record one algorithm; its name and forecasts (none unless it finished)."""
        name, model = algorithm.name, self.models[algorithm.name]
        made: Mapping[str, Forecast | None] = dict.fromkeys(at_close)  # nothing, unless it finishes
        status = "timed_out"
        declined: dict[str, Abstention] = {}
        if name not in self._busy:
            vectors = {s: FeatureVector(c.features, algorithm.features) for s, c in at_close.items()}
            started = timer.perf_counter()
            work = asyncio.ensure_future(asyncio.to_thread(_predict_symbols, algorithm, model, vectors))
            done, _ = await asyncio.wait({work}, timeout=self._budget.per_algorithm)
            if not done:
                log.warning("%s took over %ss at %s; skipped", name, self._budget.per_algorithm, bar_close)
                self._busy.add(name)
                work.add_done_callback(partial(self._finished_late, name))
                self._cost[name] = max(self._cost.get(name, 0.0), self._budget.per_algorithm)
            elif (error := work.exception()) is not None:
                log.error("%s failed at %s: %r", name, bar_close, error, exc_info=error)
                status = "error"
            else:
                (made, declined), status = work.result(), "ok"
                elapsed = timer.perf_counter() - started
                previous = self._cost.get(name)
                self._cost[name] = (
                    elapsed if previous is None else (1 - _COST_WEIGHT) * previous + _COST_WEIGHT * elapsed
                )
        entries = [(name, model.version, made, status)] if made else []
        for why in dict.fromkeys(declined.values()):  # each reason once, in the order met
            entries.append(
                (name, model.version, {s: None for s, w in declined.items() if w is why}, why.value)
            )
        self._record(bar_close, at_close, entries)
        return name, made

    def _finished_late(self, name: str, work: asyncio.Future[_Predicted]) -> None:
        self._busy.discard(name)
        if not work.cancelled() and work.exception() is not None:
            log.error("%s failed after its budget: %r", name, work.exception())

    def _record(
        self,
        bar_close: datetime,
        at_close: Mapping[str, _AtClose],
        made: Sequence[tuple[str, str, Mapping[str, Forecast | None], str]],
        blocked: Mapping[str, Blocked] | None = None,
        votes: Mapping[str, tuple[int, int, int]] | None = None,
        pooled: Mapping[str, tuple[str, ...]] | None = None,
    ) -> None:
        """Store (name, model version, forecasts by symbol, status) entries as displayed now;
        a consensus with how its members leaned (``votes``) and who they were (``pooled``),
        by symbol."""
        displayed_at = self.clock.now()
        mid_display = {symbol: self._market.mid(symbol, displayed_at) for symbol in at_close}
        special = self._special.known(before=displayed_at)
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
                        special=touching(special, symbol, bar_close, bar_close + HORIZON),
                        blocked=why.value
                        if forecast is not None and blocked and (why := blocked.get(symbol)) is not None
                        else None,
                        votes=(votes or {}).get(symbol) if forecast is not None else None,
                        pooled=(pooled or {}).get(symbol) if forecast is not None else None,
                    )
                )
        added = self.store.add_all(predictions)
        for pending in added.pending:
            heapq.heappush(self._pending, (pending.target_at, pending.prediction_id, pending))
        self._tell(PredictionsStored(bar_close, added.ids))

    def _score_until(self, due: datetime) -> None:
        scores: list[tuple[int, Score]] = []
        # The touch label of every window over (#167), predicted or not (a VI), and of each
        # bar close scored (one from before a restart says so): recorded beside the outcomes,
        # not yet what they are scored on.
        windows = dict.fromkeys(self._market.ended_windows(due))
        while self._pending and self._pending[0][0] <= due:
            _, _, pending = heapq.heappop(self._pending)
            mid_target = self._market.mid(pending.symbol, pending.target_at)
            scores.append((pending.prediction_id, _score(pending, mid_target)))
            windows[(pending.symbol, pending.target_at - HORIZON)] = None
        now = self.clock.now()
        if windows:
            self.store.add_touches([self._market.touch(symbol, close) for symbol, close in windows], now)
        if scores:
            self.store.score_all(scores, now)
            self._tell(OutcomesScored([prediction_id for prediction_id, _ in scores]))

    def _tell(self, change: PredictionsStored | OutcomesScored) -> None:
        for listener in self.listeners:
            try:
                listener(change)
            except Exception:  # a listener never stops predicting
                log.exception("engine listener failed")


def _votes(forecasts: Mapping[str, Forecast]) -> tuple[int, int, int]:
    """The members by the class each forecast most likely, as scored (ties: flat, then up):
    up, flat, down (#106)."""
    leaned = [most_likely(f.p_up, f.p_flat, f.p_down) for f in forecasts.values()]
    return leaned.count(Direction.UP), leaned.count(Direction.FLAT), leaned.count(Direction.DOWN)


def _finished(
    runs: Sequence[asyncio.Future[tuple[str, Mapping[str, Forecast | None]]]],
) -> dict[str, Mapping[str, Forecast | None]]:
    """The forecasts of the runs done (a run that failed counts as none)."""
    return dict(r.result() for r in runs if r.done() and not r.cancelled() and r.exception() is None)


def _with_status(standings: Mapping[str, Standing], status: Status) -> set[str]:
    return {name for name, s in standings.items() if s.status is status}


# An algorithm's forecasts by symbol, and why for each symbol it did not predict.
_Predicted = tuple[dict[str, Forecast], dict[str, Abstention]]


def _predict_symbols(algorithm: Algorithm, model: Model, vectors: Mapping[str, FeatureVector]) -> _Predicted:
    """Runs in a worker thread: one algorithm on every symbol, each forecast checked."""
    forecasts: dict[str, Forecast] = {}
    declined: dict[str, Abstention] = {}
    for symbol, features in vectors.items():
        try:
            f = algorithm.predict(features, model)
        except NotPredicted as e:
            declined[symbol] = e.why
            continue
        probabilities = (f.p_up, f.p_flat, f.p_down)
        distribution = all(0 <= p <= 1 for p in probabilities) and math.isclose(
            sum(probabilities), 1, abs_tol=1e-6
        )
        if not distribution:
            raise ValueError(f"{algorithm.name} gave probabilities {probabilities} for {symbol}")
        if not math.isfinite(f.expected_ticks):
            raise ValueError(f"{algorithm.name} expected {f.expected_ticks} ticks for {symbol}")
        forecasts[symbol] = f
    return forecasts, declined


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
