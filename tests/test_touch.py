"""The touch target's rules (#160, #167, #169, #187), on windows worked out by hand.

Input: a window's mid-point at its bar close and the quotes and trades given to it, by
receipt time. Output: its class, why it could not be scored, and its path.

The cost band k at a price p, with the 0.20% sell tax and a 0.015% commission per side, is
p × 0.23% over the tick, rounded up: at 81,250 won (tick 100), 186.875 / 100 → k = 2, so the
targets are 81,450 and 81,050.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from krx_quant.feeds import KST
from krx_quant.pricing import Direction
from krx_quant.reference import Bar
from krx_quant.touch import (
    MISSING_MID,
    REST_QUOTES,
    RESTARTED,
    SILENT,
    TouchWindow,
    bar_touch,
    close_side,
    restarted,
    touch_path,
)

FEE = 0.015 / 100
T = datetime(2026, 10, 12, 9, 31, tzinfo=KST)


def s(seconds: float) -> datetime:
    return T + timedelta(seconds=seconds)


def window(anchor: float | None = 81_250.0) -> TouchWindow:
    w = TouchWindow("005930", T, anchor, FEE, bid_start=81_200.0, ask_start=81_300.0)
    return w


Quote = tuple[float, float, float]  # (seconds after t, bid, ask)


def play(w: TouchWindow, *quotes: Quote, every: float = 20.0) -> TouchWindow:
    """``quotes`` given to ``w`` in receipt order, among quotes at the anchor's spread every
    ``every`` seconds through the window (so it is never silent)."""
    background = [(t, 81_200.0, 81_300.0) for t in range(0, 240, int(every))]
    for t, bid, ask in sorted([*background, *quotes], key=lambda q: q[0]):
        w.quote(s(t), bid, ask)
    return w


def test_the_targets_are_the_mid_point_plus_and_minus_k_ticks() -> None:
    w = window()
    assert (w.tick, w.k, w.upper, w.lower) == (100, 2, 81_450, 81_050)


def test_k_follows_the_price_band() -> None:
    # 1,739,000 × 0.23% = 3,999.7 won: 4 ticks of 1,000; 1,740,000 × 0.23% = 4,002 won: 5.
    assert TouchWindow("000660", T, 1_739_000.0, FEE).k == 4
    assert TouchWindow("000660", T, 1_740_000.0, FEE).k == 5


def test_the_best_bid_at_the_upper_target_is_up() -> None:
    short = play(window(), (90, 81_400, 81_500))  # the bid half a tick short of 81,450
    assert short.result().direction is Direction.FLAT
    r = play(window(), (91, 81_500, 81_600)).result()  # a market sell would get 81,500
    assert r.direction is Direction.UP
    assert r.upper_reached is not None and r.upper_reached.at == s(91)
    assert r.upper_reached.price == 81_500


def test_a_trade_print_alone_does_not_touch() -> None:
    # A buyer lifting the ask at 81,500: the bid never got there.
    w = play(window())
    w.trade(s(30), 81_500)
    r = w.result()
    assert r.direction is Direction.FLAT
    assert r.trade_high == 81_500


def test_the_best_ask_at_the_lower_target_is_down_unless_the_upper_was_reached() -> None:
    assert play(window(), (50, 80_900, 81_000)).result().direction is Direction.DOWN
    # Down first, then up: both reached, up.
    both = play(window(), (50, 80_900, 81_000), (150, 81_500, 81_600))
    assert both.result().direction is Direction.UP


def test_a_locked_quote_counts() -> None:
    assert play(window(), (30, 81_500, 81_500)).result().direction is Direction.UP


# Crossed quotes (#187): the integrated best bid above the best ask, one venue's bid above the
# other's ask. Each side is a price one could trade at, so each touches as it would uncrossed.


def test_a_crossed_quote_s_bid_at_the_upper_target_is_up() -> None:
    """Input: 81,500 / 81,400 at 09:31:30 (the bid one tick above the ask, at the upper target
    81,450). Output: up, reached then at the bid. Dropped before #187, it was flat."""
    r = play(window(), (30, 81_500, 81_400)).result()
    assert r.direction is Direction.UP
    assert r.upper_reached is not None and (r.upper_reached.at, r.upper_reached.price) == (s(30), 81_500)
    assert r.lower_reached is None


