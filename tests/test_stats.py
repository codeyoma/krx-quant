"""The Stats Engine on a small example worked by hand.

Ten scored predictions of one algorithm, each scored on its bar close's touch label (#170).
Every bar close has the same targets: a mid-point of 81,250 won between a best bid of
81,200 and a best ask of 81,300 (tick 100 won, k = 2: the upper target 81,450, the lower
81,050), round-trip cost 0.23% = 186.875 won = 1.86875 ticks:

| # | p_up/p_flat/p_down | predicted | label | reached | quotes at the end (bid/ask) | correct | Brier |
|---|---|---|---|---|---|---|---|
| 1 | .6 .3 .1 | up | up | upper | 81,400 / 81,500 | yes | .26 |
| 2 | .5 .4 .1 | up | flat | neither | 81,300 / 81,400 | no | .62 |
| 3 | .7 .2 .1 | up | down | lower | 80,900 / 81,000 | no | 1.34 |
| 4 | .1 .3 .6 | down | down | lower | 81,000 / 81,100 | yes | .26 |
| 5 | .2 .3 .5 | down | up | both | 81,500 / 81,600 | no | .98 |
| 6 | .2 .6 .2 | flat | flat | neither | | yes | .24 |
| 7 | .1 .8 .1 | flat | flat | neither | | yes | .06 |
| 8 | .3 .4 .3 | flat | up | upper | | no | .74 |
| 9 | .25 .5 .25 | flat | down | lower | | no | .875 |
| 10 | 0 1 0 | flat | flat | neither | | yes | 0 |

- direction precision 2/5 (Wilson 95%: 0.1176–0.7693), coverage 5/10, accuracy 5/10
  (Wilson: 0.2366–0.7634);
- Brier: 5.375 / 10 = 0.5375;
- the touch P&L of the trades (#1–5), in ticks, less c = 1.86875: up bought at the ask
  81,300 and sold at the upper target 81,450 when the bid reached it (#1: 1.5), else at the
  last bid (#2: 81,300, 0; #3: 80,900, −4); down (a short of the stock) sold at the bid
  81,200 and bought back at the lower target 81,050 when the ask reached it (#4: 1.5; #5
  too, though up won the label: both were reached). Total 1.5 + 0 − 4 + 1.5 + 1.5 − 5c =
  −8.84375, mean −1.76875 per trade; the deviations from the mean square to 22.7, a
  variance of 5.675 and a 95% interval of −1.76875 ± 1.959964 × √(5.675 / 5);
- against always-flat (right on #2, 6, 7, 10), the paired differences are
  +1, −1, 0, +1, 0, 0, 0, 0, 0, 0: lift 0.1. Ten pairs are too few to test, so the test
  repeats the ten three times (30 pairs): the deviations from 0.1 square to 3 × 2.9 =
  8.7, the variance is 8.7 / 29 = 0.3, the standard error √(0.3 / 30) = 0.1, so the 95%
  interval is 0.1 ± 0.196 and the one-sided p is 1 − Φ(1) = 0.1587.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from krx_quant.feeds import KST
from krx_quant.pricing import Direction
from krx_quant.stats import (
    Interval,
    Prices,
    Scored,
    benjamini_hochberg,
    lift,
    summarize,
    touch_profit,
    wilson,
)

FEE = 0.015 / 100
T0 = datetime(2026, 10, 12, 9, 31, tzinfo=KST)
UP, FLAT, DOWN = Direction.UP, Direction.FLAT, Direction.DOWN

NEITHER, UPPER, LOWER, BOTH = (False, False), (True, False), (False, True), (True, True)


def prices(
    reached: tuple[bool, bool], bid_end: float | None = 81_200, ask_end: float | None = 81_300
) -> Prices:
    """A bar close's touch path at 81,250 won (bid 81,200, ask 81,300, k = 2): whether the
    upper and the lower target were reached, and the best quotes at the end."""
    return Prices(
        reference=81_250,
        tick=100,
        upper=81_450,
        lower=81_050,
        upper_reached=reached[0],
        lower_reached=reached[1],
        bid_start=81_200,
        ask_start=81_300,
        bid_end=bid_end,
        ask_end=ask_end,
    )


EXAMPLE = [
    ((0.6, 0.3, 0.1), UP, prices(UPPER, 81_400, 81_500)),
    ((0.5, 0.4, 0.1), FLAT, prices(NEITHER, 81_300, 81_400)),
    ((0.7, 0.2, 0.1), DOWN, prices(LOWER, 80_900, 81_000)),
    ((0.1, 0.3, 0.6), DOWN, prices(LOWER, 81_000, 81_100)),
    ((0.2, 0.3, 0.5), UP, prices(BOTH, 81_500, 81_600)),
    ((0.2, 0.6, 0.2), FLAT, prices(NEITHER)),
    ((0.1, 0.8, 0.1), FLAT, prices(NEITHER)),
    ((0.3, 0.4, 0.3), UP, prices(UPPER)),
    ((0.25, 0.5, 0.25), DOWN, prices(LOWER)),
    ((0.0, 1.0, 0.0), FLAT, prices(NEITHER)),
]


def scored(
    algorithm: str, i: int, p: tuple[float, float, float], outcome: Direction, path: Prices | None
) -> Scored:
    return Scored(
        algorithm=algorithm,
        model_version="v1",
        symbol="005930",
        bar_close=T0 + timedelta(minutes=i),
        p_up=p[0],
        p_flat=p[1],
        p_down=p[2],
        outcome=outcome,
        prices=path,
    )


def known(interval: Interval | None) -> Interval:
    assert interval is not None
    return interval


ALGORITHM = [scored("x", i, p, o, m) for i, (p, o, m) in enumerate(EXAMPLE)]
ALWAYS_FLAT = [scored("baseline-flat", i, (0, 1, 0), o, m) for i, (_, o, m) in enumerate(EXAMPLE)]


def test_ratios_come_with_wilson_intervals() -> None:
    s = summarize(ALGORITHM, FEE)
    precision, coverage, accuracy = known(s.precision), known(s.coverage), known(s.accuracy)

    assert (precision.value, precision.n) == (0.4, 5)
    assert precision.low is not None and precision.high is not None
    assert (round(precision.low, 4), round(precision.high, 4)) == (0.1176, 0.7693)
    assert (coverage.value, coverage.n) == (0.5, 10)
    assert (accuracy.value, accuracy.n) == (0.5, 10)
    assert accuracy.low is not None and accuracy.high is not None
    assert (round(accuracy.low, 4), round(accuracy.high, 4)) == (0.2366, 0.7634)


def test_brier_score_and_the_touch_profit_in_ticks() -> None:
    s = summarize(ALGORITHM, FEE)

    assert known(s.brier).value == pytest.approx(0.5375)
    assert s.pnl.trades == 5
    assert s.pnl.total == pytest.approx(-8.84375)
    per_trade = known(s.pnl.per_trade)
    assert per_trade.value == pytest.approx(-1.76875)
    assert per_trade.low == pytest.approx(-1.76875 - 1.959964 * math.sqrt(5.675 / 5), abs=1e-5)


def test_the_touch_profit_follows_the_labels_rule() -> None:
    """Up: ask in, upper target out if reached, else the last bid. Down (a short of the stock): bid in,
    lower target back if reached, else the last ask. Less 1.86875 ticks of cost."""
    c = 1.86875
    assert touch_profit(UP, prices(UPPER), FEE) == pytest.approx(1.5 - c)
    assert touch_profit(UP, prices(NEITHER, 81_400, 81_500), FEE) == pytest.approx(1 - c)
    assert touch_profit(DOWN, prices(LOWER), FEE) == pytest.approx(1.5 - c)
    assert touch_profit(DOWN, prices(NEITHER, 81_100, 81_200), FEE) == pytest.approx(0 - c)
    # Both reached: the label is up, the inverse still bought back at the lower target.
    assert touch_profit(DOWN, prices(BOTH), FEE) == pytest.approx(1.5 - c)


def test_the_inverse_touch_profit_drops_the_sell_tax_on_down_calls() -> None:
    """#267: a down call through a −2x inverse product pays the commissions only, on half the
    notional: 81,250 × 0.03% ÷ 2 = 0.121875 ticks; an up call is the stock's either way."""
    c, inverse = 1.86875, 0.121875
    assert touch_profit(DOWN, prices(LOWER), FEE, inverse_leverage=2) == pytest.approx(1.5 - inverse)
    assert touch_profit(UP, prices(UPPER), FEE, inverse_leverage=2) == pytest.approx(1.5 - c)
    s = summarize(ALGORITHM, FEE)
    # Three up calls and two down calls: the two down calls each save the sell tax and half
    # the commissions.
    assert (s.pnl_inverse.trades, s.pnl.trades) == (5, 5)
    assert s.pnl_inverse.total == pytest.approx(-8.84375 + 2 * (c - inverse))
    # 009150 has no inverse product: its down calls are no trades there, the up calls stay.
    other = summarize([replace(r, symbol="009150") for r in ALGORITHM], FEE)
    assert (other.pnl_inverse.trades, other.pnl.trades) == (3, 5)
    # Before the products were listed (2026-05-27), no down call could be traded through one.
    early = [replace(r, bar_close=r.bar_close.replace(month=5, day=26)) for r in ALGORITHM]
    assert summarize(early, FEE).pnl_inverse.trades == 3
    listed = [replace(r, bar_close=r.bar_close.replace(month=5, day=27)) for r in ALGORITHM]
    assert summarize(listed, FEE).pnl_inverse.trades == 5, "from the listing day on"


