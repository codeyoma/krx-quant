"""Neyman's variants (#455): neyman as it is (A), its flips swapped back (B), without its two
longer slot returns (C), and C with the cost's distance in volatilities as a second-model input (D);
a discovery that fixes the window's N, a fixed window after 2026-11-02, and a verdict judged once.

Input: made-up samples in which programs push the price (``test_neyman.made_up``); second-model
parameters and first-model probabilities set by hand; hand-made scored records; made-up days served
by a stand-in walk-forward; and recorded mornings of two symbols, one of them cut short. Output: the
arms' models and forecasts, N, the window, the verdict, the report and lines, the job's condition,
and neyman's code digest (unchanged).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pytest

from krx_quant import neyman_variants as module
from krx_quant.algorithms import Model, Sample
from krx_quant.algorithms.forecasts import UNKNOWN, class_moves
from krx_quant.algorithms.neyman import (
    FEATURES,
    INPUTS,
    Neyman,
    _inputs,
    _second_model,
    _second_opinion,
    fitted,
    folds,
)
from krx_quant.baselines import Forecast
from krx_quant.cli import main
from krx_quant.climate import fit_climate
from krx_quant.config import Settings
from krx_quant.engine import Event
from krx_quant.feeds import KST
from krx_quant.feeds.base import kst_day
from krx_quant.neyman_variants import (
    D_INPUTS,
    DROPPED,
    LEVEL,
    WITHOUT,
    Z_LEVEL,
    Z_POWER,
    A,
    B,
    C,
    Collected,
    D,
    Progress,
    R,
    Verdict,
    chance,
    cost_distance,
    d_inputs,
    evaluate,
    fit_arms,
    forecasts,
    neyman_variants,
    opinion,
    progress,
    required_days,
    second_model,
    spread,
    swapped,
)
from krx_quant.prevalidation import code_digest
from krx_quant.pricing import Direction, most_likely
from krx_quant.scheduled import FAILED, JOBS, NEYMAN_VARIANTS, Facts, JobRecord, Night
from krx_quant.stats import Scored
from tests.prevalidation_doubles import AFTER, DAYS
from tests.replay_doubles import record
from tests.replay_events import at, book, trade
from tests.test_neyman import features, made_up, params

UP, FLAT, DOWN = Direction.UP, Direction.FLAT, Direction.DOWN
NEYMAN = Neyman()
SYMBOLS = ("005930", "000660")


def split_days(samples: list[Sample]) -> dict[date, list[Sample]]:
    out: dict[date, list[Sample]] = {}
    for s in samples:
        out.setdefault(kst_day(s.bar_close), []).append(s)
    return out


def test_neyman_s_package_is_as_its_pre_validation_passed_it() -> None:
    # Neyman's code digest when its pre-validation passed (2026-11-01, #204): on probation since
    # 2026-11-02, it is not changed by this experiment (#455), which lives outside its package.
    assert code_digest(NEYMAN) == "650316eaa37bfbe7"
    assert Path(module.__file__).parent.name == "krx_quant", "outside neyman's package"


def test_a_is_neyman_its_model_and_its_forecasts() -> None:
    days = split_days(made_up(3000))  # ten days of 300 bar closes
    *before, last = sorted(days)
    window = [s for d in before for s in days[d]]
    fit, neyman = fit_arms(window), fitted(window)
    assert fit is not None

    # The same trees, the same out-of-sample probabilities and the same parameters as neyman's fit.
    assert fit.first.final.model_to_string() == neyman.model.blob
    np.testing.assert_array_equal(fit.first.out_of_fold, neyman.out_of_fold)
    assert fit.params[A] == neyman.model.params
    assert len(fit.first.folds) == len(folds(neyman.scored)) == 4, "the fold models kept"
    # And neyman's forecasts of the next day.
    made = forecasts(fit, days[last])
    assert made.by_arm[A] == [NEYMAN.predict(s.features, neyman.model) for s in days[last]]
    # Untrained: a third each, as neyman says before its first training.
    assert fit_arms([replace(s, outcome=None) for s in window]) is None
    assert forecasts(None, days[last][:3]).by_arm == {arm: [UNKNOWN] * 3 for arm in (A, B, C, D)}


def test_b_swaps_back_only_a_flip_and_takes_the_expected_ticks_again() -> None:
    p = params(0.27)  # every call right 27% of the time; moves +2, 0, −2 ticks
    # An up call (down 0.35, flat 0.05, up 0.60): at m = 0.27 the rest goes to down, which wins.
    flipped = [0.35, 0.05, 0.60]
    a = _second_opinion(flipped, features(), p)
    assert most_likely(a.p_up, a.p_flat, a.p_down) is DOWN
    b = swapped(flipped, a, p)
    assert (b.p_up, b.p_flat, b.p_down) == (a.p_down, a.p_flat, a.p_up), "p_up and p_down swapped"
    assert most_likely(b.p_up, b.p_flat, b.p_down) is UP, "the first model's side"
    assert b.expected_ticks == pytest.approx(2.0 * b.p_up - 2.0 * b.p_down) != pytest.approx(a.expected_ticks)
    # The same down call the other way round.
    down = _second_opinion([0.60, 0.05, 0.35], features(), p)
    assert most_likely(down.p_up, down.p_flat, down.p_down) is UP
    assert swapped([0.60, 0.05, 0.35], down, p).p_down == down.p_up
    # A call turned to flat, a call kept and a flat call stay as they are.
    to_flat = [0.1, 0.3, 0.6]
    assert swapped(to_flat, _second_opinion(to_flat, features(), p), p) == _second_opinion(
        to_flat, features(), p
    )
    kept = _second_opinion(to_flat, features(), params(0.6))
    assert most_likely(kept.p_up, kept.p_flat, kept.p_down) is UP
    assert swapped(to_flat, kept, params(0.6)) is kept
    flat = [0.2, 0.5, 0.3]
    assert swapped(flat, _second_opinion(flat, features(), p), p) == _second_opinion(flat, features(), p)
    # In the arms: B differs from A exactly where A's call is the opposite of the first model's. Days
    # that never stay flat, and a second model that doubts every call (right 5% of the time): the
    # opposite side takes most of what the call gives up.
    days = split_days([replace(x, outcome=UP if (x.change_ticks or 0) > 0 else DOWN) for x in made_up(3000)])
    *before, last = sorted(days)
    fit = fit_arms([s for d in before for s in days[d]])
    assert fit is not None
    doubting = (
        fit.params[A] | {"meta.intercept": math.log(0.05 / 0.95)} | {f"meta.coef.{n}": 0.0 for n in INPUTS}
    )
    fit = replace(fit, params=fit.params | {A: doubting})
    made = forecasts(fit, days[last])
    assert made.first is not None
    flips = 0
    for first, a_f, b_f in zip(made.first, made.by_arm[A], made.by_arm[B], strict=True):
        p_down, p_flat, p_up = first / first.sum()
        call = most_likely(p_up, p_flat, p_down)
        final = most_likely(a_f.p_up, a_f.p_flat, a_f.p_down)
        flip = call is not FLAT and final not in (FLAT, call)
        flips += flip
        assert (b_f != a_f) is flip
        assert b_f.p_flat == a_f.p_flat
        if flip:
            assert most_likely(b_f.p_up, b_f.p_flat, b_f.p_down) is call
    assert flips > 0


def test_c_drops_the_two_slot_returns_from_the_fold_models_and_the_final_one() -> None:
    days = split_days(made_up(3000))
    window = [s for d in sorted(days)[:-1] for s in days[d]]
    fit = fit_arms(window)
    assert fit is not None

    assert set(DROPPED) == {"slot_return_5d", "slot_return_20d"} and set(DROPPED) <= set(FEATURES)
    assert WITHOUT == tuple(n for n in FEATURES if n not in DROPPED)
    for model in (fit.without.final, *fit.without.folds):
        assert model.feature_name() == list(WITHOUT)
    for model in (fit.first.final, *fit.first.folds):
        assert model.feature_name() == list(FEATURES)
    assert len(fit.without.folds) == len(fit.first.folds) == 4
    # Its second model learns from its own fold models' calls, on neyman's inputs.
    scored = [s for s in window if s.outcome is not None]
    moves = {"rounds": 200.0} | class_moves(scored)
    assert fit.params[C] == moves | _second_model(scored, fit.without.out_of_fold)
    assert not any(k.startswith(f"meta.coef.{R}") for k in fit.params[C])


def test_d_reads_the_cost_s_distance_in_volatilities() -> None:
    # w 4 ticks from σ̂₄ 3.2 ticks (barrier_sigma 1.25), k 2 ticks, a one-tick spread: r = 2.5 / 3.2.
    known: dict[str, float | None] = {
        "flat_ticks": 2.0,
        "barrier_width": 4.0,
        "barrier_sigma": 1.25,
        "spread_ticks": 1.0,
    }
    assert cost_distance(features(**known)) == pytest.approx((2 + 0.5) * 1.25 / 4) == pytest.approx(2.5 / 3.2)
    assert cost_distance(features(**known | {"spread_ticks": 3.0})) == pytest.approx(3.5 / 3.2)
    # No book: half a one-tick spread.
    assert cost_distance(features(**known | {"spread_ticks": None})) == pytest.approx(2.5 / 3.2)
    # No σ̂₄ (too few bars), or σ̂₄ 0: barrier_sigma is unknown, and so is r; and without k.
    assert math.isnan(cost_distance(features(**known | {"barrier_sigma": None})))
    assert math.isnan(cost_distance(features(**known | {"flat_ticks": None})))
    f = features(**known)
    assert d_inputs(0.6, 0.3, 0.1, UP, f) == [*_inputs(0.6, 0.3, 0.1, UP, f), cost_distance(f)]
    # An unknown r counts at its training mean, as neyman's other inputs.
    p = params(0.5) | {f"meta.coef.{R}": 2.0, f"meta.mean.{R}": 0.7, f"meta.scale.{R}": 1.0}
    at_mean = [0.0] * len(INPUTS) + [0.7]
    unknown = [0.0] * len(INPUTS) + [math.nan]
    assert chance(p, unknown, D_INPUTS) == pytest.approx(chance(p, at_mean, D_INPUTS)) == pytest.approx(0.5)
    assert chance(p, [0.0] * len(INPUTS) + [1.7], D_INPUTS) > 0.5
    # The second model on neyman's inputs is neyman's; D's adds r.
    days = split_days(made_up(3000))
    window = [s for d in sorted(days)[:-1] for s in days[d]]
    fit = fit_arms(window)
    assert fit is not None
    scored = [s for s in window if s.outcome is not None]
    assert second_model(scored, fit.without.out_of_fold) == _second_model(scored, fit.without.out_of_fold)
    assert set(fit.params[D]) == set(fit.params[C]) | {
        f"meta.{part}.{R}" for part in ("coef", "mean", "scale")
    }
    last = days[max(days)][0]
    assert opinion([0.1, 0.3, 0.6], last.features, fit.params[C]) == _second_opinion(
        [0.1, 0.3, 0.6], last.features, fit.params[C]
    )


def scored(arm: str, day: date, minute: int, called: Direction, outcome: Direction) -> Scored:
    t = datetime.combine(day, time(9, minute), KST)
    f = Forecast.certain(called)
    return Scored(arm, f"{arm}-v", "005930", t, f.p_up, f.p_flat, f.p_down, outcome)


def test_n_is_the_days_a_lift_of_three_tenths_of_a_point_needs() -> None:
    # #455's z: two-sided 98.333% (Bonferroni over three), and 80% power.
    assert Z_LEVEL == pytest.approx(NormalDist().inv_cdf(1 - 0.05 / 6), abs=5e-4)
    assert Z_POWER == pytest.approx(NormalDist().inv_cdf(0.8), abs=5e-4)
    assert LEVEL == pytest.approx(1 - 0.05 / 3)
    # ((2.394 + 0.842) × s_d ÷ 0.003)² is 100 at s_d = 0.3 ÷ 3.236 points: 100 days, not 101.
    assert required_days(10 * 0.003 / 3.236) == 100
    assert required_days(0.001) == 30, "never fewer than 30"
    assert required_days(0.02) == math.ceil((3.236 * 0.02 / 0.003) ** 2) == 466
    # The days' mean gains of B over A: 0, 1/4 and 1/2 (four bar closes a day).
    days = [date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14)]
    outcomes = [UP, FLAT, FLAT, DOWN]
    a_calls = [FLAT, FLAT, FLAT, FLAT]  # right on the two flat ones
    b_calls = {days[0]: a_calls, days[1]: [UP, FLAT, FLAT, FLAT], days[2]: outcomes}
    records_a = [
        scored(A, d, i + 1, c, o)
        for d in days
        for i, (c, o) in enumerate(zip(a_calls, outcomes, strict=True))
    ]
    records_b = [
        scored(B, d, i + 1, c, o)
        for d in days
        for i, (c, o) in enumerate(zip(b_calls[d], outcomes, strict=True))
    ]
    found = spread(records_b, records_a)
    assert found.days == 3 and found.mean == pytest.approx(0.25) and found.sd == pytest.approx(0.25)
    assert found.n == math.ceil((3.236 * 0.25 / 0.003) ** 2)
    assert spread(records_b[:4], records_a[:4]).n is None, "one day: no spread"


class Stand:
    """A walk-forward of made-up days: each day's samples, its 20 days before, the climate on them."""

    def __init__(self, days: dict[date, list[Sample]]) -> None:
        self.days = days
        self.fitted: list[date] = []  # the days whose window was asked for

    def samples_on(self, day: date) -> list[Sample]:
        return list(self.days.get(day, []))

    def samples_before(self, day: date) -> list[Sample]:
        self.fitted.append(day)
        return [s for d in sorted(self.days) if d < day for s in self.days[d]][-20 * 300 :]

    def climate_before(self, day: date) -> Model:
        return fit_climate(self.samples_before(day))


