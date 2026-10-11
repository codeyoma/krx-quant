"""Shannon: its own features through the Replay Harness, its L1 logistic regression on
made-up samples, and its predictions on the prediction path.

Input: integrated books with their ten levels' sizes, trades with their aggressor side
and venue, or samples whose outcome follows the order flow. Output: the feature values an
algorithm sees at each bar close, what the fitted model forecasts, and what the engine
stores.
"""

from __future__ import annotations

import math
import random
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant.algorithms import Abstention, NothingToLearn, NotPredicted, Sample
from krx_quant.algorithms.shannon import INPUTS, Shannon
from krx_quant.engine import Event
from krx_quant.features import FeatureVector, Leaders
from krx_quant.feeds import BOOK, KRX_TRADE, KST, NXT_TRADE
from krx_quant.feeds.base import LEVELS
from krx_quant.pricing import Direction
from krx_quant.training import recorded_samples
from tests.algorithm_doubles import FixedAlgorithm
from tests.replay_events import CALENDAR, DAY, FEE, at, book, every, run, trade, with_sides
from tests.test_algorithms_replay import predictions_of
from tests.test_replay import busy_morning

OWN = ("ofi_1", "ofi_5", "trade_entropy")
SHARED = ("depth_imbalance", "trade_imbalance", "nxt_share")


def first_minute() -> list[Event]:
    """Three books in the first minute: the bid's size grows, then the ask lifts a tick.
    Four trades through the integrated feed, and the same volume by venue."""
    return [
        book(at("09:00:10"), 81_200, 81_300, bid_qty=100, ask_qty=100, deeper=(10, 30)),
        book(at("09:00:30"), 81_200, 81_300, bid_qty=200, ask_qty=100, deeper=(10, 30)),
        book(at("09:00:50"), 81_200, 81_400, bid_qty=200, ask_qty=50, deeper=(10, 30)),
        *(trade(at(f"09:00:{s}"), 81_300, 10, side="buy") for s in ("15", "25", "35")),
        trade(at("09:00:45"), 81_200, 50, side="sell"),
        trade(at("09:00:15"), 81_300, 60, feed=KRX_TRADE),
        trade(at("09:00:45"), 81_200, 20, feed=NXT_TRADE),
    ]


async def test_the_order_flow_of_the_minutes_before_a_bar_close(tmp_path: Path) -> None:
    probe = FixedAlgorithm("probe", features=OWN + SHARED, unique=OWN + SHARED)
    events = sorted(first_minute(), key=lambda e: e[1]["received_at"])

    await run(tmp_path / "engine.db", events, until=at("09:03:00"), algorithms=[probe])

    at_0901, _, at_0903 = probe.seen
    # OFI: 0 for the first book; +200 − 100 as the bid grows at the same price; +100 as the
    # ask lifts (its 100 gone). Over the mean best-quote size, (100 + 150 + 125) / 3.
    assert at_0901["ofi_1"] == pytest.approx(200 / 125)
    assert at_0901["ofi_5"] == pytest.approx(200 / 125), "the five minutes start at the open"
    # Ten levels at 09:00:50: 200 + 9 × 10 bid against 50 + 9 × 30 ask.
    assert at_0901["depth_imbalance"] == pytest.approx((290 - 320) / 610)
    buy = 0.75  # three buys of four
    assert at_0901["trade_entropy"] == pytest.approx(-(buy * math.log2(buy) + (1 - buy) * math.log2(1 - buy)))
    assert at_0901["trade_imbalance"] == pytest.approx((30 - 50) / 80)
    assert at_0901["nxt_share"] == pytest.approx(20 / 80)
    # No book for over a minute by 09:03: the book's features are unknown, the trades' remain.
    assert at_0903["ofi_1"] is None and at_0903["ofi_5"] is None and at_0903["depth_imbalance"] is None
    assert at_0903["trade_imbalance"] == pytest.approx(-0.25)


