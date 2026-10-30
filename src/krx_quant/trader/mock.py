"""KIS mock trading (#405, #393 "KIS 주문"): with ``KRX_TRADER_MODE=mock`` the champion's book places
its orders on KIS's mock server through the trader's broker (``kis_orders.Broker``, #404), by the
same decisions, limits, pauses and ledger as paper (``paper.PaperTrader``): only how an order fills
changes. The challengers stay in paper beside it (``desk.py``), on the same quotes.

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

**A failed order**: KIS refused it, it had no answer (whether it went in is not known: the
account's reconciliation is #406's), or it could not be sent (no token, an inquiry KIS did not
answer). It is kept (``failures``) and sent to Telegram, and new entries stop for the rest of the day
(``limits.ORDER_FAILED``); a sale that failed is tried again ``Timing.retry`` seconds later.

One order is in flight at a time (``_work``): while it is, nothing else is bought or sold, and a
sale asked for meanwhile begins once it ends. A trader stopped waits for it (``close``).
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable, Coroutine
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
)
from krx_quant.market_calendar import Session
from krx_quant.pricing import SELL_TAX, tick_size
from krx_quant.trader import MOCK, SYMBOL
from krx_quant.trader.limits import ORDER_FAILED, SALE, Notify
from krx_quant.trader.paper import NO_SHARES, TAKE_PROFIT, PaperTrader, Rules, _Waiting
from krx_quant.trader.quotes import BestQuote
from krx_quant.trader.store import CANCELLED, FILLED, OPEN, SELL, OpenTrade, Placed, Resting, TraderStore

log = logging.getLogger(__name__)

# Why a buy decided bought nothing on KIS: nothing filled before its cancel (the ask moved).
UNFILLED = "unfilled"
# What became of a failed order (``failures.outcome``): KIS refused it, it had no answer, or it
# could not be sent.
UNKNOWN, ERROR = "unknown", "error"
# A trader stopping waits this long for the order in flight (compose gives it 10 s).
CLOSE_SECONDS = 8.0


@dataclass(frozen=True)
class Timing:
    """The mock book's waits, in seconds."""

    wait: float = TAKE_WAIT  # a marketable order's fills awaited before what is left is cancelled
    poll: float = POLL  # the inquiry read this often while it waits
    follow: float = 2.0  # the take-profit sell's row read this often while it rests
    retry: float = 30.0  # a sale whose order failed is tried again this long after
    rounds: int = 5  # sells at the best bid in one go, before the next quote or tick


