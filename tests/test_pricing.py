"""Flat band: how many ticks a move must cover to beat the round-trip cost.

Expected values are worked by hand from the KRX tick table (unified for KOSPI and
KOSDAQ since 2023-01-25), the 0.20% sell tax and a 0.015% commission per side:
cost = price × (0.20% + 2 × 0.015%) = price × 0.23%, k = cost / tick rounded up.
"""

from __future__ import annotations

import pytest

from krx_quant.pricing import flat_ticks, tick_size

FEE = 0.015 / 100


@pytest.mark.parametrize(
    ("price", "tick"),
    [
        (1_999, 1),
        (2_000, 5),
        (4_995, 5),
        (5_000, 10),
        (19_990, 10),
        (20_000, 50),
        (49_950, 50),
        (50_000, 100),
        (199_900, 100),
        (200_000, 500),
        (499_500, 500),
        (500_000, 1_000),
        # A mid-point between two ticks takes the tick of its band.
        (263_250, 500),
    ],
)
def test_tick_size_follows_the_krx_price_bands(price: float, tick: int) -> None:
    assert tick_size(price) == tick


@pytest.mark.parametrize(
    ("price", "k"),
    [
        (1_500, 4),  # cost 3.45 won, tick 1
        (3_000, 2),  # cost 6.90, tick 5
        (10_000, 3),  # cost 23, tick 10
        (30_000, 2),  # cost 69, tick 50
        (81_200, 2),  # cost 186.76, tick 100
        (263_000, 2),  # cost 604.9, tick 500
        (700_000, 2),  # cost 1,610, tick 1,000
        (100_000, 3),  # cost 230, tick 100
    ],
)
def test_flat_band_covers_the_round_trip_cost(price: float, k: int) -> None:
    assert flat_ticks(price, FEE) == k


def test_a_cost_of_exactly_whole_ticks_needs_no_extra_tick() -> None:
    # No commission: cost = 50,000 × 0.20% = 100 won, exactly one 100-won tick.
    assert flat_ticks(50_000, 0.0) == 1
    # 200,000 × 0.25% = 500 won, exactly one 500-won tick.
    assert flat_ticks(200_000, 0.025 / 100) == 1
