"""The range (#180): its answer, its bar rule, its output contract, its statistics and its two
baselines, by hand and through the Replay Harness.

Prices: at 81,250 (bid 81,200, ask 81,300) the tick is 100 and the flat band k = 2 (the cost
81,250 × 0.23% = 186.9 won covers two ticks); at 86,950 k = 2 (199.99 won), at 87,050 k = 3
(200.2 won). So a window's classes there are small r < 2, medium 2 ≤ r < 4, large r ≥ 4 (at
87,050: small r < 3, medium 3 ≤ r < 6, large r ≥ 6).
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant.algorithms.contract import KindSample, Model
from krx_quant.clock import FakeClock
from krx_quant.features import FeatureVector, Leaders
from krx_quant.kind_registry import KINDS
from krx_quant.kind_stats import summarize
from krx_quant.kinds import Answer, Followed, KindForecast, KindInputs, KindScored, QuoteLog
from krx_quant.market_calendar import MarketCalendar
from krx_quant.predictions import PredictionStore
from krx_quant.prevalidation import prevalidate
from krx_quant.pricing import flat_ticks, tick_size
from krx_quant.ranges import (
    BAR_C,
    CLIMATE,
    PERSIST,
    RANGE,
    RangeClimate,
    RangePersist,
    bar_range,
    pinball,
    quantile_stats,
)
from krx_quant.reference import Bar
from krx_quant.replay import replay_into
from krx_quant.touch import TARGET_VERSION
from krx_quant.training import bar_answers, training_day
from tests.algorithm_doubles import stand_in_card, stand_in_meanings
from tests.api_schema import check
from tests.replay_events import CALENDAR, DAY, FEE, SYMBOL, api, at, get_json, run, steady_book
from tests.test_prevalidation import AFTER, ALPHA
from tests.test_prevalidation import report as prevalidation_report
from tests.test_prevalidation import settings as prevalidation_settings
from tests.test_replay import busy_morning
from tests.test_replay_cli import record

T = at("09:31:00")


def window(
    quotes: list[tuple[str, float, float]],
    anchor: float | None = 81_250.0,
    *,
    every: int = 30,
    until: str = "09:34:59",
    rest_at: str | None = None,
) -> Followed:
    """The window of the bar close T (09:31) from ``anchor``: the book at the anchor every
    ``every`` seconds from T to ``until`` (nothing else moving), and the (time, bid, ask)
    ``quotes`` received besides, as the Market View logs them; a REST snapshot at ``rest_at``."""
    log = QuoteLog()
    received: list[tuple[datetime, float, float]] = []
    if anchor is not None:
        when = T
        while when <= at(until):
            received.append((when, anchor - 50, anchor + 50))
            when += timedelta(seconds=every)
    received += [(at(t), bid, ask) for t, bid, ask in quotes]
    for when, bid, ask in sorted(received, key=lambda q: q[0]):
        log.quote(when, bid, ask, None, None, False)
    if rest_at is not None:
        log.quote(at(rest_at), 81_200, 81_300, None, None, True)
    tick = tick_size(anchor) if anchor is not None else None
    k = flat_ticks(anchor, FEE) if anchor is not None else None
    return log.window(SYMBOL, T, anchor, tick, k, None, None, crossed=RANGE.crossed)


# The answer, by hand.


@pytest.mark.parametrize(
    ("quotes", "anchor", "label", "r", "up", "down"),
    [
        # A locked quote (81,300 / 81,300) is a mid-point half a tick up: r 0.5.
        ([("09:32:00", 81_300, 81_300)], 81_250.0, "small", 0.5, 0.5, 0.0),
        # A crossed quote (ask below bid) is dropped: it would make the mid 81,550 (3 up). It
        # still is since #187, when the touch label and the barrier began counting it.
        ([("09:32:00", 81_600, 81_500)], 81_250.0, "small", 0.0, 0.0, 0.0),
        # The ends: a quote received at T counts (mid 81,050, 2 down); one at T + 4 min does not.
        ([("09:31:00", 81_000, 81_100), ("09:35:00", 81_600, 81_700)], 81_250.0, "medium", 2.0, 0.0, 2.0),
        # The reference is the highest mid-point: everything after it lower (81,150).
        ([("09:32:00", 81_100, 81_200)], 81_250.0, "small", 1.0, 0.0, 1.0),
        # Just below k: 81,400 (bid 81,300, ask 81,500) is 1.5 ticks up.
        ([("09:32:00", 81_300, 81_500)], 81_250.0, "small", 1.5, 1.5, 0.0),
        # r = k: 81,450 is 2 up.
        ([("09:32:00", 81_400, 81_500)], 81_250.0, "medium", 2.0, 2.0, 0.0),
        # Just below 2k: 81,450 and 81,100 (bid 81,050, ask 81,150): 3.5.
        ([("09:32:00", 81_400, 81_500), ("09:33:00", 81_050, 81_150)], 81_250.0, "medium", 3.5, 2.0, 1.5),
        # r = 2k: 81,450 and 81,050.
        ([("09:32:00", 81_400, 81_500), ("09:33:00", 81_000, 81_100)], 81_250.0, "large", 4.0, 2.0, 2.0),
        # The same four ticks where k is 2 (86,950) and where it is 3 (87,050).
        ([("09:32:00", 87_100, 87_200), ("09:33:00", 86_700, 86_800)], 86_950.0, "large", 4.0, 2.0, 2.0),
        ([("09:32:00", 87_200, 87_300), ("09:33:00", 86_800, 86_900)], 87_050.0, "medium", 4.0, 2.0, 2.0),
    ],
)
def test_the_answer_is_the_mid_point_range_against_k_as_worked_out_by_hand(
    quotes: list[tuple[str, float, float]], anchor: float, label: str, r: float, up: float, down: float
) -> None:
    answer = RANGE.answer_to(window(quotes, anchor))

    assert (answer.label, answer.unscorable) == (label, None)
    assert (answer.path["r"], answer.path["up"], answer.path["down"]) == (r, up, down)
    assert answer.path["k"] == flat_ticks(anchor, FEE)
    assert answer.path["silent"] is False


def test_the_path_keeps_the_extremes_their_times_and_the_quotes() -> None:
    """Input: the book at 81,250 every 30 s from T, 81,450 at 09:32:10 and again at 09:33:00,
    81,150 at 09:33:30. Output: the highest mid first received at 09:32:10, the lowest at
    09:33:30; the reference is the highest when nothing goes above it (its time: T)."""
    answer = RANGE.answer_to(
        window([("09:32:10", 81_400, 81_500), ("09:33:00", 81_400, 81_500), ("09:33:30", 81_100, 81_200)])
    )

    path = answer.path
    assert (path["anchor"], path["tick"], path["k"]) == (81_250.0, 100, 2)
    assert (path["max_mid"], path["max_mid_at"]) == (81_450.0, "2026-10-12T09:32:10+09:00")
    assert (path["min_mid"], path["min_mid_at"]) == (81_150.0, "2026-10-12T09:33:30+09:00")
    assert (path["r"], answer.label) == (3.0, "medium")
    assert path["quotes"] == 8 + 3  # the book every 30 s from T to 09:34:30, and the three
    assert (path["max_bid"], path["min_ask"]) == (81_400.0, 81_200.0)
    falling = RANGE.answer_to(window([("09:32:00", 81_100, 81_200)])).path
    assert (falling["max_mid"], falling["max_mid_at"]) == (81_250.0, "2026-10-12T09:31:00+09:00")
    rising = RANGE.answer_to(window([("09:32:00", 81_300, 81_400)])).path  # the reference the lowest
    assert (rising["min_mid"], rising["min_mid_at"], rising["down"]) == (
        81_250.0,
        "2026-10-12T09:31:00+09:00",
        0.0,
    )


@pytest.mark.parametrize(
    ("built", "why", "silent"),
    [
        # 60 s without a quote: the book every 30 s, but none from 09:32:30 to 09:33:30.
        (lambda: window([], every=30, until="09:32:30"), "silent quotes", True),
        # A REST snapshot of the book in the window.
        (lambda: window([], rest_at="09:32:00"), "rest quotes", False),
        # No mid-point at the bar close.
        (lambda: window([("09:32:00", 81_200, 81_300)], anchor=None), "missing mid", True),
    ],
)
def test_a_window_the_shared_rules_cannot_score_has_no_range_class(
    built: Any, why: str, silent: bool
) -> None:
    answer = RANGE.answer_to(built())

    assert (answer.label, answer.unscorable) == (None, why)
    assert answer.path["silent"] is silent


# The bar rule, by hand: the close 81,200 (tick 100, k 2: 81,200 × 0.23% = 186.8 won).


def bar(minute: str, high: float, low: float, close: float = 81_200) -> Bar:
    start = at(minute)
    return Bar(start, close, high, low, close, 10, 0, start, start)


CLOSING = bar("09:30:00", 81_300, 81_100)  # ended at 09:31, closed at 81,200


@pytest.mark.parametrize(
    ("closing", "after", "c", "label", "r"),
    [
        # The highest high 81,400, the lowest low 81,100: 3 ticks, less c.
        (CLOSING, [bar("09:31:00", 81_400, 81_200), bar("09:32:00", 81_300, 81_100)], 0, "medium", 3.0),
        (CLOSING, [bar("09:31:00", 81_400, 81_200), bar("09:32:00", 81_300, 81_100)], 1, "medium", 2.0),
        # 4 ticks: large without the correction, medium with it.
        (CLOSING, [bar("09:31:00", 81_500, 81_200), bar("09:34:00", 81_300, 81_100)], 0, "large", 4.0),
        (CLOSING, [bar("09:31:00", 81_500, 81_200), bar("09:34:00", 81_300, 81_100)], 1, "medium", 3.0),
        # The close the highest: 81,500 above every bar after it (81,400 to 81,200).
        (
            bar("09:30:00", 81_500, 81_400, close=81_500),
            [bar("09:31:00", 81_400, 81_300), bar("09:33:00", 81_300, 81_200)],
            0,
            "medium",
            3.0,
        ),
        # Nothing moved: no less than 0.
        (CLOSING, [bar("09:31:00", 81_200, 81_200)], 1, "small", 0.0),
    ],
)
def test_the_bar_rule_is_the_bars_range_from_t_s_close_less_c(
    closing: Bar, after: list[Bar], c: int, label: str, r: float
) -> None:
    answer = bar_range(closing, after, FEE, c)

    assert answer is not None
    assert (answer.label, answer.path["r"], answer.path["c"]) == (label, r, c)
    assert answer.path["k"] == 2


def test_the_bar_rule_reads_the_four_minutes_bars_skipping_empty_minutes() -> None:
    """Input (``training.bar_answers``, as training reads a backfilled day): bars at 09:30
    (closed 81,200), 09:31 (81,300–81,200, closed 81,300), none from 09:32 to 09:34, 09:35
    (81,500–81,000). Output: the 09:31 window has the 09:31 bar only: one tick; the 09:32
    window starts from 09:31's close (81,300) and has 09:35's: five ticks; 09:33 has no bar
    that ended at it; 09:36 has no trade in its four minutes: neither has an answer."""
    bars = [
        bar("09:30:00", 81_300, 81_100),
        bar("09:31:00", 81_300, 81_200, close=81_300),
        bar("09:35:00", 81_500, 81_000),
    ]
    calendar = MarketCalendar({DAY: True})

    answers = bar_answers({(SYMBOL, DAY): bars}, calendar, FEE, [RANGE])

    first = answers[(RANGE.name, SYMBOL, at("09:31:00"))]
    assert first is not None and first.path["r"] == max(0.0, 1.0 - BAR_C)
    later = answers[(RANGE.name, SYMBOL, at("09:32:00"))]
    assert later is not None and (later.path["anchor"], later.path["r"]) == (81_300, 5.0 - BAR_C)
    assert answers[(RANGE.name, SYMBOL, at("09:33:00"))] is None
    assert answers[(RANGE.name, SYMBOL, at("09:36:00"))] is None
    assert bar_range(CLOSING, [], FEE) is None


# The output contract.


@pytest.mark.parametrize(
    ("forecast", "why"),
    [
        (KindForecast((0.2, 0.5, 0.3), {"q10": 0.0, "q50": 2.5, "q90": 5.0}), None),
        (KindForecast((0.2, 0.5, 0.3), {"q10": 1.0, "q50": 1.0, "q90": 1.0}), None),
        (KindForecast((0.2, 0.5, 0.4), {"q10": 0.0, "q50": 2.5, "q90": 5.0}), "distribution"),
        (KindForecast((0.2, 0.5, 0.3), {"q10": 3.0, "q50": 2.5, "q90": 5.0}), "q10 ≤ q50"),
        (KindForecast((0.2, 0.5, 0.3), {"q10": 0.0, "q50": 6.0, "q90": 5.0}), "q50 ≤ q90"),
        (KindForecast((0.2, 0.5, 0.3), {"q10": -0.5, "q50": 2.5, "q90": 5.0}), "0 ≤ q10"),
        (KindForecast((0.2, 0.5, 0.3), {"q10": 0.0, "q50": 2.5}), "fields"),
        (KindForecast((0.2, 0.5, 0.3), {"q10": 0.0, "q50": 2.5, "q90": math.inf}), "finite"),
    ],
)
def test_a_range_forecast_keeps_its_contract_or_is_an_error(forecast: KindForecast, why: str | None) -> None:
    invalid = RANGE.invalid(forecast)

    assert invalid is None if why is None else why in (invalid or "")


def test_the_range_is_registered_after_the_direction() -> None:
    assert [k.name for k in KINDS][:3] == ["direction", "range", "barrier"], "the barrier after it (#183)"
    assert (RANGE.classes, RANGE.action, RANGE.answer_version) == (
        ("small", "medium", "large"),
        ("small", "large"),
        1,
    )
    assert [b.name for b in RANGE.baselines] == [CLIMATE, PERSIST]
    assert list(RANGE.fields) == ["q10", "q50", "q90"]


# The statistics, by hand.

D1, D2 = at("09:31:00"), at("09:31:00", date(2026, 10, 13))


def scored(
    rows: list[tuple[datetime, tuple[float, float, float], tuple[float, float, float], float, int]],
    name: str = "alg",
) -> list[KindScored]:
    """(bar close, probabilities, (q10, q50, q90), r, class) as scored range predictions."""
    out = []
    for bar_close, p, (q10, q50, q90), r, outcome in rows:
        output = {**dict(zip(RANGE.p_keys(), p, strict=True)), "q10": q10, "q50": q50, "q90": q90}
        out.append(KindScored(name, "v1", SYMBOL, bar_close, p, outcome, output, {"r": r}))
    return out


# | # | day | p (small, medium, large) | q10 q50 q90 | r | class | pinball q10 | q50 | q90 | in | |r − q50|
# | 1 | 1 | .6 .3 .1 | 1 2 4 | 3 | medium | .1·2 = .2 | .5·1 = .5 | .1·1 = .1 | yes | 1
# | 2 | 1 | .1 .3 .6 | 1 2 4 | 0.5 | small | .9·.5 = .45 | .5·1.5 = .75 | .1·3.5 = .35 | no | 1.5
# | 3 | 2 | .2 .2 .6 | 0 1 2 | 5 | large | .1·5 = .5 | .5·4 = 2 | .9·3 = 2.7 | no | 4
# | 4 | 2 | .5 .3 .2 | 0 1 2 | 1 | small | .1·1 = .1 | 0 | .1·1 = .1 | yes | 0
EXAMPLE = [
    (D1, (0.6, 0.3, 0.1), (1.0, 2.0, 4.0), 3.0, 1),
    (D1 + timedelta(minutes=1), (0.1, 0.3, 0.6), (1.0, 2.0, 4.0), 0.5, 0),
    (D2, (0.2, 0.2, 0.6), (0.0, 1.0, 2.0), 5.0, 2),
    (D2 + timedelta(minutes=1), (0.5, 0.3, 0.2), (0.0, 1.0, 2.0), 1.0, 0),
]


def test_pinball_loss_coverage_and_the_median_s_error_as_worked_out_by_hand() -> None:
    stats = quantile_stats("model", scored(EXAMPLE), {})

    assert stats["n"] == 4
    assert stats["pinball"]["q10"]["mean"]["value"] == pytest.approx(1.25 / 4)
    assert stats["pinball"]["q50"]["mean"]["value"] == pytest.approx(3.25 / 4)
    assert stats["pinball"]["q90"]["mean"]["value"] == pytest.approx(3.25 / 4)
    # q10's losses .2, .45, .5, .1: standard deviation 0.19311, so ± 1.96 × 0.19311 / 2.
    q10 = stats["pinball"]["q10"]["mean"]
    assert (q10["low"], q10["high"]) == pytest.approx((0.3125 - 0.189245, 0.3125 + 0.189245), abs=1e-5)
    # By day: .325 and .3, a Student t (1 degree of freedom, 12.706) of their spread.
    by_day = stats["pinball"]["q10"]["by_day"]
    assert (by_day["value"], by_day["n"]) == (pytest.approx(0.3125), 2)
    assert by_day["low"] == pytest.approx(0.3125 - 12.7062 * 0.0125, abs=1e-4)
    # Within q10–q90: #1 and #4, 2 of 4 (Wilson 95%: 0.150–0.850).
    coverage = stats["interval_coverage"]
    assert (coverage["value"], coverage["n"]) == (0.5, 4)
    assert (coverage["low"], coverage["high"]) == pytest.approx((0.1500, 0.8500), abs=1e-3)
    assert stats["q50_abs_error"]["value"] == pytest.approx(6.5 / 4)
    assert stats["vs_climate"] is None  # no climate to compare against
    assert pinball(2.0, 0.5, 2.0) == 0.0


def test_the_pinball_loss_against_the_climate_s_pairs_the_same_bar_closes() -> None:
    """Input: the climate's q10 0 at #1 and #2 only (r 3 and 0.5: losses .3 and .05). Output:
    the predictor's q10 loss less the climate's, -.1 and +.4, a mean of .15 over 2 pairs; one
    day only, so none by day. The climate's own statistics have none."""
    climate = scored([(t, p, (0.0, 2.0, 4.0), r, c) for t, p, _, r, c in EXAMPLE[:2]], CLIMATE)

    stats = quantile_stats("model", scored(EXAMPLE), {CLIMATE: climate})

    vs = stats["vs_climate"]["q10"]
    assert (vs["mean"]["value"], vs["mean"]["n"]) == (pytest.approx(0.15), 2)
    assert vs["by_day"] is None
    assert stats["vs_climate"]["q50"]["mean"]["value"] == pytest.approx(0.0)  # q50 2 both
    assert quantile_stats(CLIMATE, climate, {CLIMATE: climate})["vs_climate"] is None
    # Before the climate has anything scored, it is still itself: no comparison (review on #197).
    assert quantile_stats(CLIMATE, [], {CLIMATE: []})["vs_climate"] is None
    check("range_stats", stats)