def test_the_window_is_fixed_and_judged_once_when_it_is_full() -> None:
    days = split_days(made_up(3600))  # twelve days of 300 bar closes
    order = sorted(days)
    short = order[8]
    days[short] = days[short][:150]  # fewer than 200 scored bar closes: never in the window
    stand = Stand(days)
    now = datetime(2026, 11, 20, 23, tzinfo=KST)
    after = order[7]

    # Two days after the start, one of them short: the window has one of its two days, nothing fitted.
    waiting = evaluate(lambda: stand, order[8:10], now, n=2, after=after, min_scored=200)
    assert (waiting.full, waiting.window, waiting.short) == (False, [order[9]], [short])
    assert stand.fitted == [] and waiting.verdicts == {}
    assert waiting.as_dict()["judged"] is False and "verdict" not in waiting.as_dict()
    # A day more: full, on the first two days with enough, never the later one; judged.
    judged = evaluate(
        lambda: stand, order[8:], now, n=2, after=after, min_scored=200, known={short: False, order[9]: True}
    )
    assert (judged.full, judged.window, judged.short) == (True, [order[9], order[10]], [short])
    assert set(stand.fitted) == {order[9], order[10]}, "fitted for the window's days only"
    assert set(judged.verdicts) == {B, C, D}
    assert {s.n for s in judged.arms.values()} == {600}, "every arm on each bar close"
    saved = judged.as_dict()
    assert saved["window"] == [order[9].isoformat(), order[10].isoformat()]
    assert set(saved["verdict"]) == {B, C, D} and saved["candidates"] == judged.candidates
    assert saved["diagnosis"]["rows"] == 600 and set(saved["diagnosis"]["inputs"]) == set(D_INPUTS)
    assert set(saved["described"]["serial"]) == {B, C, D}


