"""Skill against the class rates (#262): kappa, Gerrity, balanced accuracy and the
independence test, on tables worked out by hand."""

from __future__ import annotations

import math
from datetime import date

import pytest

from krx_quant.pricing import Direction
from krx_quant.skill import chi2_sf, day_kappa, gerrity, skill

UP, FLAT, DOWN = Direction.UP, Direction.FLAT, Direction.DOWN
# What happened: 10 up, 30 flat, 10 down.
HAPPENED = [UP] * 10 + [FLAT] * 30 + [DOWN] * 10


def test_perfect_calls_score_one() -> None:
    s = skill(list(zip(HAPPENED, HAPPENED, strict=True)))
    assert s is not None
    assert (s.accuracy, s.kappa, s.balanced_accuracy) == (1.0, 1.0, 1.0)
    assert s.gerrity == pytest.approx(1.0)
    assert s.expected_accuracy == pytest.approx((10**2 + 30**2 + 10**2) / 50**2)
    # A cell expects 10 × 10 / 50 = 2, under 5: no chi-square (Cochran's rule).
    assert (s.chi2, s.df, s.p) == (None, 4, None)
    # Three times as many: every cell expects at least 30 × 30 / 150 = 6.
    big = skill(list(zip(HAPPENED * 3, HAPPENED * 3, strict=True)))
    assert big is not None
    assert big.chi2 == pytest.approx(300.0)  # 2n for a perfect 3×3 table
    assert big.p is not None and big.p < 1e-15


def test_always_flat_scores_zero_however_accurate() -> None:
    s = skill([(FLAT, h) for h in HAPPENED])
    assert s is not None
    assert s.accuracy == pytest.approx(0.6)  # what a mostly-flat day gives it
    assert s.kappa == pytest.approx(0.0)
    assert s.gerrity == pytest.approx(0.0)
    assert s.balanced_accuracy == pytest.approx(1 / 3)
    assert (s.chi2, s.df, s.p) == (None, 0, None)  # one row: nothing to test


def test_gerrity_charges_a_call_two_classes_off_more() -> None:
    # Four of the down windows called flat, or called up: the same accuracy.
    one_off = [(FLAT if h is DOWN and i < 44 else h, h) for i, h in enumerate(HAPPENED)]
    two_off = [(UP if h is DOWN and i < 44 else h, h) for i, h in enumerate(HAPPENED)]
    near, far = skill(one_off), skill(two_off)
    assert near is not None and far is not None
    assert near.accuracy == far.accuracy == pytest.approx(46 / 50)
    assert near.gerrity is not None and far.gerrity is not None
    assert near.gerrity > far.gerrity
    assert near.kappa is not None and far.kappa is not None
    assert near.kappa < far.kappa  # kappa does not see how far off a call is


def test_undefined_values_are_none() -> None:
    assert skill([]) is None
    # Only up happened: the cumulative rates of down and of down-or-flat are 0.
    assert gerrity([[0, 0, 0], [0, 0, 0], [0, 0, 5]]) is None


def test_chi_square_tail() -> None:
    assert chi2_sf(9.487729, 4) == pytest.approx(0.05, abs=1e-6)
    assert chi2_sf(3.841459, 1) == pytest.approx(0.05, abs=1e-6)
    assert chi2_sf(5.991465, 2) == pytest.approx(0.05, abs=1e-6)
    assert chi2_sf(0.0, 4) == 1.0


PERFECT = list(zip(HAPPENED, HAPPENED, strict=True))
ALWAYS_FLAT = [(FLAT, h) for h in HAPPENED]
DAYS = [date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14)]
T_975_1 = 12.706204736174698  # Student's t, 97.5th percentile, 1 degree of freedom (tables)


def test_kappa_by_trading_day_is_a_t_interval_over_each_day_s_kappa() -> None:
    # A perfect day (kappa 1) and an always-flat day (0): a mean of 0.5, a standard error of 0.5.
    k = day_kappa({DAYS[0]: PERFECT, DAYS[1]: ALWAYS_FLAT})
    assert k is not None
    assert k.n == 2
    assert k.value == pytest.approx(0.5)
    assert k.low == pytest.approx(0.5 - T_975_1 * 0.5)
    assert k.high == pytest.approx(0.5 + T_975_1 * 0.5)
    # One degree of freedom: P(T > 1) = 1/2 - atan(1)/π = 1/4.
    assert k.p == pytest.approx(0.5 - math.atan(1) / math.pi)


def test_kappa_by_trading_day_leaves_out_a_day_without_one() -> None:
    perfect_days = {DAYS[0]: PERFECT, DAYS[1]: PERFECT}
    k = day_kappa(perfect_days)
    assert k is not None
    assert (k.n, k.value, k.low, k.high, k.p) == (2, 1.0, 1.0, 1.0, 0.0)  # no spread: that value alone
    # Flat called on a flat-only day: the expected accuracy is 1, there is no kappa.
    only_flat = [(FLAT, FLAT)] * 10
    assert day_kappa({**perfect_days, DAYS[2]: only_flat}) == k
    assert day_kappa({DAYS[0]: PERFECT, DAYS[2]: only_flat}) is None  # one day with a kappa
    assert day_kappa({}) is None
    # Always flat, day after day: no skill, and no evidence of any.
    flat = day_kappa({d: ALWAYS_FLAT for d in DAYS})
    assert flat is not None
    assert (flat.value, flat.p) == (0.0, 1.0)
