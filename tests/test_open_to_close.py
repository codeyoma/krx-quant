"""The daily kind open_to_close (#264): its flat band, its answers, its baselines and its P&L,
by hand on made-up KRX days."""

from __future__ import annotations

import math
import random
from collections import Counter
from collections.abc import Sequence
from datetime import date, datetime, timedelta

import pytest

from krx_quant.auctions import closes_pending
from krx_quant.daily import NO_BAND, DailyInputs, DayPrices
from krx_quant.feeds.base import KST
from krx_quant.kind_registry import DAILY_KINDS, KINDS
from krx_quant.kinds import KindScored
from krx_quant.open_to_close import (
    CLIMATE,
    FLAT_NAME,
    NORMAL_SCALE,
    OPEN_TO_CLOSE,
    PERSIST,
    OtcClimate,
    OtcFlat,
    OtcPersist,
    answers,
    band,
    call_profit,
    classify,
    inverse_profit,
    open_to_close_stats,
)

FEE = 0.015 / 100
FIRST = date(2024, 1, 1)


def made_days(changes: Sequence[float], first: date = FIRST, price: float = 10_000.0) -> list[DayPrices]:
    """A KRX day for each change (close ÷ open − 1), on consecutive dates, each opening at the
    last close."""
    out = []
    for i, change in enumerate(changes):
        close = price * (1 + change)
        out.append(
            DayPrices(first + timedelta(days=i), price, max(price, close), min(price, close), close, 1_000)
        )
        price = close
    return out


def test_open_to_close_is_registered_as_a_daily_kind_apart_from_the_bar_close_kinds() -> None:
    assert [k.name for k in DAILY_KINDS] == ["open_to_close", "open_to_close_5d"]  # #310 after it
    assert "open_to_close" not in [k.name for k in KINDS]
    kind = DAILY_KINDS[0]
    # Answer version 2 (#264): the close is the closing auction's, not the KRX day candle's.
    assert (kind.classes, kind.action, kind.answer_version) == (("up", "flat", "down"), ("up", "down"), 2)
    assert kind.pending is closes_pending, "its latest closes kept before a simulation or a walk-forward"
    assert kind.predict_at.isoformat() == "08:50:00"
    assert [b.name for b in kind.daily_baselines] == [FLAT_NAME, CLIMATE, PERSIST]
    # Judged on 20, 40 and 60 days with an answer, the latest 20 for the bench (#298: from 60, 120
    # and 180, and 60): the first 20 are simulated at once.
    rules = kind.review_rules
    assert (rules.probation_days, rules.bench_days, kind.recent_days, kind.min_sample) == (
        (20, 40, 60),
        (20, 40, 60),
        20,
        60,
    )
    shown = kind.as_dict(kind.baseline_cards())
    assert shown["horizon_minutes"] is None
    assert shown["daily"] == {
        "predict_at": "08:50",
        "from": "open",
        "to": "close",
        "from_title": "시가 단일가",
        "to_title": "종가 단일가",
        "sessions": 1,  # #310
        "answer_from": "15:45",
        "probation_days": [20, 40, 60],
        "bench_days": [20, 40, 60],
        "recent_days": 20,
    }


def test_a_day_has_no_band_before_20_days_and_the_normal_scale_before_120() -> None:
    """±1% days: σ̂ is 1% (their root mean square) from the 21st day; with fewer than 120
    standardized days the scale is Φ⁻¹(2/3), so the band is 0.4307%."""
    days = made_days([0.01 if i % 2 else -0.01 for i in range(40)])
    found = answers(days, FEE)

    assert [a.unscorable for a in found[:20]] == [NO_BAND] * 20
    assert all(a.label is None and a.path["band"] is None for a in found[:20])
    day = found[20]
    assert day.path["sigma"] == pytest.approx(0.01)
    assert day.path["scale"] == pytest.approx(NORMAL_SCALE)
    assert day.path["band"] == pytest.approx(0.004307, abs=1e-6)
    assert day.label == ("up" if days[20].change > 0 else "down")
    assert NORMAL_SCALE == pytest.approx(0.430727, abs=1e-6)


def test_the_scale_is_the_tercile_of_the_standardized_moves_once_there_are_120() -> None:
    """Every day ±1%: σ̂ is 1% and every standardized move 1, so from the 141st day (20 days for
    σ̂, then 120 standardized) the scale is 1 and the band 1%: a 0.99% day is flat, −1.01% down."""
    moves = [0.01 if i % 2 else -0.01 for i in range(140)]
    days = made_days([*moves, 0.0099, -0.0101, 0.0])
    found = answers(days, FEE)

    assert found[139].path["scale"] == pytest.approx(NORMAL_SCALE), "119 standardized days: still the normal"
    assert [a.path["scale"] for a in found[140:]] == pytest.approx([1.0, 1.0, 1.0])
    assert [a.path["band"] for a in found[140:]] == pytest.approx([0.01, 0.01, 0.01], abs=1e-5)
    assert [a.label for a in found[140:]] == ["flat", "down", "flat"], "0.99% is within the band; −1.01% not"
    after = band(days[:140])
    assert after is not None and (after.scale, after.value) == pytest.approx((1.0, 0.01))


