"""The range's first model, Mandelbrot (#181): its features, its regressions and forecasts by
hand, its training on the range's answers (recorded days by the quotes, bar days by the bar
rule), the night's report and its pre-validation.

Prices: at 81,200 the tick is 100 won and k = 2 (the cost 81,200 × 0.23% = 186.8 won covers
two ticks): small r < 2, medium 2 ≤ r < 4, large r ≥ 4.
"""

from __future__ import annotations

import math
import random
import sqlite3
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from krx_quant.algorithms import KIND_ALGORITHMS, Abstention, KindSample, Model, NotPredicted
from krx_quant.algorithms.mandelbrot import INPUTS, NAME, PARAM_MEANINGS, Mandelbrot, slot_of
from krx_quant.algorithms.turing import Turing
from krx_quant.backfill import BAR_SCHEMA, MINUTE_BARS_TR, PreviousDay, bars_key
from krx_quant.clock import FakeClock
from krx_quant.engine import PredictionEngine
from krx_quant.features import (
    FEATURE_MEANINGS,
    FEATURES,
    RANGE_FEATURES,
    FeatureStore,
    FeatureVector,
    Leaders,
    Snapshot,
    day_slot_ranges,
)
from krx_quant.feeds import KST
from krx_quant.kind_registry import kind_named
from krx_quant.kinds import Answer, KindForecast
from krx_quant.market_calendar import MarketCalendar
from krx_quant.nightly import train_night
from krx_quant.object_store import LocalObjectStore
from krx_quant.predictions import PredictionStore
from krx_quant.prevalidation import REGISTERED_BEFORE, admits, prevalidate
from krx_quant.ranges import CLIMATE, PERSIST, RANGE, range_class
from krx_quant.reference import Bar
from krx_quant.training import training_day
from tests.algorithm_doubles import stand_in_card, stand_in_meanings
from tests.api_schema import check
from tests.replay_events import CALENDAR, DAY, FEE, SYMBOL, at, run, vi
from tests.test_nightly import DAYS as NIGHT_DAYS
from tests.test_nightly import FINISHED, NIGHT, archive_bars, archive_events, read
from tests.test_prevalidation import AFTER, ALPHA
from tests.test_prevalidation import report as prevalidation_report
from tests.test_prevalidation import settings as prevalidation_settings
from tests.test_replay import busy_morning
from tests.test_replay_cli import record

MANDELBROT = Mandelbrot()
TUESDAY = DAY + timedelta(days=1)  # DAY is Monday 2026-10-12
TWO_DAYS = MarketCalendar({DAY: True, TUESDAY: True})


def bar(day: date, hhmm: str, low: float, high: float, close: float | None = None) -> Bar:
    start = datetime.combine(day, time.fromisoformat(hhmm), KST)
    last = high if close is None else close
    return Bar(start, low, high, low, last, 10, 1, start, start)


def snapshot(
    bars: Sequence[Bar],
    close: str,
    price: float | None = 81_200,
    previous: dict[str, float] | None = None,
    day: date = TUESDAY,
) -> Snapshot:
    return Snapshot.at(
        SYMBOL,
        at(close, day),
        [b for b in bars if b.start + timedelta(minutes=1) <= at(close, day)],
        price,
        FEE,
        session_open=at("09:00:00", day),
        previous_ranges=previous,
    )


def features(s: Snapshot) -> dict[str, Any]:
    return {name: FEATURES[name](s) for name in sorted(RANGE_FEATURES)}


# The features, by hand.

MORNING = [
    bar(TUESDAY, "08:59", 81_000, 81_600),  # the NXT pre-market: left out
    bar(TUESDAY, "09:00", 81_100, 81_300),  # 2 ticks
    bar(TUESDAY, "09:01", 81_200, 81_200),  # 0
    bar(TUESDAY, "09:02", 81_100, 81_400),  # 3
    bar(TUESDAY, "09:03", 81_200, 81_300),  # 1
    bar(TUESDAY, "09:04", 81_100, 81_300),  # 2
    bar(TUESDAY, "09:05", 81_200, 81_300),  # 1
    bar(TUESDAY, "09:06", 81_100, 81_400),  # 3
    # 09:07: no trade
    bar(TUESDAY, "09:08", 81_200, 81_400),  # 2
    bar(TUESDAY, "09:09", 81_100, 81_300),  # 2
]


