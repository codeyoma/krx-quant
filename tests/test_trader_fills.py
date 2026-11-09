"""The seam between a live book and its fills (#652, ``paper.Fills``): the book decides, sizes and marks;
its fills adapter fills the buy, sells the position and may hold the book busy. The paper fills
(``QuoteFills``) fill whole at once at the quote; the KIS book's (``mock.KisFills``) order on KIS
(``tests/test_trader_mock.py``)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from krx_quant.trader.book import HOLDING, TAKE_PROFIT, Busy
from krx_quant.trader.mock import RECONCILING
from krx_quant.trader.money import net_of
from krx_quant.trader.paper import QuoteFills, _Waiting
from krx_quant.trader.quotes import BestQuote
from krx_quant.trader.store import OpenTrade
from tests.trader_doubles import FEE, Barrier, Day, Read, at, quote

TEN = Read("10:00:02.3", [Barrier("10:00:00")])
BOUGHT = quote("10:00:02.5", 70_000, 70_100)
TAKEN = quote("10:02:00", 70_300, 70_400)


class Told(QuoteFills):
    """The paper fills, each call the book makes kept; ``held`` added to what the book holds."""

    def __init__(self, held: Busy = None) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.held = held

    def busy(self, held: Busy) -> Busy:
        return self.held or held

    def buy(self, waiting: _Waiting, quote: BestQuote, quantity: int) -> None:
        self.calls.append(("buy", waiting.signal.bar_close, quote.received_at, quantity))
        super().buy(waiting, quote, quantity)

    def sell(self, trade: OpenTrade, at: datetime, price: float, reason: str, quote: BestQuote) -> None:
        self.calls.append(("sell", trade.id, at, price, reason))
        super().sell(trade, at, price, reason, quote)


def test_the_book_hands_its_buy_and_its_sale_to_its_fills(tmp_path: Path) -> None:
    """The rise of 10:00 decided, the first quote after it fills the buy of the size the book chose
    (``whole_rules``: the capital's 14 shares); the take-profit's bid sells it at the target. The paper
    fills keep the trade and its net, the round trip's."""
    fills = Told()

    [trade] = Day(tmp_path, fills=fills).run([TEN, BOUGHT, TAKEN])

    assert fills.calls == [
        ("buy", at("10:00:00"), at("10:00:02.5"), 14),
        ("sell", trade["id"], at("10:02:00"), 70_250, TAKE_PROFIT),
    ]
    assert (trade["entry"]["price"], trade["exit"]["price"]) == (70_100, 70_250)
    assert trade["net"] == net_of(70_100, 70_250, 14, FEE)


def test_what_the_fills_hold_the_book_busy_with_is_why_it_buys_nothing(tmp_path: Path) -> None:
    """Fills that are not ready (the KIS book before its reconcile, #406) hold every rise: the book
    skips it with their reason and hands them nothing."""
    for held in (RECONCILING, HOLDING):
        fills = Told(held)
        day = Day(tmp_path / held, fills=fills)

        assert day.run([TEN, BOUGHT, TAKEN]) == []
        assert fills.calls == []
        assert [(d["action"], d["reason"]) for d in day.decisions()] == [("skip", held)]
