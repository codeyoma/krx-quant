"""The book (#652): the rules by which every book decides, one place for each. The paper book
(``paper.PaperTrader``, the champion's, a challenger's, a shadow's, the practice's), the KIS book on it
(``mock.py``) and the nightly re-fit's replay of the recorded days (``replay.Policy``) all call these:
the same predictions and the same quotes make the same decisions in each, and a rule changed here is
changed for all of them. What each rule does is in the paper book's module doc (``paper.py``), which
applies them all.

- **The entry** (``plan``): whether a prediction read may buy, and its barriers.
- **The priority** (``admit``, ``settle``): a rise waits for the cells ranked above it.
- **Why not now** (``not_now``, ``held_off``): the book's status, a stall, what it holds or waits to
  buy (``Taken``: the cell and bar close, for ``outranked``; a reason of the book's own, such as the KIS
  book's ``reconciling``), the risk rules (``risk_held``, ``cooldowns``, ``risk_state``) and the
  practice's spacing (``spaced``). Before #652 the paper book and the replay each wrote it
  (``PaperTrader._not_now``/``_held_off``, ``replay.Policy._busy``).
- **The buy's fill** (``refused``) and **the exits** (``exit_on``, ``clock_exit``; ``takes_profit`` and
  ``stopped``, which judge a bid or a day's array of them, so that the replay's search of its arrays
  and ``exit_on`` ask the same thing), and **a market halt's hold** (``halt_spans``, ``halted``).

The money of a round trip and the size are ``money.py``'s, the loss limits ``limits.py``'s. How a buy
decided fills is the fills' (the paper book on the collector's quotes, the KIS book's orders, the
replay's recorded arrays, ``replay.Fills``), never a rule's here.

What the replay leaves out on purpose stays out: it has no stall (a recorded gap leaves the signals
out instead) and spaces no practice buy (it replays policies alone).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Protocol, overload

import numpy as np

from krx_quant.feeds.base import KST, kst_day
from krx_quant.kinds import most_likely
from krx_quant.market_calendar import Session
from krx_quant.special import HALT_STATUS, Kind, Window, touching
from krx_quant.trader import (
    CAPITAL,
    COOLDOWN,
    CUMULATIVE_LOSS_PERCENT,
    DAILY_BUY_ORDERS,
    DAILY_LOSS_PERCENT,
    ENTRY_AFTER_OPEN,
    EXIT_BEFORE_AUCTION,
    LOSS_STREAK,
    PRACTICE_SPACING,
    RISK_PER_TRADE_PERCENT,
    THRESHOLD,
)
from krx_quant.trader.cells import Cell, Signal, learned_at
from krx_quant.trader.db import iso
from krx_quant.trader.limits import RUNNING, Limits
from krx_quant.trader.quotes import BestQuote

if TYPE_CHECKING:
    from krx_quant.trader.settings import TraderSettings

# Why a trade ended.
TAKE_PROFIT, STOP_LOSS, TIME = "take_profit", "stop_loss", "time"
# Why a prediction bought nothing.
NOT_PICKED, NOT_OK, BLOCKED, SPECIAL = "not_picked", "not_ok", "blocked", "special"
STALE, EXPIRED = "stale", "expired"
NOT_UP, BELOW_THRESHOLD, NO_REFERENCE = "not_up", "below_threshold", "no_reference"
CLOSED, BEFORE_ENTRY, PAST_LAST_EXIT = "closed", "before_entry", "past_last_exit"
HOLDING, OUTRANKED = "holding", "outranked"
# The collector's quotes stalled (#407): nothing new is bought until they come again.
STALLED = "quotes_stalled"
# Why a buy decided did not fill.
TARGET_REACHED, NO_QUOTE, RESTARTED = "target_reached", "no_quote", "restarted"
# A buy below one share, or its stop not below the ask, takes none: ``money.NO_SHARES``, ``BAD_STOP``.
# The risk rules hold new buys (#471, ``risk_held``): the day's buy orders, the losses in a row.
BUY_LIMIT, COOLING_DOWN = "buy_limit", "cooldown"
# A practice buy too soon after the day's last (#598, ``spaced``).
SPACING = "spacing"
BUY, SKIP = "buy", "skip"
# The status of a prediction the engine did not make for a VI (``engine._record_not_predicted``), and
# for a market halt (#625).
VI_STATUS = "vi"
NOT_MADE = (VI_STATUS, HALT_STATUS)


@dataclass(frozen=True)
class Rules:
    threshold: float = THRESHOLD
    capital: int = CAPITAL
    daily_loss_percent: float = DAILY_LOSS_PERCENT  # the loss limits, of the capital (#396)
    cumulative_loss_percent: float = CUMULATIVE_LOSS_PERCENT
    # The risk rules (#471, module doc "Size" and "Risk rules"): a trade's loss budget in percent of
    # the capital it trades on; the net losses in a row that start a cooldown; the buy orders a day.
    risk_percent: float = RISK_PER_TRADE_PERCENT
    loss_streak: int = LOSS_STREAK
    cooldown: timedelta = COOLDOWN
    daily_buys: int = DAILY_BUY_ORDERS
    # The least time between two practice buys' bar closes (#598, ``spaced``): the practice's alone.
    practice_spacing: timedelta = PRACTICE_SPACING
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

    @property
    def limits(self) -> Limits:
        return Limits(self.capital, self.daily_loss_percent, self.cumulative_loss_percent)

    @classmethod
    def of(cls, trader: TraderSettings, fee_rate: float) -> Rules:
        """The rules the trader's settings (``.env``, #394) make, at the engine's commission
        (``KRX_FEE_PERCENT``): the trader's and the nightly re-fit's replay's."""
        return cls(
            threshold=trader.threshold,
            capital=trader.capital,
            daily_loss_percent=trader.daily_loss_percent,
            cumulative_loss_percent=trader.cumulative_loss_percent,
            risk_percent=trader.risk_per_trade_percent,
            loss_streak=trader.loss_streak,
            cooldown=timedelta(minutes=trader.cooldown_minutes),
            daily_buys=trader.daily_buy_orders,
            practice_spacing=timedelta(minutes=trader.practice_spacing_minutes),
            fee_rate=fee_rate,
        )

    def budget(self, capital: float) -> float:
        """A trade's loss budget in won, on ``capital`` (the live first period's cap below the
        capital): ``risk_percent`` of it (#471)."""
        return capital * self.risk_percent / 100


