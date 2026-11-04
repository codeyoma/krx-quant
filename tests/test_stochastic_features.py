"""The stochastic processes' own features (#518) on bars worked by hand.

Input: one-minute bars from the 09:00 open (a pre-market bar before it is left out), at a
reference price of 81,400 (a tick of 100 won). Output: the feature values.

The bars (ticks above 81,200; volume):

| bar | high | low | close | volume |
|---|---|---|---|---|
| 08:50 | 90,000 | 90,000 | 90,000 | 5,000 (pre-market) |
| 09:00 | +1 | −1 | 0 | 100 |
| 09:01 | +2 | 0 | +1 | 300 |
| 09:02 | — | — | — | (no trade) |
| 09:03 | +1 | −2 | −1 | 200 |
| 09:04 | 0 | −2 | −1 | 400 |

The bar close is 09:05. The minutes after the first bar: 09:01 changes +1 (high +2, low 0
against 09:00's close), 09:02 none (0, 0, 0), 09:03 −2 (high 0, low −3), 09:04 0 (high +1,
low −1). The hour's volume a minute: (300 + 0 + 200 + 400) ÷ 4 = 225.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta

import pytest

from krx_quant.features import (
    MIN_SEMIVOL_MOVES,
    SERIES_FEATURES,
    STOCHASTIC_FEATURES,
    Snapshot,
    minute_highs,
    minute_lows,
    minute_ticks,
    minute_volumes,
    parkinson_5,
    semivol_down,
    semivol_up,
    session_drift,
    session_sigma,
    tick_bp,
)
from krx_quant.feeds import KST
from krx_quant.reference import Bar

FEE = 0.015 / 100
DAY = date(2026, 10, 12)
OPEN = datetime.combine(DAY, time(9), KST)
BASE = 81_200
TICK = 100


def at(hhmm: str) -> datetime:
    return datetime.combine(DAY, time.fromisoformat(hhmm), KST)


def bar(minute: str, high: int, low: int, close: int, volume: int) -> Bar:
    start = at(minute)
    return Bar(
        start,
        BASE + close * TICK,
        BASE + high * TICK,
        BASE + low * TICK,
        BASE + close * TICK,
        volume,
        1,
        start,
        start,
    )


BARS = [
    Bar(at("08:50"), 90_000, 90_000, 90_000, 90_000, 5_000, 1, at("08:50"), at("08:50")),
    bar("09:00", 1, -1, 0, 100),
    bar("09:01", 2, 0, 1, 300),
    bar("09:03", 1, -2, -1, 200),
    bar("09:04", 0, -2, -1, 400),
]


def snapshot(bars: list[Bar] = BARS, price: float | None = 81_400) -> Snapshot:
    return Snapshot.at("005930", at("09:05"), bars, price, FEE, session_open=OPEN)


def test_the_minutes_series_line_up_with_minute_ticks_and_leave_the_pre_market_out() -> None:
    s = snapshot()

    assert minute_ticks(s) == (1.0, 0.0, -2.0, 0.0)
    assert minute_highs(s) == (2.0, 0.0, 0.0, 1.0)
    assert minute_lows(s) == (0.0, 0.0, -3.0, -1.0)
    assert minute_volumes(s) == pytest.approx((300 / 225, 0.0, 200 / 225, 400 / 225))
    assert {"minute_highs", "minute_lows", "minute_volumes"} <= SERIES_FEATURES & STOCHASTIC_FEATURES


def test_the_series_keep_the_last_half_hour() -> None:
    bars = [bar(f"{9 + m // 60:02d}:{m % 60:02d}", m % 3, -(m % 2), m % 2, 100) for m in range(50)]
    s = Snapshot.at("005930", at("09:50"), bars, BASE, FEE, session_open=OPEN)

    assert len(minute_ticks(s)) == 49
    assert len(minute_highs(s)) == len(minute_lows(s)) == len(minute_volumes(s)) == 30


def test_the_geometric_brownian_motion_s_drift_and_sigma_are_the_log_returns_mean_and_deviation() -> None:
    returns = [1e4 * math.log(81_300 / 81_200), 0.0, 1e4 * math.log(81_100 / 81_300), 0.0]
    # Six moves at least: add two more quiet minutes before the bar close.
    s = Snapshot.at("005930", at("09:07"), BARS, 81_400, FEE, session_open=OPEN)
    moves = [*returns, 0.0, 0.0]
    mean = sum(moves) / 6

    assert session_drift(s) == pytest.approx(mean)
    assert session_sigma(s) == pytest.approx(math.sqrt(sum((m - mean) ** 2 for m in moves) / 5))
    assert session_drift(snapshot()) is None  # four moves: too few


def test_a_tick_as_a_log_return() -> None:
    assert tick_bp(snapshot()) == pytest.approx(1e4 * math.log(1 + 100 / 81_400))
    assert tick_bp(snapshot(price=None)) is None


def test_the_semideviations_split_the_last_changes_by_side() -> None:
    steps = [1, -2, 0, 1, 0, -1, 2, 0, 0, -1, 1, 0]  # 12 changes after the first bar
    level, bars = 0, [bar("09:00", 0, 0, 0, 100)]
    for m, step in enumerate(steps, start=1):
        level += step
        bars.append(bar(f"09:{m:02d}", level, level, level, 100))
    s = Snapshot.at("005930", at("09:13"), bars, BASE, FEE, session_open=OPEN)

    ups = sum(c * c for c in steps if c > 0)
    downs = sum(c * c for c in steps if c < 0)
    assert semivol_up(s) == pytest.approx(math.sqrt(2 * ups / 12))
    assert semivol_down(s) == pytest.approx(math.sqrt(2 * downs / 12))
    assert len(steps) >= MIN_SEMIVOL_MOVES
    assert semivol_up(snapshot()) is None  # four changes


def test_parkinson_s_volatility_counts_a_quiet_minute_as_no_range() -> None:
    # The last five minutes, 09:00–09:04: ranges of 2, 2, (none), 3 and 2 ticks.
    expected = math.sqrt((4 + 4 + 0 + 9 + 4) / 5 / (4 * math.log(2)))

    assert parkinson_5(snapshot()) == pytest.approx(expected)
    assert parkinson_5(snapshot(price=None)) is None


def test_no_bars_no_values() -> None:
    s = snapshot(bars=[])

    assert minute_highs(s) == minute_lows(s) == minute_volumes(s) == ()
    assert session_drift(s) is None and semivol_up(s) is None and parkinson_5(s) is None


def test_the_hour_volume_must_have_traded() -> None:
    quiet = [bar("09:00", 0, 0, 0, 0), bar("09:01", 0, 0, 0, 0)]
    s = Snapshot.at("005930", at("09:02"), quiet, BASE, FEE, session_open=OPEN)

    assert minute_volumes(s) == ()
    assert minute_highs(s) == (0.0,)


def test_the_bar_close_s_last_minutes_without_a_trade_are_quiet_minutes() -> None:
    s = Snapshot.at("005930", at("09:05") + timedelta(minutes=2), BARS, 81_400, FEE, session_open=OPEN)

    assert minute_highs(s)[-2:] == (0.0, 0.0)
    assert minute_volumes(s)[-2:] == (0.0, 0.0)
