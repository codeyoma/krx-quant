"""The barrier (#183): its width, its answer, its bar rule, its output contract, its P&L, its
statistics and its two baselines, by hand and through the Replay Harness; training on its
answers; its past answers filled in.

Prices: at 81,250 (bid 81,200, ask 81,300) the tick is 100 and the flat band k = 2 (the cost
81,250 × 0.23% = 186.875 won, 1.86875 ticks, covers two). With w = 2 the barriers are U = 81,450
and L = 81,050, half a tick off the grid: the best bid reaches U at 81,500 and the best ask L at
81,000. At 81,300 (a whole tick) w = 2 puts U at 81,500 itself.

A width from two one-minute changes a and b is worked out by hand: the exponentially weighted
standard deviation of two values is |a − b| ÷ √2 whatever the weights, so σ̂₄ = 2 × |a − b| ÷ √2
= √2 × |a − b| ticks.
"""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from krx_quant.algorithms import KIND_ALGORITHMS
from krx_quant.algorithms.contract import KindSample, Model
from krx_quant.algorithms.turing import Turing
from krx_quant.backfill import BAR_SCHEMA, MINUTE_BARS_TR, PreviousDay, bars_key
from krx_quant.barriers import (
    AMBIGUOUS,
    BARRIER,
    CLIMATE,
    PERSIST,
    SAME_MINUTE,
    BarrierClimate,
    BarrierPersist,
    bar_barrier,
    barrier_profit,
    barrier_stats,
)
from krx_quant.clock import FakeClock
from krx_quant.config import Settings
from krx_quant.engine import Event
from krx_quant.features import (
    BARRIER_FEATURES,
    FEATURE_MEANINGS,
    FEATURES,
    FeatureVector,
    Leaders,
    Snapshot,
    Width,
    barrier_width,
    ewm_std,
)
from krx_quant.kind_registry import KINDS
from krx_quant.kind_stats import summarize
from krx_quant.kinds import DIRECTION_KIND, Answer, Followed, KindForecast, KindInputs, KindScored, QuoteLog
from krx_quant.market_calendar import MarketCalendar
from krx_quant.predictions import DB_NAME, PredictionStore
from krx_quant.pricing import cost_ticks, flat_ticks, tick_size
from krx_quant.ranges import RANGE
from krx_quant.reference import Bar
from krx_quant.replay import replay_into
from krx_quant.rescore import fill_kind_answers
from krx_quant.touch import MISSING_MID, REST_QUOTES, RESTARTED, SILENT
from krx_quant.training import bar_answers, training_day
from tests.api_schema import check
from tests.replay_events import CALENDAR, DAY, FEE, SYMBOL, api, at, get_json, run, steady_book, trade
from tests.test_replay import busy_morning, stored
from tests.test_replay_cli import END_OF_DAY, record
from tests.test_rescore import NEXT_MORNING, day_events, server_db

T = at("09:31:00")
FRIDAY = date(2026, 10, 9)  # the trading day before DAY
COST = 81_250 * 0.0023 / 100  # 1.86875 ticks at 81,250


def bar(
    minute: str,
    close: float,
    day: date = DAY,
    *,
    high: float | None = None,
    low: float | None = None,
    opened: float | None = None,
) -> Bar:
    """A one-minute bar from ``minute`` (HH:MM) closing at ``close``, opening there too unless
    ``opened`` says otherwise."""
    start = at(f"{minute}:00", day)
    return Bar(
        start,
        opened if opened is not None else close,
        high if high is not None else close,
        low if low is not None else close,
        close,
        10,
        1,
        start,
        start,
    )


# The width, by hand.


def test_the_exponentially_weighted_deviation_as_worked_out_by_hand() -> None:
    """Input: 0, 0, 3 at span 3 (each older value half the next: weights ¼, ½, 1). Output: the
    weighted mean 3 ÷ 1.75 = 12/7; the weighted squares ¾·(12/7)² + (9/7)² = 27/7, scaled by
    W ÷ (W² − Σw²) = 1.75 ÷ (3.0625 − 1.3125) = 1: a variance of 27/7. Two values give their gap
    over √2 at any span; one gives nothing."""
    assert ewm_std([0.0, 0.0, 3.0], 3) == pytest.approx(math.sqrt(27 / 7))
    assert ewm_std([2.0, -1.0], 100) == pytest.approx(3 / math.sqrt(2))
    assert ewm_std([4.0], 100) is None


def two_changes(a: int, b: int, fillers: int = 16) -> list[Bar]:
    """Bars at 81,200 with two one-minute changes: ``a`` ticks from 09:00 to 09:01 and ``b``
    from 09:03 to 09:04, then ``fillers`` bars two minutes apart from 09:06 (no change between
    them): 4 + ``fillers`` bars."""
    out = [
        bar("09:00", 81_200),
        bar("09:01", 81_200 + a * 100),
        bar("09:03", 81_200),
        bar("09:04", 81_200 + b * 100),
    ]
    return out + [bar(f"{9 + (6 + 2 * i) // 60:02d}:{(6 + 2 * i) % 60:02d}", 81_200) for i in range(fillers)]


def width_of(
    bars: Sequence[Bar], previous: Sequence[Bar] = (), price: float | None = 81_250.0
) -> Width | None:
    k = flat_ticks(price, FEE) if price is not None else None
    return barrier_width(bars, previous, at("09:00:00"), price, k)


@pytest.mark.parametrize(
    ("a", "b", "w", "sigma"),
    [
        # +3 and −3: σ̂₄ = √2 × 6 = 8.49 → 8.
        (3, -3, 8, 6 * math.sqrt(2)),
        # +1 and −1: 2.83 → 3 (rounded up).
        (1, -1, 3, 2 * math.sqrt(2)),
        # +2 and −1: 4.24 → 4 (rounded down).
        (2, -1, 4, 3 * math.sqrt(2)),
        # +1 and 0: 1.41 → 1, below k = 2: 2.
        (1, 0, 2, math.sqrt(2)),
        # Nothing moved: σ̂₄ = 0, w = k.
        (0, 0, 2, 0.0),
    ],
)
def test_the_width_is_the_four_minute_deviation_rounded_never_below_k(
    a: int, b: int, w: int, sigma: float
) -> None:
    width = width_of(two_changes(a, b))

    assert width is not None and width.sigma is not None
    assert width.ticks == w and width.sigma == pytest.approx(sigma)


def test_with_fewer_than_twenty_bars_or_two_changes_the_width_is_k() -> None:
    """Input: 19 bars (two changes); 20 bars two minutes apart (no one-minute change); no
    reference price. Output: w = k = 2 without σ̂₄; nothing without a price."""
    assert width_of(two_changes(3, -3, fillers=15)) == Width(2, None)
    apart = [bar(f"{9 + 2 * i // 60:02d}:{2 * i % 60:02d}", 81_200 + 300 * (i % 2)) for i in range(20)]
    assert width_of(apart) == Width(2, None)
    assert width_of(two_changes(3, -3), price=None) is None