def test_a_variant_is_a_candidate_when_its_paired_lift_and_its_lower_bound_by_day_are_above_0() -> None:
    days = [date(2026, 11, 3) + timedelta(days=i) for i in range(5)]
    outcomes = [UP, FLAT, FLAT, DOWN]
    a = [scored(A, d, i + 1, FLAT, o) for d in days for i, o in enumerate(outcomes)]
    better = [scored(B, d, i + 1, o, o) for d in days for i, o in enumerate(outcomes)]  # always right
    worse = [scored(C, d, i + 1, UP, o) for d in days for i, o in enumerate(outcomes)]
    # Right on one more on three days of five: above 0 paired, not by trading day at 98.333%.
    mixed = [
        scored(D, d, i + 1, o if (i == 0 and n < 3) else FLAT, o)
        for n, d in enumerate(days)
        for i, o in enumerate(outcomes)
    ]
    assert Verdict.of(better, a).candidate is True
    assert Verdict.of(worse, a).candidate is False
    verdict = Verdict.of(mixed, a)
    assert verdict.pairs.value is not None and verdict.pairs.value > 0
    assert verdict.by_day is not None and verdict.by_day.low is not None and verdict.by_day.low < 0
    assert verdict.candidate is False


def mornings(day: date, symbol: str, swings: bool) -> list[Event]:
    """09:00–10:30 of ``symbol``: with ``swings``, its first half hour's mid swings six ticks up and
    down every two minutes, at its centre at each bar close, so the four minutes from a bar close
    reach both targets (k = 2 ticks): up. Otherwise, and from 09:30, it stands still: flat. A book
    with the same sizes and a trade every ten seconds."""
    events: list[Event] = []
    start = when = at("09:00:00", day)
    while when <= at("10:30:00", day):
        swing = round(6 * math.sin(2 * math.pi * (when - start).total_seconds() / 120))
        mid = 81_250 + 100 * (swing if swings and when < at("09:30:00", day) else 0)
        events += [
            book(when, mid - 50, mid + 50, symbol, bid_qty=100, ask_qty=100, deeper=(50, 50)),
            trade(when + timedelta(seconds=1), mid + 50, 3, symbol=symbol),
        ]
        when += timedelta(seconds=10)
    return events


