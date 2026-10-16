"""The barrier's first model, Breiman (#184): its features and settings, its trees on samples
made by hand (the same trees and version for the same samples, unknown features left to the
trees), its training on the barrier's answers (recorded days by the quotes, bar days by the
bar rule, windows without a class left out), the night's report of what it learned from and
left out, and its pre-validation.

Prices: at 81,200 the tick is 100 won and k = 2 (the test fee rate). A one-minute bar from
79,900 to 82,500 reaches both barriers of any bar close whose four minutes hold it, whatever
the width: the bar rule cannot tell which came first (``same minute``).
"""

from __future__ import annotations

import random
import sqlite3
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from krx_quant.algorithms import (
    KIND_ALGORITHMS,
    Abstention,
    KindSample,
    Model,
    NothingToLearn,
    NotPredicted,
)
from krx_quant.algorithms import turing as turing_module
from krx_quant.algorithms.breiman import BOOSTING, NAME, PARAM_MEANINGS, ROUNDS, Breiman
from krx_quant.algorithms.mandelbrot import NAME as MANDELBROT
from krx_quant.algorithms.turing import Turing
from krx_quant.backfill import BAR_SCHEMA, MINUTE_BARS_TR, PreviousDay, bars_key
from krx_quant.barriers import BARRIER, CLIMATE, PERSIST, SAME_MINUTE
from krx_quant.clock import FakeClock
from krx_quant.engine import PredictionEngine
from krx_quant.features import BARRIER_FEATURES, BOOK_FEATURES, TAPE_FEATURES, FeatureVector, Leaders
from krx_quant.feeds import KST
from krx_quant.kind_registry import kind_named
from krx_quant.kinds import Answer, KindForecast
from krx_quant.market_calendar import MarketCalendar
from krx_quant.nightly import train_night
from krx_quant.object_store import LocalObjectStore
from krx_quant.predictions import PredictionStore
from krx_quant.prevalidation import REGISTERED_BEFORE, admits, prevalidate
from krx_quant.reference import Bar
from krx_quant.touch import TARGET_VERSION
from krx_quant.training import IN_VI, SPECIAL, training_day
from tests.algorithm_doubles import stand_in_card, stand_in_meanings
from tests.api_schema import check
from tests.replay_events import CALENDAR, DAY, FEE, SYMBOL, at, run, vi
from tests.synthetic_bars import random_walk
from tests.test_nightly import DAYS as NIGHT_DAYS
from tests.test_nightly import FINISHED, NIGHT, archive_events, read
from tests.test_prevalidation import AFTER, ALPHA
from tests.test_prevalidation import report as prevalidation_report
from tests.test_prevalidation import settings as prevalidation_settings
from tests.test_replay import busy_morning
from tests.test_replay_cli import record

BREIMAN = Breiman()
UP, TIMEOUT, DOWN = BARRIER.classes
BAR_CLOSES = 376  # a day's prediction window: 09:01–15:16


def test_it_reads_turing_s_numbers_and_the_barrier_s_width_with_turing_s_boosting() -> None:
    """Breiman's features are Turing's (every number of the Feature Store but the series and
    the range's) and the barrier's two, its own; its boosting is Turing's; it learns 20
    trading days."""
    assert set(BREIMAN.features) == set(Turing.features) | BARRIER_FEATURES
    assert set(BREIMAN.card.unique_features) == BARRIER_FEATURES
    assert BOOK_FEATURES - {"microprice_ticks"} <= set(BREIMAN.features)
    assert TAPE_FEATURES <= set(BREIMAN.features)
    assert (BOOSTING, ROUNDS) == (turing_module.BOOSTING, turing_module.ROUNDS)
    assert BREIMAN.training_days == 20


# Fit and forecast, on samples made by hand.

START = datetime.combine(date(2026, 9, 1), time(9, 1), KST)


def vector(**values: float | None) -> FeatureVector:
    """Breiman's features: those named, the rest unknown; a width of 2 ticks unless named."""
    known: dict[str, float | None] = dict.fromkeys(BREIMAN.features) | {"barrier_width": 2.0} | values
    return FeatureVector(known, BREIMAN.features)


