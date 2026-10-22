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

import numpy as np
import pytest

from krx_quant import neyman_diagnostics as diagnostics
from krx_quant.algorithms import ALGORITHMS, Model, Sample, load_models, save_model
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


# The pre-validation's diagnostics (#283): reported, never judged.


def test_a_doubted_call_can_flip_to_the_opposite_side() -> None:
    # down, flat, up: an up call, flat the least likely. Right 27% of the time, its 0.73 left
    # goes mostly to down (0.35 to flat's 0.05): down becomes the most likely.
    flipped = _second_opinion([0.35, 0.05, 0.60], features(), params(0.27))
    assert [flipped.p_up, flipped.p_flat, flipped.p_down] == pytest.approx(
        [0.27, 0.73 * 0.05 / 0.40, 0.73 * 0.35 / 0.40]
    )
    assert flipped.p_down == pytest.approx(0.64, abs=0.005)
    assert most_likely(flipped.p_up, flipped.p_flat, flipped.p_down) is Direction.DOWN


def test_an_up_and_down_tie_is_judged_as_an_up_call() -> None:
    tied = [0.4, 0.2, 0.4]  # down, flat, up: the rule calls up (a tie goes to flat, then up)
    assert most_likely(0.4, 0.2, 0.4) is Direction.UP
    doubted = _second_opinion(tied, features(), params(0.3))  # up gives way, mostly to down
    assert [doubted.p_up, doubted.p_flat, doubted.p_down] == pytest.approx([0.3, 0.7 / 3, 1.4 / 3])
    assert most_likely(doubted.p_up, doubted.p_flat, doubted.p_down) is Direction.DOWN
    trusted = _second_opinion(tied, features(), params(0.5))
    assert most_likely(trusted.p_up, trusted.p_flat, trusted.p_down) is Direction.UP


class _Trees:
    """Stands in for a model's trees: the first model's probabilities (down, flat, up) of each
    bar close, in the order asked."""

    def __init__(self, rows: list[list[float]]) -> None:
        self.rows = rows

    def feature_name(self) -> list[str]:
        return list(module.FEATURES)

    def predict(self, x: object, num_threads: int) -> list[list[float]]:
        return self.rows


def _on_day(day: int, minutes: int, outcome: Direction) -> Sample:
    t = datetime.combine(START + timedelta(days=day), time(9, 1), KST) + timedelta(minutes=minutes)
    return Sample("005930", t, features(), outcome, 0.0)


def _measured_days(
    monkeypatch: pytest.MonkeyPatch, days: list[tuple[dict[str, float], list[tuple[list[float], Direction]]]]
) -> dict[str, object]:
    """``days``: each day's second-model parameters, and its bar closes' first-model probabilities
    and answers; the flips the diagnostics count over them."""
    measures = NEYMAN.diagnostics()
    for i, (p, closes) in enumerate(days):
        monkeypatch.setattr(diagnostics, "_trees", lambda model, rows=[r for r, _ in closes]: _Trees(rows))
        samples = [_on_day(i, j, outcome) for j, (_, outcome) in enumerate(closes)]
        measures.day(START + timedelta(days=i), Model(f"neyman-day{i}", p, blob="trees"), samples)
    flips: dict[str, object] = measures.report()["flips"]["by_symbol"]["005930"]
    return flips


def _up_doubted_down_trusted() -> dict[str, float]:
    """Second-model parameters giving an up call the chance 0.27 and a down call 0.8."""
    p = params(0.8)
    return p | {"meta.coef.up": math.log(0.27 / 0.73) - math.log(0.8 / 0.2)}


def test_the_diagnostics_count_flips_and_their_precision_against_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flips = _measured_days(
        monkeypatch,
        [
            (
                _up_doubted_down_trusted(),
                [
                    ([0.35, 0.05, 0.60], Direction.DOWN),  # up, flipped to down: right
                    ([0.60, 0.05, 0.35], Direction.DOWN),  # down, kept: right
                    ([0.10, 0.30, 0.60], Direction.UP),  # up, turned to flat
                    ([0.20, 0.60, 0.20], Direction.FLAT),  # flat: not a call
                    ([0.30, 0.05, 0.65], Direction.UP),  # up, flipped to down: wrong, the up was right
                ],
            ),
            (params(0.6), [([0.10, 0.30, 0.60], Direction.UP), ([0.60, 0.30, 0.10], Direction.UP)]),
        ],
    )

    assert flips == {
        "scored": 7,
        "calls": 6,
        "kept": 3,
        "kept_precision": pytest.approx(2 / 3, abs=1e-4),
        "to_flat": 1,
        "flipped": 2,
        "flipped_precision": 0.5,
        "flipped_first_side_right": 0.5,
        "flip_days": 1,
        "kept_precision_on_flip_days": 1.0,  # the first day's down call, kept and right
        "ties": 0,
        "ties_up": 0,
        "ties_flat": 0,
        "ties_down": 0,
    }