def test_precision_and_coverage_count_the_small_and_large_calls() -> None:
    """The example's calls: small (#1, wrong; #4, right), large (#2 wrong, #3 right): precision
    2/4, coverage 4/4; accuracy 2/4."""
    summary = summarize(scored(EXAMPLE), RANGE)

    assert summary.precision is not None and (summary.precision.value, summary.precision.n) == (0.5, 4)
    assert summary.coverage is not None and summary.coverage.value == 1.0
    assert summary.accuracy is not None and summary.accuracy.value == 0.5
    assert summary.extra is not None and summary.extra["n"] == 4


# The baselines.


def inputs(previous: Answer | None, flat: int | None = 2, bar_close: datetime = T) -> KindInputs:
    return KindInputs(SYMBOL, bar_close, 81_250.0, 100.0, flat, previous, {})


def test_the_persistence_carries_the_last_window_s_range_on() -> None:
    """Input: the last window's r 2.5 (its k 3), the bar close's k 2. Output: medium (2 ≤ 2.5 < 4)
    with certainty, every quantile 2.5; nothing without the last window's class."""
    persist = RangePersist()
    previous = Answer("small", None, {"r": 2.5, "k": 3})

    forecast = persist.predict(inputs(previous))

    assert forecast == KindForecast((0.0, 1.0, 0.0), {"q10": 2.5, "q50": 2.5, "q90": 2.5})
    assert RANGE.invalid(forecast) is None
    assert persist.predict(inputs(None)) is None
    assert persist.predict(inputs(Answer(None, "silent quotes", {"r": 1.0}))) is None