def short_morning(day: date, symbol: str) -> list[Event]:
    """``symbol``'s 09:00–09:15 only: a day short of scored bar closes."""
    return [e for e in mornings(day, symbol, False) if e[1]["received_at"] < at("09:15:00", day)]


def recorded(tmp_path: Path, days: list[date], short: date | None = None) -> Settings:
    """Recorded mornings of two symbols (one swinging at the open, one still), ``short`` cut at 09:15."""
    data, state = tmp_path / "data", tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    for day in days:
        events = (
            [*short_morning(day, SYMBOLS[0]), *short_morning(day, SYMBOLS[1])]
            if day == short
            else [*mornings(day, SYMBOLS[0], True), *mornings(day, SYMBOLS[1], False)]
        )
        record(data, sorted(events, key=lambda e: e[1]["received_at"]), day=day)
    return Settings(
        kis_app_key="",
        kis_app_secret="",
        symbols=SYMBOLS,
        etf_symbols=(),
        index_codes=(),
        us_symbols=(),
        data_dir=data,
        state_dir=state,
    )


def morning_after(day: date) -> datetime:
    return datetime.combine(day + timedelta(days=1), time(8), KST)


# Six recorded mornings of two symbols made into samples by the Market View, some twice (a day's
# count, then its window), with ten tree models a day: past the 30 s default on a loaded CI runner.
@pytest.mark.timeout(240)
def test_the_discovery_fixes_n_and_the_evaluation_judges_the_window_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    days = DAYS[:6]
    s = recorded(tmp_path, days, short=days[3])
    caplog.set_level("INFO", logger="krx_quant.neyman_variants")
    rules = {
        "period": (days[0], days[2]),
        "after": days[2],
        "min_scored": 50,
        "least": 2,
        "delta": 10.0,  # a lift of ten points: N is the least, 2
        "live_delta": 10.0,  # and the live plan's (#532) the least too
        "algorithms": [NEYMAN],
        "kind_algorithms": [],
        "kinds": [],
    }
    report = s.state_dir / "experiments" / "neyman-variants-283.json"

    # The discovery, on the period's three days; N 2.
    first = neyman_variants(s, now=morning_after(days[3]), **rules)  # type: ignore[arg-type]
    assert first.stage == module.DISCOVERED and first.discovery is not None
    found = first.discovery
    assert found.days == days[:3] and found.short == [] and found.n == 2
    assert [f.composition for f in found.folds] == ["-", "1+0", "2+0"], "the days before each, recorded"
    assert set(found.spreads) == {B, C, D} and all(sp.days == 3 for sp in found.spreads.values())
    saved = json.loads(report.read_text())
    assert saved["discovery"]["n"] == 2 and saved["discovery"]["after"] == days[2].isoformat()
    assert saved["discovery"]["rule"]["z_level"] == 2.394 and saved.get("evaluation") is None
    # The live plan (#532) with it: D - A alone at 95%, N from the discovery's s_d of D - A.
    assert (saved["plan"]["judged"], saved["plan"]["level"], saved["plan"]["n"]) == (["D"], 0.95, 2)
    assert saved["plan"]["sd"] == saved["discovery"]["spreads"]["D"]["sd"]
    assert saved["plan"]["before"]["judged"] == ["B", "C", "D"] and saved["plan"]["before"]["n"] == 2
    text = "\n".join(first.lines)
    assert "discovery on neyman's pre-validation days 2026-09-14..2026-09-16: 3 recorded days" in text
    assert "N = 2, the largest" in text and "never computed again" in text
    assert not re.search(r"\b8[01][0-9],?[0-9]{3}\b", text), "no price"
    assert progress(s, morning_after(days[3])) == Progress(True, 2, False, 1), "one day after the start"

    # The next nights: N stands; nothing computed until two days can be in the window.
    waiting = neyman_variants(s, now=morning_after(days[3]), least=30, delta=0.003)
    assert (
        waiting.stage == module.WAITING
        and "1 recorded days so far that can be in it, of 2" in waiting.lines[0]
    )
    assert json.loads(report.read_text()) == saved, "N never computed again"
    # Two days after the start, but the first is short: one of two, not judged.
    short = neyman_variants(s, now=morning_after(days[4]))
    assert short.stage == module.NOT_FULL and short.evaluation is not None
    assert (short.evaluation.window, short.evaluation.short) == ([days[4]], [days[3]])
    assert progress(s, morning_after(days[4])) == Progress(True, 2, False, 1), "the short day is counted out"
    assert "has 1 of its 2 days" in short.lines[0] and "Not judged" in short.lines[0]

    # A day more: the window is full, judged once.
    judged = neyman_variants(s, now=AFTER)
    assert judged.stage == module.JUDGED and judged.evaluation is not None
    e = judged.evaluation
    assert (e.window, e.short) == ([days[4], days[5]], [days[3]])
    assert [f.composition for f in e.folds] == ["4+0", "5+0"]
    assert {a.n for a in e.arms.values()} == {sum(f.scored for f in e.folds)}
    saved = json.loads(report.read_text())
    # The live plan judges D - A alone at 95% (#532); B - A and C - A are described.
    assert saved["evaluation"]["judged"] is True and set(saved["evaluation"]["verdict"]) == {D}
    assert set(saved["evaluation"]["described_over_a"]) == {B, C} and saved["evaluation"]["level"] == 0.95
    assert set(saved["evaluation"]["candidates"]) <= {D}
    assert e.verdicts[D] == Verdict.of(e.records[D], e.records[A], 0.95)
    assert progress(s, AFTER) == Progress(True, 2, True, 2)
    text = "\n".join(judged.lines)
    assert "verdict on the window 2026-09-18..2026-09-21, its 2 days" in text
    assert "verdict (fixed before the run; the plan of 2026-11-04, #532): D against A;" in text
    assert "a two-sided 95% Student t interval;" in text and "Bonferroni" not in text.split("verdict (")[1]
    assert len(re.findall(r"^  D .* - A: .* -> (a candidate|not a candidate)$", text, re.M)) == 1
    assert len(re.findall(r"^  [BC] .* - A \(described, not judged\): ", text, re.M)) == 2
    assert "described, not judged (every interval at 95%)" in text
    assert "(2) diagnosis (reported, not judged)" in text and f"    {R}: unknown" in text
    assert not re.search(r"\b8[01][0-9],?[0-9]{3}\b", text), "no price"
    logged = [r.getMessage() for r in caplog.records if ": memory on " in r.getMessage()]
    assert [m.split(": ")[1] for m in logged] == [
        *(f"memory on {d} (day {i} of 3)" for i, d in enumerate(days[:3], 1)),
        *(f"memory on {d} (day {i} of 2)" for i, d in enumerate(days[4:6], 1)),
    ]
    # Later: the verdict stands; nothing is computed again, the window stays.
    again = neyman_variants(s, now=AFTER + timedelta(days=7))
    assert again.stage == module.JUDGED_BEFORE and again.evaluation is None
    assert json.loads(report.read_text()) == saved
    assert "it stands, not computed again" in again.lines[0] and again.lines[2] == judged.lines[0]