def test_the_classes_come_out_about_a_third_each_with_fat_tails_and_volatility_clusters() -> None:
    rng = random.Random(264)
    changes, vol = [], 0.015
    for _ in range(2_000):
        vol = min(0.05, max(0.004, vol * math.exp(rng.gauss(0, 0.06))))
        changes.append(vol * rng.gauss(0, 1) * (1 if rng.random() > 0.1 else 3))  # a tenth of days 3x
    counted = Counter(a.label for a in answers(made_days(changes), FEE)[600:])

    assert sum(counted.values()) == 1_400
    assert all(abs(counted[c] / 1_400 - 1 / 3) < 0.04 for c in ("up", "flat", "down")), counted


def test_a_days_answer_knows_nothing_of_the_days_after_it() -> None:
    rng = random.Random(7)
    days = made_days([rng.gauss(0, 0.02) for _ in range(700)])
    everything = answers(days, FEE)
    for k in (25, 160, 400, 699):
        assert answers(days[: k + 1], FEE)[k] == everything[k]
    changed = [*days[:400], *made_days([0.2] * 300, days[400].day, days[399].close)]
    assert answers(changed, FEE)[:400] == everything[:400]


def test_an_adjusted_past_gives_the_same_answers() -> None:
    """A 50:1 split adjusts every price before it: the days' changes, bands and classes stay."""
    rng = random.Random(11)
    days = made_days([rng.gauss(0, 0.02) for _ in range(300)], price=2_500_000.0)
    adjusted = [
        DayPrices(d.day, d.open / 50, d.high / 50, d.low / 50, d.close / 50, d.volume * 50) if i < 200 else d
        for i, d in enumerate(days)
    ]
    before, after = answers(days, FEE), answers(adjusted, FEE)
    assert [a.label for a in after] == [a.label for a in before]
    for key in ("change", "band", "sigma", "scale"):
        assert [a.path[key] for a in after] == pytest.approx([a.path[key] for a in before], nan_ok=True)


def test_classify_is_flat_within_the_band_inclusive() -> None:
    assert [classify(c, 0.01) for c in (0.0101, 0.01, 0.0, -0.01, -0.0101)] == [
        "up",
        "flat",
        "flat",
        "flat",
        "down",
    ]


def inputs(past: Sequence[tuple[date, str | None]], previous: date | None) -> DailyInputs:
    return DailyInputs(
        "005930", date(2026, 10, 12), datetime(2026, 10, 12, 8, 50, tzinfo=KST), tuple(past), None, previous
    )


def test_the_baselines() -> None:
    thu = date(2026, 10, 8)
    past = [(date(2026, 10, 1), "up"), (date(2026, 10, 2), None), (date(2026, 10, 6), "up"), (thu, "down")]

    assert OtcFlat().predict(inputs(past, thu)).probabilities == (0.0, 1.0, 0.0)  # type: ignore[union-attr]
    climate = OtcClimate().predict(inputs(past, thu))
    assert climate is not None and climate.probabilities == pytest.approx((3 / 6, 1 / 6, 2 / 6))
    assert OtcClimate().predict(inputs([], thu)).probabilities == pytest.approx((1 / 3,) * 3)  # type: ignore[union-attr]
    persist = OtcPersist().predict(inputs(past, thu))
    assert persist is not None and persist.probabilities == (0.0, 0.0, 1.0)
    assert OtcPersist().predict(inputs(past[:3], thu)) is None, "the previous trading day's candle is not in"
    assert OtcPersist().predict(inputs([*past[:3], (thu, None)], thu)) is None, "no band that day"


def scored(
    predicted: str, change: float, symbol: str = "005930", day: date = date(2026, 10, 12)
) -> KindScored:
    probabilities = tuple(1.0 if c == predicted else 0.0 for c in OPEN_TO_CLOSE.classes)
    path = {"change": change, "fee_rate": FEE}
    return KindScored(
        "x",
        "x-v1",
        symbol,
        datetime.combine(day, OPEN_TO_CLOSE.predict_at, KST),
        probabilities,  # type: ignore[arg-type]
        0,
        {},
        path,
    )


def test_the_pnl_by_hand() -> None:
    """Fee 0.015%: a stock round trip costs 0.20% + 0.03% = 23 bp; the −2x inverse product
    0.03% of half the notional, 1.5 bp."""
    assert call_profit("up", {"change": 0.01, "fee_rate": FEE}) == pytest.approx(77.0)
    assert call_profit("down", {"change": -0.01, "fee_rate": FEE}) == pytest.approx(77.0)
    assert call_profit("down", {"change": 0.01, "fee_rate": FEE}) == pytest.approx(-123.0)
    assert call_profit("flat", {"change": 0.01, "fee_rate": FEE}) is None
    path = {"change": -0.01, "fee_rate": FEE}
    assert inverse_profit("down", path, "005930", date(2026, 10, 12)) == pytest.approx(98.5)
    assert inverse_profit("down", path, "009150", date(2026, 10, 12)) is None, "009150 has no inverse product"
    assert inverse_profit("down", path, "005930", date(2026, 5, 26)) is None, "before its listing"
    assert inverse_profit("up", path, "005930", date(2026, 10, 12)) is None

    stats = open_to_close_stats(
        "x",
        [scored("up", 0.01), scored("down", -0.01), scored("flat", 0.02), scored("down", 0.01, "009150")],
        {},
    )
    assert stats["n"] == 4
    assert (stats["pnl"]["trades"], stats["pnl"]["total_bp"]) == (3, pytest.approx(77 + 77 - 123))
    assert (stats["long"]["trades"], stats["short"]["trades"]) == (1, 2)
    assert (stats["short_inverse"]["trades"], stats["short_inverse"]["total_bp"]) == (1, pytest.approx(98.5))
    assert stats["pnl_inverse"]["total_bp"] == pytest.approx(77 + 98.5)
