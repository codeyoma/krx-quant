"""Cox (#264): open_to_close's first model, its features at 08:50 and their equality live and in
training, on made-up histories."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from krx_quant.algorithms import DAILY_ALGORITHMS, Model, NothingToLearn, load_models, save_model
from krx_quant.algorithms.cox import FEATURES, OWN, Cox, columns
from krx_quant.backfill import BAR_SCHEMA, MINUTE_BARS_TR, bars_key
from krx_quant.candles import read_krx_days, write_krx_days
from krx_quant.daily import Band, DayPrices
from krx_quant.daily_features import (
    DAILY_FEATURES,
    DailyFiles,
    DayContext,
    LiveFeatures,
    month_flags,
    night_move,
    samples,
    vector,
)
from krx_quant.features import FeatureVector
from krx_quant.feeds import NIGHT_FUTURES, TRADE
from krx_quant.feeds.base import KST
from krx_quant.macro import Observation
from krx_quant.market_calendar import MarketCalendar
from krx_quant.open_to_close import OPEN_TO_CLOSE
from krx_quant.recorder import ParquetRecorder
from krx_quant.supply import KIS, read_supply, write_supply
from krx_quant.us_daily import US_DAILY, read_us_daily, write_us_daily
from tests.daily_doubles import SYMBOLS, made_history

FEE = 0.015 / 100
LAST = date(2026, 10, 16)  # a Friday
CALENDAR = MarketCalendar()


def day(
    d: date, opened: float, close: float, high: float | None = None, low: float | None = None
) -> DayPrices:
    return DayPrices(d, opened, high or max(opened, close), low or min(opened, close), close, 1_000)


def test_cox_is_registered_with_features_of_its_own() -> None:
    assert [a.name for a in DAILY_ALGORITHMS] == ["cox", "cox-5d"]  # #310: its five-session twin after it
    cox = DAILY_ALGORITHMS[0]
    assert cox.kind == "open_to_close"
    assert set(cox.features) <= set(DAILY_FEATURES)
    assert set(cox.card.unique_features) == set(OWN)
    card = cox.card.as_dict()
    assert card["scientist"] == "David Cox"
    assert all(card[k] for k in ("character", "logic", "training", "forecasting", "failure"))


def test_the_features_by_hand() -> None:
    thursday, friday = date(2026, 10, 15), date(2026, 10, 16)
    history = [
        day(date(2026, 10, 12), 100.0, 101.0),
        day(date(2026, 10, 13), 101.0, 100.0),
        day(date(2026, 10, 14), 100.0, 102.0),
        day(date(2026, 10, 15) - timedelta(days=0), 103.0, 104.0, high=105.0, low=102.0),
    ][:3] + [day(thursday, 103.0, 104.0, high=105.0, low=102.0)]
    band = Band(0.005, 0.01, 0.5)
    ctx = DayContext(
        symbol="005930",
        day=friday,
        history=history,
        labels=["up", "down", "up", "up"],
        previous_day=thursday,
        band=band,
        us={"sox": -0.02, "nvda": 0.01, "mu": None, "vix": 20.0},
        flows={"foreign": 0.03, "institution": -0.01},
        premarket=104.0 * math.exp(0.005),
        month_end=False,
        month_start=False,
        night_futures=-0.004,
    )
    v = vector(FEATURES, ctx)
    assert v["us_sox"] == -0.02 and v["us_nvda"] == 0.01 and v["us_mu"] is None
    assert v["vix"] == pytest.approx(math.log(20.0))
    assert (v["foreign_net"], v["institution_net"]) == (0.03, -0.01)
    assert v["oc_1"] == pytest.approx((104 / 103 - 1) / 0.01)
    assert v["cc_1"] == pytest.approx((104 / 102 - 1) / 0.01)
    assert v["gap_1"] == pytest.approx((103 / 102 - 1) / 0.01)
    assert v["range_1"] == pytest.approx((3 / 103) / 0.01)
    assert v["oc_5"] is None, "fewer than five days before"
    assert v["vol_ratio"] is None, "fewer than 250 days before"
    assert v["sigma"] == pytest.approx(math.log(0.01))
    assert (v["climate_up"], v["climate_down"]) == pytest.approx((4 / 7, 2 / 7))
    assert v["premarket_gap"] == pytest.approx(0.5)
    assert (v["weekday"], v["month_end"], v["month_start"], v["night_futures"]) == (4.0, 0.0, 0.0, -0.004)

    stale = DayContext("005930", friday, history[:-1], ["up"] * 3, thursday, band)
    w = vector(FEATURES, stale)
    assert [w[f] for f in ("oc_1", "cc_1", "gap_1", "range_1", "premarket_gap")] == [None] * 5, (
        "the previous trading day's candle is not stored: none of its features"
    )


def test_the_turn_of_the_month() -> None:
    weekdays = MarketCalendar().is_trading_day
    assert month_flags(date(2026, 10, 30), weekdays) == (True, False), "Friday the 30th: the month's last"
    assert month_flags(date(2026, 11, 3), weekdays) == (False, True), "the 2nd, 3rd and 4th open November"
    assert month_flags(date(2026, 11, 5), weekdays) == (False, False)


def fitted(tmp_path: Path, signal: float, days: int = 800) -> tuple[Model, list]:  # type: ignore[type-arg]
    made_history(tmp_path / "data", tmp_path / "state", LAST, days, signal=signal)
    found = samples(OPEN_TO_CLOSE, DailyFiles(tmp_path / "data", tmp_path / "state"), SYMBOLS, FEATURES, FEE)
    return Cox().fit(found), found


def test_cox_learns_the_planted_lean_and_its_model_round_trips(tmp_path: Path) -> None:
    model, found = fitted(tmp_path, signal=1.0)
    assert model.version.startswith("cox-20261016-")
    assert model.target_version == OPEN_TO_CLOSE.answer_version and model.trained_until == LAST
    assert model.params["lambda"] in (0.001, 0.01, 0.03, 0.1, 0.3, 1.0)
    # The day leans against the night's SOX move: up's weight on it is below down's.
    assert model.params["w.up.us_sox"] < 0 < model.params["w.down.us_sox"]
    assert "w.up.night_futures" not in model.params, "always missing here: dropped"
    unknown = set(model.params) - set(Cox.param_meanings)
    assert not unknown, f"parameters without a meaning: {sorted(unknown)}"
    save_model(tmp_path / "state", "cox", model)
    [loaded] = load_models(tmp_path / "state", [Cox()]).values()
    s = found[-1]
    assert Cox().predict(s.features, loaded).probabilities == Cox().predict(s.features, model).probabilities
    hits = sum(
        OPEN_TO_CLOSE.classes[max(range(3), key=lambda i: Cox().predict(x.features, model).probabilities[i])]
        == x.label
        for x in found[-600:]
        if x.label is not None
    )
    assert hits / 600 > 0.45, "in sample, the lean is learned"


def test_every_forecast_is_a_distribution_whatever_the_inputs(tmp_path: Path) -> None:
    model, _ = fitted(tmp_path, signal=1.0, days=700)
    names = Cox.features
    for values in (
        dict.fromkeys(names),
        dict.fromkeys(names, 1e9),
        dict.fromkeys(names, -1e9),
        {**dict.fromkeys(names, 0.0), "weekday": 2.0},
        {**dict.fromkeys(names, float("nan")), "weekday": None},
    ):
        for m in (model, Cox().default_model()):
            forecast = Cox().predict(FeatureVector(values, names), m)
            assert OPEN_TO_CLOSE.invalid(forecast) is None, values
    assert Cox().predict(FeatureVector(dict.fromkeys(names), names), Cox().default_model()).probabilities == (
        1 / 3,
        1 / 3,
        1 / 3,
    )
    assert set(columns()) >= {"weekday.1", "weekday.4", "us_sox.missing"}


def test_too_few_days_are_nothing_to_learn() -> None:
    with pytest.raises(NothingToLearn):
        Cox().fit([])


def write_bars(data_dir: Path, d: date, symbol: str, minutes: list[tuple[time, int]]) -> None:
    rows = [
        {
            "symbol": symbol,
            "bar_time": datetime.combine(d, t, KST),
            "venue": "UN",
            "open": c,
            "high": c,
            "low": c,
            "close": c,
            "volume": 1,
            "cum_value": c,
        }
        for t, c in minutes
    ]
    path = data_dir / bars_key(d, MINUTE_BARS_TR, symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=BAR_SCHEMA), path)


def record_night(data_dir: Path, rows: list[tuple[datetime, float]]) -> None:
    recorder = ParquetRecorder(data_dir, flush_rows=100, flush_seconds=3600)
    for received, change in rows:
        row = {
            "symbol": "A01612",
            "event_time": received,
            "received_at": received.astimezone(UTC),
            "price": 400.0,
            "change_pct": change,
            "qty": 1,
        }
        full = {name: row.get(name) for name in NIGHT_FUTURES.schema.names}
        recorder.record(
            "events",
            received.astimezone(KST).date(),
            NIGHT_FUTURES.tr_id,
            "A01612",
            full,
            NIGHT_FUTURES.schema,
        )
    recorder.close()


def test_a_days_features_are_the_same_live_and_in_training(tmp_path: Path) -> None:
    """Friday the 16th's features at 08:50: from the stored files and the trades the engine
    received (live), and from the histories (training), the same."""
    data, state = tmp_path / "data", tmp_path / "state"
    made_history(data, state, LAST, 700, signal=1.0)
    thursday = date(2026, 10, 15)
    # NXT's pre-market: the last bar from 08:40 is the one read (the 08:35 one is KRX's pre-open).
    write_bars(data, LAST, "005930", [(time(8, 10), 51_000), (time(8, 35), 50_000), (time(8, 44), 51_500)])
    night = [
        (datetime.combine(thursday, time(18, 5), KST), 0.4),
        (datetime.combine(LAST, time(5, 59, 30), KST), -0.6),
        (datetime.combine(LAST, time(6, 0, 1), KST), 9.0),  # after the night: not it
    ]
    record_night(data, night)
    assert night_move(data, thursday) == pytest.approx(-0.006)

    files = DailyFiles(data, state)
    trained = [s for s in samples(OPEN_TO_CLOSE, files, SYMBOLS, FEATURES, FEE) if s.day == LAST]
    live = LiveFeatures(OPEN_TO_CLOSE, data, state, CALENDAR, SYMBOLS, FEE)
    for received, price in ((time(8, 10), 51_000), (time(8, 35), 50_000), (time(8, 44, 30), 51_500)):
        at = datetime.combine(LAST, received, KST)
        live.on_event(TRADE.tr_id, {"symbol": "005930", "event_time": at, "received_at": at, "price": price})
    late = datetime.combine(LAST, time(8, 50, 1), KST)
    live.on_event(TRADE.tr_id, {"symbol": "005930", "event_time": late, "received_at": late, "price": 1})
    cox = Cox()
    for sample in trained:
        history = [d for d in files.days(sample.symbol) if d.day < LAST]
        band = OPEN_TO_CLOSE.band_after(history)
        got = live(cox, sample.symbol, LAST, history, band).as_dict()
        assert got == pytest.approx(sample.features.as_dict(), nan_ok=True), sample.symbol
    by_symbol = {s.symbol: s.features for s in trained}
    assert by_symbol["005930"]["premarket_gap"] is not None
    assert by_symbol["000660"]["premarket_gap"] is None
    assert by_symbol["005930"]["night_futures"] == pytest.approx(-0.006)


def test_a_days_features_know_nothing_of_it_or_after(tmp_path: Path) -> None:
    """Whatever happens on the day and after it (its candle, the US closes dated from it, the
    flows from it), the day's features at 08:50 stay the same."""
    data, state = tmp_path / "data", tmp_path / "state"
    made_history(data, state, LAST, 650, signal=1.0)
    target = date(2026, 9, 30)
    files = DailyFiles(data, state)
    before = {
        s.symbol: s.features.as_dict()
        for s in samples(OPEN_TO_CLOSE, files, SYMBOLS, FEATURES, FEE)
        if s.day == target
    }
    for symbol in SYMBOLS:
        for read, write in ((read_krx_days, write_krx_days),):
            write(
                data,
                symbol,
                [{**r, "close": r["close"] * 2} if r["date"] >= target else r for r in read(data, symbol)],
            )
        write_supply(
            state,
            symbol,
            KIS,
            [
                {**r, "foreign_net": 999} if r["date"] >= target else r
                for r in read_supply(state, symbol, KIS)
            ],
        )
    for name in US_DAILY:
        write_us_daily(
            data,
            name,
            [Observation(o.day, o.value * 3) if o.day >= target else o for o in read_us_daily(data, name)],
        )
    after = {
        s.symbol: s.features.as_dict()
        for s in samples(OPEN_TO_CLOSE, DailyFiles(data, state), SYMBOLS, FEATURES, FEE)
        if s.day == target
    }
    assert after.keys() == before.keys() == set(SYMBOLS)
    for symbol in SYMBOLS:
        assert after[symbol] == pytest.approx(before[symbol], nan_ok=True), symbol


