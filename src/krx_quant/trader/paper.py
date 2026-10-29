"""Paper trading by the triple barrier (#393 "모의 체결", #394), on the cells picked from the cost map
(#395, ``selection.py``) under the champion policy (#399, ``policy.apply``).

**Entry.** A prediction of a cell, as the trader reads it, buys when all hold, else it is skipped
with its reason:

- its cell is among those picked (``not_picked``: the picks changed while it was read);
- it was made (status ok) and the engine did not block it (a final consensus, #88): ``not_ok``,
  ``blocked``. A prediction the engine made none of for a VI (status ``vi``) and a final
  consensus it blocked for a special situation are ``special``;
- it was displayed at most ``Rules.fresh`` before it was read (``stale``: a trader that was
  stopped does not act on the predictions it missed), and it is read before its time barrier
  (``expired``: an engine catching up displays an overdue bar close's prediction late, and a
  barrier prediction carries no block of its own for it);
- its most likely class is the kind's rise (up, up_first; a tie goes to the middle class, then
  the first, ``kinds.most_likely``) and that class's probability is at least the threshold θ
  (its cell's under the champion policy, ``Pick.threshold``, #399; before the first,
  ``Rules.threshold``): ``not_up``, ``below_threshold``;
- it has its reference: the bar close's mid-point, tick and width (``no_reference``);
- the day trades (``closed``); its bar close is at least five minutes after the open (09:05,
  ``before_entry``), and its time barrier, the bar close plus the kind's horizon, at least five
  minutes before the closing auction (15:15, ``past_last_exit``): every position is closed
  before the closing auction, and none is opened in the opening one;
- no special situation the engine knows of when it is read touches its window, from its bar
  close to its time barrier (``special``): a VI of 005930, a circuit breaker, a sidecar
  (``special_windows``, #236; the open span, ``special.touching``, as the engine blocks a final
  consensus);
- nothing is held, nor waiting to be bought (``holding``): one position at a time;
- no cell ranked above it buys on the same bar close (``outranked``).

**Priority.** Of the rises of one bar close, the cell ranked first (``Selection.picks``: the
champion policy's order, #399; before the first, the greatest hit rate's lower bound less p*)
buys. The cells' predictions of a bar close come at
different times (a baseline's at once, an algorithm's within its time budget, the final consensus
after every member), so a rise waits for the cells ranked above it: it is decided once each of
them has a prediction of that bar close read, or at the latest ``Rules.fresh`` after it was
displayed (it would be stale after). A higher-ranked cell whose prediction comes later than that
finds the position taken (``holding``).

**Size.** The cell's Kelly fraction (``Pick.fraction``: λ of its Kelly f*, cut to 0–1; λ the
champion policy's for the cell, #399, before the first ``KELLY_FRACTION``, a half) of the
capital, over the best ask, rounded down: ⌊min(capital − what is open, fraction × capital) ÷
ask⌋ (nothing is open: one position at a time). Below one share it buys nothing
(``no_shares``).

Its reference price is the prediction's ``anchor`` (the bar close's integrated mid-point), the
target ``anchor + w × tick``, the stop ``anchor − w × tick`` (w = k for the direction), the time
barrier the bar close plus the kind's horizon: the trade is the event its hit rate measured.

**Fills** on the collector's best quotes (``quotes.py``), in the order received:

- the buy at the best ask of the first quote received after the decision. None if a special
  window learned before that quote touches its window (``special``), if that quote's ask is at or
  above the target (``target_reached``: it was reached already), if the size is
  below one share (``no_shares``), if no quote comes within ``Rules.quote_wait``
  (``no_quote``), or if it comes at or after the time barrier (``expired``). The take-profit's
  sell rests at the target from then on;
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
waiting for is given up (``restarted``). The same picks, the same predictions read at the same
times, and the same quotes always make the same trades.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from krx_quant.feeds.base import kst_day
from krx_quant.kinds import most_likely
from krx_quant.market_calendar import Session
from krx_quant.pricing import SELL_TAX
from krx_quant.special import Window, touching
from krx_quant.trader import CAPITAL, ENTRY_AFTER_OPEN, EXIT_BEFORE_AUCTION, PAPER, SYMBOL, THRESHOLD
from krx_quant.trader.cells import Cell, Signal
from krx_quant.trader.quotes import BestQuote
from krx_quant.trader.selection import Pick, Selection
from krx_quant.trader.store import Entry, Exit, OpenTrade, TraderStore

log = logging.getLogger(__name__)

# Why a trade ended.
TAKE_PROFIT, STOP_LOSS, TIME = "take_profit", "stop_loss", "time"
# Why a prediction bought nothing.
NOT_PICKED, NOT_OK, BLOCKED, SPECIAL = "not_picked", "not_ok", "blocked", "special"
STALE, EXPIRED = "stale", "expired"
NOT_UP, BELOW_THRESHOLD, NO_REFERENCE = "not_up", "below_threshold", "no_reference"
CLOSED, BEFORE_ENTRY, PAST_LAST_EXIT = "closed", "before_entry", "past_last_exit"
HOLDING, OUTRANKED = "holding", "outranked"
# Why a buy decided did not fill.
TARGET_REACHED, NO_SHARES, NO_QUOTE, RESTARTED = "target_reached", "no_shares", "no_quote", "restarted"
BUY, SKIP = "buy", "skip"
# The status of a prediction the engine did not make for a VI (``engine._record_not_predicted``).
VI_STATUS = "vi"


@dataclass(frozen=True)
class Rules:
    threshold: float = THRESHOLD
    capital: int = CAPITAL
    fee_rate: float = 0.00015  # the commission per side, as a fraction
    entry_after_open: timedelta = ENTRY_AFTER_OPEN
    exit_before_auction: timedelta = EXIT_BEFORE_AUCTION
    # A buy waits this long for its first quote; a time exit needs a quote this recent.
    quote_wait: timedelta = timedelta(seconds=5)
    # A prediction read later than this after its display is not acted on; a rise waits for the
    # cells ranked above it until this long after its display at most.
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


def shares(fraction: float, capital: float, ask: float) -> int:
    """What a buy at ``ask`` takes: ``fraction`` of the capital, rounded down to whole shares."""
    return math.floor(min(capital, fraction * capital) / ask)


def plan(
    signal: Signal,
    rules: Rules,
    session: Session | None,
    at: datetime,
    special: Sequence[Window] = (),
    threshold: float | None = None,
) -> Plan | str:
    """A prediction read at ``at``, with the special windows known then: its buy's barriers, or why
    it buys nothing (the reasons the module doc lists, but the trader's own: ``not_picked``,
    ``holding``, ``outranked``). ``threshold`` is its cell's θ under the policy (#399), else
    ``rules.threshold``. The nightly re-fit simulates the trades by the same rules (``refit.py``)."""
    if signal.status == VI_STATUS:
        return SPECIAL
    p = signal.rise_probability
    if signal.status != "ok" or signal.probabilities is None or p is None:
        return NOT_OK
    if signal.blocked is not None:
        return SPECIAL if signal.blocked == SPECIAL else BLOCKED
    if at - signal.displayed_at > rules.fresh:
        return STALE
    if at >= signal.bar_close + signal.horizon:
        return EXPIRED
    if most_likely(signal.probabilities) != signal.rise:
        return NOT_UP
    if p < (rules.threshold if threshold is None else threshold):
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
    if touching(special, signal.symbol, signal.bar_close, barrier):
        return SPECIAL
    move = signal.width * signal.tick
    return Plan(signal.anchor + move, signal.anchor - move, barrier)


@dataclass(frozen=True)
class _Candidate:
    """A rise of a picked cell, read at ``read_at``, waiting for the cells ranked above it."""

    signal: Signal
    plan: Plan
    read_at: datetime

    @property
    def cell(self) -> Cell:
        return Cell(self.signal.kind, self.signal.predictor)

    def deadline(self, rules: Rules) -> datetime:
        return self.signal.displayed_at + rules.fresh


@dataclass(frozen=True)
class _Waiting:
    """A buy decided, waiting for the first quote received after its decision."""

    decision: int
    signal: Signal
    pick: Pick
    plan: Plan
    decided_at: datetime

    def deadline(self, rules: Rules) -> datetime:
        return self.decided_at + rules.quote_wait


class PaperTrader:
    """The picked cells' paper trades, from the picks (``select``), the special windows
    (``on_special``), the cells' predictions (``on_signal``), the quotes (``on_quote``) and the
    clock (``on_time``), into ``store``. ``sessions`` gives a day's session (the market
    calendar's); ``started_at`` is when this run began. Until the first picks it trades nothing."""

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
        self._selection: int | None = None  # the picks' row in the store
        self._policy: int | None = None  # the policy version the picks are under (#399)
        self._picks: dict[Cell, tuple[int, Pick]] = {}  # each picked cell's rank and pick
        self._special: list[Window] = []
        self._read: dict[datetime, set[Cell]] = {}  # the cells read of each recent bar close
        self._pending: dict[datetime, list[_Candidate]] = {}  # rises waiting, by bar close
        self._last: BestQuote | None = None  # the latest quote received
        self._waiting: _Waiting | None = None
        self._open: OpenTrade | None = store.open_trade(mode)
        if self._open is not None:
            log.info("trader: going on with trade %s, open since before this start", self._open.id)
        dropped = store.drop_unfilled(mode, RESTARTED)
        if dropped:
            log.info("trader: %s buy(s) waiting for a quote when the trader stopped: given up", dropped)

    @property
    def cells(self) -> list[Cell]:
        """The cells picked, first ranked first."""
        return [c for c, _ in sorted(self._picks.items(), key=lambda item: item[1][0])]

    def select(self, selection: Selection, at: datetime) -> None:
        """The cells to trade from ``at`` on, kept in the store; a rise waiting of a cell no longer
        picked is skipped (``not_picked``)."""
        self._selection = self.store.add_selection(self.mode, at, selection)
        self._policy = selection.policy
        self._picks = {p.cell: (rank, p) for rank, p in enumerate(selection.picks)}
        if selection.picks:
            log.info("trader: trading %s cell(s): %s", len(selection.picks), selection.describe())
        else:
            log.info("trader: trading no cell until the next pick: %s", selection.describe())
        for close in list(self._pending):
            kept = [c for c in self._pending[close] if c.cell in self._picks]
            for c in self._pending[close]:
                if c.cell not in self._picks:
                    self._skip(c.signal, at, NOT_PICKED)
            if kept:
                self._pending[close] = kept
            else:
                del self._pending[close]
        self._settle(at)

    def on_special(self, windows: Sequence[Window]) -> None:
        """The special windows the engine knows of now (the latest of each). A buy decided but not
        filled yet whose window one of them touches is given up (``special``)."""
        self._special = list(windows)
        waiting = self._waiting
        if waiting is not None and touching(
            self._special, waiting.signal.symbol, waiting.signal.bar_close, waiting.plan.time_barrier
        ):
            self._give_up(waiting, SPECIAL)

    def on_signal(self, signal: Signal, at: datetime) -> None:
        """A prediction of a cell, read at ``at``; one already decided on (a restart) is passed over."""
        cell = Cell(signal.kind, signal.predictor)
        self._read.setdefault(signal.bar_close, set()).add(cell)
        if self.store.decided(self.mode, signal.kind, signal.predictor, signal.prediction_id):
            return
        if cell not in self._picks:
            self._skip(signal, at, NOT_PICKED)
        else:
            session = self._sessions(kst_day(signal.bar_close))
            _, picked = self._picks[cell]
            planned = plan(signal, self.rules, session, at, self._special, picked.threshold)
            if not isinstance(planned, Plan):
                self._skip(signal, at, planned)
            elif self._busy():
                self._skip(signal, at, self._held_off(signal))
            else:
                self._pending.setdefault(signal.bar_close, []).append(_Candidate(signal, planned, at))
        self._settle(at)

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
        """The clock: a rise that waited long enough for the cells ranked above it is decided, a
        buy whose quote did not come is given up, a time barrier passed is sold on the last quote
        before it."""
        self._settle(now)
        grace = self.rules.grace
        waiting = self._waiting
        if waiting is not None and now >= waiting.deadline(self.rules) + grace:
            self._give_up(waiting, NO_QUOTE)
        trade = self._open
        if trade is not None and now >= trade.time_barrier + grace:
            standing = self._standing(trade)
            if standing is not None:
                self._exit(trade, trade.time_barrier, standing.bid, TIME, standing)
        for close in [c for c in self._read if c < now - timedelta(hours=1) and c not in self._pending]:
            del self._read[close]

    def _busy(self) -> bool:
        return self._open is not None or self._waiting is not None

    def _rank(self, cell: Cell) -> int:
        return self._picks[cell][0] if cell in self._picks else len(self._picks)

    def _held_off(self, signal: Signal) -> str:
        """Why a rise does not buy while something is held or bought: ``outranked`` when that is a
        cell ranked above it of the same bar close, else ``holding``."""
        if self._waiting is not None:
            close, kind, predictor = (
                self._waiting.signal.bar_close,
                self._waiting.signal.kind,
                self._waiting.signal.predictor,
            )
        elif self._open is not None:
            close, kind, predictor = self._open.bar_close, self._open.kind, self._open.predictor
        else:
            return HOLDING
        mine = self._rank(Cell(signal.kind, signal.predictor))
        if close == signal.bar_close and self._rank(Cell(kind, predictor)) < mine:
            return OUTRANKED
        return HOLDING

    def _settle(self, now: datetime) -> None:
        """Decide the rises waiting, oldest bar close first: of a bar close, the first ranked buys
        once every cell ranked above it was read of that bar close, or its wait is over; the others
        are outranked (or held off, when something was bought meanwhile). A rise a special window
        learned while it waited now touches is ``special``."""
        for close in sorted(self._pending):
            waiting = []
            for c in sorted(self._pending[close], key=lambda c: self._picks[c.cell][0]):
                if touching(self._special, c.signal.symbol, close, c.plan.time_barrier):
                    self._skip(c.signal, now, SPECIAL)
                else:
                    waiting.append(c)
            if not waiting:
                del self._pending[close]
                continue
            self._pending[close] = waiting
            best = waiting[0]
            rank = self._picks[best.cell][0]
            read = self._read.get(close, set())
            above = [cell for cell, (r, _) in self._picks.items() if r < rank]
            if now < best.deadline(self.rules) and any(cell not in read for cell in above):
                return  # a later bar close waits too: this one may still buy
            del self._pending[close]
            if self._busy():  # bought on another bar close meanwhile
                for c in waiting:
                    self._skip(c.signal, now, self._held_off(c.signal))
                continue
            self._decide(best, now)
            for c in waiting[1:]:
                self._skip(c.signal, now, OUTRANKED)

    def _decide(self, candidate: _Candidate, at: datetime) -> None:
        signal, planned = candidate.signal, candidate.plan
        _, pick = self._picks[candidate.cell]
        decision = self._add_decision(signal, at, BUY, None)
        log.info(
            "trader: buying on %s %s of %s (p %.3f): target %s, stop %s, until %s, %.0f%% of the capital",
            signal.kind,
            signal.predictor,
            signal.bar_close.strftime("%H:%M"),
            signal.rise_probability,
            planned.target,
            planned.stop,
            planned.time_barrier.strftime("%H:%M"),
            pick.fraction * 100,
        )
        self._waiting = _Waiting(decision, signal, pick, planned, at)

    def _skip(self, signal: Signal, at: datetime, reason: str) -> None:
        self._add_decision(signal, at, SKIP, reason)

    def _add_decision(self, signal: Signal, at: datetime, action: str, reason: str | None) -> int:
        return self.store.add_decision(
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
            self._selection,
        )

    def _buy(self, waiting: _Waiting, quote: BestQuote) -> None:
        if quote.received_at <= waiting.decided_at:
            return  # received before the decision, though it came after it
        if quote.received_at >= waiting.plan.time_barrier:
            self._give_up(waiting, EXPIRED)
            return
        if quote.received_at > waiting.deadline(self.rules):
            self._give_up(waiting, NO_QUOTE)
            return
        if quote.ask >= waiting.plan.target:
            self._give_up(waiting, TARGET_REACHED)
            return
        pick = waiting.pick
        quantity = shares(pick.fraction, self.rules.capital, quote.ask)  # nothing open: one at a time
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
                threshold=self.rules.threshold if pick.threshold is None else pick.threshold,
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
                selection_id=self._selection,
                role=pick.role,
                p_star=pick.p_star,
                low=pick.low,
                kelly=pick.kelly,
                fraction=pick.fraction,
                policy_id=self._policy,
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
        """Whether ``quote`` was received before the barrier, within ``Rules.quote_wait`` of it."""
        return timedelta(0) < trade.time_barrier - quote.received_at <= self.rules.quote_wait

    def _exit(self, trade: OpenTrade, at: datetime, price: float, reason: str, quote: BestQuote) -> None:
        paid = costs(trade.entry_price, price, trade.quantity, self.rules.fee_rate)
        self.store.exit(trade, Exit(at, price, reason, paid.sell_fee, paid.tax, quote.received_at))
        self._open = None
        net = (price - trade.entry_price) * trade.quantity - trade.buy_fee - paid.sell_fee - paid.tax
        log.info(
            "trader: sold %s at %s (%s, trade %s): net %.0f won", trade.quantity, price, reason, trade.id, net
        )
