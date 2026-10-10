"""Markov's features on bars made by hand, its forecasts, its training on made-up days, and
Markov on the prediction path.

Input: one-minute bars and the day's macro regime (a Snapshot), days of one-minute moves
drawn from a known regime-switching process, each day in a macro regime, or a replayed
morning with or without the day's regime. Output: the feature values, the forecasts, what
the fitted model forecasts, and the predictions and records the engine stores.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from krx_quant.algorithms import Model, Sample
from krx_quant.algorithms.markov import Markov, default_params
from krx_quant.features import FEATURES, FeatureVector, Snapshot, regime_code
from krx_quant.feeds import KST
from krx_quant.pricing import Direction, classify
from krx_quant.reference import Bar
from tests.replay_events import at, run
from tests.test_algorithms_replay import predictions_of
from tests.test_replay import busy_morning

MARKOV = Markov()
DAY = date(2026, 10, 12)
OPEN = datetime.combine(DAY, time(9), KST)
TICK = 100


def bar(m: int, close: float) -> Bar:
    at = OPEN + timedelta(minutes=m)
    return Bar(at, close, close, close, close, 1, 1, at, at)


def snapshot(closes: dict[int, float], *, regime: str | None = None, minutes: int = 30) -> Snapshot:
    bars = [bar(m, c) for m, c in sorted(closes.items())]
    close = OPEN + timedelta(minutes=minutes)
    return Snapshot.at("005930", close, bars, bars[-1].close, 0.00015, session_open=OPEN, regime=regime)


def test_the_minute_moves_are_in_ticks_since_the_open_a_quiet_minute_a_zero() -> None:
    pre_market = {-5: 80_000.0}
    closes = {0: 81_000.0, 1: 81_200.0, 2: 81_100.0, 4: 81_400.0}  # nothing traded at 09:03
    s = snapshot(pre_market | closes, minutes=5)

    assert FEATURES["minute_ticks"](s) == (2.0, -1.0, 0.0, 3.0)


def test_only_the_last_hour_of_moves_is_kept() -> None:
    s = snapshot({m: 81_000.0 + TICK * (m % 2) for m in range(0, 90)}, minutes=90)

    moves = FEATURES["minute_ticks"](s)

    assert isinstance(moves, tuple) and len(moves) == 60


def test_the_macro_regime_is_a_code_and_unknown_without_a_snapshot() -> None:
    assert FEATURES["macro_regime"](snapshot({0: 81_000.0}, regime="risk_on")) == regime_code("risk_on")
    assert FEATURES["macro_regime"](snapshot({0: 81_000.0})) is None


def features(moves: tuple[float, ...], regime: str | None = None, flat: float = 1.0) -> FeatureVector:
    code = regime_code(regime) if regime else None
    return FeatureVector({"minute_ticks": moves, "macro_regime": code, "flat_ticks": flat}, MARKOV.features)


@pytest.mark.parametrize(
    ("moves", "expected"),
    [
        ((0.0, 1.0, 2.0, 1.0, 2.0, 2.0, 1.0), Direction.UP),
        ((0.0, -1.0, -2.0, -1.0, -2.0, -2.0, -1.0), Direction.DOWN),
        ((0.0,) * 20, Direction.FLAT),
    ],
)
def test_the_default_model_follows_the_state_the_last_minutes_point_to(
    moves: tuple[float, ...], expected: Direction
) -> None:
    f = MARKOV.predict(features(moves), MARKOV.default_model())

    assert max(Direction, key=f.probability) is expected


# A known process: quiet, rising, falling and wild minutes (ticks), with each macro regime's
# own persistence. In risk_on a trend lasts; in risk_off it fades within a minute or two.
EMISSIONS = {"quiet": (0.0, 0.3), "up": (1.5, 0.7), "down": (-1.5, 0.7), "wild": (0.0, 3.0)}
STAY = {"risk_on": 0.97, "risk_off": 0.3}


def made_up_day(day: date, regime: str, seed: int) -> list[float]:
    rng = random.Random(seed)
    state, moves = "quiet", list[float]()
    for _ in range(375):
        if state in ("up", "down"):
            if rng.random() > STAY[regime]:
                state = "quiet"
        elif rng.random() < 0.05:
            state = rng.choice(["up", "down", "wild"]) if state == "quiet" else "quiet"
        mean, sd = EMISSIONS[state]
        moves.append(round(rng.gauss(mean, sd)))
    return moves


def day_samples(day: date, regime: str, moves: list[float]) -> list[Sample]:
    """A sample per minute, as the Feature Store gives them: the last hour of moves."""
    out = []
    for i in range(len(moves) - 4):
        t = datetime.combine(day, time(9, 1), KST) + timedelta(minutes=i)
        change = sum(moves[i + 1 : i + 5])
        outcome = classify(change * TICK, 1, TICK)
        hour = tuple(moves[max(0, i - 59) : i + 1])
        out.append(Sample("005930", t, features(hour, regime), outcome, change))
    return out


def test_baum_welch_learns_how_long_a_trend_lasts_in_each_regime() -> None:
    days = [date(2026, 9, 1) + timedelta(days=n) for n in range(12)]
    regimes = ["risk_on" if n % 2 else "risk_off" for n in range(len(days))]
    training = [
        s
        for n, (day, regime) in enumerate(zip(days, regimes, strict=True))
        for s in day_samples(day, regime, made_up_day(day, regime, n))
    ]

    model = MARKOV.fit(training)

    assert model.version.startswith("markov-20260912-")
    rising = (0.0, 0.0, 2.0, 1.0, 2.0)
    on = MARKOV.predict(features(rising, "risk_on"), model)
    off = MARKOV.predict(features(rising, "risk_off"), model)
    assert on.p_up > off.p_up + 0.05, (on, off)
    assert on.expected_ticks > off.expected_ticks
    # The states come out in their order: quiet, up, down, wild.
    means = [model.params[f"mean.{k}"] for k in range(4)]
    sds = [model.params[f"sd.{k}"] for k in range(4)]
    assert means[1] > 0.5 > -0.5 > means[2]
    assert sds[0] < min(sds[1], sds[2]) and sds[3] > max(sds[1], sds[2])


async def test_markov_predicts_and_is_scored_from_the_first_day(tmp_path: Path) -> None:
    engine = await run(
        tmp_path / "engine.db", busy_morning(), until=at("10:00:00"), regimes=lambda d: "neutral"
    )

    markov = await predictions_of(engine, "markov")

    assert len(markov) == 60 and {p["model_version"] for p in markov} == {"markov-default"}
    assert sum(p["outcome"] is not None for p in markov) == 56
    assert {p["status"] for p in markov} == {"ok"}


def runs(db: Path) -> list[tuple[str, str]]:
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT kind, detail FROM runs WHERE kind = 'no_regime'").fetchall()


async def test_a_day_without_a_macro_regime_runs_the_default_transitions_and_says_so(tmp_path: Path) -> None:
    # Trends last in risk_on; by default they fade at once.
    params = default_params()
    for i in range(4):
        for j in range(4):
            params[f"default.{i}.{j}"] = 0.25
            params[f"risk_on.{i}.{j}"] = 0.97 if i == j else 0.01
    model = {"markov": Model("markov-made", params)}

    known = await run(
        tmp_path / "known.db", busy_morning(), until=at("09:30:00"), regimes=lambda d: "risk_on", models=model
    )
    unknown = await run(
        tmp_path / "unknown.db", busy_morning(), until=at("09:30:00"), regimes=lambda d: None, models=model
    )

    trending = [abs(p["expected_ticks"]) for p in await predictions_of(known, "markov")]
    fading = [abs(p["expected_ticks"]) for p in await predictions_of(unknown, "markov")]
    assert sum(trending) > 2 * sum(fading)
    assert runs(tmp_path / "unknown.db") == [
        ("no_regime", "no macro regime for 2026-10-12: the default transitions")
    ]
    assert runs(tmp_path / "known.db") == []
