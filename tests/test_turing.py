"""Turing's own features on snapshots made by hand, and its learning on made-up samples.

Input: one-minute bars with the day's flows (program trading, foreign member firms,
investor estimates) and its calendar (a Snapshot), or samples whose outcome follows a rule
of the features. Output: the feature values, and what the fitted model forecasts.
"""

from __future__ import annotations

import math
import random
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant.algorithms import Model, Sample
from krx_quant.algorithms.turing import Turing
from krx_quant.contracts import days_to_expiry
from krx_quant.engine import Event
from krx_quant.features import FEATURES, FeatureVector, Flows, Snapshot
from krx_quant.feeds import KST
from krx_quant.market_calendar import MarketCalendar
from krx_quant.pricing import Direction
from krx_quant.reference import Bar
from tests.algorithm_doubles import FixedAlgorithm
from tests.replay_events import at, book, estimates, flows_morning, program, run
from tests.test_algorithms_replay import predictions_of
from tests.test_replay import busy_morning

TURING = Turing()
DAY = date(2026, 10, 12)  # a Monday; the October options expire on Thursday the 8th
OPEN = datetime.combine(DAY, time(9), KST)


def bar(m: int, close: float, volume: int) -> Bar:
    at = OPEN + timedelta(minutes=m)
    return Bar(at, close, close, close, close, volume, 1, at, at)


def snapshot(flows: Flows, *, minutes: int = 30, expiry_days: int | None = 3) -> Snapshot:
    bars = [bar(m, 10_000.0, 100) for m in range(minutes)]  # 100 shares, 1,000,000 won a minute
    close = OPEN + timedelta(minutes=minutes)
    return Snapshot.at(
        "005930", close, bars, 10_000.0, 0.00015, session_open=OPEN, flows=flows, expiry_days=expiry_days
    )


def value(name: str, s: Snapshot) -> float | None:
    v = FEATURES[name](s)
    assert not isinstance(v, tuple)
    return v


def test_the_flows_are_shares_of_what_traded() -> None:
    flows = Flows(
        program_now=3_000_000.0,
        program_before=2_000_000.0,  # 1,000,000 won bought by programs in the last 5 minutes
        foreign_member_now=-150.0,
        foreign_member_before=100.0,  # foreign firms sold 250 shares net
        foreign_estimate=600.0,
        institution_estimate=-300.0,
    )
    s = snapshot(flows)

    assert value("program_net", s) == pytest.approx(1_000_000 / 5_000_000)
    assert value("member_foreign_net", s) == pytest.approx(-250 / 500)
    assert value("investor_foreign", s) == pytest.approx(600 / 3_000)  # of the day's 3,000 shares
    assert value("investor_institution", s) == pytest.approx(-300 / 3_000)


def test_a_flow_is_unknown_without_where_it_stood_five_minutes_before() -> None:
    from_zero = snapshot(Flows(program_now=500_000.0, program_before=0.0))
    restarted = snapshot(Flows(program_now=500_000.0))

    assert value("program_net", from_zero) == pytest.approx(500_000 / 5_000_000)
    assert value("program_net", restarted) is None
    assert value("member_foreign_net", from_zero) is None
    assert value("investor_foreign", from_zero) is None


def test_the_time_of_day_and_the_trading_days_to_the_options_expiry() -> None:
    s = snapshot(Flows(), minutes=75, expiry_days=0)

    assert value("minutes_since_open", s) == 75.0
    assert value("days_to_expiry", s) == 0.0
    calendar = MarketCalendar({date(2026, 11, 11): False})  # a holiday before November's
    assert days_to_expiry(date(2026, 10, 8), calendar) == 0
    assert days_to_expiry(DAY, calendar) == 22, "to Thursday 12 November, the 11th not trading"


def features(**values: float | None) -> FeatureVector:
    known = dict.fromkeys(TURING.features) | values
    return FeatureVector(known, TURING.features)


