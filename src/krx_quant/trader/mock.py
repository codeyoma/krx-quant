"""KIS mock trading (#405, #393 "KIS 주문"): with ``KRX_TRADER_MODE=mock`` the champion's book places
its orders on KIS's mock server through the trader's broker (``kis_orders.Broker``, #404), by the
same decisions, limits, pauses and ledger as paper (``paper.PaperTrader``): only how an order fills
changes. The live mode (#408, ``live.py``) is the same book of mode ``live`` on the live account
(``kis_orders.trader_live_broker``): what is said below of the mock holds for it, but where it says
otherwise (the order probe's orders are the mock account's alone; the live account's value checked at
the start). It is the execution layer alone: the champion policy's paper record is kept by its paper
shadow beside it, and the challengers' in paper as ever (``desk.py``), on the same quotes; nothing
of the KIS fills reaches them (``gap.py`` shows the difference).

**Entry.** A buy decided buys at the first quote received after its decision, as in paper, but on
KIS: the orderable cash is read at that quote's best ask (``Broker.orderable``) and the size cut to
what it pays (``Orderable.most``: the mock server accepts a buy beyond it, #403); then a limit buy
at that best ask (``Broker.take``), its fills read off the day's order inquiry, what is left
cancelled after ``Timing.wait`` seconds. A partial fill is a position of that many shares, and a
fill that came before the cancel landed counts. None filled: no trade (``UNFILLED``). The trade's
entry is the fills' average price (``avg_prvs``: the mock fills half a tick better than the ask)
and quantity.

**Take-profit.** Right after the fill, a limit sell of the shares at the target, rounded up to the
tick (``tick_up``: the first price a paper take-profit fills at, once the best bid reaches the
target), rests on KIS. Its row on the order inquiry is read every ``Timing.follow`` seconds and each
fill kept; once it has filled whole, the trade exits ``take_profit`` at its average.

**Stop, time, pause, loss limit.** When paper would sell (the best bid at or below the stop, the
time barrier, a pause that sells, a loss limit, ``paper.py``), the take-profit sell is cancelled
and its row read until nothing is left of it: a fill that came before the cancel is kept, and one
that filled it whole exits the trade ``take_profit``. Then a limit sell of the shares left at the
best bid of the latest quote (``Broker.take``), and again at the then best bid while shares are
left (``Timing.rounds`` at a time, then on the next quote or tick). A sell whose cancel did not
take stays open in the ledger and is followed: before anything more is sold it is cancelled again
and its fills kept, so that a fill of it during the wait is never sold twice. The trade exits once
every share is sold, at the average of its sells' fills, with the reason the sale began with. A
take-profit sell KIS did not take is sold so, at the best bid, once that bid reaches the target.

**Ledger.** A trade's entry, exit, costs and P&L come from KIS's fills: the commission
(``KRX_FEE_PERCENT``) a side and the sell tax on the fills' notional, as in paper. Each order is
kept with KIS's order (``orders.broker_id``, ``broker_branch``) and each fill as the inquiry showed
it (``store.open_position``, ``add_order``, ``add_fill``, ``exit_on_fills``); a buy KIS took of
which nothing filled is kept too (``unfilled_buys``), so that the ledger holds every order KIS has.
The limits and pauses (#396, ``limits.py``) count these trades as they count paper's, an open
trade valued at what its sells brought so far and the rest of its shares at the best bid; the
trades are of mode ``mock``.

**A failed order**: KIS refused it, it had no answer (whether it went in is not known), or it could
not be sent (no token, an inquiry KIS did not answer). It is kept (``failures``) and sent to
Telegram, and new entries stop for the rest of the day (``limits.ORDER_FAILED``); a sale that failed
is tried again ``Timing.retry`` seconds later.

**A buy whose answer was lost** (#406): each buy is kept before it is sent (``unfilled_buys``,
``store.SENT``), with its decision and the trade it would open. One KIS took but whose answer did
not come (or the trader stopped before it came) is looked up on the day's order inquiry
(``reconcile.candidates``: a buy of 005930 at its limit and of its quantity, placed around when it
was sent, whose number the ledger does not keep, not the order probe's): when exactly one KIS
order can be it and nothing is left of it, it is adopted (its fills open the trade its decision
would have, its take-profit rests); none, and it did not go in (``store.ABSENT``, at a start: KIS
shows an order it took at once); more than one, or one still open on KIS (cancelling it would name
KIS's branch from the inquiry, ``ord_gno_brno``, not measured yet), and new entries pause until the
server resumes them (below). Right after the lost answer it is looked up for ``Timing.wait``; what
is not settled then, the next start looks up again.

**Start** (#406, ``start``, before anything is bought or sold): the ledger's orders open on KIS when
the trader stopped are read, each on the day it was placed: their fills kept, one with nothing left
closed, one of a day gone by over with what filled (a KRX limit order is a day order: it is not
cancelled, KIS ended it at the close). A trade whose sells hold every share exits. The buys sent
whose answer was not had are looked up (above). Then the account against the ledger
(``reconcile.compare``): the shares held, the other symbols held, the day's orders with something
left that the trader does not follow. What is outside the ledger beyond what was accepted last, a
share of the ledger the account does not hold, or a lost buy not adopted: the trader touches none of
it, pauses new entries (a pause of source ``reconcile``, ``limits.Guard``) and says what it found on
Telegram; the open trade is sold by its own rules still. ``krx-quant trade resume`` lifts it and
accepts what is outside the ledger then as the account's (``reconciles``). Then the open trade: past
its time barrier, it is sold at once (at the first quote's best bid, ``time``); with time left and
no take-profit resting on KIS (it was cancelled there, expired with its day, refused, or never
placed), the take-profit is placed again for the shares not sold. A restart never sends an order
twice: an order kept open is read, never sent again, and a buy decided is never decided again. While
KIS cannot be read the start is tried again every ``Timing.retry`` seconds, and nothing is bought or
sold until it is done (a prediction read meanwhile is ``reconciling``).

One order is in flight at a time (``_work``): while it is, nothing else is bought or sold, and a
sale asked for meanwhile begins once it ends. A trader stopped waits for it (``close``).

**The live account's value** (#408): at the start's reconcile, an account whose value
(``Balance.total``, ``tot_evlu_amt``) is above the operating cap (``KRX_TRADER_CAPITAL``) is told on
Telegram: the live account is meant to hold that much alone (#393). The trader buys within its cap
all the same.

**Each KIS day against KIS** (#408, ``order_checks``): once a trading day's session is over (its
closing auction ended, ``CHECK_AFTER`` later) with nothing open, the book reads that day's orders on
KIS and compares them with the ledger's (``mock_check.Check``: each order's side, quantity, fills
and fill price, and KIS's orders the ledger does not hold, the order probe's apart), and keeps the
verdict, ``same``, ``different`` or ``no_orders``; a ``different`` day is told on Telegram. A day the
inquiry could not be read is tried again ``Timing.check`` seconds later, and a start compares the
days of the last ``CHECK_REACH`` it has not yet. The live mode's execution checks and its first
period read them (``live.py``): a day not compared is no pass.

**The quotes stalled** (#407, ``stall.py``): nothing new is bought, as in paper. While a position is
open, KIS's best quotes (``Broker.best``: KRX's best bid and ask, the market the trader orders on)
are read on the trader's key, one read at a time, at the stall and then on the clock's ticks
``Timing.rest`` seconds after the last read began; each is taken as a quote, stamped with the clock
at its answer. The take-profit rests on KIS as ever, and the stop, the time barrier, a pause that
sells and the loss limits are checked on them, the sale's sells placed at their best bid. A sale
begun on them keeps that on its trade (``trade_stalls.effect`` ``rest``). A read that fails is said
once until one is answered, and read again at the next turn; it is no failed order. Once the
collector's quotes come again the reads stop (one in flight is not taken) and the stream is the
quotes again. The paper shadow and the challengers have no REST access: they wait for the stream
(``paper.py``).
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any

from krx_quant.feeds.base import kst_day
from krx_quant.kis_orders import (
    POLL,
    REFUSED,
    SIDE_BUY,
    SIDE_SELL,
    TAKE_WAIT,
    Broker,
    BrokerError,
    OrderRef,
    OrderRow,
    Sent,
    Unexpected,
    same_number,
)
from krx_quant.market_calendar import Session
from krx_quant.pricing import SELL_TAX, tick_size
from krx_quant.trader import LIVE, MOCK, SYMBOL
from krx_quant.trader.cells import Signal
from krx_quant.trader.limits import ORDER_FAILED, SALE, Notify
from krx_quant.trader.live import kis_days
from krx_quant.trader.mock_check import DIFFERENT as CHECK_DIFFERENT
from krx_quant.trader.mock_check import NO_ORDERS as CHECK_NO_ORDERS
from krx_quant.trader.mock_check import SAME as CHECK_SAME
from krx_quant.trader.mock_check import Check, failures, ledger_orders, probe_window
from krx_quant.trader.paper import NO_SHARES, TAKE_PROFIT, TIME, PaperTrader, Rules, _Waiting
from krx_quant.trader.quotes import BestQuote
from krx_quant.trader.reconcile import Finding, Outside, Window, candidates, compare
from krx_quant.trader.store import (
    ABSENT,
    BUY_REFUSED,
    CANCELLED,
    CLEAN,
    DIFFERENT,
    FILLED,
    HELD,
    NO_ORDERS,
    OPEN,
    PAUSE,
    RECONCILE,
    REST,
    SAME,
    SELL,
    SENT,
    UNSENT,
    WAITED,
    Entry,
    OpenTrade,
    Placed,
    Resting,
    Stall,
    TraderStore,
)

log = logging.getLogger(__name__)

# Why a buy decided bought nothing on KIS: nothing filled before its cancel (the ask moved).
UNFILLED = "unfilled"
# Why a prediction bought nothing (#406): the start's reconcile of the account was not done yet.
RECONCILING = "reconciling"
# What became of a failed order (``failures.outcome``): KIS refused it, it had no answer, or it
# could not be sent.
UNKNOWN, ERROR = "unknown", "error"
# A trader stopping waits this long for the order in flight (compose gives it 10 s).
CLOSE_SECONDS = 8.0
# While the collector's quotes stall (#407), KIS's best quotes are read this often, with a position
# open. Every call on the trader's key goes through one limiter, two in any 1.1 s
# (``kis_orders.PER_SECOND``: of five sent at once the mock server answered three, and none of
# thirty sent a second apart was refused, #403). Two seconds apart, at most one read falls in any
# 1.1 s window, so the other slot is always an order call's. Beside the take-profit's row read every
# two seconds (``Timing.follow``) that is one call a second; during a sale (the take-profit's reading
# stopped), its inquiry every second (``kis_orders.POLL``) and the reads make 1.5 a second, under the
# limiter's 1.8. A read every second would make a sale's 2 a second, over it: the order calls would
# wait their turn.
REST_SECONDS = 2.0
# A day's orders are compared with KIS's this long after its closing auction ends (#408), and a
# start compares the days of this far back it has not (KIS's day inquiry reaches three months).
CHECK_AFTER = timedelta(minutes=1)
CHECK_REACH = timedelta(days=80)
# With no day to compare, the days are looked at again this long after (a new day's ledger is kept
# during its session, compared after its close).
CHECK_IDLE = timedelta(minutes=10)
# The verdict a day's comparison keeps (``mock_check.Check.code``).
VERDICTS = {CHECK_SAME: SAME, CHECK_DIFFERENT: DIFFERENT, CHECK_NO_ORDERS: NO_ORDERS}


@dataclass(frozen=True)
class Timing:
    """The mock book's waits, in seconds."""

    wait: float = TAKE_WAIT  # a marketable order's fills awaited before what is left is cancelled
    poll: float = POLL  # the inquiry read this often while it waits
    follow: float = 2.0  # the take-profit sell's row read this often while it rests
    retry: float = 30.0  # a sale whose order failed is tried again this long after
    rounds: int = 5  # sells at the best bid in one go; then the next go no sooner than ``wait`` later
    rest: float = REST_SECONDS  # KIS's best quotes read this often while the quotes stall (#407)
    check: float = 600.0  # a day's comparison whose inquiry could not be read is tried again (#408)


