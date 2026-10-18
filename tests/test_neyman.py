"""Neyman (#266): gradient-boosted trees and a second opinion on their up and down calls.

Input: made-up samples in which programs buying (selling) push the price up (down), by more
when the market is volatile (a wide barrier), so the first model's up and down calls are right
in a volatile market and wrong in a quiet one; days of samples to cut into folds; and
second-model parameters set by hand. Output: the folds, the second model's coefficients, and
the forecasts it gives.
"""

from __future__ import annotations

import math
import random
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from krx_quant.algorithms import ALGORITHMS, Sample, load_models, save_model
from krx_quant.algorithms import neyman as module
from krx_quant.algorithms.neyman import (
    EMBARGO,
    INPUTS,
    MIN_CALLS,
    MIN_DAYS,
    Neyman,
    _chance,
    _second_opinion,
    folds,
)
from krx_quant.algorithms.turing import BOOSTING, Turing
from krx_quant.features import BARRIER_FEATURES, FeatureVector
from krx_quant.feeds import KST
from krx_quant.feeds.base import kst_day
from krx_quant.pricing import Direction, most_likely

NEYMAN = Neyman()
START = date(2026, 9, 1)


def features(**values: float | None) -> FeatureVector:
    return FeatureVector(dict.fromkeys(NEYMAN.features) | values, NEYMAN.features)


