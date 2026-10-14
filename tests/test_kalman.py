"""Kalman: its own features through the Replay Harness, its filter and EM on made-up
sequences, and its predictions on the prediction path.

Input: integrated books with their best quotes' sizes, KOSPI200 futures trades, or
sequences of microprices that follow a known state-space model. Output: the feature values
an algorithm sees at each bar close, what the fitted model forecasts, and what the engine
stores.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant.algorithms import Abstention, Model, NotPredicted, Sample
from krx_quant.algorithms.kalman import Kalman
from krx_quant.engine import Event
from krx_quant.features import FeatureVector, Leaders
from krx_quant.feeds import BOOK, KST
from krx_quant.lifecycle import Status
from krx_quant.predictions import PredictionStore
from krx_quant.pricing import Direction
from krx_quant.touch import TARGET_VERSION
from krx_quant.training import recorded_samples
from tests.algorithm_doubles import FixedAlgorithm
from tests.api_schema import check
from tests.replay_events import CALENDAR, DAY, FEE, api, at, book, futures_trade, get_json, run
from tests.test_algorithms_replay import predictions_of
from tests.test_replay import busy_morning

OWN = ("microprice_ticks", "kospi200f_moves")


def quiet_books(start: str, end: str, bid: int, ask: int, *, bid_qty: int, ask_qty: int) -> list[Event]:
    events: list[Event] = []
    when = at(start)
    while when < at(end):
        events.append(book(when, bid, ask, bid_qty=bid_qty, ask_qty=ask_qty))
        when += timedelta(seconds=10)
    return events


async def test_the_microprice_at_each_minute_s_end_since_the_book_last_went_missing(tmp_path: Path) -> None:
    # 81,200 / 81,300 with three times as much bid: the microprice leans to the ask, 81,275.
    # From 09:05 the book is a tick higher; from 09:06 to 09:07:30 nothing comes.
    events = [
        *quiet_books("09:00:00", "09:05:00", 81_200, 81_300, bid_qty=300, ask_qty=100),
        *quiet_books("09:05:00", "09:06:00", 81_300, 81_400, bid_qty=100, ask_qty=100),
        *quiet_books("09:07:30", "09:10:00", 81_300, 81_400, bid_qty=100, ask_qty=300),
    ]
    probe = FixedAlgorithm("probe", features=OWN, unique=OWN)

    await run(tmp_path / "engine.db", events, until=at("09:10:00"), algorithms=[probe])

    by_close = dict(zip((at(f"09:{m:02d}:00") for m in range(1, 11)), probe.seen, strict=True))
    # At 09:05 the mid is 81,250: five minutes' ends at a quarter of a tick above it. At 09:06
    # the book is a tick higher, the microprice on its mid.
    assert by_close[at("09:05:00")]["microprice_ticks"] == (0.25, 0.25, 0.25, 0.25, 0.25)
    assert by_close[at("09:06:00")]["microprice_ticks"] == (-0.75, -0.75, -0.75, -0.75, -0.75, 0.0)
    assert by_close[at("09:07:00")]["microprice_ticks"] == (), "the book went missing"
    # 81,325 against a mid of 81,350; 09:07's end had no book within a minute.
    assert by_close[at("09:10:00")]["microprice_ticks"] == (-0.25, -0.25, -0.25)


async def test_the_futures_moves_of_each_minute_from_the_open_in_basis_points(tmp_path: Path) -> None:
    trades = [futures_trade(at("08:59:30"), 399.0)]  # before the open: not a move of the day
    prices = {"09:00:10": 400.0, "09:01:10": 401.0, "09:03:10": 400.0}  # 09:02 has no trade
    trades += [futures_trade(at(t), p) for t, p in prices.items()]
    books = quiet_books("09:00:00", "09:05:00", 81_200, 81_300, bid_qty=100, ask_qty=100)
    probe = FixedAlgorithm("probe", features=OWN, unique=OWN)

    await run(
        tmp_path / "engine.db",
        sorted([*trades, *books], key=lambda e: e[1]["received_at"]),
        until=at("09:05:00"),
        algorithms=[probe],
    )

    up, down = 1e4 * math.log(401 / 400), 1e4 * math.log(400 / 401)
    at_0905 = probe.seen[-1]["kospi200f_moves"]
    assert isinstance(at_0905, tuple)
    assert at_0905 == pytest.approx((up, 0.0, down, 0.0)), "09:01, a quiet 09:02, 09:03, a quiet 09:04"


TRUE = {"q_level": 0.3, "q_trend": 0.05, "r": 0.5, "phi": 0.6, "b": 0.08}
KALMAN = Kalman()


def features(ys: Sequence[float], moves: Sequence[float] = (), flat: float | None = 1.0) -> FeatureVector:
    values = {"microprice_ticks": tuple(ys), "kospi200f_moves": tuple(moves), "flat_ticks": flat}
    return FeatureVector(values, KALMAN.features)


def simulated(n: int, seed: int = 1, minutes: int = 61) -> list[Sample]:
    """Hours of microprices from the model with ``TRUE`` parameters, the futures moving about
    4 basis points a minute; each an hour of a day of its own, in ticks from its last."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        level = trend = 0.0
        ys: list[float] = []
        us: list[float] = []
        for k in range(minutes + 4):
            if k > 0:
                level += trend + TRUE["b"] * us[-1] + rng.gauss(0, math.sqrt(TRUE["q_level"]))
                trend = TRUE["phi"] * trend + rng.gauss(0, math.sqrt(TRUE["q_trend"]))
            ys.append(level + rng.gauss(0, math.sqrt(TRUE["r"])))
            us.append(rng.gauss(0, 4))
        now = ys[minutes - 1]
        change = ys[-1] - now
        outcome = Direction.UP if change >= 1 else Direction.DOWN if change <= -1 else Direction.FLAT
        bar_close = datetime.combine(date(2026, 9, 1) + timedelta(days=i), time(15), KST)
        hour = [y - now for y in ys[:minutes]]
        out.append(Sample("005930", bar_close, features(hour, us[:minutes]), outcome, change))
    return out


