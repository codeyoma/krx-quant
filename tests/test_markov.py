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
from krx_quant.baselines import Forecast
from krx_quant.engine import Event
from krx_quant.features import FEATURES, FeatureVector, Leaders, Snapshot, regime_code
from krx_quant.feeds import KST
from krx_quant.pricing import Direction, classify
from krx_quant.reference import Bar
from krx_quant.training import recorded_samples
from tests.algorithm_doubles import FixedAlgorithm
from tests.replay_events import CALENDAR, FEE, at, option_trade, run
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
    # Nothing traded since 09:04: at 09:07 the two quiet minutes are moves of 0 too.
    assert FEATURES["minute_ticks"](snapshot(closes, minutes=7)) == (2.0, -1.0, 0.0, 3.0, 0.0, 0.0)


def test_only_the_last_hour_of_moves_is_kept() -> None:
    s = snapshot({m: 81_000.0 + TICK * (m % 2) for m in range(0, 90)}, minutes=90)

    moves = FEATURES["minute_ticks"](s)

    assert isinstance(moves, tuple) and len(moves) == 60


def test_the_macro_regime_is_a_code_and_unknown_without_a_snapshot() -> None:
    assert FEATURES["macro_regime"](snapshot({0: 81_000.0}, regime="risk_on")) == regime_code("risk_on")
    assert FEATURES["macro_regime"](snapshot({0: 81_000.0})) is None
    assert FEATURES["macro_regime"](snapshot({0: 81_000.0}, regime="unheard_of")) is None, "unknown: default"


def features(
    moves: tuple[float, ...], regime: str | None = None, flat: float = 1.0, iv: float | None = None
) -> FeatureVector:
    code = regime_code(regime) if regime else None
    return FeatureVector(
        {"minute_ticks": moves, "macro_regime": code, "flat_ticks": flat, "atm_iv": iv}, MARKOV.features
    )


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


def same(a: Forecast, b: Forecast) -> bool:
    return (a.p_up, a.p_flat, a.p_down, a.expected_ticks) == pytest.approx(
        (b.p_up, b.p_flat, b.p_down, b.expected_ticks)
    )


def test_in_a_high_iv_regime_the_states_move_more_and_without_the_iv_as_before() -> None:
    # The options' implied volatility below 18% is low, from 24% high; moves scale with it.
    params = default_params() | {"iv_cut.0": 18.0, "iv_cut.1": 24.0, "iv_scale.0": 0.7, "iv_scale.2": 1.6}
    model = Model("markov-made", params)
    moves = (0.0, 1.0, 0.0, -1.0, 0.0, 1.0)

    low = MARKOV.predict(features(moves, iv=15.0), model)
    normal = MARKOV.predict(features(moves, iv=20.0), model)
    high = MARKOV.predict(features(moves, iv=30.0), model)

    assert high.p_flat < normal.p_flat < low.p_flat
    before = MARKOV.predict(features(moves), MARKOV.default_model())
    assert same(normal, before), "a normal IV moves as the states do"
    assert same(MARKOV.predict(features(moves), model), before), "an unknown IV too"
    # A model trained before the IV (no bounds) reads none.
    assert same(MARKOV.predict(features(moves, iv=30.0), MARKOV.default_model()), before)


# A known process: quiet, rising, falling and wild minutes (ticks), with each macro regime's
# own persistence. In risk_on a trend lasts; in risk_off it fades within a minute or two.
EMISSIONS = {"quiet": (0.0, 0.3), "up": (1.5, 0.7), "down": (-1.5, 0.7), "wild": (0.0, 3.0)}
STAY = {"risk_on": 0.97, "risk_off": 0.3}


def made_up_day(day: date, regime: str, seed: int, scale: float = 1.0) -> list[float]:
    """A day's minute moves in ticks; ``scale`` times larger (the options' IV regime's)."""
    rng = random.Random(seed)
    state, moves = "quiet", list[float]()
    for _ in range(375):
        if state in ("up", "down"):
            if rng.random() > STAY[regime]:
                state = "quiet"
        elif rng.random() < 0.05:
            state = rng.choice(["up", "down", "wild"]) if state == "quiet" else "quiet"
        mean, sd = EMISSIONS[state]
        moves.append(round(scale * rng.gauss(mean, sd)))
    return moves


def day_samples(day: date, regime: str, moves: list[float], iv: float | None = None) -> list[Sample]:
    """A sample per minute, as the Feature Store gives them: the last hour of moves, and the
    options' IV around ``iv`` (a recorded day; None: backfilled)."""
    out = []
    for i in range(len(moves) - 4):
        t = datetime.combine(day, time(9, 1), KST) + timedelta(minutes=i)
        change = sum(moves[i + 1 : i + 5])
        outcome = classify(change * TICK, 1, TICK)
        hour = tuple(moves[max(0, i - 59) : i + 1])
        at_close = None if iv is None else iv + 0.1 * (i % 5)
        out.append(Sample("005930", t, features(hour, regime, iv=at_close), outcome, change))
    return out