def test_the_previous_trading_day_s_bars_carry_the_width_into_the_open() -> None:
    """Input: Friday's 18 bars, one change of +3 ticks (13:00 → 13:01, then bars two minutes
    apart from 14:00); today's NXT pre-market bar at 08:59 (80,000), then 09:00 (81,000) and
    09:01 (80,700): −3. Output: 20 bars, the two changes +3 and −3: w = 8. The pre-market bar is
    not the session's (it would add +10), and the night is no change (81,200 → 81,000)."""
    friday = [bar("13:00", 81_200, FRIDAY), bar("13:01", 81_500, FRIDAY)]
    friday += [bar(f"14:{2 * i:02d}", 81_200, FRIDAY) for i in range(16)]
    today = [bar("08:59", 80_000), bar("09:00", 81_000), bar("09:01", 80_700)]

    width = width_of(today, friday)

    assert width is not None and width.sigma is not None
    assert width.ticks == 8 and width.sigma == pytest.approx(6 * math.sqrt(2))
    assert width_of(today) == Width(2, None), "without Friday: three bars"


def test_the_latest_changes_weigh_most() -> None:
    """Input: 21 consecutive bars, one five-tick jump among nineteen still minutes: the first
    change, or the last. Output: the late jump makes the wider barrier."""
    early = [bar("09:00", 81_200)] + [bar(f"09:{m:02d}", 81_700) for m in range(1, 21)]
    late = [bar(f"09:{m:02d}", 81_200) for m in range(20)] + [bar("09:20", 81_700)]

    first, last = width_of(early), width_of(late)

    assert first is not None and last is not None
    assert first.sigma is not None and last.sigma is not None and first.sigma < last.sigma
    assert first.ticks <= last.ticks


def test_the_width_is_two_features_only_the_barrier_reads() -> None:
    """Input: the bars of +3 and −3 at 09:37. Output: ``barrier_width`` 8 and ``barrier_sigma``
    8 ÷ (6√2) = 0.943; at k (+1, 0: σ̂₄ = √2) 2 ÷ √2; unknown without σ̂₄. Turing, which reads
    every number of the Feature Store, reads neither: the direction predicts as it did."""

    def snapshot(bars: list[Bar]) -> Snapshot:
        return Snapshot.at(SYMBOL, at("09:37:00"), bars, 81_250.0, FEE, session_open=at("09:00:00"))

    wide = snapshot(two_changes(3, -3))
    assert FEATURES["barrier_width"](wide) == 8.0
    assert FEATURES["barrier_sigma"](wide) == pytest.approx(8 / (6 * math.sqrt(2)))
    assert FEATURES["barrier_sigma"](snapshot(two_changes(1, 0))) == pytest.approx(2 / math.sqrt(2))
    few = snapshot(two_changes(3, -3, fillers=10))
    assert (FEATURES["barrier_width"](few), FEATURES["barrier_sigma"](few)) == (2.0, None)
    assert BARRIER_FEATURES == {"barrier_width", "barrier_sigma"}
    assert BARRIER_FEATURES <= set(FEATURE_MEANINGS)
    assert not BARRIER_FEATURES & set(Turing.features)


# The answer, by hand.


def window(
    quotes: Sequence[tuple[str, float, float]],
    anchor: float | None = 81_250.0,
    *,
    w: int = 2,
    spread: float = 100,
    every: int = 30,
    until: str = "09:34:59",
    rest_at: str | None = None,
    trades: Sequence[tuple[str, float]] = (),
) -> Followed:
    """The window of the bar close T (09:31) from ``anchor`` with the width ``w``: the book
    around the anchor (``spread`` wide) every ``every`` seconds from T to ``until``, and the
    (time, bid, ask) ``quotes`` received besides, as the Market View logs them; the trades given;
    a REST snapshot at ``rest_at``. The best quote before T: 81,200 / 81,300."""
    log = QuoteLog()
    received: list[tuple[datetime, float, float]] = []
    if anchor is not None:
        when = T
        while when <= at(until):
            received.append((when, anchor - spread / 2, anchor + spread / 2))
            when += timedelta(seconds=every)
    received += [(at(t), bid, ask) for t, bid, ask in quotes]
    for when, bid, ask in sorted(received, key=lambda q: q[0]):
        log.quote(when, bid, ask, None, None, False)
    for when_text, price in trades:
        log.trade(at(when_text), price)
    if rest_at is not None:
        log.quote(at(rest_at), 81_200, 81_300, None, None, True)
    tick = tick_size(anchor) if anchor is not None else None
    k = flat_ticks(anchor, FEE) if anchor is not None else None
    cost = cost_ticks(anchor, FEE) if anchor is not None else None
    width = Width(w, 1.9) if anchor is not None else None
    return log.window(SYMBOL, T, anchor, tick, k, 81_200, 81_300, cost=cost, width=width)


UP_QUOTE = ("09:32:00", 81_500, 81_600)  # the bid at U (81,450 at 81,250, w = 2)
DOWN_QUOTE = ("09:33:00", 80_900, 81_000)  # the ask at L (81,050)


@pytest.mark.parametrize(
    ("quotes", "options", "label", "up_at", "down_at"),
    [
        # The bid reaches U (81,500 ≥ 81,450).
        ([UP_QUOTE], {}, "up_first", "09:32:00", None),
        # The mid-point reaches U (81,400 / 81,500: 81,450) but the bid does not.
        ([("09:32:00", 81_400, 81_500)], {}, "timeout", None, None),
        # The ask reaches L.
        ([DOWN_QUOTE], {}, "down_first", None, "09:33:00"),
        # Both: the first received wins, either way round.
        ([UP_QUOTE, DOWN_QUOTE], {}, "up_first", "09:32:00", "09:33:00"),
        (
            [("09:32:00", 80_900, 81_000), ("09:33:00", 81_500, 81_600)],
            {},
            "down_first",
            "09:33:00",
            "09:32:00",
        ),
        # Neither.
        ([], {}, "timeout", None, None),
        # A crossed quote (ask below bid) is dropped.
        ([("09:32:00", 81_600, 81_500)], {}, "timeout", None, None),
        # The ends: a quote received at T counts; one at T + 4 minutes does not.
        ([("09:31:00", 81_500, 81_600)], {}, "up_first", "09:31:00", None),
        ([("09:35:00", 81_500, 81_600)], {}, "timeout", None, None),
        # A whole-tick reference (81,300, the book 81,200 / 81,400): U = 81,500, reached at it.
        ([("09:32:00", 81_500, 81_600)], {"anchor": 81_300.0, "spread": 200}, "up_first", "09:32:00", None),
        ([("09:32:00", 81_400, 81_500)], {"anchor": 81_300.0, "spread": 200}, "timeout", None, None),
        # A wider barrier (w = 3: U = 81,550): 81,500 no longer reaches it, 81,600 does.
        ([UP_QUOTE], {"w": 3}, "timeout", None, None),
        ([("09:32:00", 81_600, 81_700)], {"w": 3}, "up_first", "09:32:00", None),
    ],
)
def test_the_answer_is_the_barrier_the_quotes_reach_first_as_worked_out_by_hand(
    quotes: list[tuple[str, float, float]],
    options: dict[str, Any],
    label: str,
    up_at: str | None,
    down_at: str | None,
) -> None:
    answer = BARRIER.answer_to(window(quotes, **options))

    assert (answer.label, answer.unscorable) == (label, None)
    assert answer.path["up_at"] == (None if up_at is None else at(up_at).isoformat())
    assert answer.path["down_at"] == (None if down_at is None else at(down_at).isoformat())
    assert answer.path["silent"] is False


