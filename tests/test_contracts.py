"""The nearest futures month on a day, by the exchange's expiry rules.

Input: a product, a day and the market calendar. Output: the KIS code of the contract
nearest to expiry that day. The expected codes were checked against KIS minute bars on
2026-10-10: the expiring contract has bars on its last trading day and none after.
"""

from __future__ import annotations

from datetime import date

import pytest

from krx_quant.contracts import KOSPI200, USD, Product, front_month
from krx_quant.market_calendar import MarketCalendar

LUNAR_NEW_YEAR_2026 = {date(2026, 2, 16): False, date(2026, 2, 17): False, date(2026, 2, 18): False}


@pytest.mark.parametrize(
    ("product", "day", "contract"),
    [
        # KOSPI200 futures expire on the second Thursday of March, June, September, December.
        (KOSPI200, date(2026, 3, 11), "A01603"),
        (KOSPI200, date(2026, 3, 12), "A01603"),  # the last trading day
        (KOSPI200, date(2026, 3, 13), "A01606"),
        (KOSPI200, date(2026, 1, 5), "A01603"),  # between expiries: the next quarter
        # USD futures expire monthly, on the third Monday.
        (USD, date(2025, 10, 17), "175W10"),
        (USD, date(2025, 10, 20), "175W10"),
        (USD, date(2025, 10, 21), "175W11"),
        # Contracts expiring in 2025 have the old code format, later ones the new.
        (KOSPI200, date(2025, 12, 11), "101W12"),
        (KOSPI200, date(2025, 12, 12), "A01603"),
        (USD, date(2025, 12, 15), "175W12"),
        (USD, date(2025, 12, 16), "A75601"),
        (KOSPI200, date(2026, 12, 11), "A01703"),  # the year's last digit
    ],
)
def test_the_nearest_month_runs_through_its_last_trading_day(
    product: Product, day: date, contract: str
) -> None:
    assert front_month(product, day, MarketCalendar()) == contract


def test_an_expiry_on_a_closed_day_moves_to_the_trading_day_before() -> None:
    # February 2026's third Monday (the 16th) fell in the Lunar New Year holidays.
    calendar = MarketCalendar(LUNAR_NEW_YEAR_2026)

    assert front_month(USD, date(2026, 2, 13), calendar) == "A75602"
    assert front_month(USD, date(2026, 2, 19), calendar) == "A75603"

    # Say the second Thursday of September 2026 were a holiday.
    calendar = MarketCalendar({date(2026, 9, 10): False})

    assert front_month(KOSPI200, date(2026, 9, 9), calendar) == "A01609"
    assert front_month(KOSPI200, date(2026, 9, 11), calendar) == "A01612"
