"""Futures contracts by the exchange's expiry rules: the nearest month on a day, and its code.

The KIS master files list only the contracts listed today (``masters``), so the backfill
builds past codes from the rules. Checked against KIS minute bars on 2026-10-10:

- KOSPI200 futures expire quarterly (March, June, September, December) on the second
  Thursday; USD futures every month on the third Monday.
- An expiry on a day KRX is closed moves to the trading day before: February 2026's third
  Monday fell in the Lunar New Year holidays, and that USD contract last traded on the 13th.
- The nearest month is a contract through its last trading day, the next one from the
  trading day after. On its last day an expiring contract stops early (KOSPI200 at 15:20,
  USD at 11:30) while the next one trades all day.

Codes: contracts expiring in 2025 have the old format, ``1`` + product + year letter +
month (``101W12``, ``W`` = 2025); later ones the new, ``A`` + product + the year's last
digit + month (``A01603``, ``A01703``). KIS answers nothing for the other format.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from krx_quant.market_calendar import MarketCalendar

NEW_CODES_FROM = 2026  # the first expiry year with new-format codes
# Old-format year letters. KIS keeps about a year of minute bars, so none earlier is needed.
_OLD_YEAR_LETTERS = {2025: "W"}


@dataclass(frozen=True)
class Product:
    series: str  # the continuous series' name in the bars layer
    code: str  # KIS product code inside a contract code
    market: str  # KIS market code for its queries (FID_COND_MRKT_DIV_CODE)
    months: tuple[int, ...]  # the months contracts expire in
    weekday: int  # the expiry's weekday (Monday 0) ...
    nth: int  # ... and which one of the month


KOSPI200 = Product("KOSPI200F", "01", "F", (3, 6, 9, 12), weekday=3, nth=2)
USD = Product("USDF", "75", "CF", tuple(range(1, 13)), weekday=0, nth=3)


def front_month(product: Product, day: date, calendar: MarketCalendar) -> str:
    """The code of ``product``'s contract nearest to expiry on ``day``."""
    year, month = day.year, day.month
    while month not in product.months or last_trading_day(product, year, month, calendar) < day:
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return contract_code(product, year, month)


def last_trading_day(product: Product, year: int, month: int, calendar: MarketCalendar) -> date:
    first = date(year, month, 1)
    day = first + timedelta(days=(product.weekday - first.weekday()) % 7 + 7 * (product.nth - 1))
    while not calendar.is_trading_day(day):
        day -= timedelta(days=1)
    return day


def contract_code(product: Product, year: int, month: int) -> str:
    if year >= NEW_CODES_FROM:
        return f"A{product.code}{year % 10}{month:02d}"
    letter = _OLD_YEAR_LETTERS.get(year)
    if letter is None:
        raise ValueError(f"no code known for contracts expiring in {year}")
    return f"1{product.code}{letter}{month:02d}"