def test_a_trade_at_the_barrier_is_not_a_touch() -> None:
    """Input: a trade printed at 81,500 (above U) while the best bid stayed at 81,200. Output:
    timeout: one could not have sold at U at the market."""
    answer = BARRIER.answer_to(window([], trades=[("09:32:00", 81_500)]))

    assert (answer.label, answer.path["up_at"]) == ("timeout", None)


def test_both_barriers_first_reached_at_the_same_received_time_cannot_be_scored() -> None:
    """Input: the bid at U and the ask at L in two quotes received at 09:32:00 exactly. Output:
    no class (``ambiguous``): the order is unknown; both times kept."""
    answer = BARRIER.answer_to(window([UP_QUOTE, ("09:32:00", 80_900, 81_000)]))

    assert (answer.label, answer.unscorable) == (None, AMBIGUOUS)
    assert answer.path["up_at"] == answer.path["down_at"] == at("09:32:00").isoformat()


def test_the_path_keeps_the_width_the_barriers_and_the_quotes_at_each_touch() -> None:
    """Input: U reached at 09:32:00 (81,500 / 81,600), then L at 09:33:00 (80,900 / 81,000), the
    book at 81,200 / 81,300 every 30 s to 09:34:30. Output: every field of the path."""
    path = BARRIER.answer_to(window([UP_QUOTE, DOWN_QUOTE])).path

    assert path == {
        "w": 2,
        "sigma": 1.9,
        "k": 2,
        "tick": 100,
        "anchor": 81_250.0,
        "upper": 81_450.0,
        "lower": 81_050.0,
        "cost": pytest.approx(COST),
        "bid_start": 81_200,
        "ask_start": 81_300,
        "up_at": at("09:32:00").isoformat(),
        "up_bid": 81_500,
        "up_ask": 81_600,
        "down_at": at("09:33:00").isoformat(),
        "down_bid": 80_900,
        "down_ask": 81_000,
        "bid_end": 81_200.0,
        "ask_end": 81_300.0,
        "quotes": 8 + 2,
        "silent": False,
    }
    check("barrier_path", path)


@pytest.mark.parametrize(
    ("built", "why"),
    [
        # 60 s without a quote: the book every 30 s, none from 09:32:30 to 09:33:30.
        (lambda: window([UP_QUOTE], until="09:32:30"), SILENT),
        # A REST snapshot of the book in the window.
        (lambda: window([UP_QUOTE], rest_at="09:32:10"), REST_QUOTES),
        # No mid-point at the bar close: no barriers either.
        (lambda: window([UP_QUOTE], anchor=None), MISSING_MID),
    ],
)
def test_a_window_the_shared_rules_cannot_score_has_no_barrier_class(built: Any, why: str) -> None:
    answer = BARRIER.answer_to(built())

    assert (answer.label, answer.unscorable) == (None, why)


# The bar rule, by hand: the close 81,200 (tick 100, k 2), w = 2.

W2 = Width(2, 1.9)


@pytest.mark.parametrize(
    ("closing", "after", "label", "upper", "lower", "minute"),
    [
        # One price, the next open the same (side 0): U 81,200 + 3 ticks, L − 3. 09:32 reaches U.
        (
            bar("09:30", 81_200),
            [bar("09:31", 81_200, high=81_400), bar("09:32", 81_300, high=81_500)],
            "up_first",
            81_500,
            80_900,
            "09:32",
        ),
        # Closed at its low (side −1, on the bid: the mid-point above it): U one tick further.
        (
            bar("09:30", 81_200, high=81_300),
            [bar("09:31", 81_200, high=81_400), bar("09:32", 81_300, high=81_500)],
            "timeout",
            81_600,
            80_900,
            None,
        ),
        # Closed at its high (side +1): L one tick further, so 80,900 does not reach it.
        (
            bar("09:30", 81_200, low=81_100),
            [bar("09:31", 81_000, low=80_900)],
            "timeout",
            81_500,
            80_800,
            None,
        ),
        # The first minute wins: L at 09:31 (opening at the close: side 0), U at 09:32.
        (
            bar("09:30", 81_200),
            [bar("09:31", 81_000, low=80_900, opened=81_200), bar("09:32", 81_300, high=81_500)],
            "down_first",
            81_500,
            80_900,
            "09:31",
        ),
    ],
)
def test_the_bar_rule_reads_the_first_minute_reaching_each_barrier_by_the_close_s_side(
    closing: Bar, after: list[Bar], label: str, upper: float, lower: float, minute: str | None
) -> None:
    answer = bar_barrier(closing, after, FEE, width=W2)

    assert answer is not None
    assert (answer.label, answer.path["upper"], answer.path["lower"]) == (label, upper, lower)
    reached = answer.path["up_minute"] if label == "up_first" else answer.path["down_minute"]
    assert reached == (None if minute is None else at(f"{minute}:00").isoformat())
    assert (answer.path["w"], answer.path["k"]) == (2, 2)


def test_both_barriers_in_the_same_minute_leave_the_window_out_of_training() -> None:
    """Input: one bar at 09:31 from 80,900 to 81,500: both barriers in its minute. Output: no
    class (``same minute``); none either without a trade in the four minutes or a width."""
    answer = bar_barrier(bar("09:30", 81_200), [bar("09:31", 81_200, high=81_500, low=80_900)], FEE, width=W2)

    assert answer is not None and (answer.label, answer.unscorable) == (None, SAME_MINUTE)
    assert bar_barrier(bar("09:30", 81_200), [], FEE, width=W2) is None
    assert bar_barrier(bar("09:30", 81_200), [bar("09:31", 81_200)], FEE) is None