# Recorded days' IV (%) and how much larger their minutes move, by IV regime.
IV_DAYS = {"low": (15.0, 0.6), "normal": (20.0, 1.0), "high": (28.0, 1.8)}


def iv_days(count: int, start: date = date(2026, 9, 1)) -> list[Sample]:
    """``count`` recorded days in risk_on, the IV regimes in turn."""
    out = []
    for n in range(count):
        day = start + timedelta(days=n)
        iv, scale = IV_DAYS[("low", "normal", "high")[n % 3]]
        out += day_samples(day, "risk_on", made_up_day(day, "risk_on", n, scale), iv)
    return out


def test_training_learns_the_iv_bounds_and_how_much_more_each_regime_moves() -> None:
    model = MARKOV.fit(iv_days(12))

    # The bounds split the recorded bar closes in three: between the days' levels.
    assert 15.4 < model.params["iv_cut.0"] < 20.0
    assert 20.4 < model.params["iv_cut.1"] < 28.0
    low, normal, high = (model.params[f"iv_scale.{r}"] for r in range(3))
    # Normal is the states' own size, as an unknown IV is: the others are against it.
    assert normal == 1.0
    assert low == pytest.approx(0.6, abs=0.1)
    assert high == pytest.approx(1.8, abs=0.25)
    moves = (0.0, 1.0, 0.0, -1.0, 0.0, 1.0)
    assert same(MARKOV.predict(features(moves, iv=20.0), model), MARKOV.predict(features(moves), model))


def test_with_fewer_than_five_recorded_days_of_the_iv_its_regimes_are_not_learned() -> None:
    backfilled = [
        s
        for n in range(4)
        for s in day_samples(
            date(2026, 8, 3) + timedelta(days=n),
            "risk_on",
            made_up_day(date(2026, 8, 3), "risk_on", 100 + n),
        )
    ]

    model = MARKOV.fit(backfilled + iv_days(4))

    assert "iv_cut.0" not in model.params and "iv_cut.1" not in model.params
    assert [model.params[f"iv_scale.{r}"] for r in range(3)] == [1.0, 1.0, 1.0]


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
        tmp_path / "engine.db", busy_morning(), until=at("10:00:00"), regimes=lambda d, before: "neutral"
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
        tmp_path / "known.db",
        busy_morning(),
        until=at("09:30:00"),
        regimes=lambda d, before: "risk_on",
        models=model,
    )
    unknown = await run(
        tmp_path / "unknown.db",
        busy_morning(),
        until=at("09:30:00"),
        regimes=lambda d, before: None,
        models=model,
    )

    trending = [abs(p["expected_ticks"]) for p in await predictions_of(known, "markov")]
    fading = [abs(p["expected_ticks"]) for p in await predictions_of(unknown, "markov")]
    assert sum(trending) > 2 * sum(fading)
    assert runs(tmp_path / "unknown.db") == [("no_regime", "no macro regime for 2026-10-12")]
    assert runs(tmp_path / "known.db") == []
    # A regime this engine does not know (a newer collector's) is no regime, said so too.
    await run(tmp_path / "new.db", busy_morning(), until=at("09:05:00"), regimes=lambda d, before: "melt_up")
    assert runs(tmp_path / "new.db") == [("no_regime", "no macro regime for 2026-10-12 (unknown: melt_up)")]


def options_morning() -> list[Event]:
    """A wandering morning, and option trades around the money: the call nearest delta 0.5
    and the put nearest −0.5 by 09:01, a newer call trade after it, then none. In receipt
    order."""
    events = [
        *busy_morning(end="09:13:00"),
        option_trade(at("09:00:20"), "C1", 21.0, 0.62),
        option_trade(at("09:00:30"), "C2", 20.0, 0.48),
        option_trade(at("09:00:40"), "P2", 22.0, -0.53),
        option_trade(at("09:00:50"), "P3", 23.5, -0.30),
        option_trade(at("09:01:00"), "C2", 30.0, 0.50),  # at the bar close: after it
        option_trade(at("09:01:30"), "C2", 19.0, 0.51),
    ]
    return sorted(events, key=lambda e: e[1]["received_at"])