def answered(features: FeatureVector, label: str | None, i: int, reason: str | None = None) -> KindSample:
    """The ``i``-th sample, 300 bar closes a day from 1 September; its barrier answer ``label``
    (none: unscorable for ``reason``)."""
    bar_close = START + timedelta(days=i // 300, minutes=i % 300)
    return KindSample(SYMBOL, bar_close, features, Answer(label, reason), BARRIER.answer_version, "mid")


def by_the_book(n: int, seed: int = 0) -> list[KindSample]:
    """The best quotes' balance decides: above 0.4 the upper barrier first, below −0.4 the
    lower, between them neither; the width varies and says nothing."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        balance = rng.uniform(-1, 1)
        label = UP if balance > 0.4 else DOWN if balance < -0.4 else TIMEOUT
        features = vector(top_imbalance=balance, barrier_width=float(rng.choice((2, 3, 4))))
        out.append(answered(features, label, i))
    return out


def test_it_learns_which_barrier_the_book_reaches_first() -> None:
    """Input: 1,500 samples whose class the best quotes' balance decides (above). Output: a
    fitted model of the barrier's answer version, its trees in the blob; at a balance of 0.8 up
    first is above 0.8, at −0.8 down first, at 0 timeout above 0.6; every forecast keeps the
    barrier's contract; saved and read back, the same forecast."""
    model = BREIMAN.fit(by_the_book(1500))

    assert model.version.startswith(f"{NAME}-2026") and model.blob
    assert model.target_version == BARRIER.answer_version
    buying = BREIMAN.predict(vector(top_imbalance=0.8), model)
    selling = BREIMAN.predict(vector(top_imbalance=-0.8), model)
    still = BREIMAN.predict(vector(top_imbalance=0.0), model)
    assert buying.probabilities[0] > 0.8 and selling.probabilities[2] > 0.8 and still.probabilities[1] > 0.6
    assert all(BARRIER.invalid(f) is None for f in (buying, selling, still))
    again = Model.from_json(model.to_json())
    assert BREIMAN.predict(vector(top_imbalance=0.8), again) == buying


def test_the_same_samples_give_the_same_trees_and_the_same_version() -> None:
    """One thread and a fixed seed: fitted twice on the same 1,000 samples, the same trees (the
    blob, character for character) and so the same version; other samples, another."""
    first, second = BREIMAN.fit(by_the_book(1000)), BREIMAN.fit(by_the_book(1000))

    assert first.blob == second.blob and first.version == second.version
    assert BREIMAN.fit(by_the_book(1000, seed=1)).version != first.version


def test_unknown_book_and_tape_features_are_left_to_the_trees() -> None:
    """Input: 900 recorded bar closes as above (the balance known) and 900 of backfilled days,
    where the book and the tape are unknown and every window timed out. Output: a forecast
    without the book goes where the trees learned the unknown ones go, timeout above 0.8; with
    it, the balance decides as before; with nothing known but the width, still a distribution."""
    recorded = by_the_book(900)
    backfilled = [answered(vector(barrier_width=2.0), TIMEOUT, 900 + i) for i in range(900)]

    model = BREIMAN.fit([*recorded, *backfilled])

    assert BREIMAN.predict(vector(top_imbalance=None), model).probabilities[1] > 0.8
    assert BREIMAN.predict(vector(top_imbalance=0.8), model).probabilities[0] > 0.6
    assert BARRIER.invalid(BREIMAN.predict(vector(), model)) is None


def test_the_windows_without_a_class_are_left_out_and_the_shares_counted() -> None:
    """Input: 300 up first, 300 timeout and 600 down first, and 100 windows the bar rule could
    not order (``same minute``). Output: trained on the 1,200 with a class; the shares 0.25,
    0.25 and 0.5; every parameter has a meaning. Nothing but unordered windows: nothing to
    learn."""
    labels = [UP] * 300 + [TIMEOUT] * 300 + [DOWN] * 600
    samples = [answered(vector(), label, i) for i, label in enumerate(labels)]
    unordered = [answered(vector(), None, 1200 + i, SAME_MINUTE) for i in range(100)]

    model = BREIMAN.fit([*samples, *unordered])

    assert model.training is not None and model.training.samples == 1200
    shares = {k: v for k, v in model.params.items() if k.startswith("share.")}
    assert shares == {"share.up_first": 0.25, "share.timeout": 0.25, "share.down_first": 0.5}
    assert model.params["rounds"] == ROUNDS
    assert set(model.params) <= set(PARAM_MEANINGS)
    with pytest.raises(NothingToLearn):
        BREIMAN.fit(unordered)


def test_before_training_a_third_each_and_without_a_width_no_forecast() -> None:
    """Before training (no trees): a third each. Without a width (no reference price at the
    bar close, so no barriers), it declines, trained or not: ``no_book``."""
    default = BREIMAN.default_model()
    trained = BREIMAN.fit(by_the_book(500))

    assert BREIMAN.predict(vector(), default) == KindForecast((1 / 3, 1 / 3, 1 / 3))
    for model in (default, trained):
        with pytest.raises(NotPredicted) as declined:
            BREIMAN.predict(vector(barrier_width=None, top_imbalance=0.8), model)
        assert declined.value.why is Abstention.NO_BOOK


def test_it_is_registered_with_the_barrier_and_waits_for_its_pre_validation(tmp_path: Path) -> None:
    """Breiman is the barrier's, registered after the range's Mandelbrot (the last, skipped
    first when the CPU budget runs short); its card is filled in and every parameter a model
    can hold has a meaning. It was registered after pre-validation existed: the engine runs it
    only once it has passed."""
    assert [a.name for a in KIND_ALGORITHMS] == [MANDELBROT, NAME]
    assert BREIMAN.kind == "barrier" and kind_named(BREIMAN.kind) is BARRIER
    card = BREIMAN.card
    assert card.scientist == "Leo Breiman" and card.logic and card.training and card.forecasting
    assert card.failure
    assert set(BREIMAN.default_model().params) <= set(PARAM_MEANINGS)
    assert all(m.text and m.unit and m.known for m in PARAM_MEANINGS.values())
    assert NAME not in REGISTERED_BEFORE
    assert not admits(tmp_path)(BREIMAN)
    engine = PredictionEngine(
        PredictionStore(tmp_path / "engine.db"),
        CALENDAR,
        (SYMBOL,),
        FEE,
        FakeClock(at("08:00:00")),
        algorithms=[],
        admits=admits(tmp_path, 2),
    )
    assert NAME in engine.waiting()
    engine.store.close()


# Training.


def store_bars(data_dir: Path, day: date, bars: Sequence[Bar], symbol: str = SYMBOL) -> None:
    """A day's bars as the backfill stores them (``data_dir`` a data directory or the store's
    root: the same layout)."""
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


def jolted(day: date) -> list[Bar]:
    """A still day of bars at 81,200, from 09:00 to 15:29, but for one bar at 09:31 from 79,900
    to 82,500: the windows of the bar closes 09:28–09:31 reach both barriers in its minute."""
    out = []
    for i in range(390):
        start = datetime.combine(day, time(9), KST) + timedelta(minutes=i)
        low, high = (79_900, 82_500) if start.time() == time(9, 31) else (81_200, 81_200)
        out.append(Bar(start, 81_200, high, low, 81_200, 10, 1, start, start))
    return out


def test_a_bar_day_trains_on_the_bar_rule_its_same_minutes_left_out(tmp_path: Path) -> None:
    """Input: Monday backfilled (``jolted``), the Friday before too. Output: every bar close
    has a sample; the four windows reaching both barriers in 09:31's minute have no class
    (``same minute``), the others time out (a still market); Breiman trains on the 372 with a
    class, one day of bars, and nothing was left out before the answers (no VI on a backfilled
    day)."""
    friday = DAY - timedelta(days=3)
    calendar = MarketCalendar({friday: True, DAY: True})
    store_bars(tmp_path, friday, jolted(friday))
    store_bars(tmp_path, DAY, jolted(DAY))

    day = training_day(tmp_path, DAY, calendar, (SYMBOL,), BREIMAN.features, FEE, Leaders(), kinds=(BARRIER,))

    assert day is not None and day.reference == "close" and day.left_out == {}
    samples = day.of_kind(BARRIER, BREIMAN.features)
    assert len(samples) == BAR_CLOSES
    unordered = {s.bar_close for s in samples if s.label is None}
    assert unordered == {at("09:28:00") + timedelta(minutes=m) for m in range(4)}
    reasons = {s.answer.unscorable for s in samples if s.label is None and s.answer is not None}
    assert reasons == {SAME_MINUTE}
    assert {s.label for s in samples} == {TIMEOUT, None}
    model = BREIMAN.fit(samples)
    assert model.training is not None and model.training.samples == BAR_CLOSES - 4
    assert model.training.days == {"recorded": 0, "bars": 1}


async def test_a_recorded_day_trains_on_the_answers_the_engine_scores_without_its_vi(tmp_path: Path) -> None:
    """Input: a busy morning with a VI from 09:30:20 to 09:32:25, recorded, replayed through the
    engine; the trading day before backfilled (Thursday: Friday the 9th is a holiday). Output:
    the training samples leave out the bar closes the engine left out: 09:31 and 09:32 in the
    VI, 09:27–09:30 reaching into it (special); the day counts them so; every other one carries
    the barrier answer the engine stored (class, reason, path); their features are the ones
    Breiman's stand-in saw live at the same bar close, the width from both days' bars."""
    thursday = DAY - timedelta(days=4)
    calendar = MarketCalendar({thursday: True, thursday + timedelta(days=1): False, DAY: True})
    data = tmp_path / "data"
    store_bars(data, thursday, random_walk(thursday))
    events = sorted(
        [*busy_morning(), vi(at("09:30:21"), "09:30:20"), vi(at("09:32:30"), "09:30:20", "09:32:25")],
        key=lambda e: e[1]["received_at"],
    )
    record(data, events)
    probe = BarrierProbe()
    engine = await run(
        tmp_path / "engine.db",
        events,
        until=at("10:00:00"),
        calendar=calendar,
        target_version=TARGET_VERSION,
        kind_algorithms=[probe],
        previous=PreviousDay(data, calendar),
    )
    stored = {
        datetime.fromisoformat(a["bar_close"]): a for a in engine.store.kind_answers(BARRIER.name, SYMBOL)
    }
    engine.store.close()

    day = training_day(data, DAY, calendar, (SYMBOL,), BREIMAN.features, FEE, Leaders(), kinds=(BARRIER,))

    assert day is not None and day.reference == "mid"
    assert day.left_out == {IN_VI: 2, SPECIAL: 4}
    samples = [s for s in day.of_kind(BARRIER, BREIMAN.features) if s.bar_close <= at("09:56:00")]
    left_out = {at("09:27:00") + timedelta(minutes=m) for m in range(6)}
    assert {s.bar_close for s in samples} == {
        at("09:01:00") + timedelta(minutes=m) for m in range(56)
    } - left_out
    for s in samples:
        assert s.answer is not None
        assert (s.answer.label, s.answer.unscorable, dict(s.answer.path)) == (
            stored[s.bar_close]["class"],
            stored[s.bar_close]["unscorable"],
            stored[s.bar_close]["path"],
        )
    assert {s.label for s in samples} >= {UP, DOWN}
    seen = dict(zip(probe.closes, probe.seen, strict=True))
    for s in samples:
        assert {n: s.features[n] for n in BREIMAN.features} == seen[s.bar_close], s.bar_close
    assert all(s.features["barrier_sigma"] is not None for s in samples), "Thursday's bars carry it"


class BarrierProbe:
    """A barrier stand-in that keeps what it was given: Breiman's features."""

    kind = BARRIER.name
    name = "probe"
    training_days = 20
    features = BREIMAN.features
    card = stand_in_card("probe", "what it is given", tuple(sorted(BARRIER_FEATURES)), "nothing")
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
        return KindForecast((1 / 3, 1 / 3, 1 / 3))


def test_the_night_counts_its_answers_by_source_and_what_it_left_out(tmp_path: Path) -> None:
    """Input: eight days in the store, each backfilled ``jolted``; the night's day recorded too
    (a busy morning with the VI above). Output: Breiman's report names the barrier and counts
    its answers by source and, by day and by why, the bar closes it left out: four ``same
    minute`` a bar day; on the recorded day the two in the VI and the four special ones, the
    four whose quotes stopped at 10:00 (``silent quotes``: 09:57–10:00) and the 316 after
    without a mid-point (10:01–15:16); answered and left out, every bar close of each day.
    Its challenger beats the defaults and counts its days by source. The summary's barrier line
    adds the left-out total and each reason, the most first (both reports of the barrier read
    the same days: each counted once); the range's line has the recorded day's reasons only (its
    bar rule always answers)."""
    root = tmp_path / "store"
    for day in NIGHT_DAYS:
        store_bars(root, day, jolted(day))
    vi_events = [vi(at("09:30:21", NIGHT), "09:30:20"), vi(at("09:32:30", NIGHT), "09:30:20", "09:32:25")]
    archive_events(root, NIGHT, [*busy_morning(NIGHT), *vi_events], tmp_path)

    result = train_night(
        LocalObjectStore(root),
        tmp_path / "cache",
        symbols=(SYMBOL,),
        fee_rate=FEE,
        algorithms=[],
        now=lambda: FINISHED,
        target_version=TARGET_VERSION,
    )

    saved = read(root, f"reports/{NIGHT}/{NAME}.json")
    assert (saved["kind"], saved["target_version"]) == ("barrier", BARRIER.answer_version)
    labels = saved["labels"]
    bar_days = [d.isoformat() for d in NIGHT_DAYS[:-1]]  # its five training days and two validation
    assert set(labels["by_day"]) == {*bar_days, NIGHT.isoformat()}
    for bar_day in bar_days:
        assert labels["by_day"][bar_day] == BAR_CLOSES - 4
        assert labels["dropped_by_day"][bar_day] == {SAME_MINUTE: 4}
    recorded = labels["dropped_by_day"][NIGHT.isoformat()]
    assert (recorded[IN_VI], recorded[SPECIAL]) == (2, 4) and SAME_MINUTE not in recorded
    assert labels["by_day"][NIGHT.isoformat()] + sum(recorded.values()) == BAR_CLOSES
    quotes, bars = labels["by_day"][NIGHT.isoformat()], 7 * (BAR_CLOSES - 4)
    assert labels["samples"] == {"recorded": quotes, "bars": bars}
    assert saved["promoted"] is True and saved["challenger"]["loss"] < saved["champion"]["loss"]
    model = read(root, f"models/{NAME}/{saved['challenger']['version']}.json")
    assert model["target_version"] == BARRIER.answer_version and model["blob"]
    assert model["training"]["days"] == {"recorded": 0, "bars": 5}
    assert set(model["params"]) <= set(PARAM_MEANINGS)
    # The barrier's climate table read the same eight days: the summary counts each day once.
    climate = read(root, f"reports/{NIGHT}/{CLIMATE}.json")["labels"]
    assert climate["dropped_by_day"] == labels["dropped_by_day"]
    lines = result.summary().splitlines()
    [barrier] = [x for x in lines if x.startswith("barrier 정답 표본")]
    assert labels["dropped"] == {
        "missing mid": 316,
        SAME_MINUTE: 7 * 4,
        "silent quotes": 4,
        SPECIAL: 4,
        IN_VI: 2,
    }
    assert barrier == (
        f"barrier 정답 표본: 녹화 호가 {quotes}개, 1분봉 {bars}개"
        " · 뺀 표본 354개 (missing mid 316, same minute 28, silent quotes 4, special 4, vi 2)"
    )
    [range_line] = [x for x in lines if x.startswith("range 정답 표본")]
    assert range_line.endswith(" · 뺀 표본 326개 (missing mid 316, silent quotes 4, special 4, vi 2)")


async def test_it_is_prevalidated_on_the_barrier_s_answers_against_the_barrier_s_baselines(
    tmp_path: Path,
) -> None:
    """Input: ten recorded days of a swinging market. Output: Breiman is replayed on them,
    retrained before each day on the days before it; its report names the barrier, its lifts
    are over the barrier's two baselines and its own statistics are the barrier's (with the
    barrier P&L)."""
    s = prevalidation_settings(tmp_path)

    await prevalidate(s, NAME, algorithms=[ALPHA], now=AFTER)

    saved = prevalidation_report(s, NAME)
    assert (saved["kind"], saved["target_version"]) == ("barrier", BARRIER.answer_version)
    assert set(saved["stats"]["lift"]["vs"]) == {CLIMATE, PERSIST}
    check("barrier_stats", saved["stats"]["extra"])
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