def store_bars(data_dir: Path, day: date, bars: Sequence[Bar]) -> None:
    """A day's bars as the backfill stores them."""
    rows = [
        {
            "symbol": SYMBOL,
            "bar_time": b.start,
            "venue": "UN",
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
            "cum_value": 0,
        }
        for b in bars
    ]
    path = data_dir / bars_key(day, MINUTE_BARS_TR, SYMBOL)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=BAR_SCHEMA), path)


# Friday's bars: one change of +3 ticks (13:00 → 13:01) and sixteen more bars, two minutes
# apart, to 14:30: eighteen bars of continuous trading. Its closing auction (15:20) and
# after-hours (15:40) bars are not continuous trading: they would add changes.
FRIDAY_BARS = (
    [bar("13:00", 81_200, FRIDAY), bar("13:01", 81_500, FRIDAY)]
    + [bar(f"14:{2 * i:02d}", 81_200, FRIDAY) for i in range(16)]
    + [bar("15:20", 90_000, FRIDAY), bar("15:21", 70_000, FRIDAY), bar("15:40", 90_000, FRIDAY)]
)


def test_a_backfilled_day_s_answers_take_their_width_from_its_bars_and_the_previous_day_s(
    tmp_path: Path,
) -> None:
    """Input (``training.bar_answers``, as training reads a backfilled day): Friday's bars above,
    Monday's 09:00 (81,000), 09:01 (80,700: −3 ticks), 09:02 (80,700) and 09:03 (from 80,400 to
    81,000). Output: the 09:02 bar close's width is 8 (+3 and −3 over 18 + 2 bars); its barriers
    80,700 ± 9 ticks (the close one price, the next open the same: w + 1): neither reached
    (timeout). Without Friday's bars the width would be k = 2, the barriers ± 3 ticks (81,000 and
    80,400), both reached in 09:03: the same minute."""
    store_bars(tmp_path, FRIDAY, FRIDAY_BARS)
    monday = [
        bar("09:00", 81_000),
        bar("09:01", 80_700),
        bar("09:02", 80_700),
        bar("09:03", 80_500, high=81_000, low=80_400),
    ]
    calendar = MarketCalendar({FRIDAY: True, DAY: True})

    answers = bar_answers(
        {(SYMBOL, DAY): monday}, calendar, FEE, [BARRIER], previous=PreviousDay(tmp_path, calendar)
    )
    alone = bar_answers({(SYMBOL, DAY): monday}, calendar, FEE, [BARRIER])

    answer = answers[(BARRIER.name, SYMBOL, at("09:02:00"))]
    assert answer is not None and (answer.label, answer.path["w"]) == ("timeout", 8)
    assert (answer.path["upper"], answer.path["lower"]) == (80_700 + 900, 80_700 - 900)
    without = alone[(BARRIER.name, SYMBOL, at("09:02:00"))]
    assert without is not None and (without.label, without.unscorable, without.path["w"]) == (
        None,
        SAME_MINUTE,
        2,
    )


# The output contract.


@pytest.mark.parametrize(
    ("forecast", "why"),
    [
        (KindForecast((0.2, 0.5, 0.3)), None),
        (KindForecast((0.2, 0.5, 0.4)), "distribution"),
        (KindForecast((0.2, 0.5, 0.3), {"width_ticks": 2.0}), "fields"),
    ],
)
def test_a_barrier_forecast_is_three_probabilities_or_an_error(
    forecast: KindForecast, why: str | None
) -> None:
    """The width is the engine's, not the forecast's: a forecast carrying one breaks the contract."""
    invalid = BARRIER.invalid(forecast)

    assert invalid is None if why is None else why in (invalid or "")


def test_the_barrier_is_registered_after_the_range() -> None:
    assert [k.name for k in KINDS] == ["direction", "range", "barrier"]
    assert (BARRIER.classes, BARRIER.action, BARRIER.answer_version) == (
        ("up_first", "timeout", "down_first"),
        ("up_first", "down_first"),
        1,
    )
    assert BARRIER.p_keys() == ("p_up_first", "p_timeout", "p_down_first")
    assert [b.name for b in BARRIER.baselines] == [CLIMATE, PERSIST]
    assert dict(BARRIER.fields) == {} and BARRIER.width is not None


# The barrier P&L, by hand: at 81,250 (bought at the ask 81,300, sold at the bid 81,200 at the
# bar close), U 81,450 and L 81,050, the cost 1.86875 ticks.

PATH = {
    "tick": 100,
    "cost": COST,
    "upper": 81_450.0,
    "lower": 81_050.0,
    "bid_start": 81_200,
    "ask_start": 81_300,
    "up_bid": 81_500,
    "up_ask": 81_600,
    "down_bid": 80_900,
    "down_ask": 81_000,
    "bid_end": 81_100,
    "ask_end": 81_200,
}


@pytest.mark.parametrize(
    ("predicted", "outcome", "ticks"),
    [
        # A long: up first sells at U (81,450 − 81,300 = 1.5 ticks); down first at the bid when L
        # was reached (80,900: −4); timeout at the last bid (81,100: −2). Each less the cost.
        ("up_first", "up_first", 1.5 - COST),
        ("up_first", "down_first", -4.0 - COST),
        ("up_first", "timeout", -2.0 - COST),
        # A short: down first buys back at L (81,200 − 81,050 = 1.5); up first at the ask when U
        # was reached (81,600: −4); timeout at the last ask (81,200: 0).
        ("down_first", "down_first", 1.5 - COST),
        ("down_first", "up_first", -4.0 - COST),
        ("down_first", "timeout", 0.0 - COST),
    ],
)
def test_the_barrier_p_and_l_as_worked_out_by_hand(predicted: str, outcome: str, ticks: float) -> None:
    assert barrier_profit(predicted, outcome, PATH) == pytest.approx(ticks)


def test_a_timeout_call_is_no_trade_and_a_missing_quote_no_p_and_l() -> None:
    assert barrier_profit("timeout", "up_first", PATH) is None
    assert barrier_profit("up_first", "up_first", {**PATH, "ask_start": None}) is None


def scored(
    rows: Sequence[tuple[datetime, tuple[float, float, float], int]], name: str = "alg"
) -> list[KindScored]:
    """(bar close, probabilities, class) as scored barrier predictions, on the path above."""
    return [
        KindScored(name, "v1", SYMBOL, close, p, outcome, dict(zip(BARRIER.p_keys(), p, strict=True)), PATH)
        for close, p, outcome in rows
    ]