def test_flat_calls_and_calls_without_their_quotes_are_no_trades() -> None:
    assert touch_profit(FLAT, prices(UPPER), FEE) is None
    assert touch_profit(UP, None, FEE) is None
    assert touch_profit(UP, prices(NEITHER, bid_end=None), FEE) is None  # no bid at the end
    assert touch_profit(UP, prices(UPPER, bid_end=None), FEE) == pytest.approx(1.5 - 1.86875)
    no_end = [scored("x", 0, (1, 0, 0), FLAT, prices(NEITHER, bid_end=None))]
    assert summarize(no_end, FEE).pnl.trades == 0


def test_the_calibration_curve_bins_every_class_probability() -> None:
    bins = {round(b.low, 1): b for b in summarize(ALGORITHM, FEE).calibration}

    # [0.7, 0.8): #3's 0.7 on up, which did not happen.
    assert (bins[0.7].count, bins[0.7].mean_p, known(bins[0.7].observed).value) == (1, 0.7, 0.0)
    # [0.9, 1.0]: #10's 1.0 on flat, which happened.
    assert (bins[0.9].count, bins[0.9].mean_p, known(bins[0.9].observed).value) == (1, 1.0, 1.0)
    # [0.0, 0.1): #10's two zeros; neither happened.
    assert (bins[0.0].count, known(bins[0.0].observed).value) == (2, 0.0)
    assert sum(b.count for b in bins.values()) == 30