@dataclass(frozen=True)
class _LiveBuy:
    """A buy KIS took whose cancel did not take, or that its order inquiry never showed: open on KIS
    still, so followed until nothing is left of it. Its fills join its trade; while none filled yet,
    its ``unfilled_buys`` row (``unfilled``) keeps its decision and the trade it would open
    (``entry``, its fill to come), so that a restart follows it too."""

    placed: Placed
    day: date
    filled: int
    amount: float  # what filled, in won
    decision: int | None = None
    entry: Entry | None = None
    unfilled: int | None = None


def tick_up(price: float) -> int:
    """The first price on KRX's tick at or above ``price``."""
    up = math.ceil(round(price / tick_size(price), 9)) * tick_size(price)
    while up % tick_size(up):  # past a band's edge, onto the coarser tick of the band above
        up = math.ceil(up / tick_size(up)) * tick_size(up)
    return int(up)


def _row_of(rows: list[OrderRow], number: str | None) -> OrderRow | None:
    """Order ``number``'s own row among ``rows`` (not a cancel of it)."""
    return next((r for r in rows if not r.cancel and same_number(r.number, number)), None)


def _unknown(sent: Sent) -> str:
    """Why an order's outcome is not known: no answer, or an acceptance that named no order."""
    answer = sent.answer
    if answer.status is None:
        return f"응답 없음({answer.error})"
    return f"HTTP {answer.status} {answer.code or '-'}: 주문번호가 없는 응답"