def sample(bar_close: datetime, label: str, r: float, symbol: str = SYMBOL) -> KindSample:
    return KindSample(symbol, bar_close, FeatureVector({}, ()), Answer(label, None, {"r": r}), 1, "mid")


def test_the_climate_counts_each_symbol_and_slot_s_classes_and_quantiles() -> None:
    """Input: the 09:00 slot's answers r 0, 1, 2, 3, 6 (small, small, medium, medium, large) and
    the 10:00 slot's r 4 (large). Output: the 09:00 cell's shares 2/5, 2/5, 1/5 and quantiles at
    0.4, 2 and 3.6 of the sorted r (q·(n − 1), interpolated: 0 + 0.4·1, 2, 3 + 0.6·3 = 4.8);
    a slot without answers backs off to the symbol's day; without a table, a third each and
    0, k, 2k."""
    closes = [at(f"09:{m:02d}:00") for m in (5, 10, 15, 20, 25)]
    samples = [
        sample(t, c, r)
        for t, c, r in zip(
            closes, ["small", "small", "medium", "medium", "large"], [0, 1, 2, 3, 6], strict=True
        )
    ]
    samples.append(sample(at("10:05:00"), "large", 4.0))
    climate = RangeClimate()

    table = climate.fit(samples)
    bound = climate.using(table)

    assert table.version.startswith(f"{CLIMATE}-20261012-") and table.target_version == 1
    cell = bound.predict(inputs(None, bar_close=at("09:31:00", DAY) - timedelta(minutes=1)))
    assert cell.probabilities == pytest.approx((0.4, 0.4, 0.2))
    assert [cell.fields[q] for q in ("q10", "q50", "q90")] == pytest.approx([0.4, 2.0, 4.8])
    day = bound.predict(inputs(None, bar_close=at("13:05:00")))  # no 13:00 cell: the whole day
    assert day.probabilities == pytest.approx((2 / 6, 2 / 6, 2 / 6))
    assert bound.version == table.version
    assert climate.predict(inputs(None)) == KindForecast(
        (1 / 3, 1 / 3, 1 / 3), {"q10": 0.0, "q50": 2.0, "q90": 4.0}
    )
    assert climate.version == f"{CLIMATE}-default"
    assert climate.fit([]) == climate.default_table()