EXAMPLE = [
    (T, (0.6, 0.3, 0.1), 0),  # a long, up first: 1.5 − cost
    (T + timedelta(minutes=1), (0.5, 0.2, 0.3), 2),  # a long, down first: −4 − cost
    (T + timedelta(minutes=2), (0.1, 0.2, 0.7), 2),  # a short, down first: 1.5 − cost
    (T + timedelta(minutes=3), (0.2, 0.6, 0.2), 1),  # timeout: no trade
]


def test_the_barrier_statistics_count_the_calls_and_their_p_and_l() -> None:
    """The example's three trades: 1.5, −4 and 1.5, each less 1.86875: a total of −1 − 3 × 1.86875;
    two longs, one short. Precision counts the up_first and down_first calls (2 right of 3),
    coverage 3 of 4, accuracy 3 of 4."""
    stats = barrier_stats("alg", scored(EXAMPLE), {})

    assert stats["n"] == 4
    assert (stats["pnl"]["trades"], stats["pnl"]["total_ticks"]) == (3, pytest.approx(-1 - 3 * COST))
    assert stats["pnl"]["per_trade"]["value"] == pytest.approx((-1 - 3 * COST) / 3)
    assert (stats["long"]["trades"], stats["long"]["total_ticks"]) == (2, pytest.approx(-2.5 - 2 * COST))
    assert (stats["short"]["trades"], stats["short"]["total_ticks"]) == (1, pytest.approx(1.5 - COST))
    check("barrier_stats", stats)
    summary = summarize(scored(EXAMPLE), BARRIER, {}, "alg")
    assert summary.precision is not None and summary.precision.n == 3
    assert summary.precision.value == pytest.approx(2 / 3)
    assert summary.coverage is not None and summary.coverage.value == 0.75
    assert summary.accuracy is not None and summary.accuracy.value == 0.75


# The baselines.


def inputs(previous: Answer | None, bar_close: datetime = T) -> KindInputs:
    return KindInputs(SYMBOL, bar_close, 81_250.0, 100.0, 2, previous, {})


def test_the_persistence_carries_the_last_window_s_answer_on() -> None:
    persist = BarrierPersist()

    assert persist.predict(inputs(Answer("down_first", None, {}))) == KindForecast((0.0, 0.0, 1.0))
    assert persist.predict(inputs(Answer("timeout", None, {}))) == KindForecast((0.0, 1.0, 0.0))
    assert persist.predict(inputs(None)) is None
    assert persist.predict(inputs(Answer(None, AMBIGUOUS, {}))) is None


def sample(bar_close: datetime, label: str | None, symbol: str = SYMBOL) -> KindSample:
    answer = Answer(label, None if label else SAME_MINUTE, {})
    return KindSample(symbol, bar_close, FeatureVector({}, ()), answer, 1, "mid")


def test_the_climate_counts_each_symbol_and_slot_s_classes() -> None:
    """Input: the 09:00 slot's answers up, up, timeout, down and one without a class (same
    minute), the 10:00 slot's one timeout. Output: the 09:00 cell's shares 2/4, 1/4, 1/4 (the
    classless one not counted); a slot without answers backs off to the symbol's day (2/5,
    2/5, 1/5); without a table, a third each."""
    closes = [at(f"09:{m:02d}:00") for m in (5, 10, 15, 20, 25)]
    samples = [
        sample(t, c)
        for t, c in zip(closes, ["up_first", "up_first", "timeout", "down_first", None], strict=True)
    ]
    samples.append(sample(at("10:05:00"), "timeout"))
    climate = BarrierClimate()

    table = climate.fit(samples)
    bound = climate.using(table)

    assert table.version.startswith(f"{CLIMATE}-20261012-") and table.target_version == 1
    assert table.params[f"{SYMBOL}.09:00.n"] == 4.0
    assert bound.predict(inputs(None, at("09:30:00"))).probabilities == pytest.approx((0.5, 0.25, 0.25))
    assert bound.predict(inputs(None, at("13:05:00"))).probabilities == pytest.approx((0.4, 0.4, 0.2))
    assert climate.predict(inputs(None)) == KindForecast((1 / 3, 1 / 3, 1 / 3))
    assert climate.version == f"{CLIMATE}-default" and bound.version == table.version
    assert climate.fit([sample(T, None)]) == climate.default_table()


# Through the Replay Harness.


def volatile_morning() -> list[Event]:
    """The book at 81,200 / 81,300 (mid 81,250) every 10 s from 09:00:30, and trades (without
    quotes) making one-minute bars: 81,200 at 09:00, 81,500 at 09:01 (+3 ticks), 81,500 at
    09:03, 81,200 at 09:04 (−3), then 81,200 every other minute from 09:06 to 09:36: twenty bars
    by 09:37, two one-minute changes. So w = k = 2 up to the 09:36 bar close and w = 8 from
    09:37 (σ̂₄ = 6√2 = 8.49). The book is at 81,500 / 81,600 from 09:38:30 to 09:38:50 (the bid
    at U for w = 2, 81,450, not for w = 8, 82,050) and at 80,300 / 80,400 from 09:44:00 to
    09:44:20 (the ask at L for w = 8, 80,450); it goes silent from 09:47:00 to 09:48:10."""
    minutes = ["09:00", "09:01", "09:03", "09:04", *(f"09:{m:02d}" for m in range(6, 37, 2))]
    prices = [81_200, 81_500, 81_500, 81_200] + [81_200] * 16
    trades = [trade(at(f"{m}:30"), p) for m, p in zip(minutes, prices, strict=True)]
    return (
        trades
        + steady_book(at("09:00:30"), at("09:38:20"), 81_200, 81_300)
        + steady_book(at("09:38:30"), at("09:38:50"), 81_500, 81_600)
        + steady_book(at("09:39:00"), at("09:43:50"), 81_200, 81_300)
        + steady_book(at("09:44:00"), at("09:44:20"), 80_300, 80_400)
        + steady_book(at("09:44:30"), at("09:47:00"), 81_200, 81_300)
        + steady_book(at("09:48:10"), at("09:55:00"), 81_200, 81_300)
    )


def barrier_answers(db: Path) -> dict[str, dict[str, Any]]:
    store = PredictionStore(db)
    try:
        return {a["bar_close"][11:16]: a for a in store.kind_answers(BARRIER.name, SYMBOL)}
    finally:
        store.close()


