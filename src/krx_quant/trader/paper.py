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
  stopped by a loss limit (``daily_limit``, ``cumulative_limit``), the status its reason;
- the collector's quotes flow (``quotes_stalled``, #407, ``stall.py``): none for more than five
  seconds in continuous trading is a stall, and nothing new is bought until a quote comes again;
- nothing is held, nor waiting to be bought (``holding``): one position at a time;
- no cell ranked above it buys on the same bar close (``outranked``).

**Priority.** Of the rises of one bar close, the cell ranked first (``Selection.picks``: the
champion policy's order, #399; picks without a policy, the greatest hit rate's lower bound less
p*) buys. The cells' predictions of a bar close come at
different times (a baseline's at once, an algorithm's within its time budget, the final consensus
after every member), so a rise waits for the cells ranked above it: it is decided once each of
them has a prediction of that bar close read, or at the latest ``Rules.fresh`` after it was
displayed (it would be stale after). A higher-ranked cell whose prediction comes later than that
finds the position taken (``holding``).

**Size.** The cell's Kelly fraction (``Pick.fraction``: λ of its Kelly f*, cut to 0–1; λ the
champion policy's for the cell, #399; a pick without a policy, ``KELLY_FRACTION``, a half) of the
capital, over the best ask, rounded down: ⌊min(capital − what is open, fraction × capital, cash)
÷ ask⌋ (nothing is open: one position at a time). The cash is the paper account's (#396): the
capital, plus the net P&L of every trade of the mode exited, less the open position's cost; a
buy pays its commission from it too. Below one share it buys nothing (``no_shares``). A cap below
the capital (``cap``: the live mode's first period, #408, ``live.py``) takes the capital's place in
both terms. A pick of fixed shares (``Pick.quantity``: the mock book's practice cell, #427) buys that
many in place of its fraction, within the same cash and capital: ⌊min(capital, cash) ÷ ask⌋ at most.

**Practice** (#427, ``desk.Desk``): the mock book's practice cell is a pick like another, its trade
marked ``practice``, of no policy (``Entry.policy_id`` None: no record counts it), its fraction none
(it buys its shares). Its paper shadow is a book of its own (``practice``), as the champion's shadow
is (``shadow``), so that the champion policy's shadow never holds a practice position.

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

**Costs** (``costs``): the commission (``KRX_FEE_PERCENT``) on the buy's and the sell's notional
and the sell tax (``pricing.SELL_TAX``) on the sell's; the net P&L is the gross less them, in won.
Unlike the touch P&L and the barrier P&L (``stats.touch_profit``, ``barriers.barrier_profit``), which
cost the round trip on the reference price per share in ticks, a trade pays on its fills.

A quote received before the trader started is the book as it stood, never a trigger. A trader
started with a trade open (a restart) goes on with it: its exits as above, and a buy it was
waiting for is given up (``restarted``). A pause or a limit holding when it starts holds on, and
one that sells sells it at the first quote. The same picks, the same predictions read at the same
times, and the same quotes always make the same trades.

**One set of rules** (#470): the entry (``plan``, ``admit``), the priority (``settle``), the buy's fill
(``refused``, ``size``), the exits (``exit_on``, ``clock_exit``) and the costs (``costs``, ``net_of``)
are functions of this module that the paper book calls, and so does the nightly re-fit's replay of
the recorded days (``replay.py``): the same predictions and the same quotes make the same trades
there (``tests/test_trader_replay.py``).

**State.** The trader writes its state (``state``: status, cash, position, the day's and the
cumulative P&L, the limits' use, the pause) into its store as it changes, at most every
``SNAPSHOT_SECONDS`` while only the open position's bid moves and at least every ``HEARTBEAT``,
with the day's ledger (``days``) on a trading day: ``GET /v1/trader``, ``/v1/trader/days``.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from krx_quant.feeds.base import kst_day
from krx_quant.kinds import most_likely
from krx_quant.market_calendar import Session
from krx_quant.pricing import SELL_TAX
from krx_quant.special import Window, touching
from krx_quant.trader import (
    CAPITAL,
    CUMULATIVE_LOSS_PERCENT,
    DAILY_LOSS_PERCENT,
    ENTRY_AFTER_OPEN,
    EXIT_BEFORE_AUCTION,
    PAPER,
    SYMBOL,
    THRESHOLD,
)
from krx_quant.trader.cells import Cell, Signal
from krx_quant.trader.limits import (
    HEARTBEAT,
    RUNNING,
    SALE,
    SNAPSHOT_SECONDS,
    Guard,
    Holds,
    Limits,
    Notify,
    Unlimited,
)
from krx_quant.trader.quotes import BestQuote
from krx_quant.trader.selection import Pick, Selection
from krx_quant.trader.store import WAITED, Entry, Exit, OpenTrade, Stall, TraderStore, iso

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
TARGET_REACHED, NO_SHARES, NO_QUOTE, RESTARTED = "target_reached", "no_shares", "no_quote", "restarted"
BUY, SKIP = "buy", "skip"
# The status of a prediction the engine did not make for a VI (``engine._record_not_predicted``).
VI_STATUS = "vi"


@dataclass(frozen=True)
class Rules:
    threshold: float = THRESHOLD
    capital: int = CAPITAL
    daily_loss_percent: float = DAILY_LOSS_PERCENT  # the loss limits, of the capital (#396)
    cumulative_loss_percent: float = CUMULATIVE_LOSS_PERCENT
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


def shares(
    fraction: float, capital: float, ask: float, cash: float | None = None, fee_rate: float = 0.0
) -> int:
    """What a buy at ``ask`` takes: ``fraction`` of the capital, within the ``cash`` (if given) its
    notional and commission may use, rounded down to whole shares."""
    budget = min(capital, fraction * capital)
    if cash is not None:
        budget = min(budget, max(0.0, cash) / (1 + fee_rate))
    return math.floor(budget / ask)


def unrealized(trade: OpenTrade, bid: float, fee_rate: float) -> float:
    """What ``trade`` would net sold at ``bid`` now (#396): the gross less the buy's commission and
    the sale's commission and tax."""
    paid = costs(trade.entry_price, bid, trade.quantity, fee_rate)
    return (bid - trade.entry_price) * trade.quantity - trade.buy_fee - paid.sell_fee - paid.tax


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


def size(
    fraction: float, quantity: int | None, capital: float, ask: float, cash: float, fee_rate: float
) -> int:
    """The shares a buy at ``ask`` takes (module doc "Size"): ``fraction`` of the capital, or a pick's
    fixed ``quantity`` (#427) within the capital, within the ``cash`` its notional and commission may
    use, rounded down."""
    if quantity is not None:
        return min(quantity, shares(1.0, capital, ask, cash, fee_rate))
    return shares(fraction, capital, ask, cash, fee_rate)


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


def net_of(entry_price: float, exit_price: float, quantity: int, fee_rate: float) -> float:
    """A round trip's net P&L in won: the gross less its costs (``costs``), as a trade keeps it."""
    return (exit_price - entry_price) * quantity - costs(entry_price, exit_price, quantity, fee_rate).total


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
    (``on_special``), the cells' predictions (``on_signal``), the quotes (``on_quote``), the
    pauses, resumes and re-arms asked for (``on_controls``) and the clock (``on_time``), into
    ``store``. ``sessions`` gives a day's session (the market calendar's); ``started_at`` is when
    this run began; ``notify`` sends a notice to Telegram (#396). Until the first picks it trades
    nothing.

    The champion's book by default: the trades the trader makes, which its limits and pauses hold
    (``guard``), with its state and ledger. With ``challenger`` (#400, ``desk.Desk``), that
    challenger policy's book: the same rules on the same quotes, in paper, held by no limit nor pause
    (``limits.Unlimited``, buying on ``account``'s cash), writing no state; its decisions and trades
    carry its version. With ``shadow`` (#405), the champion policy's paper shadow in mock mode: held
    and sized as a challenger's book is, its trades the champion policy's paper record (``champion``
    0, ``shadow`` 1), never the trader's; ``carry`` also goes on with the paper champion book's trade
    left open by paper mode."""

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
        else:
            self.holds = Unlimited(account or (lambda: 0.0))
        # A sale the status asks for sells at the first quote received after this (#396).
        self._sell_after: datetime | None = None
        self._written: tuple[datetime, dict[str, Any]] | None = None  # the state last written
        self._dirty = True  # a change to write at once
        self._selection: int | None = None  # the picks' row in the store
        self._policy: int | None = challenger  # the policy version the picks are under (#399)
        self._picks: dict[Cell, tuple[int, Pick]] = {}  # each picked cell's rank and pick
        self._special: list[Window] = []
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
        """Whether ``cell`` is among those it picked."""
        return cell in self._picks

    @property
    def busy(self) -> bool:
        """Whether it holds a position or waits to buy one."""
        return self._busy()

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
        """The cells picked, first ranked first."""
        return [c for c, _ in sorted(self._picks.items(), key=lambda item: item[1][0])]

    def select(self, selection: Selection, at: datetime) -> None:
        """The cells to trade from ``at`` on, kept in the store; a rise waiting of a cell no longer
        picked is skipped (``not_picked``)."""
        self._selection = self.store.add_selection(self.mode, at, selection)
        self._policy = selection.policy if self.challenger is None else self.challenger
        self._picks = {p.cell: (rank, p) for rank, p in enumerate(selection.picks)}
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
        self._picks = {}
        for close in list(self._pending):
            for c in self._pending.pop(close):
                self._skip(c.signal, at, NOT_PICKED)

    def on_special(self, windows: Sequence[Window]) -> None:
        """The special windows the engine knows of now (the latest of each). A buy decided but not
        filled yet whose window one of them touches is given up (``special``)."""
        self._special = list(windows)
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
        if cell not in self._picks:
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
        reached = self.holds.mark(self._unrealized(trade, quote.bid), quote.received_at)
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
        if trade is not None:
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
        else every ``HEARTBEAT``."""
        body = self.state()
        if self._written is not None and not (force or self._dirty):
            at, written = self._written
            if now - at < (SNAPSHOT_SECONDS if body != written else HEARTBEAT):
                return
        self.store.put_snapshot(now, body)
        guard = self.guard
        if self._sessions(guard.day) is not None:  # a trading day's ledger
            self.store.put_day(self.mode, guard.day, guard.ledger(), now)
        self._written = (now, body)
        self._dirty = False

    def state(self) -> dict[str, Any]:
        """The trader's state now (#396): what ``GET /v1/trader`` gives."""
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
            "today": {
                "day": guard.day.isoformat(),
                "realized": guard.realized_day,
                "unrealized": guard.unrealized,
                "net": guard.day_pnl,
                "trades": guard.trades_day,
                "worst": guard.worst,
            },
            "cumulative": {
                "since": None if since is None else iso(since),
                "realized": guard.realized_cumulative,
                "unrealized": guard.unrealized,
                "net": guard.cumulative_pnl,
                "worst": guard.cumulative_worst,
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
        }

    def _busy(self) -> bool:
        return self._open is not None or self._waiting is not None

    def _unrealized(self, trade: OpenTrade, bid: float) -> float:
        """What the open ``trade`` nets sold at ``bid`` now (``unrealized``); the mock book counts
        what of it is sold already at its fills (#405)."""
        return unrealized(trade, bid, self.rules.fee_rate)

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
        the quotes stalled (#407), something held or bought (``_held_off``); None if it may buy."""
        if self.status != RUNNING:
            return self.status
        if self._stall is not None:
            return STALLED
        if self._busy():
            return self._held_off(signal)
        return None

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
            "%s buying on %s %s of %s (p %.3f): target %s, stop %s, until %s, %.0f%% of the capital",
            self._whose(),
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
        pick = waiting.pick
        capital = self.rules.capital if self.cap is None else min(self.rules.capital, self.cap)
        cash = self.rules.capital + self.holds.realized_all  # nothing open: one at a time
        # A practice pick buys its shares, within the cash and the capital (#427).
        quantity = size(pick.fraction, pick.quantity, capital, quote.ask, cash, self.rules.fee_rate)
        if quantity < 1:
            self._give_up(waiting, NO_SHARES)
            return
        self._waiting = None
        self._fill_buy(waiting, quote, quantity)

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
            buy_fee=price * quantity * self.rules.fee_rate,
            quote_at=quote_at,
            selection_id=self._selection,
            role=pick.role,
            p_star=pick.p_star,
            low=pick.low,
            kelly=pick.kelly,
            # A practice pick buys its shares (#427): no share of the capital.
            fraction=None if pick.quantity is not None else pick.fraction,
            # A practice trade is of no policy (#427): no record, promotion or gate counts it.
            policy_id=None if pick.practice else self._policy,
            champion=self.challenger is None and not self.shadow,
            shadow=self.shadow,
            practice=pick.practice,
            model_version=signal.model_version,
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
        paid = costs(trade.entry_price, price, trade.quantity, self.rules.fee_rate)
        # The day the exit is counted in, rolled before the exit is kept: a day read again after it
        # would hold it already (an exit the clock had not reached the day of yet, #396).
        if self.holds.roll(at):
            self._dirty = True
        if self._returned is not None:  # made on the first quote after a stall: it waited (#407)
            self.store.stall_effect(trade.id, self._returned.id, WAITED)
        self.store.exit(trade, Exit(at, price, reason, paid.sell_fee, paid.tax, quote.received_at))
        self._open = None
        self._sell_after = None
        self._dirty = True
        net = (price - trade.entry_price) * trade.quantity - trade.buy_fee - paid.sell_fee - paid.tax
        log.info(
            "%s sold %s at %s (%s, trade %s): net %.0f won",
            self._whose(),
            trade.quantity,
            price,
            reason,
            trade.id,
            net,
        )
        if self.holds.exited(net, at) is not None:  # its realized loss reached a limit
            self._enforce(at)