@dataclass(frozen=True)
class Plan:
    """A buy's barriers: the target, the stop and the time barrier."""

    target: float
    stop: float
    time_barrier: datetime


def plan(
    signal: Signal,
    rules: Rules,
    session: Session | None,
    at: datetime,
    special: Sequence[Window] = (),
    threshold: float | None = None,
) -> Plan | str:
    """A prediction read at ``at``, with the special windows known then: its buy's barriers, or why
    it buys nothing (the reasons the paper module doc lists, but the trader's own: ``not_picked``,
    ``holding``, ``outranked``). ``threshold`` is its cell's θ under the policy (#399), else
    ``rules.threshold``. The nightly re-fit replays the recorded days by the same rules (``replay.py``)."""
    if signal.status in NOT_MADE:
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
class Rise:
    """A rise of a picked cell, read at ``read_at``, waiting for the cells ranked above it."""

    signal: Signal
    plan: Plan
    read_at: datetime

    @property
    def cell(self) -> Cell:
        return Cell(self.signal.kind, self.signal.predictor)

    def deadline(self, rules: Rules) -> datetime:
        return self.signal.displayed_at + rules.fresh


# The rules a book follows, as functions (#470): the paper book's and the re-fit's replay of the
# recorded days (``replay.py``) call these, so that both make the same trades.

# A book's answers to the priority (``admit``, ``settle``): ``skip`` keeps that a prediction bought
# nothing and why; ``held`` says why the book buys nothing now (its status, a stall, a position held
# or a buy waiting: ``holding``, ``outranked``), None if it may; ``decide`` decides a rise's buy.
Skip = Callable[[Signal, datetime, str], None]
Held = Callable[[Signal, datetime], str | None]
Decide = Callable[[Rise, datetime], None]