def test_the_diagnostics_count_up_and_down_ties_and_where_the_rule_sent_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flips = _measured_days(
        monkeypatch,
        [
            (_up_doubted_down_trusted(), [([0.4, 0.2, 0.4], Direction.UP)]),  # called up, flipped to down
            (params(0.6), [([0.4, 0.2, 0.4], Direction.FLAT)]),  # called up, kept
            (params(0.6), [([0.3, 0.4, 0.3], Direction.FLAT)]),  # flat above the tie: no call
        ],
    )

    assert (flips["ties"], flips["ties_up"], flips["ties_flat"], flips["ties_down"]) == (2, 1, 0, 1)
    assert (flips["calls"], flips["kept"], flips["flipped"]) == (2, 1, 1)
    assert flips["flipped_first_side_right"] == 1.0  # the answer was up


def test_the_diagnostic_fit_is_neyman_s_and_compares_its_first_model_in_and_out_of_sample() -> None:
    measures = NEYMAN.diagnostics()
    model = measures.fit(made_up(3000))  # ten days
    assert model.version == NEYMAN.fit(made_up(3000)).version, "the same model as its fit"

    # The next day, as the pre-validation gives it: the samples of a day the model never saw.
    later = [replace(s, bar_close=s.bar_close + timedelta(days=10)) for s in made_up(300, seed=1)]
    measures.day(START + timedelta(days=10), model, later)

    first = measures.report()["first_model"]
    assert (first["fits"], first["days"]) == (1, 1)
    got = first["by_symbol"]["005930"]
    assert got["n"] == {"out_of_fold": 3000, "in_sample": 3000, "live": 300}
    for measure in ("up_minus_down", "largest"):
        m = got[measure]
        assert all(m[s]["p05"] <= m[s]["p50"] <= m[s]["p95"] for s in ("out_of_fold", "in_sample", "live"))
        assert 0 < m["ks_in_sample"] <= 1 and 0 <= m["ks_live"] <= 1
        assert m["mean_abs_diff_in_sample"] > 0
    # Of the fit's own first-model probabilities: the fold models' and the final one's.
    found = module.fitted(made_up(3000))
    out_of_fold, in_sample = (p / p.sum(axis=1, keepdims=True) for p in (found.out_of_fold, found.in_sample))
    for source, p in (("out_of_fold", out_of_fold), ("in_sample", in_sample)):
        assert got["largest"][source]["p50"] == pytest.approx(np.median(p.max(axis=1)), abs=0.005)
        assert got["up_minus_down"][source]["p50"] == pytest.approx(np.median(p[:, 2] - p[:, 0]), abs=0.005)
    gap = np.mean(np.abs(out_of_fold.max(axis=1) - in_sample.max(axis=1)))
    assert got["largest"]["mean_abs_diff_in_sample"] == pytest.approx(gap, abs=1e-4)
    assert first["all"] == got  # one symbol
    lines = measures.lines()
    assert lines[0].startswith("neyman diagnostics (#283; reported, not judged): 1 replayed days")
    assert any(
        line.startswith("  first model 005930, p_up - p_down p05/p50/p95: out of fold ") for line in lines
    )


def test_the_distributions_quantiles_and_distances_come_from_their_bins() -> None:
    a = diagnostics._Histogram(diagnostics.SPREAD_EDGES)
    a.add(np.linspace(-0.5, 0.5, 1001))
    assert a.quantile(0.5) == pytest.approx(0.0, abs=0.005)
    assert a.quantile(0.05) == pytest.approx(-0.45, abs=0.005)
    same = diagnostics._Histogram(diagnostics.SPREAD_EDGES)
    same.add(np.linspace(-0.5, 0.5, 1001))
    apart = diagnostics._Histogram(diagnostics.SPREAD_EDGES)
    apart.add(np.full(10, 0.9))
    empty = diagnostics._Histogram(diagnostics.SPREAD_EDGES)
    assert diagnostics.ks(a, same) == 0.0
    assert diagnostics.ks(a, apart) == 1.0
    assert diagnostics.ks(a, empty) is None and empty.quantile(0.5) is None