def test_the_ranges_are_the_mean_one_minute_range_from_the_open_an_empty_minute_zero() -> None:
    """Input: the morning above, at the bar close 09:10 (81,200: a 100-won tick). Output: by
    hand, range_4 = (3 + 0 + 2 + 2) / 4 = 1.75 (09:07 traded nothing: 0); range_20 and range_60
    have only the ten minutes since the open: 16 / 10 = 1.6 (the pre-market's bar is not the
    session's)."""
    found = features(snapshot(MORNING, "09:10:00"))

    assert found["range_4"] == pytest.approx(1.75)
    assert found["range_20"] == pytest.approx(1.6)
    assert found["range_60"] == pytest.approx(1.6)


def test_the_ranges_are_unknown_without_a_trade_in_their_minutes_or_a_reference_price() -> None:
    """Input: the same morning at 09:14 (nothing traded 09:10–09:13), without a reference
    price, and at the open. Output: range_4 unknown (no trade in its four minutes); range_20
    the 16 ticks over the fourteen minutes since the open; all unknown without a price; none
    at the open (no minute of the session yet)."""
    quiet = features(snapshot(MORNING, "09:14:00"))
    assert quiet["range_4"] is None
    assert quiet["range_20"] == pytest.approx(16 / 14)
    assert set(features(snapshot(MORNING, "09:10:00", price=None)).values()) == {None}
    assert features(snapshot(MORNING, "09:00:00"))["range_60"] is None


PREVIOUS_DAY = [
    bar(DAY, "08:30", 81_000, 81_500),  # the pre-market: left out
    bar(DAY, "09:00", 81_000, 81_300),  # 300 won
    bar(DAY, "09:10", 81_000, 81_300),  # 300: the slot's other 28 minutes traded nothing
    bar(DAY, "15:00", 81_000, 81_200),  # 200
    bar(DAY, "15:19", 81_000, 81_200),  # 200
    bar(DAY, "15:20", 80_000, 81_000),  # the closing auction: left out
    bar(DAY, "15:30", 80_000, 81_000),  # after the close: left out
]


def test_the_previous_day_s_range_is_its_slot_s_mean_over_the_session_s_minutes() -> None:
    """Input: Monday's bars above. Output: by slot, the session's minutes' mean range in won:
    09:00 (09:00–09:29) 600 / 30 = 20; 15:00 has twenty minutes before the closing auction,
    400 / 20 = 20; a slot without a trade is left out. Tuesday's range_prev_day is that in its
    bar close's slot, in ticks of its own price: 0.2 at 09:10 and 15:16, unknown at 10:31 and
    without Monday's bars."""
    session = TWO_DAYS.session(DAY)
    assert session is not None

    slots = day_slot_ranges(PREVIOUS_DAY, session)

    assert slots == {"09:00": pytest.approx(20.0), "15:00": pytest.approx(20.0)}
    assert features(snapshot(MORNING, "09:10:00", previous=slots))["range_prev_day"] == pytest.approx(0.2)
    assert features(snapshot(MORNING, "15:16:00", previous=slots))["range_prev_day"] == pytest.approx(0.2)
    assert features(snapshot(MORNING, "10:31:00", previous=slots))["range_prev_day"] is None
    assert features(snapshot(MORNING, "09:10:00"))["range_prev_day"] is None, "no previous day"
    # 0.4 ticks at a 50-won tick (49,000 won)
    assert features(snapshot(MORNING, "09:10:00", 49_000, slots))["range_prev_day"] == pytest.approx(0.4)