def test_a_crossed_quote_s_ask_at_the_lower_target_is_down() -> None:
    """Input: 81,100 / 81,000 at 09:31:30 (the ask at the lower target 81,050, under the bid).
    Output: down, reached then at the ask."""
    r = play(window(), (30, 81_100, 81_000)).result()
    assert r.direction is Direction.DOWN
    assert r.lower_reached is not None and (r.lower_reached.at, r.lower_reached.price) == (s(30), 81_000)


def test_a_crossed_quote_reaching_both_targets_is_up_as_any_window_reaching_both() -> None:
    """Input: 81,500 / 81,000 (the bid at the upper target and the ask at the lower one in one
    quote, five ticks crossed). Output: up, both reached at the same time: both reached is up,
    the order not looked at (``touch.py``), as for two quotes."""
    r = play(window(), (30, 81_500, 81_000)).result()
    assert r.direction is Direction.UP
    assert r.upper_reached is not None and r.lower_reached is not None
    assert r.upper_reached.at == r.lower_reached.at == s(30)


def test_a_crossed_quote_short_of_the_targets_is_flat_and_moves_the_extremes() -> None:
    """Input: 81,400 / 81,100 (crossed by three ticks, each side short of its target). Output:
    flat; the highest bid and lowest ask are its own, real prices; the last quote is it."""
    w = window()
    for t, bid, ask in [
        (0, 81_200, 81_300),
        (30, 81_400, 81_100),
        *((t, 81_200, 81_300) for t in range(40, 240, 20)),
    ]:
        w.quote(s(t), bid, ask)
    w.quote(s(230), 81_300, 81_200)  # the last quote, crossed by a tick
    r = w.result()
    assert r.direction is Direction.FLAT
    assert (r.max_bid, r.max_bid_at, r.min_ask, r.min_ask_at) == (81_400, s(30), 81_100, s(30))
    assert (r.bid_end, r.ask_end) == (81_300, 81_200)


def test_crossed_quotes_are_heard_a_one_sided_one_is_not() -> None:
    """Input: a window whose quotes are all crossed (every 20 s), and one whose quotes after
    09:31:10 are all one-sided (a side 0 or missing). Output: the first is scorable (flat), the
    quotes were followed; the second went silent (no quote for 60 s or more) and its one-sided
    bid at the upper target touched nothing."""
    crossed = window()
    for t in range(0, 240, 20):
        crossed.quote(s(t), 81_300, 81_200)
    r = crossed.result()
    assert (r.direction, r.unscorable) == (Direction.FLAT, None)

    one_sided = window()
    one_sided.quote(s(0), 81_200, 81_300)
    one_sided.quote(s(10), 81_200, 81_300)
    for t in range(20, 240, 20):
        one_sided.quote(s(t), 81_500, 0 if t % 40 else None)
    r = one_sided.result()
    assert (r.direction, r.unscorable) == (None, SILENT)
    assert r.upper_reached is None and r.max_bid == 81_200


def test_the_window_runs_from_the_bar_close_up_to_four_minutes_on() -> None:
    assert play(window(), (0, 81_500, 81_600)).result().direction is Direction.UP  # received at t: in
    assert play(window(), (240, 81_500, 81_600)).result().direction is Direction.FLAT  # at t + 4 min: out
    assert play(window(), (-1, 81_500, 81_600)).result().direction is Direction.FLAT  # before t: out


