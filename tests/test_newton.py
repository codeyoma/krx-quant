"""Newton's training: it fits damping and threshold to one-minute bars.

Input: made-up days of bars, turned into samples by the live feature code. Output: the
fitted model's parameters.
"""

from __future__ import annotations

from datetime import date, timedelta

from krx_quant.algorithms.newton import DAMPINGS, Newton
from krx_quant.market_calendar import MarketCalendar
from krx_quant.reference import Bar
from krx_quant.training import samples
from tests.synthetic_bars import random_walk, trending

FEE = 0.015 / 100
DAYS = [date(2026, 9, 14) + timedelta(days=i) for i in range(5)]  # Monday to Friday


def fit(make_day: object) -> dict[str, float]:
    assert callable(make_day)
    bar_days: dict[tuple[str, date], list[Bar]] = {
        ("005930", d): make_day(d, seed=i) for i, d in enumerate(DAYS)
    }
    training = samples(bar_days, MarketCalendar(dict.fromkeys(DAYS, True)), Newton.features, FEE)
    model = Newton().fit(training)
    assert model.version.startswith("newton-20260918-")
    assert model.trained is not None and "2026-09-14..2026-09-18" in model.trained
    return dict(model.params)


def test_a_move_that_keeps_going_is_fitted_with_no_friction() -> None:
    params = fit(trending)

    assert params["damping"] == DAMPINGS[0] == 0.0
    assert params["threshold"] <= 1.0  # a four-tick move is not cut off as noise


def test_noise_is_fitted_with_heavy_friction_or_a_wide_dead_zone() -> None:
    params = fit(random_walk)

    assert params["damping"] >= 1.0 or params["threshold"] >= 1.5, params


def test_a_training_sample_does_not_change_when_the_bars_after_it_are_cut() -> None:
    day = DAYS[0]
    bars = trending(day)
    calendar = MarketCalendar({day: True})
    full = samples({("005930", day): bars}, calendar, Newton.features, FEE)
    for minutes in (30, 120, 300):
        t = full[minutes].bar_close
        cut = [b for b in bars if b.start + timedelta(minutes=1) <= t]

        truncated = samples({("005930", day): cut}, calendar, Newton.features, FEE)

        at_t = next(s for s in truncated if s.bar_close == t)
        assert dict(at_t.features) == dict(full[minutes].features)
        assert at_t.outcome is None  # what happened next is cut too
