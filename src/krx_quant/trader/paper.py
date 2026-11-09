"""Paper trading by the triple barrier (#393 "모의 체결", #394), on the cells picked from the cost map
(#395, ``selection.py``) under the champion policy (#399, ``policy.apply``).

**Entry.** A prediction of a cell, as the trader reads it, buys when all hold, else it is skipped
with its reason:

- its cell is among those picked (``not_picked``: the picks changed while it was read), and its
  predictor is active (``predictor_inactive``, #603: a policy's cell whose predictor a pick found on
  probation, on the bench, retired or a clone in the review cycle, ``Selection.inactive``; read, its
  prediction skipped, and ranked below none);
- it was made (status ok) and the engine did not block it (a final consensus, #88): ``not_ok``,
  ``blocked``. A prediction the engine made none of for a VI (status ``vi``) and a final
  consensus it blocked for a special situation are ``special``;
- it was displayed at most ``Rules.fresh`` before it was read (``stale``: a trader that was
  stopped does not act on the predictions it missed), and it is read before its time barrier
  (``expired``: an engine catching up displays an overdue bar close's prediction late, and a
  barrier prediction carries no block of its own for it);
- its most likely class is the kind's rise (up, up_first; a tie goes to the middle class, then
  the first, ``kinds.most_likely``) and that class's probability is at least the threshold θ
  (its cell's under the champion policy, ``Pick.threshold``, #399; a pick without one, the mock
  practice cell's, ``Rules.threshold``): ``not_up``, ``below_threshold``;
- it has its reference: the bar close's mid-point, tick and width (``no_reference``);
- the day trades (``closed``); its bar close is at least five minutes after the open (09:05,
  ``before_entry``), and its time barrier, the bar close plus the kind's horizon, at least five
  minutes before the closing auction (15:15, ``past_last_exit``): every position is closed
  before the closing auction, and none is opened in the opening one;
- no special situation the engine knows of when it is read touches its window, from its bar
  close to its time barrier (``special``): a VI of 005930, a circuit breaker, a sidecar
  (``special_windows``, #236; the open span, ``special.touching``, as the engine blocks a final
  consensus);
- the trader is running (#396, ``limits.py``): not paused (``entries_paused``, ``paused``) nor
  stopped by a loss limit (``daily_limit``, ``cumulative_limit``), the status its reason; a book of a
  challenger or a shadow, by its own simulated loss limits (#471, ``limits.Simulated``);
- the collector's quotes flow (``quotes_stalled``, #407, ``stall.py``): none for more than five
  seconds in continuous trading is a stall, and nothing new is bought until a quote comes again;
- nothing is held, nor waiting to be bought (``holding``): one position at a time;
- no cell ranked above it buys on the same bar close (``outranked``);
- the risk rules let the book buy (#471, ``risk_held``, module doc "Risk rules"): ``buy_limit``,
  ``cooldown``;
- a practice pick's bar close is at least ``Rules.practice_spacing`` after the bar close of the
  book's last practice buy order that day (#598, ``spaced``, module doc "Practice"): ``spacing``.

**Priority.** Of the rises of one bar close, the cell ranked first (``Selection.picks``: the
champion policy's order, #399; picks without a policy, the greatest hit rate's lower bound less
p*) buys. The cells' predictions of a bar close come at
different times (a baseline's at once, an algorithm's within its time budget, the final consensus
after every member), so a rise waits for the cells ranked above it: it is decided once each of
them has a prediction of that bar close read, or at the latest ``Rules.fresh`` after it was
displayed (it would be stale after). A higher-ranked cell whose prediction comes later than that
finds the position taken (``holding``).

**Size** (#471, spec #468 4 "크기", ``size``). A trade's loss budget over what a share loses at its
stop, rounded down: ⌊budget ÷ (ask − stop + one tick + the commissions and the sell tax)⌋. The budget
is ``Rules.risk_percent`` (``KRX_TRADER_RISK_PER_TRADE_PERCENT``, 0.5%) of the capital; a share's loss
(``money.at_risk``) is the best ask it is bought at less the stop, the commissions and the sell tax at those
prices (the buy's at the ask, the sale's at the stop) and one adverse tick (the stop sells at the
first best bid at or below the stop: a tick under it, the stop's band's, at worst). The spread is not
counted again: the ask is above the bid by it already. Within the cash and the capital, the buy's commission
included: ⌊min(capital, cash) ÷ (ask × (1 + commission))⌋ at most (nothing is open: one position at a
time). The cash is the paper account's (#396): the capital, plus the net P&L of every trade of the mode
exited, less the open position's cost. Below one share it buys nothing (``no_shares``), nor with a
stop that is not below the ask, or not a price (``bad_stop``: a fall through the stop before the buy,
which would stop it out at once). A cap below the capital (``cap``: the live mode's first period,
#408, ``live.py``) takes the capital's place in both. A pick of fixed shares (``Pick.quantity``: the
mock book's practice cell, #427) buys that many in place of its budget, within the same cash and
capital. The budget bounds the loss planned, not a gap past the stop.

> **바뀜** (2026-11-03, #478): the cell's Kelly fraction (λ × f*, cut to 0–1) of the capital over
> the ask → the loss budget over a share's loss at the stop. 이유: spec #468 "크기가 사실상 전액이다":
> with stops a few ticks away f* is mostly far above 1, the whole capital.

**Risk rules** (#471, spec #468 7 "위험 규칙", ``risk_held``), of every book (the champion's, a
challenger's, a shadow's, the practice's: #427's cell trades by the champion's rules but its shares),
from its own orders and trades of the day (00:00 KST on: the buys it placed, the trades it sold that
day, one carried overnight on the day it is sold; from the store, so a restart goes on with them;
the re-fit's replay keeps its own, whose trades are sold on their day): at ``Rules.daily_buys`` (20)
buy orders that day nothing more is bought (``buy_limit``: a paper buy is its fill; the KIS book's
each order it sent, those KIS refused counted, ``store.day_buys``); and ``Rules.loss_streak`` (3)
trades in a row that exited at a net loss start a cooldown of ``Rules.cooldown`` (30 minutes) from
the last one's exit (``cooldown``): a net of 0 or more ends the row, and each loss after the third
begins the cooldown again. Sells and cancels count for neither. ``.env`` may make them stricter,
never looser (``settings.py``).

**Practice** (#427, ``desk.Desk``): the mock book's practice cell is a pick like another, its trade
marked ``practice``, of no policy (``Entry.policy_id`` None: no record counts it), buying its shares
in place of a loss budget, by the same risk rules (#471: the champion's KIS book's, whose orders and
trades they are). No loss limit counts its P&L, realized or open (#533, ``limits.py`` "The
practice"); the cash does. Its paper shadow is a book of its own (``practice``), as the champion's
shadow is (``shadow``), so that the champion policy's shadow never holds a practice position.

Its buys are spaced (#598, ``spaced``): a practice rise buys only at a bar close at least
``Rules.practice_spacing`` (``KRX_TRADER_MOCK_PRACTICE_SPACING_MINUTES``, 15 minutes) after the bar
close of the book's last practice buy order that day (``store.last_practice_buy``: the KIS book's each
practice buy it sent or tried to send, as the day's buy orders count them; a paper book's each it
filled), else it is skipped ``spacing``. A buy given up before its order (``target_reached``,
``no_quote``) spaces nothing. By the bar closes, on the minute: the read's delay moves nothing, and a
restart goes on from the store. No champion's, challenger's or shadow's pick is spaced, and the re-fit's
replay, which replays policies alone, knows nothing of it. On 2026-11-04 and 11-05 the practice cell
(the direction's ``baseline-persist``, its rise always 1.00) bought its 20 by 13:14 and 11:04, five
times in a row from 09:11 on 11-05, so that no practice trade reached the afternoon's time exits.

Its reference price is the prediction's ``anchor`` (the bar close's integrated mid-point), the
target ``anchor + w × tick``, the stop ``anchor − w × tick`` (w = k for the direction), the time
barrier the bar close plus the kind's horizon: the trade is the event its hit rate measured.

**Fills** on the collector's best quotes (``quotes.py``), in the order received:

- the buy at the best ask of the first quote received after the decision. None if a special
  window learned before that quote touches its window (``special``), if that quote's ask is at or
  above the target (``target_reached``: it was reached already), if the size is
  below one share (``no_shares``) or the stop not below the ask (``bad_stop``), if no quote comes
  within ``Rules.quote_wait`` (``no_quote``), or if it comes at or after the time barrier
  (``expired``). The take-profit's sell rests at the target from then on;
- the take-profit at the target, once a quote received after the buy has its best bid at or above
  it (no place at the front of the queue is assumed);
- the stop at the best bid of the first quote after the buy whose best bid is at or below the stop;
- a pause selling at once, or a loss limit reached (#396, ``limits.py``), at the best bid of the
  first quote received after it (a limit reached on a quote: that quote's), its reason ``pause``,
  ``daily_limit`` or ``cumulative_limit``; the take-profit, the stop and the time exit come first
  on that quote;
- the time exit at the best bid of the last quote received before the time barrier, once a quote
  received at or after it comes, or the clock is ``Rules.grace`` past it. Without a quote in the
  ``Rules.quote_wait`` before the barrier (the stream was down), or when the trader was not
  running at the barrier, it waits for the next quote and sells at its best bid then: the trade's
  ``exit_at`` after its ``time_barrier`` shows the wait.

**A stall** of the quotes (#407, ``stall.py``, told by the desk: ``on_stall``, ``on_recover``): no
new buy (a rise read is skipped ``quotes_stalled``, a buy waiting for its quote given up so, the
rises waiting skipped so); the open trade waits for the quotes (a paper book has no other price) and
is kept as open through the stall (``trade_stalls``). When the quotes come again, an exit made on the
first quote after the stall (the stop crossed, the time barrier passed, a pause selling meanwhile)
keeps that on its trade (``waited``). The mock book reads KIS's REST quotes instead (``mock.py``).

**Costs** (``money.round_trip``, #652): the commission (``KRX_FEE_PERCENT``) on the buy's and the
sell's notional and the sell tax (``pricing.SELL_TAX``) on the sell's; the net P&L is the gross less
them, in won.
Unlike the touch P&L and the barrier P&L (``stats.touch_profit``, ``barriers.barrier_profit``), which
cost the round trip on the reference price per share in ticks, a trade pays on its fills.

A quote received before the trader started is the book as it stood, never a trigger. A trader
started with a trade open (a restart) goes on with it: its exits as above, and a buy it was
waiting for is given up (``restarted``). A pause or a limit holding when it starts holds on, and
one that sells sells it at the first quote. The same picks, the same predictions read at the same
times, and the same quotes always make the same trades.

**Execution cost** (#475, ``store.execution``): a buy decided keeps the mid-point of the latest quote
the book had then (``Entry.decided_mid``), and a sale (the stop, the time exit, a pause or a limit) the
time and the mid-point of the quote it was decided on (``Exit.decided_at``, ``Exit.mid``; the mock
book's when its sale begins, ``mock.py``). A take-profit rests at the target from the buy: its leg is
measured from the target.

**One set of rules** (#470): the entry (``plan``, ``admit``), the priority (``settle``), the buy's fill
(``refused``, ``size``), the risk rules (``risk_held``, #471), the exits (``exit_on``, ``clock_exit``)
and the costs (``money.py``, #652) are functions the paper book calls, and so
does the nightly re-fit's replay of the recorded days (``replay.py``), with the loss limits a
challenger's book simulates (``limits.Simulated``): the same predictions and the same quotes make the
same trades there (``tests/test_trader_replay.py``).

**State.** The trader writes its state (``state``: status, cash, position, the day's and the
cumulative P&L the limits count with, since #533, the mock practice's and the account's beside them,
the limits' use, the pause, and since #475 the risk rules' state, ``risk_state``)
into its store as it changes, at most every
``SNAPSHOT_SECONDS`` while only the open position's bid moves and at least every ``HEARTBEAT``,
with the day's ledger (``days``) on a trading day: ``GET /v1/trader``, ``/v1/trader/days``. The
snapshot also carries the process's resident memory for the engine's heartbeat (#547).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from krx_quant.feeds.base import KST, kst_day
from krx_quant.footprint import resident_mb
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
    PAPER,
    PRACTICE_SPACING,
    RISK_PER_TRADE_PERCENT,
    SYMBOL,
    THRESHOLD,
)
from krx_quant.trader.cells import Cell, Signal, learned_at
from krx_quant.trader.db import iso
from krx_quant.trader.limits import (
    HEARTBEAT,
    RUNNING,
    SALE,
    SNAPSHOT_SECONDS,
    Guard,
    Holds,
    Limits,
    Notify,
    Simulated,
)
from krx_quant.trader.money import commission, open_net, round_trip, size
from krx_quant.trader.quotes import BestQuote
from krx_quant.trader.selection import PREDICTOR_INACTIVE, Pick, Selection
from krx_quant.trader.store import RESIDENT, WAITED, Entry, Exit, OpenTrade, Stall, TraderStore

if TYPE_CHECKING:
    from krx_quant.trader.settings import TraderSettings

log = logging.getLogger(__name__)

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
    it buys nothing (the reasons the module doc lists, but the trader's own: ``not_picked``,
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
    """Decide the rises waiting (module doc "Priority"), oldest bar close first: of a bar close, the
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