def store_bars(data_dir: Path, day: date, bars: Sequence[Bar], symbol: str = SYMBOL) -> None:
    """A day's bars as the backfill stores them."""
    rows = [
        {
            "symbol": symbol,
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
    path = data_dir / bars_key(day, MINUTE_BARS_TR, symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=BAR_SCHEMA), path)


def test_the_previous_trading_day_is_read_from_its_backfilled_bars(tmp_path: Path) -> None:
    """Input: Monday's backfill. Output: Tuesday's previous day is Monday (its slots, as
    above); Monday's is the Friday before, not backfilled: unknown."""
    store_bars(tmp_path, DAY, PREVIOUS_DAY)
    previous = PreviousDay(tmp_path, TWO_DAYS)
    session = TWO_DAYS.session(DAY)
    assert session is not None

    assert previous(SYMBOL, TUESDAY) == day_slot_ranges(PREVIOUS_DAY, session)
    assert previous(SYMBOL, DAY) == {}


def test_only_the_range_reads_the_range_features() -> None:
    """The direction predicts as it did: Turing, which reads every number of the Feature Store,
    leaves the range's out; every feature has its meaning."""
    assert RANGE_FEATURES == {"range_4", "range_20", "range_60", "range_prev_day"}
    assert not set(Turing.features) & RANGE_FEATURES
    assert all(FEATURE_MEANINGS[name].unit == "틱" for name in RANGE_FEATURES)


# Fit and forecast, by hand.


def vector(
    ranges: Sequence[float | None] = (None, None, None, None),
    volume: float | None = None,
    spread: float | None = None,
    minutes: float = 15.0,
    k: float | None = 2.0,
) -> FeatureVector:
    values = {
        "range_4": ranges[0],
        "range_20": ranges[1],
        "range_60": ranges[2],
        "range_prev_day": ranges[3],
        "relative_volume": volume,
        "spread_ticks": spread,
        "minutes_since_open": minutes,
        "flat_ticks": k,
    }
    return FeatureVector(values, MANDELBROT.features)


def answered(features: FeatureVector, r: float, minute: int = 0) -> KindSample:
    """A sample whose range answer was ``r`` ticks (k = 2)."""
    label = range_class(r, 2)
    return KindSample(
        SYMBOL,
        at("09:31:00") + timedelta(minutes=minute),
        features,
        Answer(label, None, {"r": r, "k": 2}),
        RANGE.answer_version,
        "mid",
    )


COEFFICIENTS = (0.5, 0.2, 0.1, 0.1, 0.05)  # range_4, range_20, range_60, range_prev_day, volume
LEVELS = {0: 0.3, 1: 0.1}  # by slot: 09:01–09:30 and 09:31–10:00


def exact(rng: random.Random, minutes: float) -> tuple[FeatureVector, float]:
    """Inputs at random and the range they make exactly: log(1 + r) = the slot's level + Σ
    coefficient × log(1 + input)."""
    ranges = [rng.uniform(0, 6) for _ in range(4)]
    volume = rng.uniform(0.2, 3)
    log_r = LEVELS[slot_of(minutes)] + sum(
        b * math.log1p(x) for b, x in zip(COEFFICIENTS, [*ranges, volume], strict=True)
    )
    return vector(ranges, volume, minutes=minutes), math.expm1(log_r)


def test_the_regression_recovers_the_coefficients_that_made_the_ranges() -> None:
    """Input: 40 bar closes in two slots whose log(1 + r) is made exactly by the coefficients
    above (no book: no spread). Output: the ``bars`` regression's coefficients are those; each
    slot's intercept is its level at the inputs' means (centred inputs); slots it never saw take
    the nearest one's; the residuals are all 0, so a forecast is certain: its quantiles are the
    range the coefficients make, and every probability is on its class. No ``book`` regression
    without a spread."""
    rng = random.Random(7)
    made = [exact(rng, 5.0 + 30 * (i % 2)) for i in range(40)]

    model = MANDELBROT.fit([answered(f, r, i) for i, (f, r) in enumerate(made)])

    params = model.params
    assert (params["bars.n"], params["book.n"]) == (40, 0)
    for name, b in zip(INPUTS["bars"], COEFFICIENTS, strict=True):
        assert params[f"bars.coef.{name}"] == pytest.approx(b, abs=1e-9)
    means = {name: sum(math.log1p(f[name] or 0.0) for f, _ in made) / len(made) for name in INPUTS["bars"]}
    at_means = sum(b * means[n] for n, b in zip(INPUTS["bars"], COEFFICIENTS, strict=True))
    assert params["bars.slot.00"] == pytest.approx(LEVELS[0] + at_means, abs=1e-9)
    assert params["bars.slot.01"] == pytest.approx(LEVELS[1] + at_means, abs=1e-9)
    assert params["bars.slot.12"] == params["bars.slot.01"], "never seen: the nearest slot"
    assert max(abs(params[f"bars.residual.{p:03d}"]) for p in range(101)) < 1e-9
    assert model.target_version == RANGE.answer_version and model.version.startswith("mandelbrot-")
    features, r = exact(random.Random(99), 45.0)
    forecast = MANDELBROT.predict(features, model)
    assert forecast.fields["q50"] == pytest.approx(r, abs=1e-6)
    assert forecast.fields["q10"] == pytest.approx(r, abs=1e-6) == forecast.fields["q90"]
    assert forecast.probabilities[RANGE.index(range_class(r, 2))] == pytest.approx(1.0)


# Eleven ranges in one slot, nothing else known: the forecast is their own distribution.
ELEVEN = (0.0, 1.0, 1.0, 2.0, 2.0, 3.0, 3.0, 4.0, 5.0, 6.0, 8.0)


def test_the_probabilities_and_the_quantiles_come_from_one_distribution_of_the_residuals() -> None:
    """Input: eleven bar closes of one slot with no input known; ranges 0, 1, 1, 2, 2, 3, 3,
    4, 5, 6, 8. Output: the fit is the slot's mean log(1 + r) and the residuals are the rest, so
    the forecast is the ranges' own distribution: quantiles at q·10 of the sorted ranges, q10 1,
    q50 3, q90 6; below k = 2 three of the ten steps (p_small 0.3), at or above 2k = 4 three
    (p_large 0.3). With k = 3 the median is k: p_small is a half."""
    samples = [answered(vector(), r, i) for i, r in enumerate(ELEVEN)]

    model = MANDELBROT.fit(samples)

    logs = [math.log1p(r) for r in ELEVEN]
    mean = sum(logs) / len(logs)
    assert model.params["bars.slot.00"] == pytest.approx(mean)
    assert model.params["bars.residual.010"] == pytest.approx(logs[1] - mean)
    assert model.params["bars.residual.050"] == pytest.approx(logs[5] - mean)
    assert model.params["bars.residual.015"] == pytest.approx((logs[1] + logs[2]) / 2 - mean)
    forecast = MANDELBROT.predict(vector(), model)
    assert forecast.probabilities == pytest.approx((0.3, 0.4, 0.3))
    assert forecast.fields == pytest.approx({"q10": 1.0, "q50": 3.0, "q90": 6.0})
    wider = MANDELBROT.predict(vector(k=3.0), model)
    assert wider.probabilities[0] == pytest.approx(0.5) and wider.fields["q50"] == pytest.approx(3.0)
    assert RANGE.invalid(forecast) is None and RANGE.invalid(wider) is None


def test_without_the_spread_the_regression_without_it_forecasts() -> None:
    """Input: four recorded bar closes whose log(1 + r) is 0.2 + 0.6 log(1 + spread) (spread 1
    or 2 ticks: 0.62 or 0.86), and seven bar days' (no spread) at 0.9. Output: ``book`` is
    fitted on the four (spread coefficient 0.6, no residual) and forecasts where the spread is
    known: at 2 ticks, exactly e^(0.2 + 0.6 ln 3) − 1; ``bars``, without the spread, is fitted
    on all eleven and forecasts where it is unknown: their distribution, its median the sixth
    of the eleven, 0.9 (e^0.9 − 1 ticks), its q10 the second, 0.62."""
    book = [
        answered(vector(spread=s), math.expm1(0.2 + 0.6 * math.log1p(s)), i) for i, s in enumerate([1, 2] * 2)
    ]
    bars = [answered(vector(), math.expm1(0.9), 10 + i) for i in range(7)]

    model = MANDELBROT.fit([*book, *bars])

    params = model.params
    assert (params["book.n"], params["bars.n"]) == (4, 11)
    assert params["book.coef.spread_ticks"] == pytest.approx(0.6)
    assert "bars.coef.spread_ticks" not in params
    with_spread = MANDELBROT.predict(vector(spread=2.0), model)
    assert with_spread.fields["q50"] == pytest.approx(math.expm1(0.2 + 0.6 * math.log(3)))
    assert with_spread.fields["q10"] == pytest.approx(with_spread.fields["q90"])
    without = MANDELBROT.predict(vector(spread=None), model)
    assert without.fields["q50"] == pytest.approx(math.expm1(0.9))
    assert without.fields["q10"] == pytest.approx(math.expm1(0.2 + 0.6 * math.log(2)))


def test_before_training_a_third_each_and_without_k_no_forecast() -> None:
    """Before training: a third each, quantiles 0, k and 2k ticks. Without k (no reference
    price at the bar close) it declines: ``no_book``."""
    default = MANDELBROT.default_model()

    assert MANDELBROT.predict(vector(k=3.0), default) == KindForecast(
        (1 / 3, 1 / 3, 1 / 3), {"q10": 0.0, "q50": 3.0, "q90": 6.0}
    )
    with pytest.raises(NotPredicted) as declined:
        MANDELBROT.predict(vector(k=None), default)
    assert declined.value.why is Abstention.NO_BOOK


@pytest.mark.parametrize(
    "inputs",
    [
        {},
        {"ranges": (0.0, 0.0, 0.0, 0.0), "volume": 0.0, "spread": 0.0, "minutes": 1.0},
        {"ranges": (1e6, 1e6, 1e6, 1e6), "volume": 1e6, "spread": 1e6, "minutes": 1e6},
        {"ranges": (math.inf, -1.0, math.nan, 2.0), "volume": -5.0, "spread": math.nan},
    ],
    ids=["nothing known", "all zero", "extreme", "broken"],
)
def test_every_forecast_keeps_the_range_s_contract(inputs: dict[str, Any]) -> None:
    """Whatever the inputs (unknown, zero, extreme, broken), a fitted model's forecast is a
    distribution with ordered quantiles (``Kind.invalid``)."""
    rng = random.Random(3)
    model = MANDELBROT.fit(
        [answered(vector([rng.uniform(0, 5)] * 4, spread=1.0), rng.uniform(0, 9), i) for i in range(30)]
    )

    assert RANGE.invalid(MANDELBROT.predict(vector(**inputs), model)) is None


def test_it_is_registered_with_the_range_and_waits_for_its_pre_validation(tmp_path: Path) -> None:
    """Mandelbrot is the range's, after every direction algorithm; its own features are the
    four ranges; every parameter a model can hold has a meaning. It was registered after
    pre-validation existed: the engine runs it only once it has passed."""
    assert [a.name for a in KIND_ALGORITHMS] == [NAME]
    assert MANDELBROT.kind == "range" and kind_named(MANDELBROT.kind) is RANGE
    assert set(MANDELBROT.card.unique_features) == RANGE_FEATURES
    card = MANDELBROT.card
    assert card.scientist and card.logic and card.training and card.forecasting and card.failure
    assert set(MANDELBROT.default_model().params) <= set(PARAM_MEANINGS)
    assert all(m.text and m.unit and m.known for m in PARAM_MEANINGS.values())
    assert NAME not in REGISTERED_BEFORE
    assert not admits(tmp_path)(MANDELBROT)
    engine = PredictionEngine(
        PredictionStore(tmp_path / "engine.db"),
        CALENDAR,
        (SYMBOL,),
        FEE,
        FakeClock(at("08:00:00")),
        algorithms=[],
        admits=admits(tmp_path, 2),
    )
    assert engine.waiting() == [NAME]
    engine.store.close()


# Training.


def rising(day: date) -> list[Bar]:
    """A day of bars from 09:00: a tick up every minute, each bar's range two ticks."""
    out = []
    for i in range(390):
        price = 81_200 + 100 * i
        out.append(bar(day, f"{9 + (i // 60):02d}:{i % 60:02d}", price - 100, price + 100, price))
    return out


def test_a_bar_day_trains_on_the_bar_rule_with_the_previous_day_s_ranges(tmp_path: Path) -> None:
    """Input: Monday and Tuesday backfilled (bars two ticks wide, a tick up a minute). Output:
    Tuesday's samples carry Monday's ranges (each 30-minute slot's mean: 2 ticks) and the bar
    rule's answers: at 09:31 the highest high of the four minutes is 5 ticks above t's close,
    the lowest low t's close itself: 5 ticks wide, less the bounce c = 1, r = 4, large. No book
    on a backfilled day: no spread. Monday's samples have no previous day (Friday was not
    backfilled)."""
    for day in (DAY, TUESDAY):
        store_bars(tmp_path, day, rising(day))

    monday, tuesday = (
        training_day(tmp_path, day, TWO_DAYS, (SYMBOL,), MANDELBROT.features, FEE, Leaders(), kinds=(RANGE,))
        for day in (DAY, TUESDAY)
    )

    assert monday is not None and tuesday is not None and tuesday.reference == "close"
    samples = {s.bar_close: s for s in tuesday.of_kind(RANGE, MANDELBROT.features)}
    s = samples[at("09:31:00", TUESDAY)]
    assert s.features["range_4"] == pytest.approx(2.0)
    assert s.features["range_prev_day"] == pytest.approx(2.0)
    assert s.features["spread_ticks"] is None
    assert s.answer is not None and s.answer.path["r"] == 4.0 and s.label == "large"
    assert all(m.features["range_prev_day"] is None for m in monday.of_kind(RANGE, MANDELBROT.features))


async def test_a_recorded_day_trains_on_the_answers_the_engine_scores_without_its_vi(tmp_path: Path) -> None:
    """Input: a busy morning with a VI from 09:30:20 to 09:32:25, recorded, replayed through the
    engine; the trading day before backfilled (Thursday: Friday the 9th is a holiday). Output:
    the training samples leave out the bar closes the engine left out (09:27–09:32: in the VI
    or reaching into it), and every other one carries the range answer the engine stored;
    their features are the ones Mandelbrot's stand-in saw live at the same bar close, the
    previous day's ranges included."""
    thursday = DAY - timedelta(days=4)
    calendar = MarketCalendar({thursday: True, thursday + timedelta(days=1): False, DAY: True})
    data = tmp_path / "data"
    store_bars(data, thursday, rising(thursday))
    events = sorted(
        [*busy_morning(), vi(at("09:30:21"), "09:30:20"), vi(at("09:32:30"), "09:30:20", "09:32:25")],
        key=lambda e: e[1]["received_at"],
    )
    record(data, events)
    probe = RangeProbe()
    engine = await run(
        tmp_path / "engine.db",
        events,
        until=at("10:00:00"),
        calendar=calendar,
        target_version=2,
        kind_algorithms=[probe],
        previous=PreviousDay(data, calendar),
    )
    stored = {
        datetime.fromisoformat(a["bar_close"]): a for a in engine.store.kind_answers(RANGE.name, SYMBOL)
    }
    engine.store.close()

    day = training_day(data, DAY, calendar, (SYMBOL,), MANDELBROT.features, FEE, Leaders(), kinds=(RANGE,))

    assert day is not None and day.reference == "mid"
    samples = [s for s in day.of_kind(RANGE, MANDELBROT.features) if s.bar_close <= at("09:56:00")]
    left_out = {at("09:27:00") + timedelta(minutes=m) for m in range(6)}
    assert {s.bar_close for s in samples} == {
        at("09:01:00") + timedelta(minutes=m) for m in range(56)
    } - left_out
    for s in samples:
        assert s.answer is not None
        assert (s.answer.label, dict(s.answer.path)) == (
            stored[s.bar_close]["class"],
            stored[s.bar_close]["path"],
        )
    seen = dict(zip(probe.closes, probe.seen, strict=True))
    for s in samples:
        assert {n: s.features[n] for n in MANDELBROT.features} == seen[s.bar_close]
    assert all(s.features["range_prev_day"] is not None for s in samples)
    assert all(s.features["spread_ticks"] is not None for s in samples)


class RangeProbe:
    """A range stand-in that keeps what it was given: Mandelbrot's features."""

    kind = RANGE.name
    name = "probe"
    training_days = 20
    features = MANDELBROT.features
    card = stand_in_card("probe", "what it is given", tuple(sorted(RANGE_FEATURES)), "nothing")
    param_meanings = stand_in_meanings()

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []
        self.closes: list[datetime] = []

    def default_model(self) -> Model:
        return Model("probe-default", {})

    def fit(self, samples: Sequence[KindSample]) -> Model:
        return self.default_model()

    def predict(self, features: FeatureVector, model: Model) -> KindForecast:
        minutes = features["minutes_since_open"]
        assert minutes is not None
        self.closes.append(at("09:00:00") + timedelta(minutes=minutes))
        self.seen.append({n: features[n] for n in self.features})
        return KindForecast((1 / 3, 1 / 3, 1 / 3), {"q10": 0.0, "q50": 1.0, "q90": 2.0})


def test_the_night_trains_it_on_recorded_and_bar_days_and_counts_both(tmp_path: Path) -> None:
    """Input: eight days in the store, the last four recorded (mornings). Output: Mandelbrot's
    report names the range and counts its answers by source, recorded days by the quotes and
    the others by the bar rule; its challenger beats the defaults, has both regressions (the
    recorded days have the spread) and counts its days by source; the summary has its line."""
    root = tmp_path / "store"
    for day in NIGHT_DAYS:
        archive_bars(root, day)
    for day in NIGHT_DAYS[-4:]:
        archive_events(root, day, busy_morning(day), tmp_path)

    result = train_night(
        LocalObjectStore(root),
        tmp_path / "cache",
        symbols=(SYMBOL,),
        fee_rate=FEE,
        algorithms=[],
        now=lambda: FINISHED,
        target_version=2,
    )

    saved = read(root, f"reports/{NIGHT}/{NAME}.json")
    assert (saved["kind"], saved["target_version"]) == ("range", 1)
    labels = saved["labels"]["samples"]
    assert labels["recorded"] > 0 and labels["bars"] > 0
    assert f"range 정답 표본: 녹화 호가 {labels['recorded']}개, 1분봉 {labels['bars']}개" in result.summary()
    assert saved["promoted"] is True and saved["challenger"]["loss"] < saved["champion"]["loss"]
    model = read(root, f"models/{NAME}/{saved['challenger']['version']}.json")
    assert model["target_version"] == 1
    assert model["params"]["book.n"] > 0 and model["params"]["bars.n"] > model["params"]["book.n"]
    assert model["training"]["days"] == {"recorded": 1, "bars": 4}
    assert set(model["params"]) <= set(PARAM_MEANINGS)


async def test_it_is_prevalidated_on_the_range_s_answers_against_the_range_s_baselines(
    tmp_path: Path,
) -> None:
    """Input: ten recorded days of a swinging market. Output: Mandelbrot is replayed on them,
    retrained before each day on the days before it; its report names the range, its lifts
    are over the range's two baselines and its own statistics are the range's."""
    s = prevalidation_settings(tmp_path)

    await prevalidate(s, NAME, algorithms=[ALPHA], now=AFTER)

    saved = prevalidation_report(s, NAME)
    assert (saved["kind"], saved["target_version"]) == ("range", 1)
    assert set(saved["stats"]["lift"]["vs"]) == {CLIMATE, PERSIST}
    check("range_stats", saved["stats"]["extra"])
    assert saved["stats"]["n"] > 0
    first, second = saved["days"][:2]
    assert saved["models"][first] == f"{NAME}-default", "no day before the first"
    assert saved["models"][second].startswith(f"{NAME}-{first.replace('-', '')}-")
    with sqlite3.connect(s.state_dir / f"prevalidation-{NAME}.sqlite3") as conn:
        statuses = dict(
            conn.execute(
                "SELECT status, COUNT(*) FROM kind_predictions WHERE algorithm = ? GROUP BY status", (NAME,)
            ).fetchall()
        )
    assert statuses.get("ok", 0) > 0 and "error" not in statuses


def test_the_feature_store_computes_mandelbrot_s_features() -> None:
    """The Feature Store computes the range's features like any other's."""
    computed = FeatureStore().compute(snapshot(MORNING, "09:10:00"), MANDELBROT.features)

    assert computed["range_4"] == pytest.approx(1.75) and computed["flat_ticks"] == 2.0
