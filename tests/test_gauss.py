"""Gauss's features on bars worked by hand, and its training on made-up days.

Input: one-minute bars (a Snapshot, or days of them turned into samples). Output: the
feature values, and the fitted model's parameters.

The VWAP example: a pre-market bar (left out), then five bars from the 09:00 open with
typical prices (high + low + close) / 3 and volumes:

| bar | typical | volume | from VWAP |
|---|---|---|---|
| 08:50 | 90,000 | 1,000 | (pre-market) |
| 09:00 | 81,200 | 100 | −80 |
| 09:01 | 81,300 | 300 | +20 |
| 09:02 | 81,400 | 100 | +120 |
| 09:03 | 81,300 | 300 | +20 |
| 09:04 | 81,200 | 200 | −80 |

VWAP = 81,200 + (100·300 + 200·100 + 100·300) / 1,000 = 81,280. The volume-weighted
variance of the typical prices around it is (100·6,400 + 300·400 + 100·14,400 + 300·400 +
200·6,400) / 1,000 = 3,600. Each bar spans 120 won (60 either side of its close), whose
trades spread a further 120² / 12 = 1,200: a standard deviation of √4,800 = 69.28 won.
At a reference price of 81,400 the gap is 120 won = 1.2 ticks, and z = 120 / 69.28 = √3.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

from krx_quant.algorithms.gauss import Gauss
from krx_quant.features import Snapshot, reversion_speed, vwap_gap, vwap_z
from krx_quant.feeds import KST
from krx_quant.market_calendar import MarketCalendar
from krx_quant.reference import Bar
from krx_quant.training import samples
from tests.synthetic_bars import mean_reverting, trending

FEE = 0.015 / 100
DAY = date(2026, 10, 12)
DAYS = [date(2026, 9, 14) + timedelta(days=i) for i in range(5)]  # Monday to Friday
OPEN = datetime.combine(DAY, time(9), KST)


def bar(minute: datetime, high: int, low: int, close: int, volume: int) -> Bar:
    return Bar(minute, close, high, low, close, volume, 1, minute, minute)


def minute(hhmm: str) -> datetime:
    return datetime.combine(DAY, time.fromisoformat(hhmm), KST)


VWAP_BARS = [
    bar(minute("08:50"), 90_000, 90_000, 90_000, 1_000),
    bar(minute("09:00"), 81_260, 81_140, 81_200, 100),
    bar(minute("09:01"), 81_360, 81_240, 81_300, 300),
    bar(minute("09:02"), 81_460, 81_340, 81_400, 100),
    bar(minute("09:03"), 81_360, 81_240, 81_300, 300),
    bar(minute("09:04"), 81_260, 81_140, 81_200, 200),
]


def snapshot(bars: list[Bar], price: float | None = 81_400) -> Snapshot:
    close = bars[-1].start + timedelta(minutes=1)
    return Snapshot.at("005930", close, bars, price, FEE, session_open=OPEN)


def test_the_gap_from_vwap_counts_the_session_from_the_open() -> None:
    s = snapshot(VWAP_BARS)

    assert vwap_gap(s) == pytest.approx(1.2)
    assert vwap_z(s) == pytest.approx(3**0.5)


def closes(ticks: list[float], first_volume: int = 1) -> list[Bar]:
    """Bars from the open closing ``ticks`` above 81,200 (high = low = close), volume 1
    except the first.
    """
    return [
        bar(OPEN + timedelta(minutes=i), c, c, c, first_volume if i == 0 else 1)
        for i, c in enumerate(81_200 + round(100 * t) for t in ticks)
    ]


def test_a_gap_that_halves_every_minute_reverts_at_ln_2_per_minute() -> None:
    # A heavy first bar pins the VWAP at 81,200; the gaps then halve: 64, 32, ..., 0.25 ticks.
    halving = closes([0, 64, 32, 16, 8, 4, 2, 1, 0.5, 0.25], first_volume=1_000_000)

    assert reversion_speed(snapshot(halving)) == pytest.approx(0.6931, abs=1e-3)


def test_gaps_that_flip_sign_revert_fastest_and_gaps_that_grow_not_at_all() -> None:
    zigzag = closes([0, 2] * 5)  # the price crosses its running VWAP every minute
    drifting = closes(list(range(10)))  # it climbs away from it

    assert reversion_speed(snapshot(zigzag)) == 3.0
    assert reversion_speed(snapshot(drifting)) == 0.0
    assert reversion_speed(snapshot(drifting[:9])) is None  # too few bars to tell
    assert reversion_speed(snapshot(closes([0] * 10))) is None  # never strayed


def fit(make_day: object) -> dict[str, float]:
    assert callable(make_day)
    bar_days = {("005930", d): make_day(d, seed=i) for i, d in enumerate(DAYS)}
    training = samples(bar_days, MarketCalendar(dict.fromkeys(DAYS, True)), Gauss.features, FEE)
    model = Gauss().fit(training)
    assert model.version.startswith("gauss-20260918-")
    assert model.trained is not None and "2026-09-14..2026-09-18" in model.trained
    return dict(model.params)


def test_a_price_pulled_back_to_its_level_is_fitted_with_a_reversion_speed() -> None:
    params = fit(mean_reverting)  # pulled back at 0.3 per minute

    assert 0.15 <= params["speed"] <= 0.6, params
    assert params["threshold"] <= 1.0, params  # most gaps are worth acting on


def test_a_price_that_keeps_moving_away_is_fitted_with_no_reversion() -> None:
    params = fit(trending)

    assert params["speed"] == 0.0, params


def test_training_leaves_the_pre_market_out_of_the_vwap_too() -> None:
    day = DAYS[0]
    bars = mean_reverting(day)
    pre_market = [
        bar(datetime.combine(day, time(8, m), KST), 90_000, 90_000, 90_000, 50_000) for m in (0, 30)
    ]
    calendar = MarketCalendar({day: True})

    def vwap_features(day_bars: list[Bar]) -> list[dict[str, float | None]]:
        training = samples(
            {("005930", day): day_bars}, calendar, ("vwap_gap", "vwap_z", "reversion_speed"), FEE
        )
        return [dict(s.features) for s in training]

    assert vwap_features(pre_market + bars) == vwap_features(bars)
