"""Granger's features on bars made by hand, and its training on made-up samples.

Input: one-minute bars of the predicted symbol and of the series that may lead it (a
Snapshot), or samples whose outcomes follow a leader. Output: the feature values, and the
fitted model's parameters.

A leader's move is its one-minute log return, in standard deviations of its last 20. The
example leader alternates between returns of +a and −a: the mean of 20 is 0 and their
standard deviation is a·√(20/19), so its latest return is ±√(19/20) = ±0.9747 of them.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta

import pytest

from krx_quant.algorithms import Sample
from krx_quant.algorithms.granger import Granger
from krx_quant.features import FEATURES, FeatureVector, Leaders, Snapshot
from krx_quant.feeds import KST
from krx_quant.pricing import classify
from krx_quant.reference import Bar

FEE = 0.015 / 100
DAY = date(2026, 10, 12)
OPEN = datetime.combine(DAY, time(9), KST)
CLOSE = OPEN + timedelta(minutes=30)  # the bar close the features are computed at
ALTERNATING = math.sqrt(19 / 20)


def minute(m: int) -> datetime:
    """The start of the bar ``m`` minutes after the open."""
    return OPEN + timedelta(minutes=m)


def bars(closes: dict[int, float]) -> list[Bar]:
    """One bar per ``minute: close`` (high = low = close)."""
    return [Bar(minute(m), c, c, c, c, 1, 1, minute(m), minute(m)) for m, c in sorted(closes.items())]


def alternating(first: int = 0, last: int = 29, a: float = 0.001, up_last: bool = True) -> dict[int, float]:
    """Closes whose log return alternates between +a and −a, the last one up (or down)."""
    closes, level = {}, 0.0
    for m in range(first, last + 1):
        steps_from_last = last - m
        if m > first:
            up = (steps_from_last % 2 == 0) == up_last
            level += a if up else -a
        closes[m] = 100 * math.exp(level)
    return closes


def lead(snapshot: Snapshot, name: str) -> float | None:
    return FEATURES[name](snapshot)


def snapshot(
    leaders: Mapping[str, Sequence[Sequence[Bar]]], own: Sequence[Bar] = (), at: datetime = CLOSE
) -> Snapshot:
    return Snapshot.at("005930", at, own, 81_200, FEE, session_open=OPEN, leaders=leaders)


def test_a_leaders_last_moves_are_measured_in_its_own_standard_deviations() -> None:
    s = snapshot({"kospi200f": [bars(alternating())]})

    assert lead(s, "lead_kospi200f_1") == pytest.approx(ALTERNATING)
    assert lead(s, "lead_kospi200f_2") == pytest.approx(-ALTERNATING)
    assert lead(s, "lead_kospi200f_3") == pytest.approx(ALTERNATING)
    # The symbol's own moves are measured the same way.
    own = snapshot({}, own=bars(alternating(up_last=False)))
    assert lead(own, "return_1") == pytest.approx(-ALTERNATING)


def test_a_minute_without_a_trade_is_no_move() -> None:
    closes = alternating()
    del closes[28]  # no trade in the bar before the last: its close carries over
    s = snapshot({"usdf": [bars(closes)]})

    assert lead(s, "lead_usdf_2") == 0.0
    # The last two minutes together moved −a + a = 0; the 18 before still alternate, so the
    # standard deviation of the 20 returns is a·√(18/19).
    assert lead(s, "lead_usdf_1") == 0.0
    assert lead(s, "lead_usdf_3") == pytest.approx(math.sqrt(19 / 18))


def test_a_group_averages_the_members_that_say_something() -> None:
    up, down = bars(alternating()), bars(alternating(up_last=False))
    silent: list[Bar] = []
    stale = bars(alternating(last=18))  # its last bar ended 11 minutes before the close

    assert lead(snapshot({"etfs": [up, down]}), "lead_etfs_1") == pytest.approx(0.0)
    assert lead(snapshot({"us": [up, silent, stale]}), "lead_us_1") == pytest.approx(ALTERNATING)
    assert lead(snapshot({"us": [silent, stale]}), "lead_us_1") is None
    assert lead(snapshot({}), "lead_indices_1") is None


def test_too_few_minutes_say_nothing_yet() -> None:
    five_returns = bars(alternating(first=24))

    assert lead(snapshot({"kospi200f": [five_returns]}), "lead_kospi200f_1") is None
    assert lead(snapshot({"kospi200f": [bars(alternating(first=23))]}), "lead_kospi200f_1") is not None


def test_the_peers_are_the_other_predicted_symbols() -> None:
    mine, theirs = bars(alternating(up_last=False)), bars(alternating())
    leaders = Leaders(indices=("0013",), us=("NVDA",), etfs=("133690",))

    groups = leaders.groups("005930", {"005930": mine, "000660": theirs}, {"0013": theirs})

    assert groups["peers"] == [theirs]
    assert groups["indices"] == [theirs]
    assert groups["us"] == [[]] and groups["etfs"] == [[]], "a series with no bars yet"
    assert lead(snapshot(groups, own=mine), "lead_peers_1") == pytest.approx(ALTERNATING)


VOLATILITY = 2.0  # ticks a minute
FLAT = 1  # ticks


def vector(values: dict[str, float | None]) -> FeatureVector:
    every = dict.fromkeys(Granger.features) | {"volatility": VOLATILITY, "flat_ticks": float(FLAT)}
    return FeatureVector(every | values, Granger.features)


def made_up_samples(n: int = 4000, seed: int = 0) -> list[Sample]:
    """Four-minute moves that follow KOSPI200 futures' move two minutes before (0.5 of it, in
    the symbol's own standard deviations) plus noise of 1. Every other group with history
    moves at random; the indices and US trades have none (no backfill).
    """
    rng = random.Random(seed)
    out = []
    for i in range(n):
        values: dict[str, float | None] = {
            f"{name}_{lag}": rng.gauss(0, 1)
            for name in ("return", "lead_peers", "lead_kospi200f", "lead_usdf", "lead_etfs")
            for lag in (1, 2, 3)
        }
        move = 0.5 * (values["lead_kospi200f_2"] or 0.0) + rng.gauss(0, 1)
        change = VOLATILITY * move
        at = datetime.combine(date(2026, 9, 14) + timedelta(days=i // 400), time(9, 1), KST)
        outcome = classify(change, FLAT, 1)
        out.append(Sample("005930", at + timedelta(minutes=i % 400), vector(values), outcome, change))
    return out


def test_training_keeps_the_leader_that_moves_first_at_its_lag() -> None:
    model = Granger().fit(made_up_samples())

    assert model.version.startswith("granger-")
    params = dict(model.params)
    assert params["lead_kospi200f_2"] == pytest.approx(0.5, abs=0.05)
    assert params["lead_kospi200f_1"] == pytest.approx(0.0, abs=0.05)
    assert params["lead_kospi200f_3"] == 0.0, "two lags are enough"
    assert params["residual"] == pytest.approx(1.0, abs=0.05)
    kept = {name for name, weight in params.items() if weight != 0}
    assert kept == {"lead_kospi200f_1", "lead_kospi200f_2", "residual"}, "the noise and what has no history"


def test_a_missing_leader_counts_as_no_move_and_widens_the_forecast() -> None:
    granger = Granger()
    model = granger.fit(made_up_samples())

    led = granger.predict(vector({"lead_kospi200f_1": 0.0, "lead_kospi200f_2": 1.0}), model)
    still = granger.predict(vector({"lead_kospi200f_1": 0.0, "lead_kospi200f_2": 0.0}), model)
    unknown = granger.predict(vector({}), model)

    assert led.expected_ticks == pytest.approx(VOLATILITY * 0.5, abs=0.1)
    assert led.p_up > led.p_down
    assert unknown.expected_ticks == 0.0
    assert unknown.p_flat < still.p_flat, "less known, a wider spread"


def test_before_training_it_expects_no_move() -> None:
    granger = Granger()
    forecast = granger.predict(vector({"lead_kospi200f_2": 3.0}), granger.default_model())

    assert forecast.expected_ticks == 0.0
    assert forecast.p_up == pytest.approx(forecast.p_down)
    assert granger.predict(vector({"volatility": None}), granger.default_model()) == granger.predict(
        vector({"flat_ticks": None}), granger.default_model()
    )
