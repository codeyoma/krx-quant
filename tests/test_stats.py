"""The Stats Engine on a small example worked by hand.

Ten scored predictions of one algorithm, at a mid-point of 81,250 won (tick 100 won,
round-trip cost 0.23% = 186.875 won = 1.86875 ticks):

| # | p_up/p_flat/p_down | predicted | outcome | move (ticks) | correct | Brier |
|---|---|---|---|---|---|---|
| 1 | .6 .3 .1 | up | up | +3 | yes | .26 |
| 2 | .5 .4 .1 | up | flat | +1 | no | .62 |
| 3 | .7 .2 .1 | up | down | -3 | no | 1.34 |
| 4 | .1 .3 .6 | down | down | -4 | yes | .26 |
| 5 | .2 .3 .5 | down | up | +2 | no | .98 |
| 6 | .2 .6 .2 | flat | flat | 0 | yes | .24 |
| 7 | .1 .8 .1 | flat | flat | +1 | yes | .06 |
| 8 | .3 .4 .3 | flat | up | +3 | no | .74 |
| 9 | .25 .5 .25 | flat | down | -2 | no | .875 |
| 10 | 0 1 0 | flat | flat | 0 | yes | 0 |

- direction precision 2/5 (Wilson 95%: 0.1176–0.7693), coverage 5/10, accuracy 5/10
  (Wilson: 0.2366–0.7634);
- Brier: 5.375 / 10 = 0.5375;
- trades (#1–5): 3−c, 1−c, −3−c, 4−c, −2−c with c = 1.86875: total −6.34375, mean
  −1.26875 per trade;
- against always-flat (right on #2, 6, 7, 10), the paired differences are
  +1, −1, 0, +1, 0, 0, 0, 0, 0, 0: lift 0.1, standard error 0.1795, 95% interval
  −0.2518 to 0.4518, one-sided p 0.2887.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from krx_quant.feeds import KST
from krx_quant.pricing import Direction
from krx_quant.stats import Interval, Scored, benjamini_hochberg, lift, summarize, wilson

FEE = 0.015 / 100
T0 = datetime(2026, 10, 12, 9, 31, tzinfo=KST)
UP, FLAT, DOWN = Direction.UP, Direction.FLAT, Direction.DOWN

EXAMPLE = [
    ((0.6, 0.3, 0.1), UP, 3),
    ((0.5, 0.4, 0.1), FLAT, 1),
    ((0.7, 0.2, 0.1), DOWN, -3),
    ((0.1, 0.3, 0.6), DOWN, -4),
    ((0.2, 0.3, 0.5), UP, 2),
    ((0.2, 0.6, 0.2), FLAT, 0),
    ((0.1, 0.8, 0.1), FLAT, 1),
    ((0.3, 0.4, 0.3), UP, 3),
    ((0.25, 0.5, 0.25), DOWN, -2),
    ((0.0, 1.0, 0.0), FLAT, 0),
]


def scored(algorithm: str, i: int, p: tuple[float, float, float], outcome: Direction, move: int) -> Scored:
    return Scored(
        algorithm=algorithm,
        model_version="v1",
        symbol="005930",
        bar_close=T0 + timedelta(minutes=i),
        p_up=p[0],
        p_flat=p[1],
        p_down=p[2],
        outcome=outcome,
        start_mid=81_250,
        target_mid=81_250 + 100 * move,
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
    assert (round(precision.low, 4), round(precision.high, 4)) == (0.1176, 0.7693)
    assert (coverage.value, coverage.n) == (0.5, 10)
    assert (accuracy.value, accuracy.n) == (0.5, 10)
    assert (round(accuracy.low, 4), round(accuracy.high, 4)) == (0.2366, 0.7634)


def test_brier_score_and_cost_adjusted_profit_in_ticks() -> None:
    s = summarize(ALGORITHM, FEE)

    assert known(s.brier).value == pytest.approx(0.5375)
    assert s.pnl.trades == 5
    assert s.pnl.total == pytest.approx(-6.34375)
    assert known(s.pnl.per_trade).value == pytest.approx(-1.26875)
    assert known(s.pnl.per_trade).low == pytest.approx(-3.94178, abs=1e-5)


def test_the_calibration_curve_bins_every_class_probability() -> None:
    bins = {round(b.low, 1): b for b in summarize(ALGORITHM, FEE).calibration}

    # [0.7, 0.8): #3's 0.7 on up, which did not happen.
    assert (bins[0.7].count, bins[0.7].mean_p, bins[0.7].observed) == (1, 0.7, 0.0)
    # [0.9, 1.0]: #10's 1.0 on flat, which happened.
    assert (bins[0.9].count, bins[0.9].mean_p, bins[0.9].observed) == (1, 1.0, 1.0)
    # [0.0, 0.1): #10's two zeros; neither happened.
    assert (bins[0.0].count, bins[0.0].observed) == (2, 0.0)
    assert sum(b.count for b in bins.values()) == 30


def test_lift_is_the_paired_gain_in_accuracy_over_a_baseline() -> None:
    result = lift(ALGORITHM, ALWAYS_FLAT)

    assert result.n == 10
    assert result.value == pytest.approx(0.1)
    assert result.low is not None and result.high is not None
    assert (round(result.low, 4), round(result.high, 4)) == (-0.2518, 0.4518)
    assert result.p == pytest.approx(0.2887, abs=1e-4)


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