def barrier_predictions(db: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """The barrier's predictions by (HH:MM, predictor), with their answers."""
    store = PredictionStore(db)
    try:
        rows = store.kind_recent(BARRIER.name, BARRIER.answer_version, SYMBOL, 1000)
    finally:
        store.close()
    return {(r["bar_close"][11:16], r["algorithm"]): r for r in rows}


async def test_both_baselines_predict_and_are_scored_on_the_barrier_with_the_bar_close_s_width(
    tmp_path: Path,
) -> None:
    """Input: the morning above, replayed with the kinds the engine predicts. Output: every
    barrier prediction keeps its bar close's width (2 to 09:36, 8 from 09:37), the one its
    answer was made with; the answers by hand (09:35 and 09:36 up first at 09:38:30 with w = 2;
    09:37 timeout with w = 8; 09:41 down first at 09:44:00; 09:44 to 09:47 silent); every
    predictor of a bar close shares its answer; the persistence carries each last window on and
    does not predict where it could not be scored; both on ``/v1/leaderboard?kind=barrier``."""
    db = tmp_path / "engine.db"
    engine = await run(db, volatile_morning(), until=at("09:55:00"), target_version=2)

    answers = barrier_answers(db)
    made = barrier_predictions(db)
    widths = {
        t: {p["width_ticks"] for (c, _), p in made.items() if c == t}
        for t in ("09:20", "09:36", "09:37", "09:50")
    }
    assert widths == {"09:20": {2}, "09:36": {2}, "09:37": {8}, "09:50": {8}}
    assert answers["09:36"]["path"]["sigma"] is None, "19 bars at 09:36: w = k"
    assert answers["09:37"]["path"]["sigma"] == pytest.approx(6 * math.sqrt(2))
    for (t, _), p in made.items():
        if p["answer_path"]:
            assert p["answer_path"]["w"] == p["width_ticks"], t
    shown = {
        t: (answers[t]["class"], answers[t]["unscorable"])
        for t in ("09:30", "09:35", "09:36", "09:37", "09:41")
    }
    assert shown == {
        "09:30": ("timeout", None),
        "09:35": ("up_first", None),
        "09:36": ("up_first", None),
        "09:37": ("timeout", None),
        "09:41": ("down_first", None),
    }
    assert answers["09:36"]["path"]["up_at"] == at("09:38:30").isoformat()
    assert (answers["09:41"]["path"]["lower"], answers["09:41"]["path"]["down_at"]) == (
        80_450.0,
        at("09:44:00").isoformat(),
    )
    assert {answers[t]["unscorable"] for t in ("09:44", "09:45", "09:46", "09:47")} == {SILENT}
    for t in ("09:36", "09:41"):
        assert {p["answer_class"] for (c, _), p in made.items() if c == t} == {answers[t]["class"]}
    assert made[("09:01", PERSIST)]["status"] == "no_previous"
    assert made[("09:48", PERSIST)]["status"] == "no_previous", "the 09:44 window went silent"
    assert made[("09:40", PERSIST)]["output"] == {"p_up_first": 1.0, "p_timeout": 0.0, "p_down_first": 0.0}
    assert made[("09:45", PERSIST)]["output"]["p_down_first"] == 1.0, "09:41–09:45 went down first"
    climate = made[("09:31", CLIMATE)]
    assert climate["model_version"] == f"{CLIMATE}-default"
    assert climate["output"] == {"p_up_first": 1 / 3, "p_timeout": 1 / 3, "p_down_first": 1 / 3}
    async with api(engine) as client:
        board = await get_json(client, "/v1/leaderboard?kind=barrier")
        kinds = await get_json(client, "/v1/kinds")
        at_0937 = await get_json(
            client, f"/v1/symbols/{SYMBOL}/kinds/barrier/predictions?bar_close=2026-10-12T09:37:00%2B09:00"
        )
        range_latest = await get_json(client, f"/v1/symbols/{SYMBOL}/kinds/range/latest")
    check("kind_leaderboard", board)
    check("kind_predictions", at_0937)
    entries = {e["algorithm"]: e for e in board["baselines"]}
    assert set(entries) == {CLIMATE, PERSIST}
    assert entries[PERSIST]["n"] > 0 and entries[CLIMATE]["n"] > 0
    for entry in entries.values():
        check("barrier_stats", entry["extra"])
        assert entry["extra"]["n"] == entry["n"]
    assert entries[CLIMATE]["extra"]["pnl"]["trades"] == 0, "a third each calls timeout: no trade"
    assert entries[PERSIST]["extra"]["pnl"]["trades"] > 0
    assert [k["name"] for k in kinds["kinds"]] == ["direction", "range", "barrier"]
    assert kinds["kinds"][2]["fields"] == {} and kinds["kinds"][2]["action"] == ["up_first", "down_first"]
    for p in at_0937["predictions"]:
        assert p["width_ticks"] == 8
        check("barrier_output", p["output"])
        check("barrier_path", p["answer"]["path"])
    assert {p["width_ticks"] for p in range_latest["predictions"]} == {None}, "the range has no barriers"
    engine.store.close()


async def test_a_restart_leaves_its_windows_unscorable_and_the_width_from_what_came_after(
    tmp_path: Path,
) -> None:
    """Input: the morning above, the engine restarted at 09:39:30. Output: the windows from 09:36
    to 09:39 were open then: ``restarted`` (an empty path); after it the engine has no bars of
    the day (no trade since) nor a previous day: w = k = 2, so the 09:41 window, whose barriers
    are now 81,050 and 81,450, still goes down first; the persistence does not predict 09:40
    (its window was not followed)."""
    db = tmp_path / "engine.db"
    events = volatile_morning()
    restart = at("09:39:30")

    await run(db, [e for e in events if e[1]["received_at"] < restart], until=restart, target_version=2)
    engine = await run(
        db,
        [e for e in events if e[1]["received_at"] >= restart],
        until=at("09:55:00"),
        start=restart,
        target_version=2,
    )
    engine.store.close()

    answers = barrier_answers(db)
    assert {answers[t]["unscorable"] for t in ("09:36", "09:37", "09:38", "09:39")} == {RESTARTED}
    assert answers["09:39"]["path"] == {}
    assert (answers["09:41"]["class"], answers["09:41"]["path"]["w"]) == ("down_first", 2)
    made = barrier_predictions(db)
    assert made[("09:41", CLIMATE)]["width_ticks"] == 2
    assert made[("09:43", PERSIST)]["status"] == "no_previous", "the 09:39 window was not followed"
    assert made[("09:44", PERSIST)]["status"] == "ok", "the 09:40 window opened after the restart"


def tick_crossing() -> list[Event]:
    """The book at 49,900 / 49,950 (mid 49,925: tick 50, k = 3, and w = k = 3 without bars) every
    10 s from 09:00:30 to 09:31:50, then at 50,100 / 50,200 (mid 50,150, over 50,000: tick 100,
    k = 2) from 09:32:00 to 09:40."""
    return steady_book(at("09:00:30"), at("09:31:50"), 49_900, 49_950) + steady_book(
        at("09:32:00"), at("09:40:00"), 50_100, 50_200
    )


async def test_a_barrier_prediction_keeps_its_bar_close_s_mid_point_and_tick_after_the_price_moves(
    tmp_path: Path,
) -> None:
    """#210. Input: the book above: in the 09:31 window the mid-point crosses 50,000, where the
    tick goes from 50 to 100. Output: before its answer, every barrier prediction of the 09:31
    bar close carries the bar close's mid-point 49,925 and tick 50 (``anchor``, ``tick``), so its
    U = 49,925 + 3 × 50 = 50,075 and L = 49,775 are known, while ``/v1/symbols`` already shows
    50,150 and 100 and the 09:33 bar close's predictions carry those; after its answer (up first
    at 09:32:00: the best bid 50,100 at U or above) they still carry 49,925 and 50, the answer
    path's anchor and tick, and its U and L. The stream's ``kind_prediction`` items carry their
    own bar close's."""
    db = tmp_path / "engine.db"
    events = tick_crossing()
    split = at("09:34:00")
    engine = await run(db, [e for e in events if e[1]["received_at"] <= split], until=split, target_version=2)
    at_0931 = f"/v1/symbols/{SYMBOL}/kinds/barrier/predictions?bar_close=2026-10-12T09:31:00%2B09:00"

    async with api(engine) as client:
        before = await get_json(client, at_0931)
        at_0933 = await get_json(
            client, f"/v1/symbols/{SYMBOL}/kinds/barrier/predictions?bar_close=2026-10-12T09:33:00%2B09:00"
        )
        symbols = await get_json(client, "/v1/symbols")
        ws = await client.ws_connect("/v1/stream")
        assert isinstance(engine.clock, FakeClock)
        rest = [e for e in events if e[1]["received_at"] > split]
        await replay_into(engine, engine.clock, rest, until=at("09:36:00"))
        heard: list[dict[str, Any]] = []
        last = "2026-10-12T09:36:00+09:00"

        def barrier_made(e: dict[str, Any]) -> bool:
            return bool(e["type"] == "kind_prediction" and e["prediction_kind"] == BARRIER.name)

        async with asyncio.timeout(5):
            while not any(barrier_made(e) and e["bar_close"] == last for e in heard):
                heard.append(await ws.receive_json())
                check("event", heard[-1])
        await ws.close()
        after = await get_json(client, at_0931)
    engine.store.close()

    check("kind_predictions", before)
    check("kind_predictions", after)
    assert {CLIMATE, PERSIST} <= {p["algorithm"] for p in before["predictions"]}
    pending = {(p["anchor"], p["tick"], p["width_ticks"], p["answer"] is None) for p in before["predictions"]}
    assert pending == {(49_925.0, 50, 3, True)}
    (now,) = symbols["symbols"]
    assert (now["mid"], now["tick"]) == (50_150.0, 100), "the mid-point now"
    assert {(p["anchor"], p["tick"]) for p in at_0933["predictions"]} == {(50_150.0, 100)}
    assert {(p["anchor"], p["tick"]) for p in after["predictions"]} == {(49_925.0, 50)}, (
        "the bar close's, kept"
    )
    for p in after["predictions"]:
        path = p["answer"]["path"]
        assert (p["answer"]["class"], path["up_at"]) == ("up_first", at("09:32:00").isoformat())
        assert (p["anchor"], p["tick"]) == (path["anchor"], path["tick"])
        w = p["width_ticks"]
        barriers = (p["anchor"] + w * p["tick"], p["anchor"] - w * p["tick"])
        assert barriers == (path["upper"], path["lower"]) == (50_075.0, 49_775.0)
    made = {
        e["bar_close"][11:16]: {(p["anchor"], p["tick"]) for p in e["predictions"]}
        for e in heard
        if barrier_made(e)
    }
    assert made["09:35"] == made["09:36"] == {(50_150.0, 100)}


def rows(db: Path, sql: str) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db) as conn:
        return conn.execute(sql).fetchall()