def exit_on(
    barriers: Barriers,
    quote: BestQuote,
    standing: BestQuote | None,
    rules: Rules,
    ran_at_barrier: bool = True,
) -> Sale | None:
    """The exit ``quote``, received after the buy, makes of a position (module doc "Fills"),
    ``standing`` the latest quote received before it: at or after the time barrier, the time exit at
    the standing quote's bid at the barrier if it is recent (``fresh_before``) and the trader ran at
    the barrier, else at this one's bid now; before it, the take-profit at the target once its bid
    reaches it, the stop at its bid at or below the stop. None: held on."""
    if quote.received_at >= barriers.time_barrier:
        if ran_at_barrier and standing is not None and fresh_before(barriers.time_barrier, standing, rules):
            return Sale(barriers.time_barrier, standing.bid, TIME, standing)
        return Sale(quote.received_at, quote.bid, TIME, quote)
    if quote.bid >= barriers.target:
        return Sale(quote.received_at, barriers.target, TAKE_PROFIT, quote)
    if quote.bid <= barriers.stop:
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


def _pnl(realized: float, unrealized: float, trades: int | None = None) -> dict[str, Any]:
    """A P&L in the trader's state (#533): realized, open, their sum, and the trades exited if given."""
    found: dict[str, Any] = {"realized": realized, "unrealized": unrealized, "net": realized + unrealized}
    return found if trades is None else found | {"trades": trades}