def test_sixty_seconds_without_a_quote_cannot_be_scored() -> None:
    gap = window()
    gap.quote(s(10), 81_200, 81_300)
    gap.quote(s(70), 81_200, 81_300)  # 60 s after the last
    gap.quote(s(120), 81_200, 81_300)
    gap.quote(s(180), 81_200, 81_300)
    gap.quote(s(230), 81_200, 81_300)
    r = gap.result()
    assert (r.direction, r.unscorable) == (None, SILENT)

    tail = window()
    for t in (0, 50, 100, 150):
        tail.quote(s(t), 81_200, 81_300)
    assert tail.result().unscorable == SILENT  # 90 s from the last to the end

    nothing = window()
    assert nothing.result().unscorable == SILENT


def test_a_rest_snapshot_cannot_be_scored_and_a_missing_mid_neither() -> None:
    rest = play(window())
    rest.quote(s(30), 81_500, 81_600, rest=True)
    assert rest.result().unscorable == REST_QUOTES

    no_mid = play(window(anchor=None))
    r = no_mid.result()
    assert (r.direction, r.unscorable, r.upper, r.k) == (None, MISSING_MID, None, None)

    assert restarted("005930", T).unscorable == RESTARTED


def test_the_path_keeps_where_and_how_long_the_targets_were_reached() -> None:
    w = window()
    for t, bid, ask, size in [
        (0, 81_200, 81_300, None),
        (20, 81_200, 81_300, None),
        (41, 81_500, 81_600, 1_200),  # reached, with 1,200 at the bid
        (42.5, 81_400, 81_500, None),  # back off 1.5 s later
        (43, 81_500, 81_600, None),  # back on: the first reach stays
        (60, 80_900, 81_000, None),
        *[(t, 81_200, 81_300, None) for t in range(100, 240, 40)],
    ]:
        w.quote(s(t), bid, ask, bid_size=size)
    w.trade(s(61), 80_950)
    r = w.result()
    assert r.upper_reached is not None and r.upper_reached.size == 1_200
    assert r.upper_reached.dwell(r.end) == 1.5
    assert r.lower_reached is not None and r.lower_reached.at == s(60)
    assert r.direction is Direction.UP
    assert (r.max_bid, r.max_bid_at) == (81_500, s(41))
    assert (r.min_ask, r.min_ask_at) == (81_000, s(60))
    assert (r.bid_start, r.ask_start) == (81_200, 81_300)
    assert (r.bid_end, r.ask_end) == (81_200, 81_300)  # the last quote, at 09:34:40
    assert r.trade_low == 80_950

    path = touch_path(r)
    assert path["upper_at"] == "2026-10-12T09:31:41+09:00"
    assert path["upper_dwell"] == 1.5
    assert path["lower_dwell"] == 40.0  # left at 09:32:40, when the ask went back up
    assert path["k"] == 2


# The one-minute bar rule (#160, #169), for a day with backfilled bars only. The bar that
# ended at the bar close T (09:30–09:31) closes at 81,200: k = 2 (81,200 × 0.23% = 186.76
# won over a 100-won tick). Its close is on the ask (+1), the bid (−1) or unknown (0).
#
#   side   upper target (high ≥)               lower target (low ≤)
#   +1     81,200 + (2 + 1) × 100 = 81,500     81,200 − (2 + 1 + 1) × 100 = 80,800
#   −1     81,200 + (2 + 1 + 1) × 100 = 81,600 81,200 − (2 + 1) × 100 = 80,900
#    0     81,500                              80,900


def bar(minute: int, open_: float, high: float, low: float, close: float) -> Bar:
    """The bar of the minute ``minute`` minutes after T (−1: the one that ended at T)."""
    start = T + timedelta(minutes=minute)
    return Bar(start, open_, high, low, close, 10, 0, start, start)


AT_HIGH = bar(-1, 81_100, 81_200, 81_100, 81_200)  # closed at its high: on the ask
AT_LOW = bar(-1, 81_300, 81_300, 81_200, 81_200)  # closed at its low: on the bid
ONE_PRICE = bar(-1, 81_200, 81_200, 81_200, 81_200)


