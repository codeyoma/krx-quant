"""Skill against the class rates (#262): kappa, Gerrity, balanced accuracy and the
independence test, on tables worked out by hand."""

from __future__ import annotations

import pytest

from krx_quant.pricing import Direction
from krx_quant.skill import chi2_sf, gerrity, skill

UP, FLAT, DOWN = Direction.UP, Direction.FLAT, Direction.DOWN
# What happened: 10 up, 30 flat, 10 down.
HAPPENED = [UP] * 10 + [FLAT] * 30 + [DOWN] * 10


def test_perfect_calls_score_one() -> None:
    s = skill(list(zip(HAPPENED, HAPPENED, strict=True)))
    assert s is not None
    assert (s.accuracy, s.kappa, s.balanced_accuracy) == (1.0, 1.0, 1.0)
    assert s.gerrity == pytest.approx(1.0)
    assert s.expected_accuracy == pytest.approx((10**2 + 30**2 + 10**2) / 50**2)
    assert s.chi2 == pytest.approx(100.0)  # 2n for a perfect 3×3 table
    assert s.df == 4
    assert s.p is not None and s.p < 1e-15


def test_always_flat_scores_zero_however_accurate() -> None:
    s = skill([(FLAT, h) for h in HAPPENED])
    assert s is not None
    assert s.accuracy == pytest.approx(0.6)  # what a mostly-flat day gives it
    assert s.kappa == pytest.approx(0.0)
    assert s.gerrity == pytest.approx(0.0)
    assert s.balanced_accuracy == pytest.approx(1 / 3)
    assert (s.chi2, s.df, s.p) == (None, 0, None)  # one row: nothing to test


def test_gerrity_charges_a_call_two_classes_off_more() -> None:
    # Four of the down windows called flat, or called up: the same accuracy and kappa.
    one_off = [(FLAT if h is DOWN and i < 44 else h, h) for i, h in enumerate(HAPPENED)]
    two_off = [(UP if h is DOWN and i < 44 else h, h) for i, h in enumerate(HAPPENED)]
    near, far = skill(one_off), skill(two_off)
    assert near is not None and far is not None
    assert near.accuracy == far.accuracy == pytest.approx(46 / 50)
    assert near.gerrity is not None and far.gerrity is not None
    assert near.gerrity > far.gerrity


def test_undefined_values_are_none() -> None:
    assert skill([]) is None
    # Only up happened: the cumulative rates of down and of down-or-flat are 0.
    assert gerrity([[0, 0, 0], [0, 0, 0], [0, 0, 5]]) is None


def test_chi_square_tail() -> None:
    assert chi2_sf(9.487729, 4) == pytest.approx(0.05, abs=1e-6)
    assert chi2_sf(3.841459, 1) == pytest.approx(0.05, abs=1e-6)
    assert chi2_sf(5.991465, 2) == pytest.approx(0.05, abs=1e-6)
    assert chi2_sf(0.0, 4) == 1.0