def made_up(n: int, seed: int = 0) -> list[Sample]:
    """Programs buying (selling) push the price up (down) four minutes later; the rest is noise."""
    rng = random.Random(seed)
    out = []
    t = datetime.combine(date(2026, 9, 1), time(9, 1), KST)
    for i in range(n):
        program = rng.uniform(-1, 1)
        noise = rng.gauss(0, 0.3)
        move = 3 * program + noise
        outcome = Direction.UP if move > 1 else Direction.DOWN if move < -1 else Direction.FLAT
        day = t + timedelta(days=i // 300, minutes=i % 300)
        f = features(program_net=program, flat_ticks=1.0, volatility=rng.uniform(0.5, 2))
        out.append(Sample("005930", day, f, outcome, move))
    return out


def test_turing_learns_which_way_the_programs_push() -> None:
    model = TURING.fit(made_up(3000))

    assert model.version.startswith("turing-2026") and model.blob
    buying = TURING.predict(features(program_net=0.8, flat_ticks=1.0, volatility=1.0), model)
    selling = TURING.predict(features(program_net=-0.8, flat_ticks=1.0, volatility=1.0), model)
    still = TURING.predict(features(program_net=0.0, flat_ticks=1.0, volatility=1.0), model)
    assert buying.p_up > 0.8 and selling.p_down > 0.8 and still.p_flat > 0.6
    assert buying.expected_ticks > 1 > -1 > selling.expected_ticks
    # Saved and read back, the model forecasts the same.
    again = Model.from_json(model.to_json())
    assert TURING.predict(features(program_net=0.8, flat_ticks=1.0, volatility=1.0), again) == buying


def test_the_same_samples_give_the_same_model() -> None:
    assert TURING.fit(made_up(1000)).version == TURING.fit(made_up(1000)).version


def a_morning(end: str) -> list[Event]:
    return sorted([*busy_morning(end=end), *flows_morning(end=end)], key=lambda e: e[1]["received_at"])


def trained() -> Model:
    """Turing fitted on made-up samples where the programs push the price."""
    return TURING.fit(made_up(3000))


async def test_turing_predicts_and_is_scored_on_the_prediction_path(tmp_path: Path) -> None:
    model = trained()

    engine = await run(
        tmp_path / "engine.db", a_morning("10:00:00"), until=at("10:00:00"), models={"turing": model}
    )

    turing = await predictions_of(engine, "turing")
    assert len(turing) == 60 and {p["model_version"] for p in turing} == {model.version}
    assert {p["status"] for p in turing} == {"ok"}
    assert sum(p["outcome"] is not None for p in turing) == 56
    assert len({p["p_up"] for p in turing}) > 1, "the forecasts follow the programs"


async def test_what_comes_after_a_bar_close_never_changes_its_forecast(tmp_path: Path) -> None:
    model = trained()
    cut = at("09:40:00")
    everything = a_morning("10:00:00")
    until_then = [e for e in everything if e[1]["received_at"] < cut]

    full = await run(tmp_path / "full.db", everything, until=at("10:00:00"), models={"turing": model})
    short = await run(tmp_path / "short.db", until_then, until=at("09:40:30"), models={"turing": model})

    def at_cut(engine_predictions: list[dict[str, Any]]) -> dict[str, Any]:
        [p] = [p for p in engine_predictions if p["bar_close"] == "2026-10-12T09:40:00+09:00"]
        return {k: p[k] for k in ("p_up", "p_flat", "p_down", "expected_ticks")}

    assert at_cut(await predictions_of(full, "turing")) == at_cut(await predictions_of(short, "turing"))


async def test_a_day_s_flows_start_fresh_the_next_morning(tmp_path: Path) -> None:
    tuesday = date(2026, 10, 13)
    flows = ("program_net", "investor_foreign")
    probe = FixedAlgorithm("probe", features=flows, unique=flows)
    monday = [program(at("10:00:00"), 9_000_000), *estimates(at("10:01:00"), {"5": (40_000, 0)})]
    next_morning = [
        *busy_morning(day=tuesday, end="09:40:00"),
        program(at("09:00:30", tuesday), 1_000_000),
        *estimates(at("09:31:00", tuesday), {"1": (500, 100)}),
    ]
    events = sorted([*a_morning("10:30:00"), *monday, *next_morning], key=lambda e: e[1]["received_at"])

    await run(
        tmp_path / "engine.db",
        events,
        until=at("09:40:00", tuesday),
        start=at("00:00:00"),
        calendar=MarketCalendar({DAY: True, tuesday: True}),
        algorithms=[probe],
    )

    monday_closes = 376  # 09:01–15:16
    at_0902, at_0935 = probe.seen[monday_closes + 1], probe.seen[monday_closes + 34]
    program_0902, foreign_0935 = at_0902["program_net"], at_0935["investor_foreign"]
    assert isinstance(program_0902, float) and isinstance(foreign_0935, float)
    assert program_0902 > 0, "today's 1,000,000 from 0"
    assert at_0902["investor_foreign"] is None, "Monday's estimate is not Tuesday's"
    assert foreign_0935 > 0


BOOK_OWN = ("spread_ticks", "top_imbalance", "top_size_change")


async def test_the_best_quotes_spread_balance_and_change_at_a_bar_close(tmp_path: Path) -> None:
    probe = FixedAlgorithm("probe", features=BOOK_OWN, unique=BOOK_OWN)
    events = [
        book(at("09:00:00"), 81_200, 81_300, bid_qty=100, ask_qty=100),  # right at the open
        book(at("09:00:50"), 81_200, 81_400, bid_qty=200, ask_qty=50),
        book(at("09:01:20"), 81_300, 81_400, bid_qty=60, ask_qty=90),
    ]

    await run(tmp_path / "engine.db", events, until=at("09:04:00"), algorithms=[probe])

    at_0901, at_0902, _, at_0904 = probe.seen
    # 09:01: the 09:00:50 quotes, 200 won apart at a 100-won tick; no change in the first
    # minute, even with a quote right at the open.
    assert at_0901["spread_ticks"] == pytest.approx(2.0)
    assert at_0901["top_imbalance"] == pytest.approx((200 - 50) / 250)
    assert at_0901["top_size_change"] is None
    # 09:02: the 09:01:20 quotes, against the 09:00:50 ones a minute before: 150 waiting at
    # the best quotes, down from 250.
    assert at_0902["spread_ticks"] == pytest.approx(1.0)
    assert at_0902["top_imbalance"] == pytest.approx((60 - 90) / 150)
    assert at_0902["top_size_change"] == pytest.approx(math.log(150 / 250))
    # 09:04: no quote for over a minute: the book is missing.
    assert at_0904 == dict.fromkeys(BOOK_OWN)


def test_turing_reads_the_book_the_tape_and_the_options_besides_its_own() -> None:
    for name in ("depth_imbalance", "ofi_1", "trade_entropy", "nxt_share", "atm_iv", *BOOK_OWN):
        assert name in TURING.features
    assert "microprice_ticks" not in TURING.features, "a series: Markov's and Kalman's"


def test_a_model_trained_before_the_book_s_features_still_predicts() -> None:
    class BeforeTheBook(Turing):  # the one-minute version's features (#16)
        features = tuple(
            n
            for n in TURING.features
            if n
            not in {
                "depth_imbalance",
                "ofi_1",
                "ofi_5",
                "trade_imbalance",
                "trade_entropy",
                "nxt_share",
                "atm_iv",
                *BOOK_OWN,
            }
            and not n.startswith(("excitation_", "arrivals_"))
        )

    older = BeforeTheBook()
    model = older.fit([replace(s, features=FeatureVector(s.features, older.features)) for s in made_up(1000)])
    now = features(program_net=0.8, flat_ticks=1.0, volatility=1.0, spread_ticks=3.0, top_imbalance=-0.5)

    assert TURING.predict(now, model) == older.predict(FeatureVector(now, older.features), model)


def test_a_model_reads_the_features_it_was_trained_on_whatever_is_registered_since() -> None:
    class Older(Turing):  # trained before the last feature was registered, in another order
        features = tuple(reversed(TURING.features[:-1]))

    older = Older()
    model = older.fit([replace(s, features=FeatureVector(s.features, older.features)) for s in made_up(1000)])
    now = features(program_net=0.8, flat_ticks=1.0, volatility=1.0)

    assert TURING.predict(now, model) == older.predict(FeatureVector(now, older.features), model)


async def test_after_a_restart_a_flow_waits_for_its_own_five_minutes(tmp_path: Path) -> None:
    flows = ("program_net",)
    probe = FixedAlgorithm("probe", features=flows, unique=flows)
    # The engine came back at 11:00: the day's 90,000,000 so far is not five minutes' worth.
    events = [
        *busy_morning(end="11:10:00"),
        program(at("11:00:10"), 90_000_000),
        program(at("11:05:20"), 91_000_000),
    ]
    late = sorted(
        (e for e in events if e[1]["received_at"] >= at("11:00:00")), key=lambda e: e[1]["received_at"]
    )

    await run(tmp_path / "engine.db", late, until=at("11:10:00"), start=at("11:00:00"), algorithms=[probe])

    by_close = dict(zip((at(f"11:{m:02d}:00") for m in range(1, 11)), probe.seen, strict=True))
    assert by_close[at("11:02:00")]["program_net"] is None
    assert by_close[at("11:10:00")]["program_net"] is not None, "1,000,000 since 11:05"


async def test_an_estimate_not_entered_in_a_slot_keeps_the_earlier_one(tmp_path: Path) -> None:
    flows = ("investor_foreign", "investor_institution")
    probe = FixedAlgorithm("probe", features=flows, unique=flows)
    # 10:00 brings institutions only: the foreign field is blank, not a zero.
    polls = [
        *estimates(at("09:31:00"), {"1": (12_000, None)}),
        *estimates(at("10:01:00"), {"1": (12_000, None), "2": (None, 5_000)}),
    ]
    events = sorted([*busy_morning(end="10:05:00"), *polls], key=lambda e: e[1]["received_at"])

    await run(tmp_path / "engine.db", events, until=at("10:05:00"), algorithms=[probe])

    at_1005 = probe.seen[-1]
    foreign, institution = at_1005["investor_foreign"], at_1005["investor_institution"]
    assert isinstance(foreign, float) and isinstance(institution, float)
    assert foreign > 0 and institution > 0, "foreign from 09:30, institutions from 10:00"
