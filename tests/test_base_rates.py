"""The day's prices and the bar rule's labels by period, from one-minute bars (``base_rates.py``):
what the cost map's period base rates (#307), the closing flow (#305) and the structure check read.

Input: hand-made one-minute bars, with every expected value worked out by hand below. Output: the
periods, the day's prices, the labels, the auction days and the shares of pairs that moved together.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from krx_quant.backfill import BAR_SCHEMA, MINUTE_BARS_TR
from krx_quant.base_rates import (
    LISTING,
    PERIODS,
    DayPrices,
    auction_days,
    day_labels,
    day_prices,
    label_shares,
    opposite_share,
    period_of,
    same_share,
)
from krx_quant.feeds import KST
from krx_quant.market_calendar import MarketCalendar
from krx_quant.pricing import Direction
from krx_quant.products import LISTED
from krx_quant.reference import Bar

SYMBOL = "005930"
FEE = 0.00015
DAY = date(2026, 6, 8)  # a Monday after the listing
UP, FLAT, DOWN = Direction.UP, Direction.FLAT, Direction.DOWN


def bar(day: date, hh: int, mm: int, o: int, h: int, low: int, c: int) -> Bar:
    at = datetime.combine(day, time(hh, mm), KST)
    return Bar(start=at, open=o, high=h, low=low, close=c, volume=10, trades=0, first_at=at, last_at=at)


def flat_bar(day: date, hh: int, mm: int, price: int) -> Bar:
    return bar(day, hh, mm, price, price, price, price)


def write_bars(data: Path, day: date, bars: list[Bar]) -> None:
    path = data / "bars" / f"date={day}" / f"feed={MINUTE_BARS_TR}" / f"symbol={SYMBOL}" / "bars.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "symbol": SYMBOL,
            "bar_time": b.start,
            "venue": "UN",
            "open": int(b.open),
            "high": int(b.high),
            "low": int(b.low),
            "close": int(b.close),
            "volume": b.volume,
            "cum_value": 0,
        }
        for b in bars
    ]
    pq.write_table(pa.Table.from_pylist(rows, schema=BAR_SCHEMA), path)


def test_periods_split_at_the_listing_the_rules_and_the_after_market() -> None:
    names = [name for name, _, _ in PERIODS]
    assert period_of(date(2026, 5, 26)) == names[0]
    assert period_of(LISTING) == names[1]
    assert period_of(date(2026, 7, 30)) == names[1]
    assert period_of(date(2026, 7, 31)) == names[2]
    assert period_of(date(2026, 9, 13)) == names[2]
    assert period_of(date(2026, 9, 14)) == names[3]


def test_day_prices_take_the_regular_session_and_the_closing_auction() -> None:
    session = MarketCalendar({DAY: True}).session(DAY)
    assert session is not None
    bars = [
        flat_bar(DAY, 8, 30, 80_000),  # NXT pre-market: not the open
        bar(DAY, 9, 0, 81_000, 81_200, 81_000, 81_100),  # the opening auction's print opens it
        flat_bar(DAY, 14, 59, 81_500),  # the last price 20 minutes before the auction
        flat_bar(DAY, 15, 0, 81_600),
        flat_bar(DAY, 15, 19, 82_000),  # the last price before the auction
        flat_bar(DAY, 15, 30, 82_300),  # the auction's print
        flat_bar(DAY, 15, 45, 82_500),  # NXT after-market: not the close
    ]
    found = day_prices(bars, session)
    assert found is not None
    assert (found.open, found.drift_from, found.pre_close, found.close) == (81_000, 81_500, 82_000, 82_300)
    # A day whose auction printed nothing is left out, not closed at its last trade.
    assert day_prices([b for b in bars if b.start.time() != time(15, 30)], session) is None


def test_day_labels_follow_the_bar_rule_and_the_previous_window() -> None:
    session = MarketCalendar({DAY: True}).session(DAY)
    assert session is not None
    price = 81_200  # tick 100, k 2: the bar rule's targets are 81,500 and 80,900 at a flat close
    bars = [flat_bar(DAY, h, m, price) for h in range(9, 16) for m in range(60) if (h, m) < (15, 20)]
    spike = next(i for i, b in enumerate(bars) if b.start.time() == time(10, 0))
    bars[spike] = bar(DAY, 10, 0, price, price + 300, price, price)
    labels = day_labels(bars, session, FEE)
    # Bar closes 09:01 to 15:16, each with its closing bar and trades in the four minutes after.
    assert len(labels) == 376
    assert Counter(x.label for x in labels) == {FLAT: 372, UP: 4}  # the windows 09:57 to 10:00
    assert {(x.tick, x.k) for x in labels} == {(100, 2)}
    paired = [x for x in labels if x.previous is not None]
    assert len(paired) == 372  # 09:01 to 09:04 have no window four minutes before
    # Wrong: the four up windows (flat before) and the four after them (up before).
    assert sum(x.previous == x.label for x in paired) == 364


def test_the_listing_period_starts_on_the_products_listing() -> None:
    assert LISTING == LISTED


def test_label_shares_count_the_classes_k_and_the_previous_window() -> None:
    session = MarketCalendar({DAY: True}).session(DAY)
    assert session is not None
    price = 81_200
    bars = [flat_bar(DAY, h, m, price) for h in range(9, 16) for m in range(60) if (h, m) < (15, 20)]
    spike = next(i for i, b in enumerate(bars) if b.start.time() == time(10, 0))
    bars[spike] = bar(DAY, 10, 0, price, price + 300, price, price)
    f = label_shares(day_labels(bars, session, FEE))
    assert (f.windows, f.k, f.paired) == (376, 2, 372)
    assert (f.up, f.flat, f.down) == (4 / 376, 372 / 376, 0.0)
    assert f.previous == 364 / 372


def test_an_auction_day_needs_the_trading_day_before_and_its_next_day_the_one_after() -> None:
    days = [date(2026, 6, d) for d in (8, 9, 10, 12)]  # the 11th is a trading day with no prices
    calendar = MarketCalendar({d: True for d in (*days, date(2026, 6, 11))})
    prices = [
        DayPrices(days[0], open=100, drift_from=100, pre_close=100, close=100),
        DayPrices(days[1], open=101, drift_from=100, pre_close=102, close=104),
        DayPrices(days[2], open=103, drift_from=104, pre_close=104, close=102),
        DayPrices(days[3], open=100, drift_from=100, pre_close=100, close=100),
    ]
    found = auction_days(prices, calendar)
    assert [d.day for d in found] == days[1:3], "the 12th's day before (the 11th) has no prices"
    first, second = found
    assert first.move == 104 / 102 - 1 and first.flow == 102 / 100 - 1 and first.drift == 102 / 100 - 1
    assert first.next_gap == 103 / 104 - 1 and first.next_close == 102 / 104 - 1
    assert second.next_gap is None and second.next_close is None, "the 11th is missing after it"


def test_the_shares_count_only_the_pairs_where_both_moved() -> None:
    pairs = [(0.01, 0.02), (0.01, -0.01), (-0.02, -0.01), (0.0, 0.01), (0.01, None), (0.01, 0.0)]
    assert same_share(pairs) == (2 / 3, 3)
    assert opposite_share(pairs) == (1 / 3, 3)
    assert same_share([(0.0, 0.01)]) is None and opposite_share([]) is None