def test_em_learns_the_book_s_noise_and_how_far_the_price_follows_the_futures() -> None:
    model = KALMAN.fit(simulated(200))

    assert model.version.startswith("kalman-")
    assert model.params["b"] == pytest.approx(TRUE["b"], rel=0.15)
    assert model.params["r"] == pytest.approx(TRUE["r"], rel=0.2)
    assert 0 <= model.params["phi"] <= 0.99


def test_the_same_samples_give_the_same_model() -> None:
    assert KALMAN.fit(simulated(30)).version == KALMAN.fit(simulated(30)).version


def test_the_forecast_follows_the_trend_and_the_futures_minute_before() -> None:
    # A trend that lasts, behind little noise.
    model = Model("kalman-test", {"q_level": 0.1, "q_trend": 0.01, "r": 0.1, "phi": 0.95, "b": 0.1})
    rising = [float(k - 60) for k in range(61)]  # a tick a minute, up to the bar close's mid

    up = KALMAN.predict(features(rising), model)
    futures_up = KALMAN.predict(features([0.0] * 61, [0.0] * 59 + [30.0]), model)
    futures_down = KALMAN.predict(features([0.0] * 61, [0.0] * 59 + [-30.0]), model)

    assert up.p_up > 0.5 > up.p_down and up.expected_ticks > 1
    assert futures_up.expected_ticks == pytest.approx(3.0), "0.1 tick a basis point, 30 of them"
    assert futures_up.p_up > futures_up.p_down and futures_down.p_down > futures_down.p_up


def test_a_day_without_the_book_is_not_learned_from() -> None:
    no_book = [replace(s, features=features([], s.features.series("kospi200f_moves"))) for s in simulated(5)]

    with pytest.raises(ValueError, match="with the book"):
        KALMAN.fit(no_book)
    with pytest.raises(NotPredicted) as declined:
        KALMAN.predict(no_book[0].features, KALMAN.default_model())
    assert declined.value.why is Abstention.NO_BOOK


def a_book_morning(end: str = "10:00:00") -> list[Event]:
    """The busy morning's book and trades, the sizes at the best quotes leaning now one way,
    now the other."""
    events: list[Event] = []
    for feed, row in busy_morning(end=end):
        if feed == BOOK.tr_id:
            step = int(row["received_at"].timestamp()) // 13
            row = {**row, "bid_qty_1": 100 + 50 * (step % 5), "ask_qty_1": 100 + 50 * ((step * 3) % 5)}
        events.append((feed, row))
    return events