def test_a_missing_candle_on_the_previous_trading_day_stays_missing_in_training_as_live(
    tmp_path: Path,
) -> None:
    """005930 has no KRX candle on Thursday the 15th (a halt, or a gap at KIS): Friday's training
    sample must not take Wednesday's candle as the previous day's. The previous trading day is the
    market's (the other symbols traded), as the engine's calendar gives it live, so the previous
    day's features are missing in both, and the night's US move and flows are the market day's."""
    data, state = tmp_path / "data", tmp_path / "state"
    made_history(data, state, LAST, 650, signal=1.0)
    thursday = date(2026, 10, 15)
    write_krx_days(data, "005930", [r for r in read_krx_days(data, "005930") if r["date"] != thursday])
    files = DailyFiles(data, state)
    friday = {s.symbol: s for s in samples(OPEN_TO_CLOSE, files, SYMBOLS, FEATURES, FEE) if s.day == LAST}
    gap = friday["005930"].features
    for name in ("oc_1", "cc_1", "gap_1", "range_1", "premarket_gap"):
        assert gap[name] is None, name
    assert gap["us_sox"] == friday["000660"].features["us_sox"], "the night since the market's previous day"
    assert friday["000660"].features["oc_1"] is not None, "the others' Thursday is there"

    live = LiveFeatures(OPEN_TO_CLOSE, data, state, CALENDAR, SYMBOLS, FEE)
    history = [d for d in files.days("005930") if d.day < LAST]
    got = live(Cox(), "005930", LAST, history, OPEN_TO_CLOSE.band_after(history)).as_dict()
    assert got == pytest.approx(gap.as_dict(), nan_ok=True)

    from krx_quant.daily_features import TradingDays
    from krx_quant.daily_training import baseline_forecasts

    walked = baseline_forecasts(OPEN_TO_CLOSE, files, SYMBOLS, FEE, LAST, LAST)
    persisted = {r.symbol for r in walked["otc-persist"]}
    assert persisted == {"000660", "009150"}, "the walk-forward's persistence has no Thursday for 005930"

    market = TradingDays([date(2026, 10, 14), date(2026, 10, 16)], CALENDAR)
    assert market.previous(date(2026, 10, 16)) == date(2026, 10, 14), "the 15th is no trading day here"
    assert market.previous(date(2026, 10, 19)) == date(2026, 10, 16), "past the candles: the calendar"
    assert market.previous(date(2026, 10, 14)) is None
    assert not market.trades(date(2026, 10, 15)) and market.trades(date(2026, 10, 19))