async def test_the_direction_and_the_range_go_on_as_before_beside_the_barrier(tmp_path: Path) -> None:
    """Input: a busy morning replayed with the kinds before #183 (the direction and the range)
    and with every kind registered. Output: the direction's predictions, outcomes and touch
    labels, and the range's predictions (but the new, empty ``width_ticks``) and answers, are
    the same; only the barrier is new."""
    before, after = tmp_path / "before.db", tmp_path / "after.db"
    old = await run(
        before,
        busy_morning(),
        until=at("10:00:00"),
        target_version=2,
        kinds=(DIRECTION_KIND, RANGE),
        kind_algorithms=[a for a in KIND_ALGORITHMS if a.kind == RANGE.name],  # the barrier's need it
    )
    new = await run(after, busy_morning(), until=at("10:00:00"), target_version=2)
    old.store.close()
    new.store.close()

    assert stored(before) == stored(after)
    touch_rows = "SELECT symbol, bar_close, target_version, class, unscorable, path, scored_at FROM touches"
    assert rows(before, touch_rows + " ORDER BY bar_close") == rows(after, touch_rows + " ORDER BY bar_close")
    made = (
        "SELECT symbol, algorithm, model_version, bar_close, displayed_at, status, output, flat_ticks"
        " FROM kind_predictions WHERE kind = 'range' ORDER BY bar_close, algorithm"
    )
    assert rows(before, made) == rows(after, made)
    assert rows(after, "SELECT DISTINCT width_ticks FROM kind_predictions WHERE kind = 'range'") == [(None,)]
    assert kind_answers(before, "range") == kind_answers(after, "range")
    assert kind_answers(before, "barrier") == [] and kind_answers(after, "barrier")


# Training.


async def test_a_recorded_day_trains_on_the_barrier_answers_and_widths_the_engine_has(tmp_path: Path) -> None:
    """Input: a busy morning, recorded, and replayed through the engine. Output: the day's
    barrier samples carry the answers the engine stored for the same windows (class, reason,
    path), and their ``barrier_width`` feature is the width its predictions keep."""
    events = busy_morning()
    data = tmp_path / "data"
    record(data, events)
    engine = await run(tmp_path / "engine.db", events, until=at("10:00:00"), target_version=2)
    stored = {
        datetime.fromisoformat(a["bar_close"]): a for a in engine.store.kind_answers(BARRIER.name, SYMBOL)
    }
    kept = {
        datetime.fromisoformat(p["bar_close"]): p["width_ticks"]
        for p in engine.store.kind_recent(BARRIER.name, BARRIER.answer_version, SYMBOL, 1000)
    }
    engine.store.close()

    day = training_day(
        data, DAY, CALENDAR, (SYMBOL,), ("barrier_width",), FEE, Leaders(), target_version=2, kinds=(BARRIER,)
    )

    assert day is not None
    samples = [s for s in day.of_kind(BARRIER, ("barrier_width",)) if s.bar_close <= at("09:56:00")]
    assert len(samples) == 56
    assert {s.answer.label for s in samples if s.answer} >= {"up_first", "down_first"}
    for s in samples:
        assert s.answer is not None
        assert (s.answer.label, s.answer.unscorable) == (
            stored[s.bar_close]["class"],
            stored[s.bar_close]["unscorable"],
        )
        assert dict(s.answer.path) == stored[s.bar_close]["path"]
        assert s.features["barrier_width"] == kept[s.bar_close]


