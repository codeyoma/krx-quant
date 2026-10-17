"""Entry delay (#262): a call acted on seconds after its bar close, on quotes worked out by hand.

Input: a bar close at 81,250 (k = 2: targets 81,450 and 81,050, bought at the ask 81,300 or sold
at the bid 81,200 then; the cost 1.86875 ticks at the default fee) and the quotes received
after it. Output: the start quote and the targets reached as the delayed call sees them, the
touch P&L, and a predictor's tally.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pytest

from krx_quant.entry_delay import DELAYS, Quotes, Tally, delayed
from krx_quant.pricing import Direction
from krx_quant.stats import Prices, Scored, touch_profit
from tests.replay_events import FEE, at

CLOSE = at("09:01:00")
COST = 81_250 * (0.002 + 2 * FEE) / 100  # ticks
PRICES = Prices(
    reference=81_250,
    tick=100,
    upper=81_450,
    lower=81_050,
    upper_reached=True,
    lower_reached=False,
    bid_start=81_200,
    ask_start=81_300,
    bid_end=81_300,
    ask_end=81_400,
)


def quotes(*rows: tuple[datetime, float, float]) -> Quotes:
    return Quotes(
        np.array([int(t.timestamp() * 1_000_000) for t, *_ in rows], dtype=np.int64),
        np.array([bid for _, bid, _ in rows], dtype=np.float64),
        np.array([ask for *_, ask in rows], dtype=np.float64),
    )


# Half a second in, a tick up; at 3 s the bid on the upper target for 7 s; back at 10 s; down a
# tick at 2 minutes.
AFTER = quotes(
    (CLOSE - timedelta(seconds=5), 81_200, 81_300),
    (CLOSE + timedelta(seconds=0.5), 81_300, 81_400),
    (CLOSE + timedelta(seconds=3), 81_450, 81_500),
    (CLOSE + timedelta(seconds=10), 81_300, 81_400),
    (CLOSE + timedelta(minutes=2), 81_200, 81_300),
    (CLOSE + timedelta(minutes=4), 81_500, 81_600),  # the next window's: not this one's
)


def test_at_the_bar_close_the_stored_path_as_it_is() -> None:
    assert delayed(PRICES, CLOSE, AFTER, 0) is PRICES
    assert delayed(PRICES, CLOSE, None, 0) is PRICES


def test_a_second_late_it_buys_the_ask_then_and_still_reaches_the_target() -> None:
    late = delayed(PRICES, CLOSE, AFTER, 1)
    assert late is not None
    assert (late.bid_start, late.ask_start) == (81_300, 81_400)  # the quote received at 0.5 s
    assert late.upper_reached and not late.lower_reached
    assert touch_profit(Direction.UP, late, FEE) == pytest.approx((81_450 - 81_400) / 100 - COST)


def test_while_the_bid_stands_on_the_target_its_quote_counts() -> None:
    # 5 s late: the quote standing is the one at 3 s, its bid on the target; bought at its ask.
    late = delayed(PRICES, CLOSE, AFTER, 5)
    assert late is not None
    assert late.ask_start == 81_500 and late.upper_reached
    assert touch_profit(Direction.UP, late, FEE) == pytest.approx((81_450 - 81_500) / 100 - COST)


def test_half_a_minute_late_the_target_is_gone() -> None:
    late = delayed(PRICES, CLOSE, AFTER, 30)
    assert late is not None
    assert late.ask_start == 81_400 and not late.upper_reached
    # Sold at the end's bid: the window's last quote before 09:05 is unchanged by the delay.
    assert touch_profit(Direction.UP, late, FEE) == pytest.approx((81_300 - 81_400) / 100 - COST)


def test_nothing_received_since_the_bar_close_keeps_the_quote_standing_at_it() -> None:
    quiet = quotes(
        (CLOSE - timedelta(seconds=5), 81_200, 81_300), (CLOSE + timedelta(seconds=40), 81_000, 81_050)
    )
    late = delayed(PRICES, CLOSE, quiet, 30)
    assert late is not None
    assert (late.bid_start, late.ask_start) == (81_200, 81_300)
    assert not late.upper_reached and late.lower_reached  # the quote at 40 s, after the entry
    assert delayed(PRICES, CLOSE, None, 1) is None  # no recording: no delayed entry


def test_a_crossed_quote_is_entered_at_as_it_stands() -> None:
    # #187: each side of a crossed quote was a price one could trade at.
    crossed = quotes((CLOSE + timedelta(seconds=0.5), 81_400, 81_300))
    late = delayed(PRICES, CLOSE, crossed, 1)
    assert late is not None and (late.bid_start, late.ask_start) == (81_400, 81_300)


def test_a_tally_counts_up_and_down_calls_after_each_delay() -> None:
    def scored(p_up: float, p_down: float, outcome: Direction) -> Scored:
        return Scored("newton", "v1", "005930", CLOSE, p_up, 1 - p_up - p_down, p_down, outcome, PRICES)

    t = Tally()
    t.add(scored(0.6, 0.1, Direction.UP), AFTER, FEE)  # up: as above
    t.add(scored(0.1, 0.6, Direction.UP), AFTER, FEE)  # down: its lower target never reached
    t.add(scored(0.2, 0.2, Direction.FLAT), AFTER, FEE)  # flat: no trade
    assert t.calls == 2
    assert {d: t.reached[d] for d in DELAYS} == {
        0: [True, False],
        1: [True, False],
        5: [True, False],
        30: [False, False],
    }
    assert [len(t.profits[d]) for d in DELAYS] == [2, 2, 2, 2]
    # Without the day's quotes only the bar close is measured.
    missing = Tally()
    missing.add(scored(0.6, 0.1, Direction.UP), None, FEE)
    assert [len(missing.reached[d]) for d in DELAYS] == [1, 0, 0, 0]
