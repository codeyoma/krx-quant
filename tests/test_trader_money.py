"""A round trip's money (#652, ``trader/money.py``): one expression for a trade's net, of one trade and of
a day's quotes at once, which the paper book, the KIS book, the store and the replay all ask."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pytest

from krx_quant.feeds.base import KST
from krx_quant.pricing import cost_ticks
from krx_quant.trader.money import commission, net_of, open_net, round_trip, settled
from krx_quant.trader.store import OpenTrade
from tests.trader_doubles import FEE


def trade(entry: float, quantity: int) -> OpenTrade:
    at = datetime(2026, 10, 12, 10, tzinfo=KST)
    return OpenTrade(
        1, None, entry, quantity, commission(entry, quantity, FEE), entry + 150, entry - 250, at, "k", "p", at
    )


def test_the_costs_of_a_round_trip_worked_by_hand() -> None:
    """14 shares bought at 70,100 and sold at 70,250, 0.015% a side and the 0.20% sell tax:
    the buy's commission 981,400 × 0.00015 = 147.21; the sell's 983,500 × 0.00015 = 147.525; the
    tax 983,500 × 0.002 = 1,967. 2,261.735 won in all, against 150 × 14 = 2,100 gained: the
    target of k = 2 ticks, reached from the ask half a tick above the reference, loses 161.735."""
    trip = round_trip(70_100, 14, commission(70_100, 14, FEE), 70_250 * 14, FEE)

    assert (trip.buy_fee, trip.sell_fee, trip.tax) == pytest.approx((147.21, 147.525, 1_967.0))
    assert trip.cost == pytest.approx(2_261.735)
    assert trip.gross == 2_100
    assert trip.net == pytest.approx(-161.735)
    assert net_of(70_100, 70_250, 14, FEE) == trip.net
    # The same round trip on the reference price per share, in ticks (``pricing.cost_ticks``, what
    # the touch and barrier P&L take off): 70,050 × 0.23% ÷ 100.
    assert cost_ticks(70_050, FEE) == pytest.approx(1.61115)


def test_a_trade_kept_nets_what_its_round_trip_nets() -> None:
    """The store keeps a sale's commission and tax as the book paid them (``settled``): the same net."""
    trip = round_trip(70_100, 9, commission(70_100, 9, FEE), 69_800 * 9, FEE)
    kept = settled(70_100, 9, trip.buy_fee, 69_800 * 9, trip.sell_fee, trip.tax)

    assert kept == trip
    assert kept.net == net_of(70_100, 69_800, 9, FEE)


@pytest.mark.parametrize("sold", [0, 4])
def test_an_open_position_nets_its_round_trip_at_the_bid(sold: int) -> None:
    """Open at a bid: the shares sold already at what they brought (the KIS book's take-profit filled in
    part), the rest at the bid."""
    held = trade(70_100, 9)
    amount = 70_250.0 * sold

    found = open_net(held, 70_000, FEE, sold, amount)

    assert found == round_trip(70_100, 9, held.buy_fee, amount + 70_000 * (9 - sold), FEE).net
    if not sold:
        assert found == net_of(70_100, 70_000, 9, FEE)


def test_the_array_form_is_the_scalar_one_on_each_quote() -> None:
    """The replay marks a held position on each quote of its window at once (``replay.Policy._limited``):
    each element is exactly what the paper book marks on that quote alone (``open_net``)."""
    held = trade(70_100, 9)
    bids = np.array([70_000.0, 70_050.0, 69_850.0, 70_200.0, 69_700.0])

    nets = round_trip(70_100, 9, held.buy_fee, bids * 9, FEE).net

    assert list(nets) == [open_net(held, float(b), FEE) for b in bids]