async def test_a_window_counts_only_what_came_in_it_after_a_busy_pre_market(tmp_path: Path) -> None:
    # NXT's pre-market buys from 08:00 to 08:50: long gone from the first minute's windows.
    early = every(at("08:00:00"), at("08:50:00"), 10, lambda w: trade(w, 81_300, 7, side="buy"))
    early += every(at("08:00:05"), at("08:50:00"), 10, lambda w: trade(w, 81_300, 7, feed=NXT_TRADE))
    probe = FixedAlgorithm("probe", features=OWN + SHARED, unique=OWN + SHARED)
    events = sorted([*early, *first_minute()], key=lambda e: e[1]["received_at"])

    await run(tmp_path / "engine.db", events, until=at("09:01:00"), algorithms=[probe])

    [at_0901] = probe.seen
    assert at_0901["trade_imbalance"] == pytest.approx((30 - 50) / 80)
    assert at_0901["nxt_share"] == pytest.approx(20 / 80)


async def test_the_order_flow_restarts_after_the_book_went_missing(tmp_path: Path) -> None:
    # The bid grows all pre-market; no book from 08:10 until 10:27, when it comes back larger
    # and stands still. The first book back is not a move from the one two hours before.
    early = [
        book(at("08:00:00") + timedelta(seconds=10 * i), 81_200, 81_300, bid_qty=100 + 10 * i, ask_qty=100)
        for i in range(60)
    ]
    late = every(
        at("10:27:00"), at("10:31:00"), 10, lambda w: book(w, 81_200, 81_300, bid_qty=900, ask_qty=100)
    )
    probe = FixedAlgorithm("probe", features=OWN + SHARED, unique=OWN + SHARED)

    await run(tmp_path / "engine.db", [*early, *late], until=at("10:31:00"), algorithms=[probe])

    assert probe.seen[-1]["ofi_5"] == 0.0, "nothing moved since the book came back"


async def test_without_nxt_s_trades_its_share_is_unknown(tmp_path: Path) -> None:
    # The NXT feed is not there (cut by the session limit, or KRX_VENUE_TRADES off).
    events = [e for e in first_minute() if e[0] != NXT_TRADE.tr_id]
    probe = FixedAlgorithm("probe", features=OWN + SHARED, unique=OWN + SHARED)

    await run(
        tmp_path / "engine.db",
        sorted(events, key=lambda e: e[1]["received_at"]),
        until=at("09:01:00"),
        algorithms=[probe],
    )

    assert probe.seen[-1]["nxt_share"] is None


SHANNON = Shannon()
NOISE = ("ofi_5", "depth_imbalance", "trade_imbalance", "trade_entropy", "nxt_share")


def inputs(**values: float | None) -> FeatureVector:
    return FeatureVector(dict.fromkeys(INPUTS) | {"depth_imbalance": 0.0} | values, SHANNON.features)