def after(high: float, low: float, open_: float = 81_200) -> list[Bar]:
    """The four minutes after T: one bar reaching ``high`` and ``low``, quiet ones around it."""
    return [
        bar(0, open_, max(open_, 81_200), min(open_, 81_200), 81_200),
        bar(1, 81_200, high, low, 81_200),
        bar(2, 81_200, 81_200, 81_200, 81_200),
        bar(3, 81_200, 81_200, 81_200, 81_200),
    ]


def test_a_bar_closed_at_its_high_or_low_traded_on_the_ask_or_the_bid() -> None:
    assert close_side(AT_HIGH, 81_300) == 1  # the bar says so: the next open is not looked at
    assert close_side(AT_LOW, 81_100) == -1
    # One price, or a close between the high and the low: the next trade tells.
    assert close_side(ONE_PRICE, 81_100) == 1  # the next trade was lower: the close was the ask
    assert close_side(ONE_PRICE, 81_300) == -1
    assert close_side(ONE_PRICE, 81_200) == 0
    assert close_side(bar(-1, 81_100, 81_300, 81_100, 81_200), 81_300) == -1
    assert close_side(ONE_PRICE, None) == 0


def test_the_bar_rule_takes_one_tick_more_and_one_more_on_the_far_side() -> None:
    # Closed on the ask: up from 81,500, down from 80,800.
    assert bar_touch(AT_HIGH, after(81_500, 81_200), FEE) is Direction.UP
    assert bar_touch(AT_HIGH, after(81_400, 81_200), FEE) is Direction.FLAT  # the bid only reached 81,300
    assert bar_touch(AT_HIGH, after(81_200, 80_900), FEE) is Direction.FLAT
    assert bar_touch(AT_HIGH, after(81_200, 80_800), FEE) is Direction.DOWN
    # Closed on the bid: up from 81,600, down from 80,900.
    assert bar_touch(AT_LOW, after(81_500, 81_200), FEE) is Direction.FLAT
    assert bar_touch(AT_LOW, after(81_600, 81_200), FEE) is Direction.UP
    assert bar_touch(AT_LOW, after(81_200, 80_900), FEE) is Direction.DOWN
    # Unknown side: one tick of buffer each way.
    assert bar_touch(ONE_PRICE, after(81_500, 81_200), FEE) is Direction.UP
    assert bar_touch(ONE_PRICE, after(81_200, 80_900), FEE) is Direction.DOWN
    assert bar_touch(ONE_PRICE, after(81_400, 81_000), FEE) is Direction.FLAT


def test_the_next_bars_open_places_the_close_of_a_one_price_bar() -> None:
    # The next trade was lower (81,100): the close was on the ask, so down needs 80,800.
    assert bar_touch(ONE_PRICE, after(81_200, 80_900, open_=81_100), FEE) is Direction.FLAT
    # Higher (81,300): on the bid, so up needs 81,600.
    assert bar_touch(ONE_PRICE, after(81_500, 81_200, open_=81_300), FEE) is Direction.FLAT


def test_both_reached_is_up_and_no_trade_is_no_label() -> None:
    assert bar_touch(AT_HIGH, after(81_500, 80_800), FEE) is Direction.UP
    assert bar_touch(AT_HIGH, [], FEE) is None


def test_the_bar_rule_takes_k_at_the_close() -> None:
    # 1,740,000 × 0.23% = 4,002 won: k = 5 ticks of 1,000. One price, the next open the same:
    # up from 1,746,000.
    closing = bar(-1, 1_740_000, 1_740_000, 1_740_000, 1_740_000)
    quiet = bar(0, 1_740_000, 1_740_000, 1_740_000, 1_740_000)
    assert (
        bar_touch(closing, [quiet, bar(1, 1_740_000, 1_746_000, 1_740_000, 1_740_000)], FEE) is Direction.UP
    )
    assert (
        bar_touch(closing, [quiet, bar(1, 1_740_000, 1_745_000, 1_740_000, 1_740_000)], FEE) is Direction.FLAT
    )
