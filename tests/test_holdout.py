"""A pre-registered claim judged on a fixed holdout by trading day (#447; the SMT experiment's holdout
judges by it, #523).

Input: made-up verdict intervals, and cells of made-up records against a baseline's on ten trading
days. Output: the verdicts (a positive claim on the lower end of the interval by trading day, a
negative one on its upper end; too few not judged) and the interval at 98.75%.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import pytest

from krx_quant.climate import CLIMATE
from krx_quant.holdout import DAYS, FAILS, LEVEL, MIN_SCORED, STANDS, TOO_FEW, judge, verdict
from krx_quant.pricing import Direction
from krx_quant.stats import Lift, Scored, t_quantile
from tests.scheduled_doubles import at

UP = Direction.UP
SYMBOL = "005930"
# Trading days, Monday 2 November on, no weekend.
AFTER_DAYS = [date(2026, 11, d) for d in (2, 3, 4, 5, 6, 9, 10, 11, 12, 13, 16, 17)]


def test_the_pre_registered_settings_are_447_s() -> None:
    assert (DAYS, MIN_SCORED, LEVEL) == (10, 200, 0.9875)


def by_day(low: float, high: float, days: int = DAYS) -> Lift:
    return Lift(days, (low + high) / 2, low, high, None)


def test_a_positive_hypothesis_stands_only_on_the_lower_end_and_a_negative_one_on_the_upper() -> None:
    enough = MIN_SCORED
    assert verdict(enough, by_day(0.001, 0.2), 1) == STANDS
    assert verdict(enough, by_day(-0.001, 0.2), 1) == FAILS, "a positive lift whose lower end is below 0"
    assert verdict(enough, by_day(0.0, 0.2), 1) == FAILS, "a lower end of 0 is not above it"
    assert verdict(enough, by_day(-0.3, -0.1), 1) == FAILS, (
        "a negative lift does not stand for a positive claim"
    )
    assert verdict(enough, by_day(-0.2, -0.001), -1) == STANDS
    assert verdict(enough, by_day(-0.2, 0.001), -1) == FAILS, "a negative lift whose upper end is above 0"
    assert verdict(enough, by_day(-0.2, 0.0), -1) == FAILS
    assert verdict(enough, by_day(0.1, 0.3), -1) == FAILS, (
        "a positive lift does not stand for a negative claim"
    )
    # Too few: not judged, however clear.
    assert verdict(enough - 1, by_day(0.1, 0.3), 1) == TOO_FEW, "199 scored"
    assert verdict(enough, by_day(0.1, 0.3, DAYS - 1), 1) == TOO_FEW, "pairs on nine days"
    assert verdict(enough, None, 1) == TOO_FEW


def scored(algorithm: str, day: date, minute: int, correct: bool) -> Scored:
    """A sure call at 09:``minute`` on ``day`` of an up answer: up if ``correct``, else flat."""
    p = (1.0, 0.0, 0.0) if correct else (0.0, 1.0, 0.0)
    return Scored(algorithm, "v1", SYMBOL, at(day, f"09:{minute:02d}"), *p, UP)


def cell(gains: Sequence[Sequence[int]]) -> tuple[list[Scored], list[Scored]]:
    """turing's and the climate's records of a cell whose day i gains ``gains[i]`` (+1, 0 or −1)."""
    ours: list[Scored] = []
    theirs: list[Scored] = []
    for day, day_gains in zip(AFTER_DAYS, gains, strict=False):
        for minute, gain in enumerate(day_gains, start=1):
            ours.append(scored("turing", day, minute, gain >= 0))
            theirs.append(scored(CLIMATE, day, minute, gain <= 0))
    return ours, theirs


def test_judge_takes_the_interval_by_trading_day_at_98_75_percent() -> None:
    # Ten days of 21 pairs: day i gains +1 on 10 + i of them, 0 on the rest.
    gains = [[1] * (10 + i) + [0] * (11 - i) for i in range(DAYS)]
    ours, theirs = cell(gains)
    j = judge(ours, theirs, 1)
    means = [(10 + i) / 21 for i in range(DAYS)]
    mean = sum(means) / DAYS
    sd = (sum((m - mean) ** 2 for m in means) / (DAYS - 1)) ** 0.5
    half = t_quantile(1 - (1 - LEVEL) / 2, DAYS - 1) * sd / DAYS**0.5
    assert (j.scored, j.lift.n, j.days) == (210, 210, DAYS)
    assert j.by_day is not None and j.by_day.n == DAYS
    assert j.by_day.value == pytest.approx(mean)
    assert j.by_day.low == pytest.approx(mean - half) and j.by_day.high == pytest.approx(mean + half)
    assert j.verdict == STANDS
    assert judge(ours, theirs, -1).verdict == FAILS
    at_95 = judge(ours, theirs, 1, level=0.95).lift
    assert at_95.low is not None and j.lift.low is not None and j.lift.low < at_95.low, "98.75% is wider"
    assert judge(ours[:-11], theirs, 1).verdict == TOO_FEW, "199 scored"
    assert judge(*cell(gains[: DAYS - 1]), 1).verdict == TOO_FEW, "nine days"