# Through the Replay Harness.


def morning() -> list[Any]:
    """The book at 81,200 / 81,300 (mid 81,250) every 10 s from 09:00:30, at 81,400 / 81,500
    (81,450, two ticks up) from 09:32:00 to 09:32:50, back at 81,250 from 09:33:00, silent from
    09:39:50 to 09:41:10, then at 81,250 again to 09:50."""
    return (
        steady_book(at("09:00:30"), at("09:31:50"), 81_200, 81_300)
        + steady_book(at("09:32:00"), at("09:32:50"), 81_400, 81_500)
        + steady_book(at("09:33:00"), at("09:39:50"), 81_200, 81_300)
        + steady_book(at("09:41:10"), at("09:50:00"), 81_200, 81_300)
    )


def range_answers(db: Path) -> dict[str, dict[str, Any]]:
    store = PredictionStore(db)
    try:
        return {a["bar_close"][11:16]: a for a in store.kind_answers(RANGE.name, SYMBOL)}
    finally:
        store.close()


def range_predictions(db: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """The range's predictions by (HH:MM, predictor)."""
    store = PredictionStore(db)
    try:
        rows = store.kind_recent(RANGE.name, RANGE.answer_version, SYMBOL, 1000)
    finally:
        store.close()
    return {(r["bar_close"][11:16], r["algorithm"]): r for r in rows}


async def test_both_baselines_predict_and_are_scored_and_ranked_on_the_range(tmp_path: Path) -> None:
    """Input: the morning above, replayed with the kinds the engine predicts. Output: the
    answers by hand (09:28 nothing moves; 09:29 to 09:32 see the two-tick rise; 09:33 starts at
    81,450 and falls two ticks; 09:37 to 09:40 went silent; 09:41 has no mid-point); the
    persistence carries each last window on and does not predict where it could not be scored
    (09:01: no window followed; 09:41: the 09:37 window went silent); the climate its defaults
    (no table: a third each, 0, k and 2k); both scored and on ``/v1/leaderboard?kind=range``."""
    db = tmp_path / "engine.db"
    engine = await run(db, morning(), until=at("09:50:00"), target_version=TARGET_VERSION)

    answers = range_answers(db)
    shown = {
        t: (answers[t]["class"], answers[t]["path"].get("r"))
        for t in ("09:28", "09:29", "09:32", "09:33", "09:34")
    }
    assert shown == {
        "09:28": ("small", 0.0),
        "09:29": ("medium", 2.0),
        "09:32": ("medium", 2.0),
        "09:33": ("medium", 2.0),
        "09:34": ("small", 0.0),
    }
    assert (answers["09:33"]["path"]["up"], answers["09:33"]["path"]["down"]) == (0.0, 2.0)
    assert [answers[t]["unscorable"] for t in ("09:37", "09:40", "09:41")] == [
        "silent quotes",
        "silent quotes",
        "missing mid",
    ]
    made = range_predictions(db)
    assert made[("09:01", PERSIST)]["status"] == "no_previous"
    assert made[("09:41", PERSIST)]["status"] == "no_previous"
    assert made[("09:32", PERSIST)]["output"] == {
        "p_small": 1.0,
        "p_medium": 0.0,
        "p_large": 0.0,
        "q10": 0.0,
        "q50": 0.0,
        "q90": 0.0,
    }
    assert made[("09:33", PERSIST)]["output"]["p_medium"] == 1.0, "09:29–09:33 rose two ticks"
    climate = made[("09:31", CLIMATE)]
    assert climate["model_version"] == f"{CLIMATE}-default"
    assert climate["output"] == {
        "p_small": 1 / 3,
        "p_medium": 1 / 3,
        "p_large": 1 / 3,
        "q10": 0.0,
        "q50": 2.0,
        "q90": 4.0,
    }
    async with api(engine) as client:
        board = await get_json(client, "/v1/leaderboard?kind=range")
        kinds = await get_json(client, "/v1/kinds")
        latest = await get_json(client, f"/v1/symbols/{SYMBOL}/kinds/range/latest")
        persist = await get_json(client, f"/v1/algorithms/{PERSIST}/stats")
    check("kind_leaderboard", board)
    check("kind_algorithm_stats", persist)
    entries = {e["algorithm"]: e for e in board["baselines"]}
    assert set(entries) == {CLIMATE, PERSIST}
    assert entries[PERSIST]["n"] > 0 and entries[CLIMATE]["n"] > 0
    assert entries[PERSIST]["excluded"]["status"]["no_previous"] >= 2
    for entry in entries.values():
        check("range_stats", entry["extra"])
        assert entry["extra"]["n"] == entry["n"], "every scored answer has its r"
    assert entries[CLIMATE]["extra"]["vs_climate"] is None
    assert entries[PERSIST]["extra"]["vs_climate"] is not None
    assert [k["name"] for k in kinds["kinds"]][:3] == ["direction", "range", "barrier"]
    assert [b["name"] for b in kinds["kinds"][1]["baselines"]] == [CLIMATE, PERSIST]
    for p in latest["predictions"]:
        check("range_output", p["output"])


async def test_the_climate_predicts_from_the_table_it_was_given(tmp_path: Path) -> None:
    """Input: a fitted table (005930's 09:30 slot: small 0.7, medium 0.2, large 0.1, quantiles
    0.5, 1 and 3). Output: the climate's predictions in that slot are the cell, with the table's
    version; the 08:00 swap's new table runs from the next bar close."""
    table = Model(
        f"{CLIMATE}-20261009-abcdef",
        {
            f"{SYMBOL}.09:30.p_small": 0.7,
            f"{SYMBOL}.09:30.p_medium": 0.2,
            f"{SYMBOL}.09:30.p_large": 0.1,
            f"{SYMBOL}.09:30.q10": 0.5,
            f"{SYMBOL}.09:30.q50": 1.0,
            f"{SYMBOL}.09:30.q90": 3.0,
            f"{SYMBOL}.09:30.n": 300.0,
        },
        target_version=1,
    )
    db = tmp_path / "engine.db"
    engine = await run(
        db, morning(), until=at("09:35:00"), target_version=TARGET_VERSION, models={CLIMATE: table}
    )

    made = range_predictions(db)
    assert made[("09:31", CLIMATE)]["model_version"] == table.version
    assert made[("09:31", CLIMATE)]["output"] == {
        "p_small": 0.7,
        "p_medium": 0.2,
        "p_large": 0.1,
        "q10": 0.5,
        "q50": 1.0,
        "q90": 3.0,
    }
    # 09:30 is the 09:00 slot, which the table lacks, as the symbol's day: the defaults.
    assert made[("09:30", CLIMATE)]["model_version"] == table.version
    assert made[("09:30", CLIMATE)]["output"]["p_small"] == pytest.approx(1 / 3)
    newer = Model(f"{CLIMATE}-20261012-123456", {}, target_version=1)
    assert engine.switch_models({CLIMATE: newer})
    assert engine.kind_tables[CLIMATE] == newer
    assert not engine.switch_models({CLIMATE: newer})
    engine.store.close()


def band_crossing() -> list[Any]:
    """The book at 86,900 / 87,000 (mid 86,950: k = 2) every 10 s from 09:00:30 to 09:31:50,
    then at 87,100 / 87,200 (mid 87,150, two ticks up, where k = 3) from 09:32:00 to 09:40."""
    return steady_book(at("09:00:30"), at("09:31:50"), 86_900, 87_000) + steady_book(
        at("09:32:00"), at("09:40:00"), 87_100, 87_200
    )


async def test_a_range_prediction_keeps_its_bar_close_s_k_after_the_price_moves_into_another_band(
    tmp_path: Path,
) -> None:
    """#200. Input: the book above: in the 09:31 window the mid-point crosses the cost-rounding
    boundary, from k = 2 to k = 3. Output: before its answer, every range prediction of the
    09:31 bar close carries k = 2 (``flat_ticks``, what its predictors were given), while
    ``/v1/symbols`` already shows 3 and the 09:33 bar close's predictions carry 3; after its
    answer (r = 2: medium against k = 2, where k = 3 would call it small) they still carry 2,
    the answer's k. The stream's ``kind_prediction`` items carry their own bar close's k."""
    db = tmp_path / "engine.db"
    events = band_crossing()
    split = at("09:34:00")
    engine = await run(
        db, [e for e in events if e[1]["received_at"] <= split], until=split, target_version=TARGET_VERSION
    )
    at_0931 = f"/v1/symbols/{SYMBOL}/kinds/range/predictions?bar_close=2026-10-12T09:31:00%2B09:00"

    async with api(engine) as client:
        before = await get_json(client, at_0931)
        at_0933 = await get_json(
            client, f"/v1/symbols/{SYMBOL}/kinds/range/predictions?bar_close=2026-10-12T09:33:00%2B09:00"
        )
        symbols = await get_json(client, "/v1/symbols")
        ws = await client.ws_connect("/v1/stream")
        assert isinstance(engine.clock, FakeClock)
        rest = [e for e in events if e[1]["received_at"] > split]
        await replay_into(engine, engine.clock, rest, until=at("09:36:00"))
        heard: list[dict[str, Any]] = []
        last = "2026-10-12T09:36:00+09:00"
        async with asyncio.timeout(5):
            while not any(e["type"] == "kind_prediction" and e["bar_close"] == last for e in heard):
                heard.append(await ws.receive_json())
                check("event", heard[-1])
        await ws.close()
        after = await get_json(client, at_0931)

    check("kind_predictions", before)
    check("kind_predictions", after)
    assert {CLIMATE, PERSIST} <= {p["algorithm"] for p in before["predictions"]}
    assert {(p["flat_ticks"], p["answer"] is None) for p in before["predictions"]} == {(2, True)}
    assert symbols["symbols"][0]["flat_ticks"] == 3, "k at the mid-point now"
    assert {p["flat_ticks"] for p in at_0933["predictions"]} == {3}
    assert {p["flat_ticks"] for p in after["predictions"]} == {2}, "the bar close's, kept"
    answered = {(p["answer"]["class"], p["answer"]["path"]["r"]) for p in after["predictions"]}
    assert answered == {("medium", 2.0)}, "two ticks against k = 2"
    assert {p["answer"]["path"]["k"] for p in after["predictions"]} == {2}
    made = {
        e["bar_close"][11:16]: {p["flat_ticks"] for p in e["predictions"]}
        for e in heard
        if e["type"] == "kind_prediction" and e["prediction_kind"] == RANGE.name
    }
    assert made["09:35"] == made["09:36"] == {3}


# Training and pre-validation.


async def test_a_recorded_day_trains_on_the_range_answers_the_engine_scores(tmp_path: Path) -> None:
    """Input: a busy morning, recorded, and replayed through the engine. Output: the day's range
    samples carry the answers the engine stored for the same windows (class, reason, path)."""
    events = busy_morning()
    data = tmp_path / "data"
    record(data, events)
    engine = await run(tmp_path / "engine.db", events, until=at("10:00:00"), target_version=TARGET_VERSION)
    stored = {
        datetime.fromisoformat(a["bar_close"]): a for a in engine.store.kind_answers(RANGE.name, SYMBOL)
    }
    engine.store.close()

    day = training_day(
        data,
        DAY,
        CALENDAR,
        (SYMBOL,),
        ("velocity",),
        FEE,
        Leaders(),
        target_version=TARGET_VERSION,
        kinds=(RANGE,),
    )

    assert day is not None
    samples = [s for s in day.of_kind(RANGE, ("velocity",)) if s.bar_close <= at("09:56:00")]
    assert len(samples) == 56
    for s in samples:
        assert s.answer is not None
        assert (s.answer.label, s.answer.unscorable) == (stored[s.bar_close]["class"], None)
        assert dict(s.answer.path) == stored[s.bar_close]["path"]


class RangeShares:
    """A range stand-in: the classes' shares it was trained on, quantiles 0, 1 and 2 ticks."""

    kind = RANGE.name
    training_days = 4
    name = "shares"
    features: tuple[str, ...] = ("velocity",)
    card = stand_in_card("shares", "the range's class shares", ("velocity",), "counting")
    param_meanings = stand_in_meanings("small", "medium", "large")

    def default_model(self) -> Model:
        return Model("shares-default", {"small": 1 / 3, "medium": 1 / 3, "large": 1 / 3})

    def fit(self, samples: Sequence[KindSample]) -> Model:
        labelled = [s for s in samples if s.label is not None]
        params = {c: sum(s.label == c for s in labelled) / len(labelled) for c in RANGE.classes}
        return Model.fitted(self.name, params, labelled, 0.0)

    def predict(self, features: FeatureVector, model: Model) -> KindForecast:
        small, medium, large = (model.params[c] for c in RANGE.classes)
        return KindForecast((small, medium, large), {"q10": 0.0, "q50": 1.0, "q90": 2.0})


async def test_a_range_algorithm_is_prevalidated_against_a_climate_refitted_before_each_day(
    tmp_path: Path,
) -> None:
    """Input: ten recorded days of a swinging market, and a range stand-in. Output: its report
    names the range and its answer version, its lifts are over the range's two baselines, its
    own statistics are the range's; the climate's table of each day replayed was fitted on the
    days before it (the first day none: its defaults)."""
    s = prevalidation_settings(tmp_path)

    await prevalidate(s, "shares", algorithms=[ALPHA], kind_algorithms=[RangeShares()], now=AFTER)

    saved = prevalidation_report(s, "shares")
    assert (saved["kind"], saved["target_version"]) == ("range", 1)
    assert set(saved["stats"]["lift"]["vs"]) == {CLIMATE, PERSIST}
    check("range_stats", saved["stats"]["extra"])
    store = PredictionStore(s.state_dir / "prevalidation-shares.sqlite3")
    try:
        versions = {
            day: [
                r["model_version"]
                for r in store.kind_at_close(
                    RANGE.name, 1, SYMBOL, at("09:31:00", date.fromisoformat(day)), CLIMATE
                )
            ]
            for day in saved["days"]
        }
    finally:
        store.close()
    first, second = saved["days"][:2]
    assert versions[first] == [f"{CLIMATE}-default"], "no day before the first"
    [fitted] = versions[second]
    assert fitted.startswith(f"{CLIMATE}-{first.replace('-', '')}-"), "fitted on the days before it"