def tick_up(price: float) -> int:
    """The first price on KRX's tick at or above ``price``."""
    up = math.ceil(round(price / tick_size(price), 9)) * tick_size(price)
    while up % tick_size(up):  # past a band's edge, onto the coarser tick of the band above
        up = math.ceil(up / tick_size(up)) * tick_size(up)
    return int(up)


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
        self._closing = False
        super().__init__(store, rules, sessions, started_at, MOCK, notify)
        if self._open is not None:  # a restart: its sells open on KIS when it stopped go on
            for order in store.open_sells(self._open.id):
                if order.purpose == TAKE_PROFIT:
                    self._resting = order
                else:  # a sale was under way: it goes on, that order taken off first
                    self._live, self._selling = order, order.purpose

    # The paper book's hooks.

    def _busy(self) -> bool:
        return super()._busy() or self._work is not None

    def _fill_buy(self, waiting: _Waiting, quote: BestQuote, quantity: int) -> None:
        self._start(self._enter(waiting, quote, quantity))

    def _exit(self, trade: OpenTrade, at: datetime, price: float, reason: str, quote: BestQuote) -> None:
        if reason == TAKE_PROFIT and (self._resting is not None or self._work is not None):
            return  # the resting sell takes it (its fills are read as it rests), or is being placed
        if self._selling is None:
            self._selling = reason
            log.info("trader: selling trade %s on KIS (%s)", trade.id, reason)
        self._kick(self._clock())

    def on_quote(self, quote: BestQuote) -> None:
        super().on_quote(quote)
        self._kick(self._clock())

    def on_time(self, now: datetime) -> None:
        super().on_time(now)
        self._kick(now)

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
        """Until no order is in flight (one ending may begin the next: a sale asked for meanwhile)."""
        while (work := self._work) is not None:
            await asyncio.wait({work})

    async def close(self, timeout: float = CLOSE_SECONDS) -> None:
        """The trader stopping: the take-profit sell's reading stops (it rests on KIS), and the order
        in flight is waited for ``timeout`` seconds at most, then cancelled."""
        self._closing = True
        follower, work = self._follower, self._work
        if follower is not None:
            follower.cancel()
            await asyncio.gather(follower, return_exceptions=True)
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
            self._failed("sell" if trade is not None else "buy", ERROR, f"{type(e).__name__}: {e}")
            if trade is not None:
                self._retry_at = self._clock() + timedelta(seconds=self._timing.retry)
        finally:
            self._work = None
            self._dirty = True
        self._kick(self._clock())

    def _kick(self, now: datetime) -> None:
        """What the open trade needs next, when no order is in flight: its sale begun (or tried again
        once ``_retry_at`` is past), or its resting take-profit sell's row followed."""
        trade = self._open
        if self._closing or self._work is not None or trade is None:
            return
        status = self.status
        if self._selling is None and status in SALE:  # a pause or a limit while it was bought
            self._selling = SALE[status]
        if self._selling is not None:
            if (self._retry_at is None or now >= self._retry_at) and self._bid() is not None:
                self._retry_at = None
                self._stop_following()
                self._start(self._sell(trade, self._selling))
            return
        if self._resting is not None and (self._follower is None or self._follower.done()):
            self._follower = asyncio.get_running_loop().create_task(self._follow_resting(trade.id))

    def _stop_following(self) -> None:
        follower = self._follower
        if follower is not None and not follower.done() and follower is not asyncio.current_task():
            follower.cancel()
        self._follower = None

    def _bid(self) -> int | None:
        """The best bid a sell is placed at: the latest quote's, received since the trader started."""
        last = self._last
        if last is None or last.received_at < self._started_at:
            return None
        return round(last.bid)

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
        try:
            taken = await self._broker.take(
                SYMBOL, SIDE_BUY, quantity, ask, day, wait=self._timing.wait, poll=self._timing.poll
            )
        except BrokerError as e:  # no token: nothing was sent
            self._buy_failed(waiting, "buy", ERROR, str(e))
            return
        sent = taken.sent
        if sent.outcome == REFUSED:
            self._buy_failed(waiting, "buy", REFUSED, sent.reason, sent.code)
            return
        if taken.left is None or sent.ref is None:
            self._buy_failed(waiting, "buy", UNKNOWN, _unknown(sent))
            return
        at = self._clock()
        if taken.left > 0:  # its cancel did not take: what is left of it may fill still
            self._cancel_failed(taken.cancel, None)
        if taken.filled < 1:  # kept all the same: the ledger holds every order KIS has (``mock_check``)
            placed = Placed(ask, quantity, sent.ref.branch, sent.ref.number)
            self.store.add_unfilled_buy(waiting.decision, MOCK, placed, at, OPEN if taken.left else CANCELLED)
            self.store.unfilled(waiting.decision, UNFILLED)
            log.info("trader: no fill of the buy at %s on KIS before its cancel", ask)
            return
        entry = self._entry(waiting, at, taken.average, taken.filled, quote.received_at)
        trade = self.store.open_position(
            waiting.decision, entry, Placed(ask, quantity, sent.ref.branch, sent.ref.number)
        )
        self._open = trade
        self._dirty = True
        log.info(
            "trader: bought %s of %s at %s on KIS, %.1f on average (trade %s)",
            taken.filled,
            quantity,
            ask,
            taken.average,
            trade.id,
        )
        await self._place_take_profit(trade)

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
        try:
            sent = await self._broker.sell(SYMBOL, trade.quantity, price)
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
        placed = Placed(price, trade.quantity, sent.ref.branch, sent.ref.number)
        order = self.store.add_order(trade.id, SELL, TAKE_PROFIT, placed, self._clock())
        self._open = replace(trade, take_profit_order=order)
        self._resting = Resting(order, price, trade.quantity, sent.ref.branch, sent.ref.number, 0, 0.0)
        log.info(
            "trader: take-profit sell of %s resting at %s on KIS (trade %s)", trade.quantity, price, trade.id
        )

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
                    self._day_of(trade), SYMBOL, OrderRef(resting.branch, resting.number)
                )
            except BrokerError as e:
                log.warning("trader: could not read the take-profit sell (%s); read again", e)
                continue
            if row is None or self._resting is not resting or self._work is not None:
                continue
            self._keep(resting, row)
            if row.left > 0:
                continue
            now = self._clock()
            if row.filled >= trade.quantity:
                self.store.close_order(resting.order_id, FILLED, now)
                self._resting = None
                self._finish(trade, TAKE_PROFIT)
            else:  # cancelled on KIS, not by the trader: the stop and the time barrier sell the rest
                log.warning(
                    "trader: the take-profit sell of trade %s ended on KIS with %s of %s filled",
                    trade.id,
                    row.filled,
                    trade.quantity,
                )
                self.store.close_order(resting.order_id, CANCELLED, now)
                self._resting = None
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
        # First the sells open on KIS: the take-profit's, and a sale's whose cancel did not take
        # before. Each is cancelled and its fills kept until nothing is left of it, so that what is
        # sold next is what is left (a fill can come before the cancel lands).
        for still in (self._resting, self._live):
            if still is None:
                continue
            row = await self._take_off(trade, still)
            if row is None:
                return
            if still.purpose == TAKE_PROFIT and row.filled >= trade.quantity:  # filled before its cancel
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
            try:
                taken = await self._broker.take(
                    SYMBOL,
                    SIDE_SELL,
                    left,
                    bid,
                    kst_day(self._clock()),
                    wait=self._timing.wait,
                    poll=self._timing.poll,
                )
            except BrokerError as e:
                self._sale_failed(trade, "sell", ERROR, str(e))
                return
            sent, at = taken.sent, self._clock()
            if sent.accepted and sent.ref is not None:
                # Open still when its cancel did not take (or it was never seen on the inquiry): it is
                # followed, and taken off before anything more is sold.
                live = taken.left is None or taken.left > 0
                status = FILLED if taken.filled >= left else OPEN if live else CANCELLED
                placed = Placed(bid, left, sent.ref.branch, sent.ref.number)
                order = self.store.add_order(trade.id, SELL, reason, placed, at, status)
                if taken.filled:
                    self.store.add_fill(order, at, taken.average, taken.filled)
                if status == OPEN:
                    amount = taken.average * taken.filled
                    self._live = Resting(
                        order, bid, left, sent.ref.branch, sent.ref.number, taken.filled, amount, reason
                    )
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

    async def _take_off(self, trade: OpenTrade, resting: Resting) -> OrderRow | None:
        """A sell open on KIS (the resting take-profit's, or a sale's whose cancel did not take)
        cancelled, and its row read until nothing is left of it (a fill can come before the cancel
        lands: KIS then finds less or nothing to cancel). Its row, its fills kept; None when it could
        not be taken off (a failed order, tried again later, the order followed still)."""
        ref = OrderRef(resting.branch, resting.number)
        try:
            cancel = await self._broker.cancel(ref)
        except BrokerError as e:
            self._sale_failed(trade, "cancel", ERROR, str(e))
            return None
        day = self._day_of(trade)
        deadline = asyncio.get_running_loop().time() + self._timing.wait
        row: OrderRow | None = None
        while True:
            try:
                row = await self._broker.order(day, SYMBOL, ref) or row
            except BrokerError as e:
                log.warning("trader: could not read the take-profit sell after its cancel (%s)", e)
            if (row is not None and row.left == 0) or asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(self._timing.poll)
        if row is not None:
            resting = self._keep(resting, row)
        if row is None or row.left > 0:
            self._cancel_failed(cancel, trade.id)
            self._retry_at = self._clock() + timedelta(seconds=self._timing.retry)
            return None
        filled = row.filled >= resting.quantity
        self.store.close_order(resting.order_id, FILLED if filled else CANCELLED, self._clock())
        if resting.purpose == TAKE_PROFIT:
            self._resting = None
            self._open = replace(trade, take_profit_order=None)
        else:
            self._live = None
        return row

    def _cancel_failed(self, cancel: Sent | None, trade: int | None) -> None:
        if cancel is None:
            self._failed("cancel", UNKNOWN, "취소를 보내지 않았다", trade=trade)
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