# The discovery as the server saved it on the night of 2026-11-03 (#455's comment): what later runs read
# of it (its days, folds and arms left out).
SERVER_DISCOVERY = {
    "made_at": "2026-11-04T00:21:14.754831+09:00",
    "period": ["2026-10-12", "2026-10-30"],
    "short": [],
    "spreads": {
        "B": {"days": 15, "mean": 0.0028455032886261756, "sd": 0.009997573371821807, "n": 117},
        "C": {"days": 15, "mean": 0.005830264632555385, "sd": 0.0100640652048084, "n": 118},
        "D": {"days": 15, "mean": 0.009392958976663056, "sd": 0.013283747868090213, "n": 206},
    },
    "rule": {
        "z_level": 2.394,
        "z_power": 0.842,
        "delta": 0.003,
        "least": 30,
        "level": 0.9833333333333333,
        "decide_above": 60,
    },
    "n": 206,
    "after": "2026-11-02",
    "min_scored": 200,
}


def saved_discovery(s: Settings) -> Path:
    report = s.state_dir / module.REPORT
    report.parent.mkdir(parents=True, exist_ok=True)
    saved = {"experiment": module.EXPERIMENT, "issue": 455, "discovery": SERVER_DISCOVERY}
    report.write_text(json.dumps(saved))
    return report