def admit(
    pending: dict[datetime, list[Rise]],
    signal: Signal,
    planned: Plan | str,
    at: datetime,
    skip: Skip,
    held: Held,
) -> bool:
    """A picked cell's prediction read at ``at``, with its plan (``plan``): skipped with why it buys
    nothing (its plan's reason, or why the book buys nothing now), else waiting among its bar close's
    rises for the cells ranked above it (``settle``). Whether it waits."""
    if not isinstance(planned, Plan):
        skip(signal, at, planned)
        return False
    why = held(signal, at)
    if why is not None:
        skip(signal, at, why)
        return False
    pending.setdefault(signal.bar_close, []).append(Rise(signal, planned, at))
    return True


def settle(
    pending: dict[datetime, list[Rise]],
    read: Mapping[datetime, Collection[Cell]],
    ranks: Mapping[Cell, int],
    special: Sequence[Window],
    now: datetime,
    rules: Rules,
    *,
    skip: Skip,
    held: Held,
    decide: Decide,
) -> None:
    """Decide the rises waiting (paper module doc "Priority"), oldest bar close first: of a bar close, the
    first ranked buys once every cell ranked above it (``ranks``, 0 first) was read of that bar close
    (``read``), or its wait is over; the others are outranked (or held off, when something was bought
    meanwhile). A rise a special window known now touches is ``special``. A bar close still waiting
    holds the later ones too: they are decided after it."""
    for close in sorted(pending):
        waiting = []
        for c in sorted(pending[close], key=lambda c: ranks[c.cell]):
            if touching(special, c.signal.symbol, close, c.plan.time_barrier):
                skip(c.signal, now, SPECIAL)
            else:
                waiting.append(c)
        if not waiting:
            del pending[close]
            continue
        pending[close] = waiting
        best = waiting[0]
        rank = ranks[best.cell]
        seen = read.get(close, ())
        if now < best.deadline(rules) and any(r < rank and cell not in seen for cell, r in ranks.items()):
            return  # a later bar close waits too: this one may still buy
        del pending[close]
        why = held(best.signal, now)
        if why is not None:  # paused, stopped or stalled meanwhile, or bought on another bar close
            for c in waiting:
                skip(c.signal, now, held(c.signal, now) or why)
            continue
        decide(best, now)
        for c in waiting[1:]:
            skip(c.signal, now, OUTRANKED)


class Barriers(Protocol):
    """A buy's barriers: a ``Plan``'s, an open trade's."""

    @property
    def target(self) -> float: ...

    @property
    def stop(self) -> float: ...

    @property
    def time_barrier(self) -> datetime: ...


def refused(decided_at: datetime, barriers: Barriers, quote: BestQuote, rules: Rules) -> str | None:
    """Why the first quote received after a buy's decision at ``decided_at`` does not fill it (module
    doc "Fills"): received at or after its time barrier (``expired``), later than
    ``Rules.quote_wait`` after the decision (``no_quote``), its ask at or above the target
    (``target_reached``); None: it fills at that ask, if the size makes a share (``size``)."""
    if quote.received_at >= barriers.time_barrier:
        return EXPIRED
    if quote.received_at > decided_at + rules.quote_wait:
        return NO_QUOTE
    if quote.ask >= barriers.target:
        return TARGET_REACHED
    return None


def risk_held(rules: Rules, at: datetime, buys: int, exits: Sequence[tuple[datetime, float]]) -> str | None:
    """Why the risk rules hold a book's new buy at ``at`` (#471, module doc "Risk rules"), from its buy
    orders of ``at``'s day (``buys``) and its trades exited that day (``exits``: each one's exit time and
    net, in the order they exited): ``buy_limit`` at ``rules.daily_buys`` of them, ``cooldown`` less than
    ``rules.cooldown`` after the exit that made ``rules.loss_streak`` net losses in a row; None if
    neither holds."""
    if buys >= rules.daily_buys:
        return BUY_LIMIT
    began, _ = cooldowns(rules, exits)
    return COOLING_DOWN if began and at < began[-1][1] else None


def spaced(rules: Rules, bar_close: datetime, last: datetime | None) -> str | None:
    """Why the practice's spacing holds a practice rise of ``bar_close`` (#598, module doc "Practice"):
    ``spacing`` when it is less than ``rules.practice_spacing`` after ``last``, the bar close of the
    book's last practice buy order that day; None if it is not, or none was."""
    if last is None or bar_close >= last + rules.practice_spacing:
        return None
    return SPACING