async def test_without_the_book_at_a_bar_close_kalman_declines_and_is_left_out(tmp_path: Path) -> None:
    # Active, so in the consensus; no book from 09:06 to 09:07:30.
    db = tmp_path / "engine.db"
    store = PredictionStore(db)
    store.add_status("kalman", at("08:00:00"), None, Status.ACTIVE, "by hand")
    store.mark_target(TARGET_VERSION, at("08:00:00"))  # active on the touch target already (#170)
    store.close()
    events = [e for e in a_book_morning() if not at("09:06:00") <= e[1]["received_at"] < at("09:07:30")]

    engine = await run(db, events, until=at("09:20:00"), algorithms=[KALMAN])

    kalman = {p["bar_close"]: p for p in await predictions_of(engine, "kalman")}
    declined = kalman[at("09:07:00").isoformat()]
    assert declined["status"] == "no_book" and declined["p_up"] is None and declined["outcome"] is None
    assert kalman[at("09:08:00").isoformat()]["status"] == "ok", "the book is back"
    assert {p["status"] for t, p in kalman.items() if t != at("09:07:00").isoformat()} == {"ok"}
    async with api(engine) as client:
        body = await get_json(client, "/v1/symbols/005930/predictions?algorithm=consensus-final&limit=100")
    check("predictions", body)
    consensus = {p["bar_close"]: p["status"] for p in body["predictions"]}
    assert consensus[at("09:07:00").isoformat()] == "none_finished"
    assert consensus[at("09:08:00").isoformat()] == "ok"


async def test_kalman_predicts_and_is_scored_on_the_prediction_path(tmp_path: Path) -> None:
    model = KALMAN.fit(
        recorded_samples(
            a_book_morning("11:00:00"), CALENDAR, DAY, ("005930",), KALMAN.features, FEE, Leaders()
        )
    )

    engine = await run(
        tmp_path / "engine.db", a_book_morning(), until=at("10:00:00"), models={"kalman": model}
    )

    kalman = await predictions_of(engine, "kalman")
    assert len(kalman) == 60 and {p["status"] for p in kalman} == {"ok"}
    assert {p["model_version"] for p in kalman} == {model.version}
    assert sum(p["outcome"] is not None for p in kalman) == 56
    assert len({p["p_up"] for p in kalman}) > 1, "the forecasts follow the book"


async def test_what_comes_after_a_bar_close_never_changes_kalman_s_forecast(tmp_path: Path) -> None:
    cut = at("09:40:00")
    everything = a_book_morning()
    until_then = [e for e in everything if e[1]["received_at"] < cut]

    full = await run(tmp_path / "full.db", everything, until=at("10:00:00"), algorithms=[KALMAN])
    short = await run(tmp_path / "short.db", until_then, until=at("09:40:30"), algorithms=[KALMAN])

    def at_cut(predictions: list[dict[str, Any]]) -> dict[str, Any]:
        [p] = [p for p in predictions if p["bar_close"] == cut.isoformat()]
        return {k: p[k] for k in ("p_up", "p_flat", "p_down", "expected_ticks")}

    assert at_cut(await predictions_of(full, "kalman")) == at_cut(await predictions_of(short, "kalman"))


async def test_without_both_sizes_the_microprice_is_the_mid_point(tmp_path: Path) -> None:
    # Bid size only (a side's size missing): no lean either way.
    events = quiet_books("09:00:00", "09:03:00", 81_200, 81_300, bid_qty=300, ask_qty=0)
    probe = FixedAlgorithm("probe", features=OWN, unique=OWN)

    await run(tmp_path / "engine.db", events, until=at("09:03:00"), algorithms=[probe])

    assert probe.seen[-1]["microprice_ticks"] == (0.0, 0.0, 0.0)


def test_single_minutes_of_the_book_teach_nothing() -> None:
    one_minute = [
        replace(s, features=features([0.0], s.features.series("kospi200f_moves"))) for s in simulated(5)
    ]

    with pytest.raises(ValueError, match="two minutes"):
        KALMAN.fit(one_minute)