@dataclass(frozen=True)
class _Waiting:
    """A buy decided, waiting for the first quote received after its decision."""

    decision: int
    signal: Signal
    pick: Pick
    plan: Plan
    decided_at: datetime
    mid: float | None = None  # the latest quote's mid-point at the decision (#475); None without one

    def deadline(self, rules: Rules) -> datetime:
        return self.decided_at + rules.quote_wait


class PaperTrader:
    """The picked cells' paper trades, from the picks (``select``), the special windows
    (``on_special``), the cells' predictions (``on_signal``), the quotes (``on_quote``), the
    pauses, resumes and re-arms asked for (``on_controls``) and the clock (``on_time``), into
    ``store``. ``sessions`` gives a day's session (the market calendar's); ``started_at`` is when
    this run began; ``notify`` sends a notice to Telegram (#396). Until the first picks it trades
    nothing.

    The champion's book by default: the trades the trader makes, which its limits and pauses hold
    (``guard``), with its state and ledger. With ``challenger`` (#400, ``desk.Desk``), that
    challenger policy's book: the same rules on the same quotes, in paper, held by no pause, its loss
    limits simulated on its own trades (#471, ``limits.Simulated``: the cumulative one counted from
    ``since``; buying on ``account``'s cash), writing no state; its decisions and trades carry its
    version. With ``shadow`` (#405), the champion policy's paper shadow in mock mode: held and sized as
    a challenger's book is, its trades the champion policy's paper record (``champion`` 0, ``shadow``
    1), never the trader's; ``carry`` also goes on with the paper champion book's trade left open by
    paper mode. Every book sizes by the loss budget and keeps the risk rules on its own orders and
    trades (#471, module doc)."""

    def __init__(
        self,
        store: TraderStore,
        rules: Rules,
        sessions: Callable[[date], Session | None],
        started_at: datetime,
        mode: str = PAPER,
        notify: Notify | None = None,
        *,
        challenger: int | None = None,
        account: Callable[[], float] | None = None,
        shadow: bool = False,
        carry: bool = False,
        practice: bool = False,
        since: datetime | None = None,
    ) -> None:
        self.store = store
        self.rules = rules
        self._sessions = sessions
        self._started_at = started_at
        self.mode = mode
        self.challenger = challenger
        self.shadow = shadow
        self.practice = shadow and practice  # the practice cell's paper shadow (#427)
        # What GET /v1/trader's ``practice`` gives (#427): whether the mock practice is on, whether the
        # book practises now and its cell; set by the desk at each pick.
        self.practice_state: dict[str, Any] | None = None
        # What a buy sizes on in place of the capital, if below it (#408: the live first period's cap).
        self.cap: int | None = None
        # The live mode's state as the trader last judged it (#408, ``live.live_state``): its state's.
        self.live: dict[str, Any] | None = None
        self._guard: Guard | None = None
        self.holds: Holds
        if challenger is None and not shadow:
            self._guard = Guard(store, mode, rules.limits, started_at, notify or (lambda text: None))
            self.holds = self._guard
        else:  # its own trades' P&L, of the day and since ``since`` (#471)
            book = self._book()

            def day_realized(day: date) -> float:
                return store.realized_of(mode, datetime.combine(day, time(), KST), **book)

            self.holds = Simulated(
                rules.limits,
                kst_day(started_at),
                day_realized(kst_day(started_at)),
                store.realized_of(mode, since, **book),
                account or (lambda: 0.0),
                day_realized,
            )
        # A sale the status asks for sells at the first quote received after this (#396).
        self._sell_after: datetime | None = None
        self._written: tuple[datetime, dict[str, Any]] | None = None  # the state last written
        # The risk rules' state last read from the store, and when (#475, ``_risk_state``).
        self._risk_read: tuple[datetime, dict[str, Any]] | None = None
        self._dirty = True  # a change to write at once
        self._selection: int | None = None  # the picks' row in the store
        self._policy: int | None = challenger  # the policy version the picks are under (#399)
        self._picks: dict[Cell, tuple[int, Pick]] = {}  # each picked cell's rank and pick
        self._inactive: tuple[Cell, ...] = ()  # the policy's cells held off, read and never bought (#603)
        self._special: list[Window] = []
        self._halts: list[tuple[datetime, datetime]] = []  # the market halts' spans (#625, ``halt_spans``)
        self._read: dict[datetime, set[Cell]] = {}  # the cells read of each recent bar close
        self._pending: dict[datetime, list[Rise]] = {}  # rises waiting, by bar close
        self._last: BestQuote | None = None  # the latest quote received
        self._waiting: _Waiting | None = None
        self._stall: Stall | None = None  # the collector's quotes stalled since (#407)
        self._returned: Stall | None = None  # the stall the quote being taken is the first after
        self._linked: tuple[int, int] | None = None  # the (trade, stall) kept as open through it
        self._open: OpenTrade | None = (
            store.open_trade(mode, challenger)
            if not shadow
            # A shadow goes on with its own trade left open (the practice shadow with its own, #427); in
            # mock mode (``carry``) with the paper champion book's too, left open by paper mode (#405):
            # no guard counts either in memory.
            else store.open_trade(mode, shadow=True, practice=self.practice)
            or (store.open_trade(mode) if carry else None)
        )
        if self._open is not None:
            log.info("%s going on with trade %s, open since before this start", self._whose(), self._open.id)
        if challenger is not None:
            return
        dropped = store.drop_unfilled(mode, RESTARTED)  # every book's of the mode: none is waiting at a start
        if shadow:
            if dropped:
                log.info(
                    "trader: shadow: %s paper buy(s) waiting for a quote when the trader stopped: given up",
                    dropped,
                )
            return
        if dropped:
            log.info("trader: %s buy(s) waiting for a quote when the trader stopped: given up", dropped)
        if self.guard.status != RUNNING:
            log.warning("trader: starting %s", self.guard.status)
        self.on_controls(started_at)  # asked for while it was not running
        self._enforce(started_at)
        self.write(started_at)

    async def start(self) -> None:
        """Before it trades: nothing in paper (``mock.py`` reconciles the KIS account first, #406)."""

    async def idle(self) -> None:
        """Until no order is in flight: a paper book's fill at once, so at once (``mock.py``'s waits)."""

    async def close(self, timeout: float = 0.0) -> None:
        """The trader stopping: nothing in flight in paper (``mock.py``'s waits for its order)."""

    @property
    def guard(self) -> Guard:
        """The champion's limits and pauses (#396); a challenger's book has none."""
        if self._guard is None:
            raise AttributeError(f"challenger {self.challenger}'s book has no limits")
        return self._guard

    @property
    def status(self) -> str:
        return self.holds.status

    @property
    def policy(self) -> int | None:
        """The policy version it trades under (None: before the first champion)."""
        return self._policy

    def trades(self, cell: Cell) -> bool:
        """Whether it reads ``cell``'s predictions: among those it picked, or held off (#603)."""
        return cell in self._picks or cell in self._inactive

    @property
    def busy(self) -> bool:
        """Whether it holds a position or waits to buy one."""
        return self._busy()

    def _book(self) -> dict[str, Any]:
        """Which book's trades and orders are its own in the store (#471): the champion's, a
        challenger's, the shadow's or the practice shadow's."""
        return {"challenger": self.challenger, "shadow": self.shadow, "practice": self.practice}

    def rearm(self, at: datetime) -> None:
        """The trader's cumulative loss limit re-armed at ``at`` (#471): a challenger's or a shadow's
        simulated one counts again from then too."""
        if isinstance(self.holds, Simulated):
            self.holds.rearm()
            log.info("%s: its simulated cumulative loss counted again from %s", self._whose(), iso(at))

    def _whose(self) -> str:
        """Its log lines' start: the champion's as they were (``trader: bought …``), a challenger's
        with its version (``trader: challenger N bought …``), the shadow's (``trader: shadow …``), the
        practice shadow's (``trader: practice shadow …``, #427)."""
        if self.practice:
            return "trader: practice shadow"
        if self.shadow:
            return "trader: shadow"
        return "trader:" if self.challenger is None else f"trader: challenger {self.challenger}"

    @property
    def cells(self) -> list[Cell]:
        """The cells it reads: those picked, first ranked first, then those held off (#603)."""
        picked = [c for c, _ in sorted(self._picks.items(), key=lambda item: item[1][0])]
        return [*picked, *(c for c in self._inactive if c not in self._picks)]

    def select(self, selection: Selection, at: datetime) -> None:
        """The cells to trade from ``at`` on, kept in the store; a rise waiting of a cell no longer
        picked is skipped (``not_picked``)."""
        self._selection = self.store.add_selection(self.mode, at, selection)
        self._policy = selection.policy if self.challenger is None else self.challenger
        self._picks = {p.cell: (rank, p) for rank, p in enumerate(selection.picks)}
        self._inactive = selection.inactive
        if self.challenger is not None:
            log.info("trader: challenger %s in paper: %s", self.challenger, selection.describe())
        elif self.shadow:
            log.info("%s in paper: %s", self._whose(), selection.describe())
        elif selection.practising:
            log.info("trader: practising, no champion cell trades: %s", selection.describe())
        elif selection.picks:
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

    def retire(self, at: datetime) -> None:
        """A challenger no longer run (#400: dropped, or promoted, its paper book giving way to the
        champion's): it picks nothing more, a rise waiting is skipped (``not_picked``); a buy waiting
        for its quote and an open position go on to their ends."""
        self._picks, self._inactive = {}, ()
        for close in list(self._pending):
            for c in self._pending.pop(close):
                self._skip(c.signal, at, NOT_PICKED)

    def on_special(self, windows: Sequence[Window]) -> None:
        """The special windows the engine knows of now (the latest of each). A buy decided but not
        filled yet whose window one of them touches is given up (``special``)."""
        self._special = list(windows)
        self._halts = halt_spans(self._special)
        waiting = self._waiting
        if waiting is not None and touching(
            self._special, waiting.signal.symbol, waiting.signal.bar_close, waiting.plan.time_barrier
        ):
            self._give_up(waiting, SPECIAL)

    def on_stall(self, stall: Stall, at: datetime) -> None:
        """The collector's quotes stalled, seen at ``at`` (#407, module doc): nothing new bought (the
        buy waiting for its quote given up, the rises waiting skipped, ``quotes_stalled``); the open
        trade kept as open through it."""
        self._stall = stall
        if self._waiting is not None:
            self._give_up(self._waiting, STALLED)
        for close in list(self._pending):
            for c in self._pending.pop(close):
                self._skip(c.signal, at, STALLED)
        self._link_stall()
        self._dirty = True

    def on_recover(self, stall: Stall, at: datetime) -> None:
        """The quotes back at ``at`` (#407): the quote that ends ``stall`` comes next (``on_quote``);
        the open trade's exit made on it keeps that it waited for the quotes (``WAITED``)."""
        self._stall = None
        self._returned = stall if self._open is not None else None
        self._dirty = True

    def _link_stall(self) -> None:
        """The open trade kept as open through the stall, once (``trade_stalls``)."""
        stall, trade = self._stall, self._open
        if stall is None or trade is None or self._linked == (trade.id, stall.id):
            return
        self.store.add_trade_stall(trade.id, stall.id)
        self._linked = (trade.id, stall.id)

    def on_signal(self, signal: Signal, at: datetime) -> None:
        """A prediction of a cell, read at ``at``; one already decided on (a restart) is passed over."""
        cell = Cell(signal.kind, signal.predictor)
        self._read.setdefault(signal.bar_close, set()).add(cell)
        if self.store.decided(self.mode, self._policy, signal.kind, signal.predictor, signal.prediction_id):
            return
        if cell in self._inactive and cell not in self._picks:
            self._skip(signal, at, PREDICTOR_INACTIVE)
        elif cell not in self._picks:
            self._skip(signal, at, NOT_PICKED)
        else:
            session = self._sessions(kst_day(signal.bar_close))
            _, picked = self._picks[cell]
            planned = plan(signal, self.rules, session, at, self._special, picked.threshold)
            admit(self._pending, signal, planned, at, self._skip, self._not_now)
        self._settle(at)

    def on_quote(self, quote: BestQuote) -> None:
        """A quote from the stream (in mock mode during a stall, KIS's REST one, #407). One not newer
        than the latest (a reconnect's first) is passed over."""
        if self._in_halt(quote.received_at):
            return  # a market halt's quote (#625): not one to fill on
        standing = self._last
        if standing is not None and quote.received_at <= standing.received_at:
            self._returned = None  # the first quote after a stall, passed over: none waited on it
            return
        self._last = quote
        try:
            self._take(quote, standing)
        finally:
            self._returned = None  # the first quote taken after a stall alone waited through it

    def _take(self, quote: BestQuote, standing: BestQuote | None) -> None:
        """``quote`` taken, ``standing`` the latest before it: the buy waiting, the exits, the limits."""
        if quote.received_at < self._started_at:
            return  # the book as it stood before this run: no trigger
        if self._waiting is not None:
            self._buy(self._waiting, quote)
        elif self._open is not None:
            self._follow(self._open, quote, standing)
        trade = self._open
        if trade is None:
            return
        status = self.status
        if status in SALE and quote.received_at > (self._sell_after or self._started_at):
            self._exit(trade, quote.received_at, quote.bid, SALE[status], quote)
            return
        reached = self.holds.mark(self._unrealized(trade, quote.bid), quote.received_at, trade.practice)
        if reached is not None:
            self._dirty = True
            self._enforce(quote.received_at, quote)

    def on_controls(self, now: datetime) -> None:
        """The pauses, resumes and re-arms asked for and not read yet (#396), read at ``now``, in
        the order asked: the status they make holds from now (a sale it asks for at the next
        quote). The champion's book alone: no pause holds a challenger's."""
        if self._guard is None:
            return
        controls = self.store.controls(applied=False)
        for control in controls:
            self.guard.apply(control, now)
        if controls:
            self._dirty = True
            self._enforce(now)

    def _enforce(self, at: datetime, quote: BestQuote | None = None) -> None:
        """What the status asks for at ``at``: nothing bought (a buy waiting for its quote given up,
        the rises waiting skipped, the status their reason); the open position sold if it sells, at
        ``quote``'s bid if the status began on it, else at the next quote's."""
        status = self.status
        if status == RUNNING:
            return
        if self._waiting is not None:
            self._give_up(self._waiting, status)
        for close in list(self._pending):
            for c in self._pending.pop(close):
                self._skip(c.signal, at, status)
        trade = self._open
        if trade is None or status not in SALE:
            return
        if quote is not None:
            self._exit(trade, quote.received_at, quote.bid, SALE[status], quote)
        elif self._sell_after is None:
            self._sell_after = at
            log.info("trader: %s: trade %s sells at the next quote", status, trade.id)

    def _in_halt(self, at: datetime) -> bool:
        """Whether a market halt holds this book's fills at ``at`` (#625, ``halt_spans``): a paper book's;
        the KIS book's never (``mock.MockTrader``: its orders and fills are KIS's)."""
        return halted(self._halts, at)

    def on_time(self, now: datetime) -> None:
        """The clock: a rise that waited long enough for the cells ranked above it is decided, a
        buy whose quote did not come is given up, a time barrier passed is sold on the last quote
        before it; a new day's figures (#396); the champion's state written."""
        if self.holds.roll(now):
            self._dirty = True
        self._settle(now)
        grace = self.rules.grace
        waiting = self._waiting
        if waiting is not None and now >= waiting.deadline(self.rules) + grace:
            self._give_up(waiting, NO_QUOTE)
        trade = self._open
        if trade is not None and not self._in_halt(now):  # no time exit by the clock in a halt (#625)
            sale = clock_exit(trade, self._last, now, self.rules, trade.time_barrier >= self._started_at)
            if sale is not None:
                self._exit(trade, sale.at, sale.price, sale.reason, sale.quote)
        for close in [c for c in self._read if c < now - timedelta(hours=1) and c not in self._pending]:
            del self._read[close]
        if self._guard is not None:
            self.write(now, force=False)

    def write(self, now: datetime, force: bool = True) -> None:
        """The state (``state``) and the day's ledger into the store: at once if something changed
        or ``force``; if only the open position's P&L moved, at most every ``SNAPSHOT_SECONDS``;
        else every ``HEARTBEAT``. The snapshot also carries the process's resident memory then
        (#547, ``store.RESIDENT``), which is not the state: its moves alone write nothing."""
        body = self.state(now)
        if self._written is not None and not (force or self._dirty):
            at, written = self._written
            if now - at < (SNAPSHOT_SECONDS if body != written else HEARTBEAT):
                return
        self.store.put_snapshot(now, {**body, RESIDENT: resident_mb()})
        guard = self.guard
        if self._sessions(guard.day) is not None:  # a trading day's ledger
            self.store.put_day(self.mode, guard.day, guard.ledger(), now)
        self._written = (now, body)
        self._dirty = False

    def state(self, now: datetime | None = None) -> dict[str, Any]:
        """The trader's state at ``now`` (#396; its start's if None): what ``GET /v1/trader`` gives."""
        guard, trade, last = self.guard, self._open, self._last
        position = None
        cost = 0.0
        if trade is not None:
            cost = trade.entry_price * trade.quantity + trade.buy_fee
            seen = last is not None and (trade.entry_at is None or last.received_at >= trade.entry_at)
            position = {
                "trade_id": trade.id,
                "kind": trade.kind,
                "predictor": trade.predictor,
                "quantity": trade.quantity,
                "entry_price": trade.entry_price,
                "entry_at": None if trade.entry_at is None else iso(trade.entry_at),
                "target": trade.target,
                "stop": trade.stop,
                "time_barrier": iso(trade.time_barrier),
                "bid": last.bid if seen and last is not None else None,
                "bid_at": iso(last.received_at) if seen and last is not None else None,
                "unrealized": guard.unrealized,
                "selling": self.status in SALE,
                "practice": trade.practice,  # the mock book's practice trade (#427)
            }
        limits = guard.limits
        since = guard.since
        return {
            "mode": self.mode,
            "status": guard.status,
            "started_at": iso(self._started_at),
            "capital": self.rules.capital,
            "cash": self.rules.capital + guard.realized_all - cost,
            "position": position,
            # What the loss limits count (#533: no practice trade's), and beside it the mock book's
            # practice trades' and the account's (both).
            "today": {
                "day": guard.day.isoformat(),
                "realized": guard.realized_day,
                "unrealized": guard.counted_unrealized,
                "net": guard.day_pnl,
                "trades": guard.trades_day,
                "worst": guard.worst,
                "practice": _pnl(guard.practice_day, guard.practice_unrealized, guard.practice_trades_day),
                "account": _pnl(
                    guard.realized_day + guard.practice_day,
                    guard.unrealized,
                    guard.trades_day + guard.practice_trades_day,
                ),
            },
            "cumulative": {
                "since": None if since is None else iso(since),
                "realized": guard.realized_cumulative,
                "unrealized": guard.counted_unrealized,
                "net": guard.cumulative_pnl,
                "worst": guard.cumulative_worst,
                "practice": _pnl(guard.practice_cumulative, guard.practice_unrealized),
                "account": _pnl(guard.realized_cumulative + guard.practice_cumulative, guard.unrealized),
            },
            "limits": {
                "daily": {
                    "percent": limits.daily_percent,
                    "won": limits.daily,
                    "used": max(0.0, -guard.day_pnl) / limits.daily,
                    "reached_at": None if guard.daily_halt is None else iso(guard.daily_halt.at),
                },
                "cumulative": {
                    "percent": limits.cumulative_percent,
                    "won": limits.cumulative,
                    "used": max(0.0, -guard.cumulative_pnl) / limits.cumulative,
                    "reached_at": None if guard.cumulative_halt is None else iso(guard.cumulative_halt.at),
                },
            },
            "pause": None
            if guard.pause is None
            else {"at": iso(guard.pause.at), "sell_now": guard.pause.sell_now, "source": guard.pause.source},
            "live": self.live,
            "practice": self.practice_state,
            # #475: the loss budget and the risk rules' state (#471) of the champion's book's day.
            "risk": self._risk_state(now or self._started_at),
        }

    def _risk_state(self, at: datetime) -> dict[str, Any]:
        """``risk_state`` at ``at``, read from the store again when something changed (a buy or an exit
        makes the book dirty) or ``SNAPSHOT_SECONDS`` after the last read: ``state`` is asked at every
        tick of the clock, the store's day read at most every second (a cooldown ends to the second)."""
        kept = self._risk_read
        if kept is not None and not self._dirty and timedelta(0) <= at - kept[0] < SNAPSHOT_SECONDS:
            return kept[1]
        since, book = midnight(at), self._book()
        found = risk_state(
            self.rules,
            at,
            self.store.day_buys(self.mode, since, **book),
            self.store.day_exits(self.mode, since, **book),
            self._capital(),
        )
        self._risk_read = (at, found)
        return found

    def _busy(self) -> bool:
        return self._open is not None or self._waiting is not None

    def _unrealized(self, trade: OpenTrade, bid: float) -> float:
        """What the open ``trade`` nets sold at ``bid`` now (``money.open_net``); the mock book counts
        what of it is sold already at its fills (#405)."""
        return open_net(trade, bid, self.rules.fee_rate)

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

    def _not_now(self, signal: Signal, at: datetime) -> str | None:
        """Why a rise read or decided at ``at`` buys nothing now: paused or stopped (#396, its status),
        the quotes stalled (#407), something held or bought (``_held_off``), the risk rules (#471), a
        practice buy's spacing (#598); None if it may buy."""
        if self.status != RUNNING:
            return self.status
        if self._stall is not None:
            return STALLED
        if self._busy():
            return self._held_off(signal)
        return self._risk(at) or self._spacing(signal, at)

    def _spacing(self, signal: Signal, at: datetime) -> str | None:
        """Why the practice's spacing holds ``signal``'s buy (#598, ``spaced``): of a practice pick alone,
        from the bar close of the book's last practice buy order of ``at``'s day, as the store has it."""
        picked = self._picks.get(Cell(signal.kind, signal.predictor))
        if picked is None or not picked[1].practice or self.rules.practice_spacing <= timedelta(0):
            return None
        return spaced(self.rules, signal.bar_close, self.store.last_practice_buy(self.mode, midnight(at)))

    def _risk(self, at: datetime) -> str | None:
        """Why the risk rules hold a buy at ``at`` (#471, ``risk_held``): the book's buy orders and
        exits of the day, as the store has them."""
        since, book = midnight(at), self._book()
        return risk_held(
            self.rules,
            at,
            self.store.day_buys(self.mode, since, **book),
            self.store.day_exits(self.mode, since, **book),
        )

    def _settle(self, now: datetime) -> None:
        """Decide the rises waiting (``settle``): a rise a special window learned while it waited now
        touches is ``special``."""
        ranks = {cell: rank for cell, (rank, _) in self._picks.items()}
        settle(
            self._pending,
            self._read,
            ranks,
            self._special,
            now,
            self.rules,
            skip=self._skip,
            held=self._not_now,
            decide=self._decide,
        )

    def _decide(self, candidate: Rise, at: datetime) -> None:
        signal, planned = candidate.signal, candidate.plan
        _, pick = self._picks[candidate.cell]
        decision = self._add_decision(signal, at, BUY, None)
        log.info(
            "%s buying on %s %s of %s (p %.3f): target %s, stop %s, until %s, %s",
            self._whose(),
            signal.kind,
            signal.predictor,
            signal.bar_close.strftime("%H:%M"),
            signal.rise_probability,
            planned.target,
            planned.stop,
            planned.time_barrier.strftime("%H:%M"),
            f"{pick.quantity} share(s)"
            if pick.quantity is not None
            else f"a loss budget of {self.rules.budget(self._capital()):.0f} won",
        )
        self._waiting = _Waiting(decision, signal, pick, planned, at, mid_of(self._last))

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
            self._policy,
        )

    def _buy(self, waiting: _Waiting, quote: BestQuote) -> None:
        if quote.received_at <= waiting.decided_at:
            return  # received before the decision, though it came after it
        if self.status != RUNNING:
            self._give_up(waiting, self.status)
            return
        reason = refused(waiting.decided_at, waiting.plan, quote, self.rules)
        if reason is not None:
            self._give_up(waiting, reason)
            return
        capital = self._capital()
        cash = self.rules.capital + self.holds.realized_all  # nothing open: one at a time
        # The loss budget over a share's loss at the stop (#471); a practice pick buys its shares,
        # within the cash and the capital (#427).
        quantity = size(
            quote.ask,
            waiting.plan.stop,
            self.rules.budget(capital),
            capital,
            cash,
            self.rules.fee_rate,
            waiting.pick.quantity,
        )
        if isinstance(quantity, str):
            self._give_up(waiting, quantity)
            return
        self._waiting = None
        self._fill_buy(waiting, quote, quantity)

    def _capital(self) -> float:
        """The capital a buy sizes on: the cap, if below it (#408: the live first period's)."""
        return self.rules.capital if self.cap is None else min(self.rules.capital, self.cap)

    def _fill_buy(self, waiting: _Waiting, quote: BestQuote, quantity: int) -> None:
        """The buy decided, of ``quantity`` shares at ``quote``'s ask: in paper, filled whole at once.
        The mock book (#405, ``mock.MockTrader``) orders it on KIS instead."""
        self._open = self.store.enter(
            waiting.decision, self._entry(waiting, quote.received_at, quote.ask, quantity, quote.received_at)
        )
        self._dirty = True
        log.info("%s bought %s at %s (trade %s)", self._whose(), quantity, quote.ask, self._open.id)

    def _entry(
        self, waiting: _Waiting, at: datetime, price: float, quantity: int, quote_at: datetime
    ) -> Entry:
        """The trade ``waiting`` enters: ``quantity`` shares filled at ``price`` at ``at``."""
        signal, planned, pick = waiting.signal, waiting.plan, waiting.pick
        assert signal.anchor is not None and signal.tick is not None and signal.width is not None
        p = signal.rise_probability
        assert p is not None
        return Entry(
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
            entry_at=at,
            entry_price=price,
            quantity=quantity,
            buy_fee=commission(price, quantity, self.rules.fee_rate),
            quote_at=quote_at,
            selection_id=self._selection,
            role=pick.role,
            p_star=pick.p_star,
            low=pick.low,
            kelly=pick.kelly,  # f*, a diagnosis alone (#471): no trade is sized by it
            # A practice trade is of no policy (#427): no record, promotion or gate counts it.
            policy_id=None if pick.practice else self._policy,
            champion=self.challenger is None and not self.shadow,
            shadow=self.shadow,
            practice=pick.practice,
            model_version=signal.model_version,
            decided_mid=waiting.mid,
        )

    def _give_up(self, waiting: _Waiting, reason: str) -> None:
        self._waiting = None
        self.store.unfilled(waiting.decision, reason)
        log.info("%s no buy on %s (%s)", self._whose(), waiting.signal.bar_close.strftime("%H:%M"), reason)

    def _follow(self, trade: OpenTrade, quote: BestQuote, standing: BestQuote | None) -> None:
        # At the time barrier, the last quote before it sells only if the trader was running then.
        sale = exit_on(trade, quote, standing, self.rules, trade.time_barrier >= self._started_at)
        if sale is not None:
            self._exit(trade, sale.at, sale.price, sale.reason, sale.quote)

    def _exit(self, trade: OpenTrade, at: datetime, price: float, reason: str, quote: BestQuote) -> None:
        trip = round_trip(
            trade.entry_price, trade.quantity, trade.buy_fee, price * trade.quantity, self.rules.fee_rate
        )
        # The day the exit is counted in, rolled before the exit is kept: a day read again after it
        # would hold it already (an exit the clock had not reached the day of yet, #396).
        if self.holds.roll(at):
            self._dirty = True
        if self._returned is not None:  # made on the first quote after a stall: it waited (#407)
            self.store.stall_effect(trade.id, self._returned.id, WAITED)
        # The sale's decision (#475): on the quote it is made on; a take-profit rests from the buy.
        sold = (None, None) if reason == TAKE_PROFIT else (at, mid_of(quote))
        self.store.exit(trade, Exit(at, price, reason, trip.sell_fee, trip.tax, quote.received_at, *sold))
        self._open = None
        self._sell_after = None
        self._dirty = True
        net = trip.net  # as the store keeps it
        log.info(
            "%s sold %s at %s (%s, trade %s): net %.0f won",
            self._whose(),
            trade.quantity,
            price,
            reason,
            trade.id,
            net,
        )
        if self.holds.exited(net, at, trade.practice) is not None:  # its realized loss reached a limit
            self._enforce(at)
