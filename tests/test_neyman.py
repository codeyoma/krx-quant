"""Neyman (#266): Turing's trees and a second opinion on their up and down calls.

Input: made-up samples in which programs buying (selling) push the price up (down), by more
when the market is volatile (a wide barrier), so the first model's up and down calls are right
in a volatile market and wrong in a quiet one; and second-model parameters set by hand. Output:
the second model's coefficients, and the forecasts it gives.
"""

from __future__ import annotations

import math
import random
from datetime import date, datetime, time, timedelta

import pytest

from krx_quant.algorithms import ALGORITHMS, Sample
from krx_quant.algorithms.neyman import INPUTS, MIN_CALLS, Neyman, _second_opinion
from krx_quant.algorithms.turing import Turing
from krx_quant.features import BARRIER_FEATURES, FeatureVector
from krx_quant.feeds import KST
from krx_quant.pricing import Direction, most_likely

NEYMAN = Neyman()


def features(**values: float | None) -> FeatureVector:
    return FeatureVector(dict.fromkeys(NEYMAN.features) | values, NEYMAN.features)


def made_up(n: int, per_day: int = 300, seed: int = 0) -> list[Sample]:
    """Programs push the price their way, three times as far in a volatile market (a barrier 3
    ticks wide) as in a quiet one (1 tick): a move beyond a tick is up or down."""
    rng = random.Random(seed)
    out = []
    start = datetime.combine(date(2026, 9, 1), time(9, 1), KST)
    for i in range(n):
        program = rng.uniform(-1, 1)
        width = rng.choice((1.0, 3.0))
        move = 1.2 * program * width + rng.gauss(0, 0.3)
        outcome = Direction.UP if move > 1 else Direction.DOWN if move < -1 else Direction.FLAT
        t = start + timedelta(days=i // per_day, minutes=i % per_day)
        f = features(program_net=program, flat_ticks=1.0, barrier_width=width, barrier_sigma=width / 2)
        out.append(Sample("005930", t, f, outcome, move))
    return out


def test_it_comes_after_turing_with_the_barrier_s_features_its_own() -> None:
    assert [a.name for a in ALGORITHMS][-2:] == ["turing", "neyman"]
    assert set(NEYMAN.card.unique_features) == BARRIER_FEATURES
    assert not BARRIER_FEATURES & set(Turing.features)  # no direction algorithm before it reads them
    assert set(Turing.features) < set(NEYMAN.features)


def test_it_doubts_up_and_down_calls_in_a_quiet_market() -> None:
    model = NEYMAN.fit(made_up(3000))  # ten days: four runs of out-of-sample calls

    assert model.version.startswith("neyman-2026") and model.blob
    params = model.params
    assert params["meta.calls"] >= MIN_CALLS
    # The width tells right calls from wrong ones; the second model learned so.
    assert params["meta.coef.barrier_width"] > 0
    volatile = NEYMAN.predict(
        features(program_net=0.8, flat_ticks=1.0, barrier_width=3.0, barrier_sigma=1.5), model
    )
    quiet = NEYMAN.predict(
        features(program_net=0.8, flat_ticks=1.0, barrier_width=1.0, barrier_sigma=0.5), model
    )
    assert most_likely(volatile.p_up, volatile.p_flat, volatile.p_down) is Direction.UP
    assert quiet.p_up < volatile.p_up
    for f in (volatile, quiet):
        assert f.p_up + f.p_flat + f.p_down == pytest.approx(1.0)
    # The same samples, the same trees and the same second model.
    assert NEYMAN.fit(made_up(3000)).version == model.version


def test_up_and_down_are_multiplied_by_the_chance_the_rest_goes_to_flat() -> None:
    params = {f"meta.{part}.{name}": 0.0 for name in INPUTS for part in ("coef", "mean")}
    params |= {f"meta.scale.{name}": 1.0 for name in INPUTS}
    params |= {"meta.intercept": math.log(0.25 / 0.75), "move.up": 2.0, "move.flat": 0.0, "move.down": -2.0}
    # down, flat, up as the trees give them: an up call, right a quarter of the time.
    called = _second_opinion([0.1, 0.3, 0.6], features(), params)
    assert [called.p_up, called.p_down] == pytest.approx([0.15, 0.025])
    assert called.p_flat == pytest.approx(0.825)
    assert called.expected_ticks == pytest.approx(0.15 * 2.0 - 0.025 * 2.0)
    # Flat the most likely: as the trees gave it.
    flat = _second_opinion([0.2, 0.5, 0.3], features(), params)
    assert [flat.p_down, flat.p_flat, flat.p_up] == pytest.approx([0.2, 0.5, 0.3])


def test_with_few_out_of_sample_calls_the_second_model_is_the_share_right() -> None:
    model = NEYMAN.fit(made_up(240, per_day=60))  # four days of 60: too few calls to learn from
    params = model.params
    assert 0 < params["meta.calls"] < MIN_CALLS
    assert all(params[f"meta.coef.{name}"] == 0.0 for name in INPUTS)
    assert 1 / (1 + math.exp(-params["meta.intercept"])) == pytest.approx(params["meta.precision"], abs=1e-6)


def test_on_a_single_day_it_says_nothing_of_its_calls() -> None:
    model = NEYMAN.fit(made_up(300))  # one day: no out-of-sample call
    assert model.params["meta.calls"] == 0.0
    assert 1 / (1 + math.exp(-model.params["meta.intercept"])) == pytest.approx(1.0, abs=1e-5)


def test_before_training_it_has_no_view() -> None:
    f = NEYMAN.predict(features(program_net=0.8), NEYMAN.default_model())
    assert [f.p_up, f.p_flat, f.p_down] == pytest.approx([1 / 3, 1 / 3, 1 / 3])