class MockTrader(PaperTrader):
    """The champion's book in mock mode (module doc): ``paper.PaperTrader``'s decisions, its orders
    on KIS through ``broker``. ``clock`` is the time (KST) an order's fills are read at; ``timing``
    the waits. Made, and driven, on the running event loop: its orders are tasks of it."""

    def __init__(
        self,
        store: TraderStore,
        rules: Rules,
        sessions: Callable[[date], Session | None],
        started_at: datetime,
        broker: Broker,
        clock: Callable[[], datetime],
        notify: Notify | None = None,
        timing: Timing | None = None,
        mode: str = MOCK,
    ) -> None:
        self._broker = broker
        self._clock = clock
        self._timing = timing or Timing()
        self._work: asyncio.Task[None] | None = None  # the order in flight: a buy, or a sale
        self._follower: asyncio.Task[None] | None = None  # the take-profit sell's row, read as it rests
        self._selling: str | None = None  # the reason the open trade's sale began with
        self._retry_at: datetime | None = None  # a sale that failed goes again from then
        self._resting: Resting | None = None  # the take-profit sell resting on KIS
        self._live: Resting | None = None  # a sale's sell whose cancel did not take: open on KIS still
        self._live_buy: _LiveBuy | None = None  # a buy whose cancel did not take: open on KIS still
        self._reading: asyncio.Task[None] | None = None  # KIS's best quotes read in a stall (#407)
        self._checking: asyncio.Task[None] | None = None  # a day's orders compared with KIS's (#408)
        self._check_after: datetime | None = None  # a comparison that could not read KIS goes again then
        self._rest_at: datetime | None = None  # when the last of those reads began
        self._rest_failing = False  # the last read failed (said once until one is answered)
        self._closing = False
        self._reconciled = False  # the start's reconcile done (#406): nothing is bought or sold before
        self._unreadable = False  # a start that could not read KIS, told
        self._notify: Notify = notify or (lambda text: None)
        super().__init__(store, rules, sessions, started_at, mode, notify)
        if self._open is not None:  # a restart: its orders open on KIS when it stopped go on
            entry = store.open_entry(self._open.id)
            if entry is not None:
                placed, filled, amount, day = entry
                self._live_buy = _LiveBuy(placed, day, filled, amount)
            for order in store.open_sells(self._open.id):
                if order.purpose == TAKE_PROFIT:
                    self._resting = order
                else:  # a sale was under way: it goes on, that order taken off first
                    self._live, self._selling = order, order.purpose
            # A sale begun goes on, though none of its sells is open (each cancelled, or refused).
            self._selling = store.sale_begun(self._open.id) or self._selling
        else:  # a restart: a buy of which nothing filled yet, open on KIS, is followed still
            kept = store.open_unfilled_buy(self.mode)
            if kept is not None:
                unfilled, decision, ordered, since, would = kept
                self._live_buy = _LiveBuy(ordered, since, 0, 0.0, decision, would, unfilled)
                store.unfilled(decision, None)  # not given up at the start: it may fill still
                log.warning("trader: a buy left open on KIS at the stop, nothing filled yet: taken off now")

    # The paper book's hooks.

    def _busy(self) -> bool:
        return super()._busy() or self._work is not None or self._live_buy is not None or not self._reconciled

    def _held_off(self, signal: Signal) -> str:
        return RECONCILING if not self._reconciled else super()._held_off(signal)

    def _fill_buy(self, waiting: _Waiting, quote: BestQuote, quantity: int) -> None:
        self._start(self._enter(waiting, quote, quantity))

    def _exit(self, trade: OpenTrade, at: datetime, price: float, reason: str, quote: BestQuote) -> None:
        if reason == TAKE_PROFIT and (self._resting is not None or self._work is not None):
            return  # the resting sell takes it (its fills are read as it rests), or is being placed
        if self._selling is None:
            self._begin_sale(trade, reason)
        self._kick(self._clock())

    def on_quote(self, quote: BestQuote) -> None:
        super().on_quote(quote)
        self._kick(self._clock())

    def on_stall(self, stall: Stall, at: datetime) -> None:
        super().on_stall(stall, at)
        if self._open is not None:
            log.info(
                "trader: reading KIS's best quotes over REST every %gs while the quotes stall (trade %s)",
                self._timing.rest,
                self._open.id,
            )
        self._read_rest(at)

    def on_time(self, now: datetime) -> None:
        super().on_time(now)
        self._read_rest(now)
        self._kick(now)
        self._check_days(now)

    def state(self) -> dict[str, Any]:
        body = super().state()
        if body["position"] is not None and self._selling is not None:
            body["position"]["selling"] = True
        return body

    def _unrealized(self, trade: OpenTrade, bid: float) -> float:
        """What ``trade`` nets once the shares not sold yet are sold at ``bid``: what its sells' fills
        brought (a take-profit filled in part, a sale under way) and the rest at ``bid``, less the
        buy's commission and the sales' commission and tax."""
        sold, amount = self.store.sells(trade.id)
        proceeds = amount + bid * (trade.quantity - sold)
        paid = trade.buy_fee + proceeds * (self.rules.fee_rate + SELL_TAX)
        return proceeds - trade.entry_price * trade.quantity - paid

    # The orders in flight.

    async def idle(self) -> None:
        """Until no order is in flight (one ending may begin the next: a sale asked for meanwhile), nor
        a REST read of a stall (#407)."""
        while (task := self._work or self._reading) is not None:
            await asyncio.wait({task})

    async def close(self, timeout: float = CLOSE_SECONDS) -> None:
        """The trader stopping: the take-profit sell's reading stops (it rests on KIS), and so do the
        REST quotes' (#407); the order in flight is waited for ``timeout`` seconds at most, then
        cancelled."""
        self._closing = True
        follower, work, reading, checking = self._follower, self._work, self._reading, self._checking
        for task in (follower, reading, checking):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if work is not None:
            try:
                await asyncio.wait_for(asyncio.shield(work), timeout)
            except TimeoutError:
                log.error("trader: an order in flight did not end within %gs of the stop", timeout)
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)

    def _start(self, work: Coroutine[Any, Any, None]) -> None:
        self._work = asyncio.get_running_loop().create_task(self._run(work))

    async def _run(self, work: Coroutine[Any, Any, None]) -> None:
        try:
            await work
        except Exception as e:  # a broken answer or store: kept as a failed order, the trader goes on
            log.exception("trader: an order on KIS broke off")
            trade = self._open
            what = "reconcile" if not self._reconciled else "sell" if trade is not None else "buy"
            self._failed(what, ERROR, f"{type(e).__name__}: {e}")
            if trade is not None or not self._reconciled:
                self._retry_at = self._clock() + timedelta(seconds=self._timing.retry)
        finally:
            self._work = None
            self._dirty = True
        self._kick(self._clock())

    def _kick(self, now: datetime) -> None:
        """What the open trade needs next, when no order is in flight: its sale begun (or tried again
        once ``_retry_at`` is past), or its resting take-profit sell's row followed."""
        trade = self._open
        self._link_stall()  # a trade its buy opened during a stall (#407)
        if self._closing or self._work is not None:
            return
        if not self._reconciled:  # the start's reconcile first (#406), again once ``_retry_at`` is past
            if self._retry_at is None or now >= self._retry_at:
                self._retry_at = None
                self._start(self._reconcile())
            return
        if self._live_buy is not None and self._selling is None:  # a sale takes it off first itself
            if self._retry_at is None or now >= self._retry_at:
                self._retry_at = None
                self._start(self._settle_buy())
            return
        if trade is None:
            return
        status = self.status
        if self._selling is None and status in SALE:  # a pause or a limit while it was bought
            if self._sell_after is not None and not self._quoted_after(self._sell_after):
                return  # it sells on the first quote received after the status began, as in paper
            self._begin_sale(trade, SALE[status])
        if self._selling is not None:
            if (self._retry_at is None or now >= self._retry_at) and self._bid() is not None:
                self._retry_at = None
                self._stop_following()
                self._start(self._sell(trade, self._selling))
            return
        if self._resting is not None and (self._follower is None or self._follower.done()):
            self._follower = asyncio.get_running_loop().create_task(self._follow_resting(trade.id))

    def _begin_sale(self, trade: OpenTrade, reason: str) -> None:
        """The sale of ``trade`` decided: latched, and kept on the trade for a restart (#405); begun
        during a stall, on the REST quotes, or on the first quote after one, kept so (#407)."""
        self._selling = reason
        self.store.begin_sale(trade.id, reason)
        if self._stall is not None:
            self.store.stall_effect(trade.id, self._stall.id, REST)
        elif self._returned is not None:
            self.store.stall_effect(trade.id, self._returned.id, WAITED)
        log.info(
            "trader: selling trade %s on KIS (%s%s)",
            trade.id,
            reason,
            ", on KIS's REST quotes: the quotes stalled" if self._stall is not None else "",
        )

    def _quoted_after(self, moment: datetime) -> bool:
        last = self._last
        return last is not None and last.received_at > moment

    def _stop_following(self) -> None:
        follower = self._follower
        if follower is not None and not follower.done() and follower is not asyncio.current_task():
            follower.cancel()
        self._follower = None

    def _bid(self) -> int | None:
        """The best bid a sell is placed at: the latest quote's, received since the trader started (in a
        stall, KIS's REST one, #407)."""
        last = self._last
        if last is None or last.received_at < self._started_at:
            return None
        return round(last.bid)

    # The start (#406).

    async def start(self) -> None:
        """Before anything is bought or sold: the ledger's orders read on KIS, the buys whose answer
        was lost looked up, the account compared with the ledger, the open trade sold or its
        take-profit placed again (module doc). While KIS cannot be read it is tried again every
        ``Timing.retry`` seconds (``_kick``), and nothing is bought or sold until it is done."""
        self._kick(self._clock())
        await self.idle()

    async def _reconcile(self) -> None:
        now = self._clock()
        today = kst_day(now)
        days: dict[date, list[OrderRow]] = {}

        async def rows(day: date) -> list[OrderRow]:
            """``day``'s orders on KIS, every symbol's, read once a reconcile."""
            if day not in days:
                days[day] = await self._broker.orders(day)
            return days[day]

        try:
            await self._settle_kept(rows, today)
            doubtful = await self._find_sent(rows, today)
            today_rows = await rows(today)  # before the balance: a fill between is in the bounds
            balance = await self._broker.balance()
        except BrokerError as e:
            self._retry_at = self._clock() + timedelta(seconds=self._timing.retry)
            log.warning(
                "trader: could not read the KIS account to reconcile it with the ledger (%s): nothing is"
                " bought or sold until it is, read again in %gs",
                e,
                self._timing.retry,
            )
            if not self._unreadable:
                self._unreadable = True
                self._notify(
                    f"자동 매매({self.mode}): 시작할 때 KIS 계좌를 읽지 못했습니다({e}). 장부와 맞추기 전에는"
                    f" 사지도 팔지도 않고, {self._timing.retry:g}초마다 다시 읽습니다."
                )
            return
        finding = compare(
            balance,
            today_rows,
            self._followed(),
            self._held(),
            Outside.of(self.store.accepted_outside(self.mode)),
            self._probe(today),
            doubtful,
        )
        at = self._clock()
        if self.mode == LIVE and balance.total > self.rules.capital:  # #408: the account holds the cap alone
            log.warning("trader: the live account's value is above the operating cap")
            self._notify(
                f"자동 매매({self.mode}): 실전 계좌의 평가금({balance.total:,}원)이 운용 상한"
                f"({self.rules.capital:,}원)보다 큽니다. 이 계좌에는 운용 상한만큼만 두기로 했습니다(#393)."
                " trader는 운용 상한 안에서만 사지만, 계좌를 확인하세요."
            )
        if finding.clean:
            self.store.add_reconcile(at, self.mode, CLEAN, finding.outside.as_json(), finding.detail())
            log.info(
                "trader: the KIS account is as the ledger has it (outside it, accepted: %s)",
                finding.outside.as_json(),
            )
        else:
            self._hold(finding, at)
        if self._unreadable:
            self._notify(f"자동 매매({self.mode}): KIS 계좌를 읽어 장부와 맞췄습니다.")
        self._reconciled, self._unreadable, self._dirty = True, False, True
        await self._go_on(at, finding)

    async def _settle_kept(self, rows: Callable[[date], Awaitable[list[OrderRow]]], today: date) -> None:
        """The ledger's orders open on KIS when the trader stopped, each read on the day it was
        placed: its fills kept; one with nothing left closed; one of a day gone by over with what
        filled (a day order: KIS ended it at the close, nothing is cancelled). A trade whose sells
        now hold every share exits."""
        live = self._live_buy
        if live is not None:
            over = live.day < today
            row = _row_of(await rows(live.day), live.placed.number)
            if row is not None:
                self._absorb(row.filled, row.average, 0 if over else row.left)
            elif over:
                self._absorb(live.filled, live.amount / live.filled if live.filled else 0.0, 0)
            if over:
                log.info("trader: the buy left open on KIS on %s is over with its day", live.day)
        reason = self._selling or TAKE_PROFIT
        for still in (self._resting, self._live):
            trade = self._open
            if still is None or trade is None:
                continue
            day = still.day or self._day_of(trade)
            row = _row_of(await rows(day), still.number)
            if row is not None:
                still = self._keep(still, row)
            if still.purpose == TAKE_PROFIT and still.filled >= still.quantity:
                reason = TAKE_PROFIT  # it sold the rest, whatever sale began meanwhile
            if day < today or (row is not None and row.left == 0):
                self._close_sell(still)
                log.info(
                    "trader: the %s sell of trade %s left on KIS is over (%s of %s filled)",
                    still.purpose,
                    trade.id,
                    still.filled,
                    still.quantity,
                )
        trade = self._open
        if trade is not None and self._live_buy is None and self._all_sold(trade):
            self._finish(trade, reason)

    async def _find_sent(self, rows: Callable[[date], Awaitable[list[OrderRow]]], today: date) -> int:
        """Each buy sent whose answer was not had looked up on its day's inquiry (module doc): adopted,
        not on KIS, or doubtful (left as sent). How many are doubtful."""
        doubtful = 0
        for unfilled, decision, placed, day, would in self.store.sent_buys(self.mode):
            found = candidates(
                await rows(day), placed, day, self.store.broker_numbers(self.mode), self._probe(day)
            )
            alone = self._open is None and self._live_buy is None
            if len(found) == 1 and alone and (day < today or found[0].left == 0):
                self._adopt(unfilled, decision, placed, day, would, found[0])
            elif not found:
                self.store.close_unfilled_buy(unfilled, ABSENT)
                log.info(
                    "trader: the buy at %s sent on %s is not on KIS: it did not go in", placed.price, day
                )
            else:
                doubtful += 1
                log.warning(
                    "trader: the buy at %s sent on %s, whose answer was lost, is not adopted: %s KIS order(s)"
                    " can be it%s",
                    placed.price,
                    day,
                    len(found),
                    "" if alone else ", and another trade is open",
                )
        return doubtful

    async def _look_up(self, unfilled: int, decision: int, placed: Placed, day: date, would: Entry) -> None:
        """A buy whose answer was lost, looked up on KIS for ``Timing.wait`` seconds right after: adopted
        once one KIS order alone can be it and nothing is left of it; more than one, or one still open
        then, pauses new entries (``_hold``); none, and the next start looks it up again."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timing.wait
        found: list[OrderRow] = []
        while True:
            try:
                rows = await self._broker.orders(day)
                found = candidates(rows, placed, day, self.store.broker_numbers(self.mode), self._probe(day))
            except BrokerError as e:
                log.warning("trader: could not look up the buy whose answer was lost (%s)", e)
            if len(found) == 1 and found[0].left == 0:
                self._adopt(unfilled, decision, placed, day, would, found[0])
                trade = self._open
                if trade is not None:
                    await self._place_take_profit(trade)
                return
            if len(found) > 1 or loop.time() >= deadline:
                break
            await asyncio.sleep(self._timing.poll)
        if not found:
            log.warning("trader: the buy whose answer was lost is not on KIS yet: the next start looks again")
            return
        accepted = Outside.of(self.store.accepted_outside(self.mode))
        self._hold(Finding(accepted, Outside(), doubtful=1), self._clock())

    def _adopt(
        self, unfilled: int, decision: int, placed: Placed, day: date, would: Entry, row: OrderRow
    ) -> None:
        """The buy ``unfilled`` whose answer was lost is KIS's order ``row``, over: its fills open the
        trade its decision would have (``would``), or none filled and it bought nothing."""
        self.store.accept_buy(unfilled, None, row.number)
        self.store.unfilled(decision, None)  # not given up: its fills open its trade
        self._live_buy = _LiveBuy(replace(placed, number=row.number), day, 0, 0.0, decision, would, unfilled)
        self._absorb(row.filled, row.average, 0)
        log.warning(
            "trader: the buy at %s whose answer was lost was found on KIS: %s of %s filled, adopted",
            placed.price,
            row.filled,
            placed.quantity,
        )
        self._notify(
            f"자동 매매({self.mode}): 응답이 없던 매수를 KIS 주문 체결 조회에서 찾아 장부에 넣었습니다"
            f"({placed.quantity}주 가운데 {row.filled}주 체결). 들고 있는 것은 원래 규칙"
            "(목표가, 손절가, 시간)대로 팝니다."
        )

    def _hold(self, finding: Finding, at: datetime) -> None:
        """The account unlike the ledger (``finding``): nothing of it touched, new entries paused by a
        pause of source ``reconcile`` until ``krx-quant trade resume``, and told on Telegram; once
        only while the same is found again and that pause holds."""
        held = self.store.held_reconcile(self.mode)
        outside = finding.outside.as_json()
        if held is not None and self.guard.pause is not None and finding.same_as(held["detail"]):
            self.store.add_reconcile(at, self.mode, HELD, outside, finding.detail())
            log.warning(
                "trader: the KIS account is unlike the ledger as before (%s): paused still", finding.detail()
            )
            return
        control = self.store.add_control(at, PAUSE, RECONCILE, sell_now=False)
        self.store.add_reconcile(at, self.mode, HELD, outside, finding.detail(), control.id)
        log.warning("trader: the KIS account is unlike the ledger (%s): new entries paused", finding.detail())
        self.guard.apply(control, at)
        self._notify(finding.notice(self.mode))
        self._dirty = True
        self._enforce(at)

    async def _go_on(self, now: datetime, finding: Finding) -> None:
        """The open trade after the reconcile: past its time barrier, sold at once; with time left
        and no take-profit resting on KIS, the take-profit placed again for the shares not sold."""
        trade = self._open
        if trade is None:
            return
        if self._selling is None and now >= trade.time_barrier:
            self._begin_sale(trade, TIME)
            return
        if self._resting is None and self._live_buy is None and not finding.missing:
            await self._place_take_profit(trade)

    def _followed(self) -> list[str]:
        """KIS's numbers of the orders the trader follows: its open buy, its resting take-profit, a
        sale's sell open."""
        numbers = [o.number for o in (self._resting, self._live) if o is not None]
        live = self._live_buy
        if live is not None and live.placed.number is not None:
            numbers.append(live.placed.number)
        return numbers

    def _held(self) -> tuple[int, int]:
        """The fewest and the most shares of 005930 the ledger can hold now: its open trade's shares
        not sold yet, less what its open sells may still sell, plus what its open buy may still buy."""
        trade = self._open
        shares = 0 if trade is None else trade.quantity - self.store.sold(trade.id)
        low = shares - sum(o.quantity - o.filled for o in (self._resting, self._live) if o is not None)
        live = self._live_buy
        high = shares + (0 if live is None else live.placed.quantity - live.filled)
        return max(0, low), high

    def _probe(self, day: date) -> Window:
        """When the order probe ran on ``day`` on the same account (``mock_check.probe_window``): the
        mock account's alone, the probe never orders on the live one (#408)."""
        if self.mode != MOCK:
            return None
        return probe_window(self.store.path.parent, day)

    # Each KIS day against KIS (#408).

    def _check_days(self, now: datetime) -> None:
        """The oldest KIS day not compared yet whose session is over, compared with KIS's inquiry, when
        nothing is open nor in flight (module doc)."""
        if self._closing or not self._reconciled or self._checking is not None or self._work is not None:
            return
        if self._open is not None or self._live_buy is not None:
            return
        if self._check_after is not None and now < self._check_after:
            return
        done = self.store.order_checks(self.mode)
        for day in kis_days(self.store.db, self.mode):
            session = self._sessions(day)
            if day in done or session is None or now - session.close > CHECK_REACH:
                continue
            if now < session.close + CHECK_AFTER:
                self._check_after = session.close + CHECK_AFTER
                return
            self._checking = asyncio.get_running_loop().create_task(self._check_day(day))
            return
        self._check_after = now + CHECK_IDLE

    async def _check_day(self, day: date) -> None:
        try:
            try:
                rows = await self._broker.orders(day, SYMBOL)
            except BrokerError as e:
                self._check_after = self._clock() + timedelta(seconds=self._timing.check)
                log.warning(
                    "trader: could not read %s's orders on KIS to compare them with the ledger (%s); again"
                    " in %gs",
                    day,
                    e,
                    self._timing.check,
                )
                return
            db = self.store.db
            check = Check(
                day,
                ledger_orders(db, day, self.mode),
                rows,
                self._probe(day),
                len(failures(db, day, self.mode)),
            )
            verdict = VERDICTS[check.code]
            probe, others = check.extra()
            detail = {
                "orders": len(check.ledger),
                "compared": len(check.compared),
                "same": sum(1 for c in check.compared if c.same),
                "missing": len(check.missing),
                "extra": len(others),
                "probe": len(probe),
                "failed": check.failed,
            }
            self.store.add_order_check(self.mode, day, verdict, self._clock(), detail)
            log.info("trader: %s's orders against KIS: %s (%s)", day, verdict, detail)
            if verdict == DIFFERENT:
                self._notify(
                    f"자동 매매({self.mode}): {day}의 주문이 장부와 KIS 주문 체결 조회에서 다릅니다(장부 "
                    f"{detail['orders']}건, 같은 것 {detail['same']}건, KIS에 없는 것 {detail['missing']}건, "
                    f"장부에 없는 것 {detail['extra']}건). 실전 점검(#408)이 이날을 통과로 세지 않습니다."
                    " 밤 리포트와 서버의 trader 로그를 보세요."
                )
        except Exception:  # a broken answer or store: said, and the day compared again later
            log.exception("trader: comparing a day's orders with KIS broke off")
            self._check_after = self._clock() + timedelta(seconds=self._timing.check)
        finally:
            if self._checking is asyncio.current_task():
                self._checking = None

    # The REST quotes while the collector's quotes stall (#407).

    def _read_rest(self, now: datetime) -> None:
        """A read of KIS's best quotes begun at ``now``, if the quotes stall with a position open, none
        is in flight, and the last began ``Timing.rest`` seconds ago at least (module doc)."""
        stall = self._stall
        if stall is None or self._open is None or self._closing or self._reading is not None:
            return
        if self._rest_at is not None and (now - self._rest_at).total_seconds() < self._timing.rest:
            return
        self._rest_at = now
        self._reading = asyncio.get_running_loop().create_task(self._read_best(stall))

    async def _read_best(self, stall: Stall) -> None:
        """One read of KIS's best quotes, taken as a quote while ``stall`` lasts."""
        try:
            try:
                best = await self._broker.best(SYMBOL)
                if best.bid <= 0 or best.ask <= 0:  # a side empty (an upper or lower limit): none to sell on
                    raise Unexpected(f"book: a side is empty (bid {best.bid > 0}, ask {best.ask > 0})")
            except BrokerError as e:  # not answered, or not as measured: read again at the next turn
                if not self._rest_failing:
                    log.warning(
                        "trader: could not read KIS's best quotes over REST (%s); read again every %gs",
                        e,
                        self._timing.rest,
                    )
                self._rest_failing = True
                return
            if self._rest_failing:
                log.info("trader: KIS's best quotes read over REST again")
            self._rest_failing = False
            if self._stall is not stall:  # the quotes back meanwhile: the stream's are the quotes
                return
            self.store.read_rest(stall.id)
            at = self._clock()
            self.on_quote(
                BestQuote(at, float(best.bid), float(best.ask), float(best.bid_size), float(best.ask_size))
            )
        except Exception:  # a broken store: said, and read again at the next turn
            log.exception("trader: a REST quote broke off")
        finally:
            if self._reading is asyncio.current_task():
                self._reading = None

    # The buy.

    async def _enter(self, waiting: _Waiting, quote: BestQuote, quantity: int) -> None:
        ask = round(quote.ask)
        day = kst_day(quote.received_at)
        try:
            orderable = await self._broker.orderable(SYMBOL, ask)
        except BrokerError as e:
            self._buy_failed(waiting, "orderable", ERROR, str(e))
            return
        quantity = min(quantity, orderable.most(ask, self.rules.fee_rate))
        if quantity < 1:
            self._give_up(waiting, NO_SHARES)
            return
        # The trade it would open, its fill to come: kept with the order, so that a restart follows it.
        would = self._entry(waiting, self._clock(), ask, quantity, quote.received_at)
        # Kept before it is sent (#406): a buy whose answer is lost, or a stop before it comes, leaves
        # its row and decision for the order to be looked up on KIS and adopted.
        sent_at = self._clock()
        unfilled = self.store.add_unfilled_buy(
            waiting.decision, self.mode, Placed(ask, quantity, at=sent_at), sent_at, SENT, would
        )

        def accepted(sent: Sent) -> None:  # write-ahead: the order kept before its fills are read
            assert sent.ref is not None
            self.store.accept_buy(unfilled, sent.ref.branch, sent.ref.number)

        try:
            taken = await self._broker.take(
                SYMBOL,
                SIDE_BUY,
                quantity,
                ask,
                day,
                wait=self._timing.wait,
                poll=self._timing.poll,
                on_accepted=accepted,
            )
        except BrokerError as e:  # no token: nothing was sent
            self.store.close_unfilled_buy(unfilled, UNSENT)
            self._buy_failed(waiting, "buy", ERROR, str(e))
            return
        sent = taken.sent
        if sent.outcome == REFUSED:
            self.store.close_unfilled_buy(unfilled, BUY_REFUSED)
            self._buy_failed(waiting, "buy", REFUSED, sent.reason, sent.code)
            return
        if sent.ref is None:  # no answer: whether it went in, KIS's inquiry tells (#406)
            self._buy_failed(waiting, "buy", UNKNOWN, _unknown(sent))
            await self._look_up(unfilled, waiting.decision, Placed(ask, quantity, at=sent_at), day, would)
            return
        at = self._clock()
        placed = Placed(ask, quantity, sent.ref.branch, sent.ref.number, sent_at)
        # Open on KIS still: its cancel did not take, or the inquiry never showed it. It is followed
        # until nothing is left of it (``_settle_buy``), and no take-profit rests before.
        live = taken.unsettled
        if live:
            if taken.left is None:
                self._failed("buy", UNKNOWN, "주문이 받아들여졌으나 주문 체결 조회에 보이지 않는다")
            else:
                self._cancel_failed(taken.cancel, None)
            self._retry_at = at + timedelta(seconds=self._timing.retry)
        if taken.filled < 1:  # kept all the same: the ledger holds every order KIS has (``mock_check``)
            if live:
                self._live_buy = _LiveBuy(placed, day, 0, 0.0, waiting.decision, would, unfilled)
                return
            self.store.close_unfilled_buy(unfilled, CANCELLED)
            self.store.unfilled(waiting.decision, UNFILLED)
            log.info("trader: no fill of the buy at %s on KIS before its cancel", ask)
            return
        entry = self._entry(waiting, at, taken.average, taken.filled, quote.received_at)
        trade = self.store.open_position(waiting.decision, entry, placed, OPEN if live else None)
        self.store.close_unfilled_buy(unfilled, FILLED)  # its trade holds the order now
        self._open = trade
        self._dirty = True
        if live:
            self._live_buy = _LiveBuy(placed, day, taken.filled, taken.average * taken.filled)
        log.info(
            "trader: bought %s of %s at %s on KIS, %.1f on average (trade %s)",
            taken.filled,
            quantity,
            ask,
            taken.average,
            trade.id,
        )
        if not live:
            await self._place_take_profit(trade)

    async def _settle_buy(self) -> None:
        """The buy open on KIS taken off: cancelled again and read until nothing is left of it, its
        fills joined to its trade (or a trade opened by them); then the take-profit placed. Tried
        again ``Timing.retry`` later while it does not come off."""
        if not await self._take_off_buy():
            return
        trade = self._open
        if trade is not None:
            await self._place_take_profit(trade)

    async def _take_off_buy(self) -> bool:
        """``_live_buy`` cancelled and read until nothing is left of it, its fills kept; whether it
        came off (else it is tried again later, a failed order)."""
        live = self._live_buy
        assert live is not None
        assert live.placed.branch is not None and live.placed.number is not None
        cancel, row = await self._withdraw(live.day, OrderRef(live.placed.branch, live.placed.number))
        if row is not None:
            self._absorb(row.filled, row.average, row.left)
        if row is None or row.left > 0:
            if isinstance(cancel, str):
                self._failed("cancel", ERROR, cancel, trade=None if self._open is None else self._open.id)
            else:
                self._cancel_failed(cancel, None if self._open is None else self._open.id)
            self._retry_at = self._clock() + timedelta(seconds=self._timing.retry)
            return False
        return True

    def _absorb(self, filled: int, average: float, left: int) -> None:
        """The live buy's fills its row shows (``filled`` at ``average``, ``left`` still open) and not
        kept yet: joined to its trade, or a trade opened by them; once nothing is left of it, it
        settles."""
        live = self._live_buy
        assert live is not None
        at = self._clock()
        if filled > live.filled:
            amount = average * filled
            if self._open is None:
                assert live.decision is not None and live.entry is not None and live.unfilled is not None
                # Its fills read now; the order placed when it was sent (``Placed.at``, #406).
                entry = replace(
                    live.entry,
                    entry_at=at,
                    entry_price=average,
                    quantity=filled,
                    buy_fee=amount * self.rules.fee_rate,
                )
                status = OPEN if left else None
                self._open = self.store.open_position(live.decision, entry, live.placed, status)
                self.store.close_unfilled_buy(live.unfilled, FILLED)
                log.info("trader: the buy at %s filled %s on KIS, read later", live.placed.price, filled)
            else:
                trade = self._open
                fee = amount * self.rules.fee_rate
                new = filled - live.filled
                fill = (at, (amount - live.amount) / new, new)
                self.store.grow_position(trade.id, filled, average, fee, fill)
                self._open = replace(trade, quantity=filled, entry_price=average, buy_fee=fee)
                log.info("trader: %s more of the buy filled on KIS (trade %s)", new, trade.id)
            live = self._live_buy = replace(
                live, filled=filled, amount=amount, decision=None, entry=None, unfilled=None
            )
            self._dirty = True
        if left > 0:
            return
        if self._open is None:  # nothing filled: no trade
            assert live.decision is not None and live.unfilled is not None
            self.store.close_unfilled_buy(live.unfilled, CANCELLED)
            self.store.unfilled(live.decision, UNFILLED)
        else:
            self.store.close_entry(self._open.id, FILLED if filled >= live.placed.quantity else CANCELLED, at)
        self._live_buy = None

    def _buy_failed(
        self, waiting: _Waiting, what: str, outcome: str, detail: str, code: str | None = None
    ) -> None:
        self.store.unfilled(waiting.decision, ORDER_FAILED)
        self._failed(what, outcome, detail, code)

    # The take-profit.

    async def _place_take_profit(self, trade: OpenTrade) -> None:
        if self._selling is not None or self.status in SALE:
            return  # it sells at once instead
        price = tick_up(trade.target)
        left = trade.quantity - self.store.sold(trade.id)  # placed again after a restart (#406): the rest
        if left <= 0:
            return
        try:
            sent = await self._broker.sell(SYMBOL, left, price)
        except BrokerError as e:
            self._failed("take_profit", ERROR, str(e), trade=trade.id)
            return
        if not sent.accepted or sent.ref is None:
            refused = sent.outcome == REFUSED
            self._failed(
                "take_profit",
                REFUSED if refused else UNKNOWN,
                sent.reason if refused else _unknown(sent),
                sent.code if refused else None,
                trade.id,
            )
            return
        placed = Placed(price, left, sent.ref.branch, sent.ref.number, self._clock())
        order = self.store.add_order(trade.id, SELL, TAKE_PROFIT, placed, self._clock())
        self._open = replace(trade, take_profit_order=order)
        day = kst_day(self._clock())
        self._resting = Resting(
            order, price, left, sent.ref.branch, sent.ref.number, 0, 0.0, TAKE_PROFIT, day
        )
        log.info("trader: take-profit sell of %s resting at %s on KIS (trade %s)", left, price, trade.id)

    async def _follow_resting(self, trade_id: int) -> None:
        """The resting take-profit sell's row, read every ``Timing.follow`` seconds: its fills kept,
        the trade exited once it has filled whole. Cancelled when a sale begins."""
        while True:
            await asyncio.sleep(self._timing.follow)
            resting, trade = self._resting, self._open
            if resting is None or trade is None or trade.id != trade_id:
                return
            try:
                row = await self._broker.order(
                    resting.day or self._day_of(trade), SYMBOL, OrderRef(resting.branch, resting.number)
                )
            except BrokerError as e:
                log.warning("trader: could not read the take-profit sell (%s); read again", e)
                continue
            if row is None or self._resting is not resting or self._work is not None:
                continue
            resting = self._keep(resting, row)
            if row.left > 0:
                continue
            self._close_sell(resting)
            if self.store.sold(trade.id) >= trade.quantity:
                self._finish(trade, TAKE_PROFIT)
            else:  # cancelled on KIS, not by the trader: the stop and the time barrier sell the rest
                log.warning(
                    "trader: the take-profit sell of trade %s ended on KIS with %s of %s filled",
                    trade.id,
                    row.filled,
                    resting.quantity,
                )
            return

    def _keep(self, resting: Resting, row: OrderRow) -> Resting:
        """The take-profit sell's fills shown by ``row`` and not kept yet, kept (at their average)."""
        if row.filled <= resting.filled:
            return resting
        amount = row.average * row.filled
        price = (amount - resting.amount) / (row.filled - resting.filled)
        self.store.add_fill(resting.order_id, self._clock(), price, row.filled - resting.filled)
        kept = replace(resting, filled=row.filled, amount=amount)
        if self._resting is resting:
            self._resting = kept
        if self._live is resting:
            self._live = kept
        return kept

    # The sale.

    async def _sell(self, trade: OpenTrade, reason: str) -> None:
        # First a buy still open on KIS (its cancel did not take): taken off, its fills joined to the
        # trade, so that every share bought is sold.
        if self._live_buy is not None:
            if not await self._take_off_buy():
                return
            assert self._open is not None
            trade = self._open
        # Then the sells open on KIS: the take-profit's, and a sale's whose cancel did not take
        # before. Each is cancelled and its fills kept until nothing is left of it, so that what is
        # sold next is what is left (a fill can come before the cancel lands).
        for still in (self._resting, self._live):
            if still is None:
                continue
            row = await self._take_off(trade, still)
            if row is None:
                return
            # Filled before its cancel, and every share sold by it.
            if still.purpose == TAKE_PROFIT and row.filled >= still.quantity and self._all_sold(trade):
                self._finish(trade, TAKE_PROFIT)
                return
        if self.store.sold(trade.id) >= trade.quantity:
            self._finish(trade, reason)
            return
        for _ in range(self._timing.rounds):
            left = trade.quantity - self.store.sold(trade.id)
            bid = self._bid()
            if left <= 0 or bid is None:
                break

            def accepted(sent: Sent, trade: OpenTrade = trade, bid: int = bid, left: int = left) -> None:
                # Write-ahead: the sell kept open the moment KIS takes it, before its fills are read,
                # so that a stop meanwhile leaves it to be taken off and reconciled.
                assert sent.ref is not None
                placed = Placed(bid, left, sent.ref.branch, sent.ref.number)
                order = self.store.add_order(trade.id, SELL, reason, placed, self._clock(), OPEN)
                day = kst_day(self._clock())  # the day its row is on: a sale may come after the entry's
                self._live = Resting(order, bid, left, sent.ref.branch, sent.ref.number, 0, 0.0, reason, day)

            try:
                taken = await self._broker.take(
                    SYMBOL,
                    SIDE_SELL,
                    left,
                    bid,
                    kst_day(self._clock()),
                    wait=self._timing.wait,
                    poll=self._timing.poll,
                    on_accepted=accepted,
                )
            except BrokerError as e:
                self._sale_failed(trade, "sell", ERROR, str(e))
                return
            sent, at = taken.sent, self._clock()
            if sent.accepted and sent.ref is not None:
                live = self._live
                assert live is not None
                if taken.filled:
                    self.store.add_fill(live.order_id, at, taken.average, taken.filled)
                if taken.unsettled:  # open still: followed, and taken off before anything more is sold
                    self._live = replace(live, filled=taken.filled, amount=taken.average * taken.filled)
                else:
                    self.store.close_order(live.order_id, FILLED if taken.filled >= left else CANCELLED, at)
                    self._live = None
                log.info(
                    "trader: sold %s of %s at %s on KIS (%s, trade %s)",
                    taken.filled,
                    left,
                    bid,
                    reason,
                    trade.id,
                )
            if sent.outcome == REFUSED:
                self._sale_failed(trade, "sell", REFUSED, sent.reason, sent.code)
                return
            if taken.left is None or sent.ref is None:
                self._sale_failed(trade, "sell", UNKNOWN, _unknown(sent))
                return
            if taken.left > 0:  # its cancel did not take
                self._cancel_failed(taken.cancel, trade.id)
                self._retry_at = self._clock() + timedelta(seconds=self._timing.retry)
                return
        if trade.quantity - self.store.sold(trade.id) <= 0:
            self._finish(trade, reason)
        else:  # rounds spent with shares left (no bid that sells them): the next batch waits a while
            self._retry_at = self._clock() + timedelta(seconds=self._timing.wait)
            log.info(
                "trader: trade %s: %s share(s) left after %s sell(s) at the bid; again at a later quote",
                trade.id,
                trade.quantity - self.store.sold(trade.id),
                self._timing.rounds,
            )

    async def _take_off(self, trade: OpenTrade, resting: Resting) -> OrderRow | None:
        """A sell open on KIS (the resting take-profit's, or a sale's whose cancel did not take)
        cancelled, and its row read until nothing is left of it (a fill can come before the cancel
        lands: KIS then finds less or nothing to cancel). Its row, its fills kept; None when it could
        not be taken off (a failed order, tried again later, the order followed still)."""
        day = resting.day or self._day_of(trade)  # the day it was placed on
        cancel, row = await self._withdraw(day, OrderRef(resting.branch, resting.number))
        if row is not None:
            resting = self._keep(resting, row)
        if row is None or row.left > 0:
            if isinstance(cancel, str):
                self._sale_failed(trade, "cancel", ERROR, cancel)
            else:
                self._cancel_failed(cancel, trade.id)
                self._retry_at = self._clock() + timedelta(seconds=self._timing.retry)
            return None
        self._close_sell(resting)
        return row

    def _close_sell(self, resting: Resting) -> None:
        """A sell with nothing left of it on KIS: closed in the ledger, filled whole or cancelled for
        the rest (by the trader, on KIS, or with its day)."""
        filled = resting.filled >= resting.quantity
        self.store.close_order(resting.order_id, FILLED if filled else CANCELLED, self._clock())
        if resting.purpose == TAKE_PROFIT:
            self._resting = None
            if self._open is not None:
                self._open = replace(self._open, take_profit_order=None)
        else:
            self._live = None

    def _all_sold(self, trade: OpenTrade) -> bool:
        return self.store.sold(trade.id) >= trade.quantity

    async def _withdraw(self, day: date, ref: OrderRef) -> tuple[Sent | str, OrderRow | None]:
        """Order ``ref`` of ``day`` cancelled, and its row read until nothing is left of it or
        ``Timing.wait`` has passed (a fill can come before the cancel lands: KIS then finds less or
        nothing to cancel). The cancel's outcome (why, when it could not be sent) and the last row."""
        try:
            cancel: Sent | str = await self._broker.cancel(ref)
        except BrokerError as e:
            return str(e), None
        deadline = asyncio.get_running_loop().time() + self._timing.wait
        row: OrderRow | None = None
        while True:
            try:
                row = await self._broker.order(day, SYMBOL, ref) or row
            except BrokerError as e:
                log.warning("trader: could not read an order after its cancel (%s)", e)
            if (row is not None and row.left == 0) or asyncio.get_running_loop().time() >= deadline:
                return cancel, row
            await asyncio.sleep(self._timing.poll)

    def _cancel_failed(self, cancel: Sent | None, trade: int | None) -> None:
        if cancel is None:  # it could not be sent (no token): the order is open still
            self._failed("cancel", ERROR, "취소를 보내지 못했다(접근토큰 없음)", trade=trade)
        elif cancel.outcome == REFUSED:
            self._failed("cancel", REFUSED, cancel.reason, cancel.code, trade)
        elif not cancel.accepted:
            self._failed("cancel", UNKNOWN, _unknown(cancel), trade=trade)
        else:  # accepted, but the order still shows something left
            self._failed("cancel", UNKNOWN, "취소가 받아들여졌으나 주문에 남은 수량이 있다", trade=trade)

    def _sale_failed(
        self, trade: OpenTrade, what: str, outcome: str, detail: str, code: str | None = None
    ) -> None:
        self._failed(what, outcome, detail, code, trade.id)
        self._retry_at = self._clock() + timedelta(seconds=self._timing.retry)

    def _finish(self, trade: OpenTrade, reason: str) -> None:
        """Every share of ``trade`` sold: its exit kept from its sells' fills."""
        at = self._clock()
        if self.holds.roll(at):
            self._dirty = True
        net = self.store.exit_on_fills(trade, at, reason, self.rules.fee_rate)
        self._open = None
        self._sell_after = None
        self._selling = None
        self._resting = None
        self._live = None
        self._live_buy = None
        self._retry_at = None
        self._stop_following()
        self._dirty = True
        log.info("trader: trade %s sold on KIS (%s): net %.0f won", trade.id, reason, net)
        if self.holds.exited(net, at) is not None:  # its realized loss reached a limit
            self._enforce(at)

    def _failed(
        self, what: str, outcome: str, detail: str, code: str | None = None, trade: int | None = None
    ) -> None:
        """A KIS order that failed (module doc): kept and told, and no new entry today."""
        at = self._clock()
        self.guard.order_failed(at, what, outcome, detail, code, trade)
        self._dirty = True
        self._enforce(at)

    @staticmethod
    def _day_of(trade: OpenTrade) -> date:
        """The day of ``trade``'s orders on KIS: the day it was entered."""
        assert trade.entry_at is not None
        return kst_day(trade.entry_at)