def test_the_live_window_judges_d_alone_its_n_56_from_the_saved_s_d(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #532: the plan moves into the saved report; the discovery is never run again.
    s = recorded(tmp_path, [])
    report = saved_discovery(s)

    def never(*args: object, **kwargs: object) -> None:
        raise AssertionError("the discovery ran again")

    monkeypatch.setattr(module, "discover", never)
    night = datetime(2026, 11, 5, 23, tzinfo=KST)
    run = neyman_variants(s, now=night)
    assert run.stage == module.WAITING
    saved = json.loads(report.read_text())
    assert saved["discovery"] == SERVER_DISCOVERY, "the discovery as it was"
    plan = saved["plan"]
    assert (plan["judged"], plan["described"]) == (["D"], ["B", "C"])
    assert (plan["level"], plan["z_level"], plan["z_power"], plan["delta"], plan["least"]) == (
        0.95,
        1.96,
        0.842,
        0.005,
        30,
    )
    # N = max(30, ceil(((1.96 + 0.842) x s_d / 0.005)^2)) from D - A's saved s_d: 56.
    sd = SERVER_DISCOVERY["spreads"]["D"]["sd"]  # type: ignore[index]
    assert plan["sd"] == sd and math.ceil(((1.96 + 0.842) * sd / 0.005) ** 2) == 56 == plan["n"]
    assert required_days(sd, 30, 0.005, 1.96) == 56
    assert (plan["changed"], plan["issue"]) == ("2026-11-04", 532) and "2027" in plan["reason"]
    assert plan["before"] | {"reason": None} == {
        "judged": ["B", "C", "D"],
        "described": [],
        "level": LEVEL,
        "z_level": Z_LEVEL,
        "z_power": Z_POWER,
        "delta": 0.003,
        "least": 30,
        "sd": None,
        "n": 206,
        "changed": None,
        "issue": None,
        "reason": None,
    }
    text = "\n".join(run.lines)
    assert "the live window's plan, changed on 2026-11-04 (#532): D against A, a lift of 0.5 points" in text
    assert "two-sided at 95% with 80% power" in text and "= 56 from the discovery's s_d of D - A" in text
    assert "before: B, C and D each against A, 0.3 points at 98.333%, N 206" in text
    assert "moved into the report now" in text and "of 56" in text
    assert progress(s, night) == Progress(True, 56, False, 0)

    # Later runs read it: nothing moves again, and the job waits for 56 recorded days.
    again = neyman_variants(s, now=night + timedelta(days=1))
    assert json.loads(report.read_text()) == saved
    assert "moved into the report now" not in "\n".join(again.lines)
    sunday = date(2026, 11, 8)
    ran = JobRecord(sunday, "ok", datetime(2026, 11, 8, 23, tzinfo=KST), "abc", 0, 600.0)

    def due(found: Progress) -> bool:
        return NEYMAN_VARIANTS.due(Night(sunday, False, "abc", ran, {}, Facts(neyman_variants=found)))

    assert not due(Progress(True, 56, False, 55)) and due(Progress(True, 56, False, 56))
    # A report with a plan keeps it; one whose discovery holds no s_d of D - A cannot set N.
    assert module.migrated(saved) == (saved, False)
    with pytest.raises(ValueError, match="no s_d of D - A"):
        module.live_plan(SERVER_DISCOVERY | {"spreads": {}})


def test_the_command_says_what_it_cannot_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KRX_SYMBOLS", "005930")

    assert main(["neyman-variants"]) == 1
    assert "no recorded day of 005930 from 2026-10-12 to 2026-10-30" in capsys.readouterr().err
    report = tmp_path / "state" / module.REPORT
    assert not report.exists()
    # A report that cannot be read holds N: it is not computed again over it.
    report.parent.mkdir(parents=True)
    report.write_text("{cut short")
    assert main(["neyman-variants"]) == 1
    assert "never computed again" in capsys.readouterr().err
    assert report.read_text() == "{cut short"
    # The night's condition reads no discovery from it: the job runs, and its command says why not.
    assert progress(Settings.from_env(require_kis=False), AFTER) == Progress(read=True)


def test_the_job_discovers_once_then_judges_the_window_once_it_can_be_full() -> None:
    job = NEYMAN_VARIANTS
    assert (job.name, job.command, job.timeout) == (
        "neyman-variants-283",
        ("neyman-variants",),
        timedelta(hours=4),
    )
    assert [t.issue for t in job.targets] == [455] and "시세 값이 없다" in job.targets[0].note
    assert [a.path for a in job.attach] == ["experiments/neyman-variants-283.json"]
    after = max(i for i, j in enumerate(JOBS) if j.name.startswith(("prevalidate-", "simulate-")))
    assert JOBS.index(job) > after, "last: it holds back no simulation nor pre-validation"
    assert job.gives_way_to is None and job.session is None
    sunday = date(2026, 11, 8)
    started = datetime(2026, 11, 8, 23, 0, tzinfo=KST)
    ran = JobRecord(sunday, "ok", started, "abc", 0, 600.0)
    failed = JobRecord(sunday, FAILED, started, "abc", None, 14400.0, timed_out=True)

    def night(found: Progress, record: JobRecord | None = None, commit: str = "abc") -> Night:
        return Night(sunday + timedelta(days=1), False, commit, record, {}, Facts(neyman_variants=found))

    new = Progress(read=True)
    assert job.due(night(new)), "the discovery, trading or not"
    assert not job.due(night(Progress())), "not on a night its progress was not read"
    assert not job.due(night(new, failed)), "a failure is not run again on the same code"
    assert job.due(night(new, failed, "def")), "but again once a deploy brings other code"
    assert not job.due(night(Progress(True, 30, False, 0), ran)), "discovered: the window waits"
    assert not job.due(night(Progress(True, 30, False, 29), ran))
    assert job.due(night(Progress(True, 30, False, 30), ran)), "the window can be full"
    assert not job.due(night(Progress(True, 30, False, 30), failed))
    assert job.due(night(Progress(True, 30, False, 30), failed, "def"))
    assert not job.due(night(Progress(True, 30, True, 45), ran)), "judged once"


def test_what_is_collected_names_each_day_s_window() -> None:
    # A collected day without a fit (no labelled day before it) says a third each and no window.
    days = split_days(made_up(600))
    first = sorted(days)[0]
    labelled = [x for x in days[first] if x.outcome is not None]
    collected = Collected()
    collected.add(first, None, fit_climate([]), labelled)
    assert collected.folds[0].composition == "-" and collected.folds[0].trained_on == 0
    assert {(r.p_up, r.p_flat, r.p_down) for r in collected.records[A]} == {(1 / 3, 1 / 3, 1 / 3)}
    assert len(collected.records["climate"]) == len(labelled)
