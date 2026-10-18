"""Futures contracts by the exchange's expiry rules: the month a day's bars are taken from.

The KIS master files list today's contracts, not past ones (``masters``), so the backfill
builds past codes from the rules. Checked against KIS minute bars on 2026-10-10:

- KOSPI200 futures expire quarterly (March, June, September, December) on the second
  Thursday; USD futures every month on the third Monday.
- An expiry on a day KRX is closed moves to the trading day before: February 2026's third
  Monday fell in the Lunar New Year holidays, and that USD contract last traded on the 13th.
- On its last trading day an expiring contract stops early (KOSPI200 at 15:20, USD at
  11:30), and the next month already trades more. So a day takes the nearest month that
  does not expire that day: the series rolls on the last trading day.

Codes: contracts expiring in 2025 have the old format, ``1`` + product + year letter +
month (``101W12``, ``W`` = 2025); later ones the new, ``A`` + product + the year's last
digit + month (``A01603``, ``A01703``). KIS answers nothing for the other format.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from krx_quant.feeds.base import KST
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
    value_unit: int  # won per unit of the traded value KIS reports


KOSPI200 = Product("KOSPI200F", "01", "F", (3, 6, 9, 12), weekday=3, nth=2, value_unit=1000)
USD = Product("USDF", "75", "CF", tuple(range(1, 13)), weekday=0, nth=3, value_unit=1)


def front_month(product: Product, day: date, calendar: MarketCalendar) -> str:
    """The code of the ``product`` contract ``day``'s bars are taken from: the nearest month
    whose last trading day is after ``day``.
    """
    year, month = day.year, day.month
    while month not in product.months or last_trading_day(product, year, month, calendar) <= day:
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return contract_code(product, year, month)


# The night session ends at 06:00: until then, the night going on is the previous evening's.
NIGHT_END = timedelta(hours=6)


def night_front_month(product: Product, at: datetime, calendar: MarketCalendar) -> str:
    """The code of the ``product`` contract recorded in the night session going on at ``at``,
    or in the next one (#264): the nearest month still trading in it.

    KRX's night session runs from 18:00 to 06:00 the next morning and counts as the next
    trading day's, with its regular session. A contract trades until the end of its last
    trading day's regular session, and the next month starts in the night that begins that
    evening (KRX, OPN01041402). So the night begun on an evening takes the month that
    evening's day takes (``front_month``): the expiring month up to the night before its
    last trading day, the next one from that day's night on. Before 06:00 the night going
    on is the previous evening's.
    """
    evening = (at.astimezone(KST) - NIGHT_END).date()
    return front_month(product, evening, calendar)


def nominal_expiry(product: Product, year: int, month: int) -> date:
    """The rule's expiry day (the nth weekday of the month), before a closed day moves it."""
    first = date(year, month, 1)
    return first + timedelta(days=(product.weekday - first.weekday()) % 7 + 7 * (product.nth - 1))


def last_trading_day(product: Product, year: int, month: int, calendar: MarketCalendar) -> date:
    day = nominal_expiry(product, year, month)
    while not calendar.is_trading_day(day):
        day -= timedelta(days=1)
    return day


def option_expired(year: int, month: int, day: date, calendar: MarketCalendar) -> bool:
    """Whether the KOSPI200 options of ``year``/``month`` expired before ``day``: they last
    trade as the futures do, earlier when the rule's day is closed. On that last day they
    still trade."""
    return last_trading_day(KOSPI200, year, month, calendar) < day


def days_to_expiry(day: date, calendar: MarketCalendar) -> int:
    """Trading days from ``day`` to the next KOSPI200 monthly options expiry (the second
    Thursday, earlier if that is a holiday, as the futures'); 0 on the expiry itself."""
    year, month = day.year, day.month
    while (expiry := last_trading_day(KOSPI200, year, month, calendar)) < day:
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    days, at = 0, day
    while at < expiry:
        at += timedelta(days=1)
        days += calendar.is_trading_day(at)
    return days


def contract_code(product: Product, year: int, month: int) -> str:
    if year >= NEW_CODES_FROM:
        return f"A{product.code}{year % 10}{month:02d}"
    letter = _OLD_YEAR_LETTERS.get(year)
    if letter is None:
        raise ValueError(f"no code known for contracts expiring in {year}")
    return f"1{product.code}{letter}{month:02d}"