def made_up(n: int, per_day: int = 300, seed: int = 0, *, flat: bool = False) -> list[Sample]:
    """Programs push the price their way, three times as far in a volatile market (a barrier 3
    ticks wide) as in a quiet one (1 tick): a move beyond a tick is up or down. ``flat``: every
    outcome flat."""
    rng = random.Random(seed)
    out = []
    start = datetime.combine(START, time(9, 1), KST)
    for i in range(n):
        program = rng.uniform(-1, 1)
        width = rng.choice((1.0, 3.0))
        move = 1.2 * program * width + rng.gauss(0, 0.3)
        outcome = Direction.UP if move > 1 else Direction.DOWN if move < -1 else Direction.FLAT
        t = start + timedelta(days=i // per_day, minutes=i % per_day)
        f = features(program_net=program, flat_ticks=1.0, barrier_width=width, barrier_sigma=1.0)
        out.append(Sample("005930", t, f, Direction.FLAT if flat else outcome, move))
    return out


def params(m: float, *, calls: float = 500.0) -> dict[str, float]:
    """Second-model parameters giving every call the chance ``m``."""
    out = {f"meta.{part}.{name}": 0.0 for name in INPUTS for part in ("coef", "mean")}
    out |= {f"meta.scale.{name}": 1.0 for name in INPUTS}
    return out | {
        "meta.intercept": math.log(m / (1 - m)),
        "meta.calls": calls,
        "move.up": 2.0,
        "move.flat": 0.0,
        "move.down": -2.0,
    }


def test_it_comes_after_turing_with_the_barrier_s_features_its_own() -> None:
    assert [a.name for a in ALGORITHMS][-2:] == ["turing", "neyman"]
    assert set(NEYMAN.card.unique_features) == BARRIER_FEATURES
    assert not BARRIER_FEATURES & set(Turing.features)  # no direction algorithm before it reads them
    assert set(NEYMAN.features) == set(Turing.features) | BARRIER_FEATURES
    # Not Turing's trees (the #266 review: a clone is refused): another window and seed.
    assert NEYMAN.training_days == 20 != Turing.training_days
    assert module.FIRST["seed"] != BOOSTING["seed"]


def test_the_call_s_probability_becomes_the_chance_it_is_right() -> None:
    # down, flat, up as the trees give them: an up call, right a quarter of the time. The rest
    # (0.75) goes to flat and down as 0.3 to 0.1.
    called = _second_opinion([0.1, 0.3, 0.6], features(), params(0.25))
    assert [called.p_up, called.p_flat, called.p_down] == pytest.approx([0.25, 0.5625, 0.1875])
    assert called.expected_ticks == pytest.approx(0.25 * 2.0 - 0.1875 * 2.0)
    assert most_likely(called.p_up, called.p_flat, called.p_down) is Direction.FLAT
    # No threshold: a call right 60% of the time stays a call; a down call likewise.
    sure = _second_opinion([0.1, 0.3, 0.6], features(), params(0.6))
    assert [sure.p_up, sure.p_flat, sure.p_down] == pytest.approx([0.6, 0.3, 0.1])
    down = _second_opinion([0.55, 0.35, 0.10], features(), params(0.45))
    assert [down.p_down, down.p_flat, down.p_up] == pytest.approx(
        [0.45, 0.55 * 0.35 / 0.45, 0.55 * 0.10 / 0.45]
    )
    # Flat the most likely, or no second model: as the trees gave it.
    flat = _second_opinion([0.2, 0.5, 0.3], features(), params(0.25))
    assert [flat.p_down, flat.p_flat, flat.p_up] == pytest.approx([0.2, 0.5, 0.3])
    none = _second_opinion([0.1, 0.3, 0.6], features(), params(0.25, calls=0.0))
    assert [none.p_down, none.p_flat, none.p_up] == pytest.approx([0.1, 0.3, 0.6])


def test_an_unknown_input_counts_at_its_training_mean() -> None:
    p = params(0.5) | {"meta.coef.spread_ticks": 2.0, "meta.mean.spread_ticks": 1.5}
    at_mean = [0.0] * len(INPUTS)
    at_mean[INPUTS.index("spread_ticks")] = 1.5
    unknown = [0.0] * len(INPUTS)
    unknown[INPUTS.index("spread_ticks")] = math.nan
    assert _chance(p, unknown) == pytest.approx(_chance(p, at_mean)) == pytest.approx(0.5)


def test_it_doubts_up_and_down_calls_in_a_quiet_market() -> None:
    model = NEYMAN.fit(made_up(3000))  # ten days: four runs of out-of-sample calls

    assert model.version.startswith("neyman-2026") and model.blob
    p = model.params
    assert p["meta.calls"] >= MIN_CALLS and p["meta.days"] >= MIN_DAYS
    # The width tells right calls from wrong ones; the second model learned so.
    assert p["meta.coef.barrier_width"] > 0
    volatile = NEYMAN.predict(
        features(program_net=0.8, flat_ticks=1.0, barrier_width=3.0, barrier_sigma=1.0), model
    )
    quiet = NEYMAN.predict(
        features(program_net=0.8, flat_ticks=1.0, barrier_width=1.0, barrier_sigma=1.0), model
    )
    assert most_likely(volatile.p_up, volatile.p_flat, volatile.p_down) is Direction.UP
    assert quiet.p_up < volatile.p_up
    for f in (volatile, quiet):
        assert f.p_up + f.p_flat + f.p_down == pytest.approx(1.0)
    # The same samples, the same trees and the same second model.
    assert NEYMAN.fit(made_up(3000)).version == model.version


def test_the_folds_hold_recorded_and_backfilled_runs_and_never_train_on_a_held_day() -> None:
    # Eight backfilled days, then eight recorded ones: each fold holds two of each.
    samples = [
        Sample(
            "005930",
            datetime.combine(START + timedelta(days=d), time(9, 1), KST),
            features(),
            Direction.FLAT,
            0.0,
        )
        for d in range(16)
    ]
    samples = [s if i < 8 else replace(s, reference="mid") for i, s in enumerate(samples)]
    found = folds(samples)
    assert len(found) == 4
    day = [kst_day(s.bar_close) for s in samples]
    for held, trained in found:
        held_days = {day[i] for i in held}
        assert sum(1 for i in held if samples[i].reference == "mid") == 2
        assert len(held_days) == 4
        assert not held_days & {day[i] for i in trained}
        # The embargo: the day after each held day is not trained on by its fold.
        after = {d + timedelta(days=EMBARGO) for d in held_days} - held_days
        assert not after & {day[i] for i in trained}
    assert folds(samples[:1]) == []  # one day: nothing out of sample


def test_with_few_out_of_sample_days_the_second_model_is_the_share_right() -> None:
    model = NEYMAN.fit(made_up(1500))  # five days: fewer than MIN_DAYS
    p = model.params
    assert 0 < p["meta.days"] < MIN_DAYS
    assert all(p[f"meta.coef.{name}"] == 0.0 for name in INPUTS)
    assert 1 / (1 + math.exp(-p["meta.intercept"])) == pytest.approx(p["meta.precision"], abs=1e-6)


def test_without_an_out_of_sample_call_there_is_no_second_model() -> None:
    assert NEYMAN.fit(made_up(300)).params["meta.calls"] == 0.0  # one day
    always_flat = NEYMAN.fit(made_up(1500, flat=True))  # five days, nothing but flat to call
    assert always_flat.params["meta.calls"] == 0.0
    f = NEYMAN.predict(features(program_net=0.8, barrier_width=3.0), always_flat)
    assert f.p_up + f.p_flat + f.p_down == pytest.approx(1.0)


def test_a_saved_model_forecasts_the_same(tmp_path: Path) -> None:
    model = NEYMAN.fit(made_up(3000))
    x = features(program_net=0.8, flat_ticks=1.0, barrier_width=3.0, barrier_sigma=1.0)
    before = NEYMAN.predict(x, model)
    save_model(tmp_path, NEYMAN.name, model)
    module._TREES.clear()  # read the trees from the saved text again
    loaded = load_models(tmp_path, [NEYMAN])[NEYMAN.name]
    assert NEYMAN.predict(x, loaded) == before


def test_before_training_it_has_no_view() -> None:
    f = NEYMAN.predict(features(program_net=0.8), NEYMAN.default_model())
    assert [f.p_up, f.p_flat, f.p_down] == pytest.approx([1 / 3, 1 / 3, 1 / 3])