def cooldowns(
    rules: Rules, exits: Sequence[tuple[datetime, float]]
) -> tuple[list[tuple[datetime, datetime]], int]:
    """The cooldowns a book's ``exits`` of a day began (#471, module doc "Risk rules"; each exit's time
    and net, in the order they exited): each exit that made ``rules.loss_streak`` net losses in a row or
    more, with when it ends; and the net losses in a row at the last exit."""
    losses, began = 0, []
    for exited_at, net in exits:
        losses = losses + 1 if net < 0 else 0
        if losses >= rules.loss_streak:
            began.append((exited_at, exited_at + rules.cooldown))
    return began, losses


def risk_state(
    rules: Rules, at: datetime, buys: int, exits: Sequence[tuple[datetime, float]], capital: float
) -> dict[str, Any]:
    """The risk rules' state at ``at`` (#475, ``GET /v1/trader``'s ``risk``), from a book's buy orders
    of ``at``'s day and its exits that day (``risk_held``'s): the loss budget (its percent of
    ``capital``, the capital a buy sizes on, and in won), the buy orders and their limit, the net
    losses in a row and how many start a cooldown, the cooldown's minutes, those begun that day and the
    end of the one in force (None when none is), and what holds a buy now (``buy_limit``,
    ``cooldown``; None)."""
    began, losses = cooldowns(rules, exits)
    until = began[-1][1] if began and at < began[-1][1] else None
    return {
        "day": kst_day(at).isoformat(),
        "budget_percent": rules.risk_percent,
        "budget": rules.budget(capital),
        "buys": buys,
        "daily_buys": rules.daily_buys,
        "losses": losses,
        "loss_streak": rules.loss_streak,
        "cooldown_minutes": rules.cooldown.total_seconds() / 60,
        "cooldowns": len(began),
        "cooldown_until": None if until is None else iso(until),
        "held": risk_held(rules, at, buys, exits),
    }


@dataclass(frozen=True)
class Taken:
    """What a book holds, or waits to buy: its signal's cell and bar close (``held_off``)."""

    cell: Cell
    bar_close: datetime


# What a book is busy with, as ``not_now`` asks: nothing (None), a position held or a buy waiting
# (``Taken``), or a reason of the book's own (the KIS book's ``reconciling``; ``holding`` for an order in
# flight of no signal known).
Busy = Taken | str | None


def held_off(signal: Signal, taken: Taken, rank: Callable[[Cell], int]) -> str:
    """Why a rise does not buy while ``taken`` is held or bought: ``outranked`` when that is of the
    same bar close and of a cell ranked above it (``rank``, 0 first), else ``holding``."""
    mine = rank(Cell(signal.kind, signal.predictor))
    if taken.bar_close == signal.bar_close and rank(taken.cell) < mine:
        return OUTRANKED
    return HOLDING


def not_now(
    signal: Signal,
    at: datetime,
    *,
    status: str,
    busy: Busy,
    rank: Callable[[Cell], int],
    risk: Callable[[datetime], str | None],
    stalled: bool = False,
    spacing: Callable[[Signal, datetime], str | None] | None = None,
) -> str | None:
    """Why a rise read or decided at ``at`` buys nothing now (paper module doc "Entry"), in this order:
    the book's ``status`` when it is not running (a pause, a loss limit, #396, #471), the quotes
    ``stalled`` (#407: the replay has no stall), what it is ``busy`` with (``held_off``), the risk rules
    (``risk``, #471: ``risk_held`` on the book's day), a practice buy's ``spacing`` (#598: the live
    books' alone); None if it may buy. ``risk`` and ``spacing`` are asked last, and only then: the
    paper book reads them from its store."""
    if status != RUNNING:
        return status
    if stalled:
        return STALLED
    if isinstance(busy, str):
        return busy
    if busy is not None:
        return held_off(signal, busy, rank)
    return risk(at) or (None if spacing is None else spacing(signal, at))


def mid_of(quote: BestQuote | None) -> float | None:
    """A quote's mid-point (#475: a leg's execution cost is from the one the book knew at its decision)."""
    return None if quote is None else (quote.bid + quote.ask) / 2


def midnight(at: datetime) -> datetime:
    """00:00 KST of ``at``'s day: where a day's risk rules count from (#471)."""
    return datetime.combine(kst_day(at), time(), KST)