def made_up(n: int, seed: int = 0) -> list[Sample]:
    """The last minute's order flow imbalance sets the next four minutes; the other inputs
    are noise, and the flows unknown (a restart)."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        ofi = rng.uniform(-2, 2)
        move = 2 * ofi + rng.gauss(0, 1)
        outcome = Direction.UP if move > 1.5 else Direction.DOWN if move < -1.5 else Direction.FLAT
        noise = {
            "ofi_5": rng.gauss(0, 1),
            "depth_imbalance": rng.uniform(-1, 1),
            "trade_imbalance": rng.uniform(-1, 1),
            "trade_entropy": rng.uniform(0, 1),
            "nxt_share": rng.uniform(0, 0.3),
        }
        bar_close = datetime.combine(date(2026, 9, 1), time(9, 1), KST) + timedelta(
            days=i // 300, minutes=i % 300
        )
        out.append(Sample("005930", bar_close, inputs(ofi_1=ofi, **noise), outcome, move))
    return out


def test_shannon_learns_which_way_the_order_flow_pushes_and_drops_the_noise() -> None:
    model = SHANNON.fit(made_up(1000))

    assert model.version.startswith("shannon-2026")
    buying = SHANNON.predict(inputs(ofi_1=1.5), model)
    selling = SHANNON.predict(inputs(ofi_1=-1.5), model)
    still = SHANNON.predict(inputs(ofi_1=0.0), model)
    assert buying.p_up > 0.6 and selling.p_down > 0.6 and still.p_flat > 0.5
    assert buying.expected_ticks > 1 > -1 > selling.expected_ticks
    weights = {k: v for k, v in model.params.items() if k.startswith("w.")}
    assert weights["w.up.ofi_1"] > 1 and weights["w.down.ofi_1"] < -1
    noise = [v for k, v in weights.items() if k.split(".")[2] in NOISE]
    assert sum(v == 0 for v in noise) > len(noise) / 2, "L1 leaves most of the noise at 0"
    assert max(abs(v) for v in noise) < weights["w.up.ofi_1"] / 10, "the rest, small beside the order flow"
    assert all(
        weights[f"w.{c}.{f}"] == 0
        for c in ("up", "flat", "down")
        for f in ("program_net", "member_foreign_net")
    )


def test_the_same_samples_give_the_same_model() -> None:
    assert SHANNON.fit(made_up(300)).version == SHANNON.fit(made_up(300)).version


def test_without_the_book_shannon_neither_learns_nor_predicts() -> None:
    no_book = [replace(s, features=inputs(depth_imbalance=None, ofi_1=1.0)) for s in made_up(20)]

    with pytest.raises(NothingToLearn, match="with the book"):
        SHANNON.fit(no_book)
    with pytest.raises(NotPredicted) as declined:
        SHANNON.predict(no_book[0].features, SHANNON.default_model())
    assert declined.value.why is Abstention.NO_BOOK


def an_order_book_morning(end: str = "10:00:00") -> list[Event]:
    """The busy morning with sizes on every level, leaning now one way, now the other, and
    each trade's aggressor."""
    events: list[Event] = []
    for feed, row in busy_morning(end=end):
        if feed == BOOK.tr_id:
            step = int(row["received_at"].timestamp()) // 13
            sizes = {f"bid_qty_{i}": 100 + 50 * (step % 5) for i in LEVELS}
            sizes |= {f"ask_qty_{i}": 100 + 50 * ((step * 3) % 5) for i in LEVELS}
            row = {**row, **sizes}
        events.append((feed, row))
    return with_sides(events)


async def test_without_the_book_at_a_bar_close_shannon_declines(tmp_path: Path) -> None:
    events = [
        e for e in an_order_book_morning() if not at("09:06:00") <= e[1]["received_at"] < at("09:07:30")
    ]

    engine = await run(tmp_path / "engine.db", events, until=at("09:20:00"), algorithms=[SHANNON])

    shannon = {p["bar_close"]: p for p in await predictions_of(engine, "shannon")}
    declined = shannon[at("09:07:00").isoformat()]
    assert declined["status"] == "no_book" and declined["p_up"] is None and declined["outcome"] is None
    assert {p["status"] for t, p in shannon.items() if t != at("09:07:00").isoformat()} == {"ok"}


async def test_shannon_predicts_and_is_scored_on_the_prediction_path(tmp_path: Path) -> None:
    morning = an_order_book_morning("11:00:00")
    model = SHANNON.fit(
        recorded_samples(morning, CALENDAR, DAY, ("005930",), SHANNON.features, FEE, Leaders())
    )

    engine = await run(
        tmp_path / "engine.db", an_order_book_morning(), until=at("10:00:00"), models={"shannon": model}
    )

    shannon = await predictions_of(engine, "shannon")
    assert len(shannon) == 60 and {p["status"] for p in shannon} == {"ok"}
    assert {p["model_version"] for p in shannon} == {model.version}
    assert sum(p["outcome"] is not None for p in shannon) == 56


async def test_what_comes_after_a_bar_close_never_changes_shannon_s_forecast(tmp_path: Path) -> None:
    model = SHANNON.fit(made_up(1000))
    cut = at("09:40:00")
    everything = an_order_book_morning()
    until_then = [e for e in everything if e[1]["received_at"] < cut]

    full = await run(tmp_path / "full.db", everything, until=at("10:00:00"), models={"shannon": model})
    short = await run(tmp_path / "short.db", until_then, until=at("09:40:30"), models={"shannon": model})

    def at_cut(predictions: list[dict[str, Any]]) -> dict[str, Any]:
        [p] = [p for p in predictions if p["bar_close"] == cut.isoformat()]
        return {k: p[k] for k in ("p_up", "p_flat", "p_down", "expected_ticks")}

    assert at_cut(await predictions_of(full, "shannon")) == at_cut(await predictions_of(short, "shannon"))