async def test_the_atm_implied_vol_is_the_call_and_put_nearest_the_money(tmp_path: Path) -> None:
    probe = FixedAlgorithm("probe", features=("atm_iv",), unique=("atm_iv",))

    await run(tmp_path / "engine.db", options_morning(), until=at("09:13:00"), algorithms=[probe])

    iv = dict(zip(range(1, 13), (seen["atm_iv"] for seen in probe.seen), strict=False))  # by minute
    assert iv[1] == pytest.approx((20.0 + 22.0) / 2), "09:01: C2 and P2, what came before the close"
    assert iv[2] == pytest.approx(((30.0 + 19.0) / 2 + 22.0) / 2), "09:02: C2's two trades in the minute"
    assert iv[10] == pytest.approx((19.0 + 22.0) / 2), "no trade in the last minute: the latest"
    # A trade older than ten minutes says nothing: by 09:11 P2's (09:00:40) is gone, by
    # 09:12 every one.
    assert iv[11] == pytest.approx(19.0)
    assert iv[12] is None


async def test_options_equally_near_the_money_count_alike(tmp_path: Path) -> None:
    probe = FixedAlgorithm("probe", features=("atm_iv",), unique=("atm_iv",))
    events = [
        *busy_morning(end="09:02:00"),
        option_trade(at("09:00:20"), "C1", 24.0, 0.55),
        option_trade(at("09:00:30"), "C2", 20.0, 0.45),  # as near 0.5: neither IV wins
        option_trade(at("09:00:40"), "P2", 22.0, -0.5),
    ]

    await run(tmp_path / "engine.db", events, until=at("09:01:00"), algorithms=[probe])

    assert probe.seen[0]["atm_iv"] == pytest.approx(((24.0 + 20.0) / 2 + 22.0) / 2)


async def test_an_odd_last_trade_is_outvoted_by_the_minute_before_it(tmp_path: Path) -> None:
    probe = FixedAlgorithm("probe", features=("atm_iv",), unique=("atm_iv",))
    events = [
        *busy_morning(end="09:02:00"),
        option_trade(at("08:59:50"), "C2", 30.0, 0.50),  # over a minute before 09:01: not counted
        option_trade(at("09:00:10"), "C2", 20.0, 0.50),
        option_trade(at("09:00:20"), "C2", 20.4, 0.50),
        option_trade(at("09:00:30"), "C2", 20.2, 0.50),
        option_trade(at("09:00:40"), "C2", 12.0, 0.50),  # an odd print, the call's last
        option_trade(at("09:00:50"), "P2", 22.0, -0.50),
    ]

    await run(tmp_path / "engine.db", events, until=at("09:01:00"), algorithms=[probe])

    # The call: the median of its four trades in the minute (20.0, 20.4, 20.2, 12.0), not the
    # last one's 12.0; the put: its one.
    assert probe.seen[0]["atm_iv"] == pytest.approx((20.1 + 22.0) / 2)


async def test_training_reads_the_atm_implied_vol_as_the_engine_does(tmp_path: Path) -> None:
    samples = recorded_samples(options_morning(), CALENDAR, DAY, ("005930",), ("atm_iv",), FEE, Leaders())

    assert [s.features["atm_iv"] for s in samples[:2]] == [pytest.approx(21.0), pytest.approx(23.25)]


def iv_morning(iv: float) -> list[Event]:
    """A wandering morning with the at-the-money call and put trading at ``iv`` every 20 s."""
    options = []
    when = at("09:00:00")
    while when <= at("10:00:00"):
        options += [option_trade(when, "C2", iv, 0.5), option_trade(when, "P2", iv, -0.5)]
        when += timedelta(seconds=20)
    return [*busy_morning(), *options]


async def test_markov_reads_the_iv_regime_on_the_prediction_path_and_is_scored(tmp_path: Path) -> None:
    params = default_params() | {"iv_cut.0": 18.0, "iv_cut.1": 24.0, "iv_scale.0": 0.7, "iv_scale.2": 1.6}
    models = {"markov": Model("markov-made", params)}

    def neutral(day: date, before: datetime) -> str:
        return "neutral"

    calm = await run(
        tmp_path / "calm.db", iv_morning(15.0), until=at("10:00:00"), regimes=neutral, models=models
    )
    jumpy = await run(
        tmp_path / "jumpy.db", iv_morning(30.0), until=at("10:00:00"), regimes=neutral, models=models
    )

    low, high = await predictions_of(calm, "markov"), await predictions_of(jumpy, "markov")
    assert len(high) == 60 and {p["status"] for p in high} == {"ok"}
    assert {p["model_version"] for p in high} == {"markov-made"}
    assert sum(p["outcome"] is not None for p in high) == 56
    # The same moves: in a high IV regime they are read as larger, so a flat four minutes
    # is less likely.
    assert sum(p["p_flat"] for p in high) < sum(p["p_flat"] for p in low)