@dataclass(frozen=True)
class Sale:
    """An exit: sold at ``price`` at ``at`` for ``reason``, on ``quote`` (the one whose bid it took)."""

    at: datetime
    price: float
    reason: str
    quote: BestQuote


def fresh_before(barrier: datetime, quote: BestQuote, rules: Rules) -> bool:
    """Whether ``quote`` was received before ``barrier``, within ``Rules.quote_wait`` of it: the quote
    a time exit sells on at the barrier."""
    return timedelta(0) < barrier - quote.received_at <= rules.quote_wait


@overload
def takes_profit(bid: float, target: float) -> bool: ...
@overload
def takes_profit(bid: np.ndarray, target: float) -> np.ndarray: ...
def takes_profit(bid: float | np.ndarray, target: float) -> bool | np.ndarray:
    """Whether a best bid, or each of a day's array of them (the replay's search, ``replay.Fills``),
    reaches the take-profit's ``target``: the paper sell resting there fills (no place at the front of
    the queue is assumed)."""
    return bid >= target


@overload
def stopped(bid: float, stop: float) -> bool: ...
@overload
def stopped(bid: np.ndarray, stop: float) -> np.ndarray: ...
def stopped(bid: float | np.ndarray, stop: float) -> bool | np.ndarray:
    """Whether a best bid, or each of an array of them, is at or below the ``stop``: the stop sells at
    it."""
    return bid <= stop


def exit_on(
    barriers: Barriers,
    quote: BestQuote,
    standing: BestQuote | None,
    rules: Rules,
    ran_at_barrier: bool = True,
) -> Sale | None:
    """The exit ``quote``, received after the buy, makes of a position (paper module doc "Fills"),
    ``standing`` the latest quote received before it: at or after the time barrier, the time exit at
    the standing quote's bid at the barrier if it is recent (``fresh_before``) and the trader ran at
    the barrier, else at this one's bid now; before it, the take-profit at the target once its bid
    reaches it, the stop at its bid at or below the stop. None: held on."""
    if quote.received_at >= barriers.time_barrier:
        if ran_at_barrier and standing is not None and fresh_before(barriers.time_barrier, standing, rules):
            return Sale(barriers.time_barrier, standing.bid, TIME, standing)
        return Sale(quote.received_at, quote.bid, TIME, quote)
    if takes_profit(quote.bid, barriers.target):
        return Sale(quote.received_at, barriers.target, TAKE_PROFIT, quote)
    if stopped(quote.bid, barriers.stop):
        return Sale(quote.received_at, quote.bid, STOP_LOSS, quote)
    return None


def halt_spans(windows: Sequence[Window]) -> list[tuple[datetime, datetime]]:
    """The spans in which a market halt holds a paper book (#625): from when the engine had learned
    it, or its start if later, to its end. A paper book takes no quote received in one (the reopening
    auction's book is not one to trade on) and makes no time exit by the clock in one; a recorded gap
    that begins in one is the halt's silence, not data lost. The paper book and the re-fit's replay
    (``replay.Fills``) both go by these spans."""
    return [
        (max(learned_at(w), w.start), w.end)
        for w in windows
        if w.kind is Kind.MARKET_HALT and max(learned_at(w), w.start) < w.end
    ]


def halted(spans: Sequence[tuple[datetime, datetime]], at: datetime) -> bool:
    """Whether ``at`` is in one of a market halt's ``spans`` (``halt_spans``)."""
    return any(start <= at < end for start, end in spans)


def clock_exit(
    barriers: Barriers, last: BestQuote | None, now: datetime, rules: Rules, ran_at_barrier: bool = True
) -> Sale | None:
    """The time exit the clock makes at ``now``, ``Rules.grace`` past the time barrier, before a quote
    at or after it came: at the bid of ``last``, the latest quote received, at the barrier, if it is
    recent (``fresh_before``) and the trader ran at the barrier. None: it waits for the next quote."""
    if now < barriers.time_barrier + rules.grace or not ran_at_barrier or last is None:
        return None
    if not fresh_before(barriers.time_barrier, last, rules):
        return None
    return Sale(barriers.time_barrier, last.bid, TIME, last)