def test_a_backfilled_day_trains_on_the_bar_rule_its_same_minutes_left_out(tmp_path: Path) -> None:
    """Input: Friday's bars above and Monday's backfill only: 81,200 every minute but 09:31, one
    bar from 79,900 to 82,500 (both barriers of the 09:31 bar close in its minute, whatever the
    width). Output: each sample's answer is the bar rule's with the width from both days' bars,
    the same as its ``barrier_width`` feature; the 09:31 window has no label (same minute):
    training leaves it out; a still window times out."""
    data = tmp_path / "data"
    store_bars(data, FRIDAY, FRIDAY_BARS)
    monday = [bar(f"09:{m:02d}", 81_200) for m in range(0, 31)]
    monday += [bar("09:31", 81_200, high=82_500, low=79_900)]
    monday += [bar(f"09:{m:02d}", 81_200) for m in range(32, 60)]
    store_bars(data, DAY, monday)
    calendar = MarketCalendar({FRIDAY: True, DAY: True})

    day = training_day(
        data, DAY, calendar, (SYMBOL,), ("barrier_width",), FEE, Leaders(), target_version=2, kinds=(BARRIER,)
    )

    assert day is not None and day.reference == "close"
    samples = {s.bar_close: s for s in day.of_kind(BARRIER, ("barrier_width",))}
    same = samples[at("09:31:00")]
    assert same.answer is not None and (same.label, same.answer.unscorable) == (None, SAME_MINUTE)
    for close in (at("09:31:00"), at("09:50:00")):
        answer = samples[close].answer
        assert answer is not None and samples[close].features["barrier_width"] == answer.path["w"]
    assert samples[at("09:50:00")].label == "timeout"


# The past answers, filled in.


@pytest.fixture(scope="module")
def replayed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The rescore tests' day, with Friday's bars backfilled, replayed afresh through the Replay
    Harness as the server replays it (the previous trading day's bars read): its database, the
    answers to match."""
    root = tmp_path_factory.mktemp("barrier-replayed")
    data = root / "data"
    record(data, day_events())
    store_bars(data, FRIDAY, FRIDAY_BARS)
    db = root / "replayed.sqlite3"

    async def replay_it() -> None:
        engine = await run(db, day_events(), until=END_OF_DAY, previous=PreviousDay(data, CALENDAR))
        engine.store.close()

    asyncio.run(replay_it())
    return db


def kind_answers(db: Path, kind: str) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT kind, symbol, bar_close, answer_version, class, unscorable, special, path, scored_at"
            " FROM kind_answers WHERE kind = ? ORDER BY bar_close, symbol",
            (kind,),
        ).fetchall()


def test_the_fill_answers_a_recorded_day_as_a_fresh_replay_does_and_once(
    tmp_path: Path, replayed: Path
) -> None:
    """Input: the server's database without the barrier's answers nor predictions (an engine
    before #183), the day recorded and Friday backfilled in the data directory. Output: one
    barrier answer per bar close, each the fresh replay's (class, path with the width from
    Friday's bars too, scored_at, special mark); no baseline predicts the past; a second run
    reads nothing."""
    fresh = replayed
    s = Settings(
        kis_app_key="",
        kis_app_secret="",
        symbols=(SYMBOL,),
        etf_symbols=(),
        index_codes=(),
        us_symbols=(),
        data_dir=tmp_path / "data",
        state_dir=tmp_path / "state",
    )
    record(s.data_dir, day_events())
    store_bars(s.data_dir, FRIDAY, FRIDAY_BARS)
    db = server_db(s.state_dir, fresh, keep=1)
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM kind_answers WHERE kind = 'barrier'")
        conn.execute("DELETE FROM kind_predictions WHERE kind = 'barrier'")

    [filled] = fill_kind_answers(s, NEXT_MORNING, kinds=(BARRIER,))

    assert filled.written == 376 and filled.not_recorded == 0
    assert kind_answers(db, "barrier") == kind_answers(fresh, "barrier")
    sigmas = {row[2][11:16]: json.loads(row[7]).get("sigma") for row in kind_answers(db, "barrier")}
    assert sigmas["09:01"] is None, "Friday's 18 bars and 09:00's: 19, w = k"
    assert sigmas["09:02"] is not None, "Friday's bars carry the width into the open"
    assert {row[4] for row in kind_answers(db, "barrier")} >= {"up_first", "timeout", "down_first", None}
    assert {row[6] for row in kind_answers(db, "barrier")} == {0, 1}, "special: the VI and the breaker"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM kind_predictions WHERE kind = 'barrier'").fetchone() == (0,)

    assert fill_kind_answers(s, NEXT_MORNING, kinds=(BARRIER,)) == []
    assert kind_answers(db, "barrier") == kind_answers(fresh, "barrier")


def test_a_table_given_to_the_engine_is_what_the_climate_predicts_from() -> None:
    """The climate's table from the morning swap: its cell and version."""
    table = Model(
        f"{CLIMATE}-20261009-abcdef",
        {
            f"{SYMBOL}.09:30.p_up_first": 0.2,
            f"{SYMBOL}.09:30.p_timeout": 0.5,
            f"{SYMBOL}.09:30.p_down_first": 0.3,
            f"{SYMBOL}.09:30.n": 100.0,
        },
        target_version=1,
    )

    forecast = BarrierClimate().using(table).predict(inputs(None, at("09:45:00")))

    assert forecast == KindForecast((0.2, 0.5, 0.3))
    assert BARRIER.invalid(forecast) is None


def test_the_path_of_a_window_without_a_book_keeps_its_place(tmp_path: Path) -> None:
    """A window not followed has an empty path in the store, as the range's."""
    store = PredictionStore(tmp_path / DB_NAME)
    try:
        store.add_kind_answers(BARRIER.name, 1, [(SYMBOL, T, Answer(None, RESTARTED), False)])
        [row] = store.kind_answers(BARRIER.name, SYMBOL)
    finally:
        store.close()
    assert (row["class"], row["unscorable"], row["path"]) == (None, RESTARTED, {})
    check("barrier_path", row["path"])
