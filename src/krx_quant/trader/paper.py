"""Paper trading by the triple barrier (#393 "모의 체결", #394).

**Entry.** A prediction of the cell, as the trader reads it (its decision time), buys when all
hold, else it is skipped with its reason:

- it was made (status ok) and the engine did not block it (a final consensus, #88): ``not_ok``,
  ``blocked``;
- it was displayed at most ``Rules.fresh`` before the decision (``stale``: a trader that was
  stopped does not act on the predictions it missed);
- its most likely class is the kind's rise (up, up_first; a tie goes to the middle class, then
  the first, ``kinds.most_likely``) and that class's probability is at least the threshold θ:
  ``not_up``, ``below_threshold``;
- it has its reference: the bar close's mid-point, tick and width (``no_reference``);
- the day trades (``closed``); its bar close is at least five minutes after the open (09:05,
  ``before_entry``), and its time barrier, the bar close plus the kind's horizon, at least five
  minutes before the closing auction (15:15, ``past_last_exit``): every position is closed
  before the closing auction, and none is opened in the opening one;
- nothing is held, nor waiting to be bought (``holding``): one position at a time.

Its reference price is the prediction's ``anchor`` (the bar close's integrated mid-point), the
target ``anchor + w × tick``, the stop ``anchor − w × tick`` (w = k for the direction), the time
barrier the bar close plus the kind's horizon: the trade is the event its hit rate measured.

**Fills** on the collector's best quotes (``quotes.py``), in the order received:

- the buy at the best ask of the first quote received after the decision. None if that quote's
  ask is at or above the target (``target_reached``: it was reached already), if the capital
  does not buy one share (``no_shares``), or if no quote comes within ``Rules.quote_wait``
  (``no_quote``). The quantity is the capital over that ask, rounded down. The take-profit's sell
  rests at the target from then on;
- the take-profit at the target, once a quote received after the buy has its best bid at or above
  it (no place at the front of the queue is assumed);
- the stop at the best bid of the first quote after the buy whose best bid is at or below the stop;
- the time exit at the best bid of the last quote received before the time barrier, once a quote
  received at or after it comes, or the clock is ``Rules.grace`` past it. Without a quote in the
  ``Rules.quote_wait`` before the barrier (the stream was down), or when the trader was not
  running at the barrier, it waits for the next quote and sells at its best bid then: the trade's
  ``exit_at`` after its ``time_barrier`` shows the wait.

**Costs** (``costs``): the commission (``KRX_FEE_PERCENT``) on the buy's and the sell's notional
and the sell tax (``pricing.SELL_TAX``) on the sell's; the net P&L is the gross less them, in won.
Unlike the touch P&L and the barrier P&L (``stats.touch_profit``, ``barriers.barrier_profit``), which
cost the round trip on the reference price per share in ticks, a trade pays on its fills.

A quote received before the trader started is the book as it stood, never a trigger. A trader
started with a trade open (a restart) goes on with it: its exits as above, and a buy it was
waiting for is given up (``restarted``). The same predictions, read at the same times, and the
same quotes always make the same trades.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from krx_quant.feeds.base import kst_day
from krx_quant.kinds import most_likely
from krx_quant.market_calendar import Session
from krx_quant.pricing import SELL_TAX
from krx_quant.trader import CAPITAL, ENTRY_AFTER_OPEN, EXIT_BEFORE_AUCTION, PAPER, SYMBOL, THRESHOLD
from krx_quant.trader.cells import Signal
from krx_quant.trader.quotes import BestQuote
from krx_quant.trader.store import Entry, Exit, OpenTrade, TraderStore

log = logging.getLogger(__name__)

# Why a trade ended.
TAKE_PROFIT, STOP_LOSS, TIME = "take_profit", "stop_loss", "time"
# Why a prediction bought nothing.
NOT_OK, BLOCKED, STALE = "not_ok", "blocked", "stale"
NOT_UP, BELOW_THRESHOLD, NO_REFERENCE = "not_up", "below_threshold", "no_reference"
CLOSED, BEFORE_ENTRY, PAST_LAST_EXIT, HOLDING = "closed", "before_entry", "past_last_exit", "holding"
# Why a buy decided did not fill.
TARGET_REACHED, NO_SHARES, NO_QUOTE, RESTARTED = "target_reached", "no_shares", "no_quote", "restarted"
BUY, SKIP = "buy", "skip"


@dataclass(frozen=True)
class Rules:
    threshold: float = THRESHOLD
    capital: int = CAPITAL
    fee_rate: float = 0.00015  # the commission per side, as a fraction
    entry_after_open: timedelta = ENTRY_AFTER_OPEN
    exit_before_auction: timedelta = EXIT_BEFORE_AUCTION
    # A buy waits this long for its first quote; a time exit needs a quote this recent.
    quote_wait: timedelta = timedelta(seconds=5)
    # A prediction read later than this after its display is not acted on.
    fresh: timedelta = timedelta(seconds=10)
    # The clock passes a deadline by this much before it acts without a quote: quotes received
    # before the deadline but still on their way come in first.
    grace: timedelta = timedelta(seconds=1)


@dataclass(frozen=True)
class Plan:
    """A buy's barriers: the target, the stop and the time barrier."""

    target: float
    stop: float
    time_barrier: datetime


