"""The touch target's rules (#160, #167), on windows worked out by hand.

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
from krx_quant.touch import (
    MISSING_MID,
    REST_QUOTES,
    RESTARTED,
    SILENT,
    TouchWindow,
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


def test_a_locked_quote_counts_and_a_crossed_one_is_dropped() -> None:
    assert play(window(), (30, 81_500, 81_500)).result().direction is Direction.UP
    assert play(window(), (30, 81_600, 81_500)).result().direction is Direction.FLAT


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