def thrice(records: list[Scored]) -> list[Scored]:
    """The example three times over, at later bar closes."""
    return [replace(r, bar_close=r.bar_close + timedelta(minutes=10 * k)) for k in range(3) for r in records]


def test_lift_is_the_paired_gain_in_accuracy_over_a_baseline() -> None:
    result = lift(thrice(ALGORITHM), thrice(ALWAYS_FLAT))

    assert result.n == 30
    assert result.value == pytest.approx(0.1)
    assert result.low == pytest.approx(0.1 - 0.195996, abs=1e-6)
    assert result.high == pytest.approx(0.1 + 0.195996, abs=1e-6)
    assert result.p == pytest.approx(0.158655, abs=1e-6)


def test_lift_only_compares_bar_closes_both_predicted() -> None:
    result = lift(ALGORITHM[:4], ALWAYS_FLAT[2:])

    assert result.n == 2  # #3 and #4: wrong / wrong, right / wrong
    assert result.value == pytest.approx(0.5)


def test_benjamini_hochberg_adjusts_p_values_in_their_order() -> None:
    q = benjamini_hochberg([0.01, 0.04, 0.03, 0.2])

    # Sorted 0.01, 0.03, 0.04, 0.2 give 0.04, 0.06, 0.0533, 0.2, then a running minimum
    # from the largest down.
    assert q == pytest.approx([0.04, 0.16 / 3, 0.16 / 3, 0.2])


def test_no_data_gives_no_interval() -> None:
    assert wilson(0, 0) is None
    empty = summarize([], FEE)
    assert empty.accuracy is None and empty.precision is None and empty.pnl.trades == 0


def test_a_lift_on_too_few_pairs_is_not_tested() -> None:
    # Two bar closes both gained: sure-looking, but far too few to test.
    result = lift(ALGORITHM[:1] + ALGORITHM[3:4], ALWAYS_FLAT)

    assert (result.n, result.value, result.p) == (2, 1.0, None)


def test_a_single_trade_has_a_profit_but_no_interval() -> None:
    per_trade = known(summarize(ALGORITHM[:1], FEE).pnl.per_trade)

    assert per_trade.value == pytest.approx(1.5 - 1.86875)
    assert (per_trade.low, per_trade.high, per_trade.n) == (None, None, 1)


def test_each_calibration_bin_says_how_sure_its_observed_rate_is() -> None:
    bins = {round(b.low, 1): b for b in summarize(ALGORITHM, FEE).calibration}

    observed = known(bins[0.0].observed)  # 0 of 2 happened
    assert (observed.value, observed.n, observed.low) == (0.0, 2, 0.0)
    assert observed.high is not None and round(observed.high, 4) == 0.6576