@dataclass(frozen=True)
class Costs:
    buy_fee: float
    sell_fee: float
    tax: float

    @property
    def total(self) -> float:
        return self.buy_fee + self.sell_fee + self.tax


def costs(entry_price: float, exit_price: float, quantity: int, fee_rate: float) -> Costs:
    """A round trip's costs, in won: the commission on the buy's and the sell's notional, and the
    sell tax on the sell's."""
    sold = exit_price * quantity
    return Costs(entry_price * quantity * fee_rate, sold * fee_rate, sold * SELL_TAX)


def plan(signal: Signal, rules: Rules, session: Session | None, at: datetime) -> Plan | str:
    """A prediction read at ``at``: its buy's barriers, or why it buys nothing (the reasons the
    module doc lists, but ``holding``, which is the trader's)."""
    p = signal.rise_probability
    if signal.status != "ok" or signal.probabilities is None or p is None:
        return NOT_OK
    if signal.blocked is not None:
        return BLOCKED
    if at - signal.displayed_at > rules.fresh:
        return STALE
    if most_likely(signal.probabilities) != signal.rise:
        return NOT_UP
    if p < rules.threshold:
        return BELOW_THRESHOLD
    if signal.anchor is None or not signal.tick or not signal.width:
        return NO_REFERENCE
    if session is None:
        return CLOSED
    if signal.bar_close < session.open + rules.entry_after_open:
        return BEFORE_ENTRY
    barrier = signal.bar_close + signal.horizon
    if barrier > session.closing_auction - rules.exit_before_auction:
        return PAST_LAST_EXIT
    move = signal.width * signal.tick
    return Plan(signal.anchor + move, signal.anchor - move, barrier)


@dataclass(frozen=True)
class _Waiting:
    """A buy decided, waiting for the first quote received after its decision."""

    decision: int
    signal: Signal
    plan: Plan
    decided_at: datetime

    def deadline(self, rules: Rules) -> datetime:
        return self.decided_at + rules.quote_wait


class PaperTrader:
    """One cell's paper trades, from its predictions (``on_signal``), the quotes (``on_quote``) and
    the clock (``on_time``), into ``store``. ``sessions`` gives a day's session (the market
    calendar's); ``started_at`` is when this run began."""

    def __init__(
        self,
        store: TraderStore,
        rules: Rules,
        sessions: Callable[[date], Session | None],
        started_at: datetime,
        mode: str = PAPER,
    ) -> None:
        self.store = store
        self.rules = rules
        self._sessions = sessions
        self._started_at = started_at
        self.mode = mode
        self._last: BestQuote | None = None  # the latest quote received
        self._waiting: _Waiting | None = None
        self._open: OpenTrade | None = store.open_trade(mode)
        if self._open is not None:
            log.info("trader: going on with trade %s, open since before this start", self._open.id)
        dropped = store.drop_unfilled(mode, RESTARTED)
        if dropped:
            log.info("trader: %s buy(s) waiting for a quote when the trader stopped: given up", dropped)

    def on_signal(self, signal: Signal, at: datetime) -> None:
        """A prediction of the cell, read at ``at`` (its decision time); one already decided on (a
        restart) is passed over."""
        if self.store.decided(self.mode, signal.kind, signal.predictor, signal.prediction_id):
            return
        planned = plan(signal, self.rules, self._sessions(kst_day(signal.bar_close)), at)
        if isinstance(planned, Plan) and (self._open is not None or self._waiting is not None):
            planned = HOLDING
        action, reason = (BUY, None) if isinstance(planned, Plan) else (SKIP, planned)
        decision = self.store.add_decision(
            self.mode,
            signal.symbol,
            signal.kind,
            signal.predictor,
            signal.prediction_id,
            signal.bar_close,
            signal.displayed_at,
            at,
            signal.rise_probability,
            action,
            reason,
        )
        if isinstance(planned, Plan):
            log.info(
                "trader: buying on %s %s of %s (p %.3f): target %s, stop %s, until %s",
                signal.kind,
                signal.predictor,
                signal.bar_close.strftime("%H:%M"),
                signal.rise_probability,
                planned.target,
                planned.stop,
                planned.time_barrier.strftime("%H:%M"),
            )
            self._waiting = _Waiting(decision, signal, planned, at)

    def on_quote(self, quote: BestQuote) -> None:
        """A quote from the stream. One not newer than the latest (a reconnect's first) is passed over."""
        standing = self._last
        if standing is not None and quote.received_at <= standing.received_at:
            return
        self._last = quote
        if quote.received_at < self._started_at:
            return  # the book as it stood before this run: no trigger
        if self._waiting is not None:
            self._buy(self._waiting, quote)
        elif self._open is not None:
            self._follow(self._open, quote, standing)

    def on_time(self, now: datetime) -> None:
        """The clock: a buy whose quote did not come is given up, a time barrier passed is sold on the
        last quote before it."""
        grace = self.rules.grace
        waiting = self._waiting
        if waiting is not None and now >= waiting.deadline(self.rules) + grace:
            self._give_up(waiting, NO_QUOTE)
        trade = self._open
        if trade is not None and now >= trade.time_barrier + grace:
            standing = self._standing(trade)
            if standing is not None:
                self._exit(trade, trade.time_barrier, standing.bid, TIME, standing)

    def _buy(self, waiting: _Waiting, quote: BestQuote) -> None:
        if quote.received_at <= waiting.decided_at:
            return  # received before the decision, though it came after it
        if quote.received_at > waiting.deadline(self.rules):
            self._give_up(waiting, NO_QUOTE)
            return
        if quote.ask >= waiting.plan.target:
            self._give_up(waiting, TARGET_REACHED)
            return
        quantity = math.floor(self.rules.capital / quote.ask)
        if quantity < 1:
            self._give_up(waiting, NO_SHARES)
            return
        signal, planned = waiting.signal, waiting.plan
        assert signal.anchor is not None and signal.tick is not None and signal.width is not None
        p = signal.rise_probability
        assert p is not None
        self._waiting = None
        self._open = self.store.enter(
            waiting.decision,
            Entry(
                mode=self.mode,
                symbol=SYMBOL,
                kind=signal.kind,
                predictor=signal.predictor,
                prediction_id=signal.prediction_id,
                bar_close=signal.bar_close,
                probability=p,
                threshold=self.rules.threshold,
                anchor=signal.anchor,
                tick=signal.tick,
                width_ticks=signal.width,
                target=planned.target,
                stop=planned.stop,
                time_barrier=planned.time_barrier,
                decided_at=waiting.decided_at,
                entry_at=quote.received_at,
                entry_price=quote.ask,
                quantity=quantity,
                buy_fee=quote.ask * quantity * self.rules.fee_rate,
                quote_at=quote.received_at,
            ),
        )
        log.info("trader: bought %s at %s (trade %s)", quantity, quote.ask, self._open.id)

    def _give_up(self, waiting: _Waiting, reason: str) -> None:
        self._waiting = None
        self.store.unfilled(waiting.decision, reason)
        log.info("trader: no buy on %s (%s)", waiting.signal.bar_close.strftime("%H:%M"), reason)

    def _follow(self, trade: OpenTrade, quote: BestQuote, standing: BestQuote | None) -> None:
        if quote.received_at >= trade.time_barrier:
            if (
                trade.time_barrier >= self._started_at
                and standing is not None
                and self._fresh(trade, standing)
            ):
                self._exit(trade, trade.time_barrier, standing.bid, TIME, standing)
            else:  # no recent quote before the barrier, or the trader was not running then
                self._exit(trade, quote.received_at, quote.bid, TIME, quote)
        elif quote.bid >= trade.target:
            self._exit(trade, quote.received_at, trade.target, TAKE_PROFIT, quote)
        elif quote.bid <= trade.stop:
            self._exit(trade, quote.received_at, quote.bid, STOP_LOSS, quote)

    def _standing(self, trade: OpenTrade) -> BestQuote | None:
        """The quote a time exit sells on at the barrier: the last one received before it, if recent
        enough and the trader was running then."""
        last = self._last
        if trade.time_barrier < self._started_at or last is None or not self._fresh(trade, last):
            return None
        return last

    def _fresh(self, trade: OpenTrade, quote: BestQuote) -> bool:
        return trade.time_barrier - quote.received_at <= self.rules.quote_wait

    def _exit(self, trade: OpenTrade, at: datetime, price: float, reason: str, quote: BestQuote) -> None:
        paid = costs(trade.entry_price, price, trade.quantity, self.rules.fee_rate)
        self.store.exit(trade, Exit(at, price, reason, paid.sell_fee, paid.tax, quote.received_at))
        self._open = None
        net = (price - trade.entry_price) * trade.quantity - trade.buy_fee - paid.sell_fee - paid.tax
        log.info(
            "trader: sold %s at %s (%s, trade %s): net %.0f won", trade.quantity, price, reason, trade.id, net
        )
