"""The symbol and climate prior experiment for turing (#448): turing's trees with the symbol, the
climate prior or both as inputs more, against turing as it is and the climate baseline, on the
folds of turing's pre-validation, overall and by σ̂₄ tercile and 30-minute slot.

Input: made-up samples whose labels follow the symbol, or each symbol and slot, while their
features say nothing; recorded days of two symbols, one whose first half hour swings both ways
(each four minutes reach both targets: up) and one that stands still, with the barrier's σ̂₄ in
the engine DB; hand-made scored records. Output: the arms' models and forecasts, the experiment's
report and lines, and the rule's candidates. Its scheduled job (``turing-prior-448``) ran on
2026-11-02 and left ``JOBS`` (#505).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pytest

from krx_quant import turing_prior
from krx_quant.algorithms import Sample
from krx_quant.algorithms.contract import MissingContext, Model, PredictionContext, forecast_of
from krx_quant.algorithms.priors import PriorTally, prior_inputs, prior_of
from krx_quant.algorithms.turing import Turing
from krx_quant.barriers import BARRIER
from krx_quant.baselines import Forecast
from krx_quant.cli import main
from krx_quant.config import Settings
from krx_quant.consensus import slot
from krx_quant.engine import Event
from krx_quant.features import FeatureVector, Leaders
from krx_quant.feeds import KST
from krx_quant.feeds.base import kst_day
from krx_quant.kinds import Answer
from krx_quant.market_calendar import calendar_store
from krx_quant.predictions import DB_NAME, PredictionStore
from krx_quant.prevalidation import _WalkForward
from krx_quant.pricing import Direction
from krx_quant.stats import Interval, Scored
from krx_quant.training import finished_days
from krx_quant.turing_prior import (
    ARMS,
    BOTH_ARM,
    CLIMATE_ARM,
    PRIOR_ARM,
    SYMBOL_ARM,
    TURING_ARM,
    Experiment,
    TuringPlus,
    compared,
    inputs_of,
    lines,
    split,
    turing_prior_experiment,
)
from tests.prevalidation_doubles import AFTER, DAYS
from tests.replay_doubles import record
from tests.replay_events import at, book, trade

UP, FLAT, DOWN = Direction.UP, Direction.FLAT, Direction.DOWN
TURING = Turing()
A, B = "005930", "000660"


def features(**values: float | None) -> FeatureVector:
    """Turing's features, all unknown but ``values``."""
    return FeatureVector(dict.fromkeys(TURING.features) | values, TURING.features)


def made_up(days: int, label: dict[str, Direction], per_day: int = 200, seed: int = 0) -> list[Sample]:
    """``per_day`` bar closes of each symbol of ``label`` a day from 09:01, a minute apart; each
    labelled as its symbol's label says, but one in ten flat, and with noise for features."""
    rng = np.random.default_rng(seed)
    out = []
    for d in range(days):
        open_at = datetime.combine(date(2026, 9, 1) + timedelta(days=d), time(9), KST)
        for symbol, usual in label.items():
            for m in range(1, per_day + 1):
                outcome = FLAT if m % 10 == 0 else usual
                f = features(program_net=float(rng.normal()), volatility=float(rng.uniform(0.5, 2)))
                out.append(Sample(symbol, open_at + timedelta(minutes=m), f, outcome, 1.0))
    return out


def forecast(algorithm: Turing | TuringPlus, model: Model, s: Sample) -> Forecast:
    return forecast_of(algorithm, s.features, model, PredictionContext(s.symbol, s.bar_close))


def test_the_symbol_input_tells_apart_symbols_turing_cannot() -> None:
    window = made_up(3, {A: UP, B: FLAT})
    arm = TuringPlus(symbol=True)
    alone, plus = TURING.fit(window), arm.fit(window)

    assert plus.version.startswith("turing-symbol-20260903-") and plus.blob
    assert plus.params["symbol.000660"] == 0.0 and plus.params["symbol.005930"] == 1.0
    on_a, on_b = (replace(window[0], symbol=s) for s in (A, B))
    assert forecast(arm, plus, on_a).p_up > 0.8 and forecast(arm, plus, on_b).p_flat > 0.8
    # Without the symbol the features say nothing: turing gives both the same.
    assert forecast(TURING, alone, on_a) == forecast(TURING, alone, on_b)
    # A symbol the window never had is a missing input: a forecast all the same.
    other = forecast(arm, plus, replace(window[0], symbol="009150"))
    assert math.isclose(other.p_up + other.p_flat + other.p_down, 1.0)


def test_the_prior_input_follows_each_symbol_and_slot_s_frequencies() -> None:
    # A goes up in its first half hour and is flat in the next; B the other way round.
    window = [
        replace(s, outcome=UP if (slot(s.bar_close) == "09:00") == (s.symbol == A) else FLAT)
        for s in made_up(4, {A: FLAT, B: FLAT}, per_day=60)
    ]
    arm = TuringPlus(prior=True)
    plus, alone = arm.fit(window), TURING.fit(window)

    tally = PriorTally()
    tally.add(window)
    assert {k: v for k, v in plus.params.items() if k.startswith("prior.")} == tally.table()
    assert not any(k.startswith("symbol.") for k in plus.params), "no symbol input in (d)"
    a_open = window[-120]  # A's 09:01 on the last day
    assert (a_open.symbol, slot(a_open.bar_close)) == (A, "09:00")
    b_open, a_later = (
        replace(a_open, symbol=B),
        replace(a_open, bar_close=a_open.bar_close + timedelta(minutes=45)),
    )
    assert forecast(arm, plus, a_open).p_up > 0.6
    assert forecast(arm, plus, b_open).p_flat > 0.6 and forecast(arm, plus, a_later).p_flat > 0.6
    # The features say nothing: turing gives each the same.
    assert (
        forecast(TURING, alone, a_open) == forecast(TURING, alone, b_open) == forecast(TURING, alone, a_later)
    )


def test_a_training_sample_s_prior_never_sees_its_own_day_s_labels_or_a_later_day_s() -> None:
    window = made_up(4, {A: UP, B: FLAT}, per_day=20)
    days = sorted({kst_day(s.bar_close) for s in window})
    arm = TuringPlus(prior=True)

    def priors(samples: list[Sample]) -> dict[tuple[str, datetime], tuple[float, ...]]:
        rows = arm.training_inputs(samples, samples, {})
        return {(s.symbol, s.bar_close): tuple(row) for s, row in zip(samples, rows, strict=True)}

    before = priors(window)
    # The third day's labels and the fourth's all turned down.
    changed = priors([replace(s, outcome=DOWN) if kst_day(s.bar_close) >= days[2] else s for s in window])

    for s in window:
        key, day = (s.symbol, s.bar_close), kst_day(s.bar_close)
        if day <= days[2]:
            assert changed[key] == before[key], f"{day}'s prior saw its own day or a later one"
        else:
            assert changed[key] != before[key], "the fourth day's counts the third"
        if day == days[0]:
            assert before[key] == pytest.approx((0.0, 0.0)), "nothing before the first day: the even split"
    # Each row's prior is the table of the days before its own.
    tally = PriorTally()
    tally.add([s for s in window if kst_day(s.bar_close) < days[3]])
    last = next(s for s in window if kst_day(s.bar_close) == days[3])
    assert before[(last.symbol, last.bar_close)] == pytest.approx(
        prior_inputs(prior_of(tally.table(), last.symbol, last.bar_close))
    )


def test_the_arms_add_their_inputs_after_turing_s_and_ask_for_the_context() -> None:
    window = made_up(2, {A: UP, B: FLAT}, per_day=60)
    both = TuringPlus(symbol=True, prior=True)

    assert both.name == "turing-symbol-prior"
    assert both.added == ("symbol", "climate_up", "climate_down")
    assert both.features == TURING.features and both.training_days == 60
    scored = [s for s in window if s.outcome is not None]
    rows = np.hstack([inputs_of(scored), both.training_inputs(window, scored, {A: 1, B: 0})])
    assert rows.shape == (len(scored), len(TURING.features) + 3)
    model = both.fit(window)
    assert model.version.startswith("turing-symbol-prior-")
    with pytest.raises(MissingContext):
        forecast_of(both, window[0].features, model, None)
    default = both.default_model()
    assert (
        default.version == "turing-symbol-prior-default" and default.params == TURING.default_model().params
    )
    assert forecast(both, default, window[0]) == Forecast(1 / 3, 1 / 3, 1 / 3, 0.0)
    with pytest.raises(ValueError, match="turing alone is"):
        TuringPlus()


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


def settings(tmp_path: Path, days: list[date]) -> Settings:
    """Recorded ``days`` of A (swinging at the open) and B (still), and the barrier's σ̂₄ in the
    engine DB at their bar closes from 09:06 (none before), rising each minute of a symbol's day."""
    data, state = tmp_path / "data", tmp_path / "state"
    state.mkdir(parents=True)
    store = PredictionStore(state / DB_NAME)
    for day in days:
        record(
            data,
            sorted([*mornings(day, A, True), *mornings(day, B, False)], key=lambda e: e[1]["received_at"]),
            day=day,
        )
        answers = []
        for symbol in (A, B):
            for m in range(6, 91):
                t = at("09:00:00", day) + timedelta(minutes=m)
                path = {"w": 2, "sigma": 1.0 + m / 30, "k": 2, "tick": 100, "anchor": 81_250}
                answers.append((symbol, t, Answer("timeout", None, path), False))
        store.add_kind_answers(BARRIER.name, BARRIER.answer_version, answers)
    store.close()
    return Settings(
        kis_app_key="",
        kis_app_secret="",
        symbols=(A, B),
        etf_symbols=(),
        index_codes=(),
        us_symbols=(),
        data_dir=data,
        state_dir=state,
    )


def value(i: Interval | None) -> float:
    assert i is not None
    return i.value


# Five recorded mornings of two symbols, made into samples by the Market View twice (the experiment,
# then the window checked by hand), with four tree models a day: past the 30 s default on a loaded
# CI runner.
@pytest.mark.timeout(180)
def test_the_five_arms_are_compared_on_the_same_bar_closes_of_the_pre_validation_s_folds(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    days = DAYS[:5]
    s = settings(tmp_path, days)
    caplog.set_level("INFO", logger="krx_quant.turing_prior")

    result = turing_prior_experiment(s, algorithms=[Turing()], kind_algorithms=[], kinds=[], now=AFTER)

    assert result.days == days
    first, *later = result.folds
    assert first.models == {
        "climate": "baseline-climate-default",
        "turing": "turing-default",
        "turing_symbol": "turing-symbol-default",
        "turing_prior": "turing-prior-default",
        "turing_symbol_prior": "turing-symbol-prior-default",
    }, "nothing before the first day"
    for fold, before in zip(later, days, strict=False):
        assert all(f"-{before:%Y%m%d}-" in v for v in fold.models.values()), "trained up to the day before"
    total = sum(f.scored for f in result.folds)
    assert {c.stats.n for c in result.arms.values()} == {total} and total > 0, "every arm on each bar close"
    happened = result.arms[CLIMATE_ARM].stats.happened
    assert happened[UP] > 0 and happened[FLAT] > happened[UP], "A's open's half hour up, the rest flat"
    # (b) is turing as it predicts now: the same model its pre-validation trains, the same forecasts.
    available = finished_days(s.data_dir, s.symbols, AFTER)
    trainer = _WalkForward(s, Turing(), available, calendar_store(s).calendar, Leaders.of(s))
    window = trainer.samples_before(days[-1])
    assert {kst_day(x.bar_close) for x in window} == set(days[:-1])
    model = Turing().fit(window)
    assert result.folds[-1].models[TURING_ARM] == model.version
    ours = [r for r in result.records[TURING_ARM] if kst_day(r.bar_close) == days[-1]]
    theirs = [x for x in trainer.samples_on(days[-1]) if x.outcome is not None]
    assert [(r.symbol, r.bar_close) for r in ours] == [(x.symbol, x.bar_close) for x in theirs]
    assert [(r.p_up, r.p_flat, r.p_down) for r in ours] == [
        (f.p_up, f.p_flat, f.p_down) for f in (Turing().predict(x.features, model) for x in theirs)
    ]
    # (c) to (e) learn from the samples (b) learns from; (d) holds its window's prior: the days
    # before the day, never its own.
    for arm, variant in ((SYMBOL_ARM, TuringPlus(symbol=True)), (PRIOR_ARM, TuringPlus(prior=True))):
        assert result.folds[-1].models[arm] == variant.fit(window).version
    tally = PriorTally()
    tally.add(window)
    kept = {k: v for k, v in TuringPlus(prior=True).fit(window).params.items() if k.startswith("prior.")}
    assert kept == tally.table()
    tally.add(trainer.samples_on(days[-1]))
    assert kept != tally.table(), "with the day it predicts, it would be another prior"
    gain = result.gain[PRIOR_ARM]
    assert gain is not None and set(gain) == {"climate_up", "climate_down"}
    assert set(result.gain[SYMBOL_ARM] or {}) == {"symbol"}
    # The cells: each tercile and slot holds its bar closes, every arm the same ones.
    cells = result.by_volatility
    assert list(cells) == ["low", "mid", "high", "none"]
    assert sum(c[CLIMATE_ARM].stats.n for c in cells.values()) == total
    assert result.with_sigma == total - cells["none"][CLIMATE_ARM].stats.n > 0
    assert all({c.stats.n for c in arms.values()} == {arms[CLIMATE_ARM].stats.n} for arms in cells.values())
    assert list(result.by_slot) == ["09:00", "09:30", "10:00"]
    assert sum(c[CLIMATE_ARM].stats.n for c in result.by_slot.values()) == total
    morning = result.by_slot["09:00"][CLIMATE_ARM].stats.happened
    assert morning[UP] > 0 and result.by_slot["09:30"][CLIMATE_ARM].stats.happened[UP] == 0
    for arms in (result.arms, *cells.values(), *result.by_slot.values()):
        assert arms[CLIMATE_ARM].over_climate is None and arms[TURING_ARM].over_turing is None
        assert all(arms[arm].over_turing is not None for arm in (SYMBOL_ARM, PRIOR_ARM, BOTH_ARM))
    # Its memory a day (#383), and the report.
    logged = [r.getMessage() for r in caplog.records if ": memory on " in r.getMessage()]
    assert [m.split(": ")[1] for m in logged] == [
        f"memory on {d} (day {i} of 5)" for i, d in enumerate(days, 1)
    ]
    saved = json.loads((s.state_dir / "experiments" / "turing-prior-448.json").read_text())
    assert saved["days"] == [d.isoformat() for d in days] and saved["turing_days"] == 60
    assert set(saved["arms"]) == set(ARMS)
    assert saved["arms"][TURING_ARM]["confusion"]["up"]["flat"] == result.arms[TURING_ARM].confusion[UP][FLAT]
    assert saved["arms"][PRIOR_ARM]["over_turing"]["pairs"]["n"] == total
    assert set(saved["by_volatility"]["cells"]) == {"low", "mid", "high", "none"}
    assert set(saved["by_slot"]) == {"09:00", "09:30", "10:00"}
    assert set(saved["rule"]) == {SYMBOL_ARM, PRIOR_ARM, BOTH_ARM}
    assert saved["candidates"] == result.candidates
    text = "\n".join(lines(result, Path("state/experiments/turing-prior-448.json")))
    assert text.startswith(
        "turing symbol and prior experiment (#448): 5 recorded days, 2026-09-14..2026-09-18\n"
    )
    assert len(re.findall(r"^#448 rule, \((c|d|e)\)", text, re.M)) == 3
    assert "\n  low: " in text and "\n  09:00: " in text
    assert not re.search(r"\b8[01][0-9],?[0-9]{3}\b", text), "no price"


def scored(arm: str, day: date, minute: int, called: Direction, outcome: Direction) -> Scored:
    t = datetime.combine(day, time(9, minute), KST)
    f = Forecast.certain(called)
    return Scored(arm, f"{arm}-v", A, t, f.p_up, f.p_flat, f.p_down, outcome)


def test_an_arm_is_a_candidate_when_it_beats_turing_and_lifts_its_lower_bound_by_day() -> None:
    # Three days of four bar closes: up, flat, flat, down. The climate says flat: two right a day.
    # Turing gets three right on the first day, four on the second and two on the third.
    outcomes = [UP, FLAT, FLAT, DOWN]
    days = [date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14)]
    turing = {days[0]: [UP, FLAT, FLAT, FLAT], days[1]: outcomes, days[2]: [FLAT] * 4}
    records: dict[str, list[Scored]] = {arm: [] for arm in ARMS}
    for day in days:
        for i, outcome in enumerate(outcomes):
            calls = {
                CLIMATE_ARM: FLAT,
                TURING_ARM: turing[day][i],
                SYMBOL_ARM: turing[day][i],  # the same as turing: not better
                PRIOR_ARM: outcome,  # always right: better, and the same gain every day
                BOTH_ARM: UP,  # worse
            }
            for arm, called in calls.items():
                records[arm].append(scored(arm, day, i + 1, called, outcome))
    result = Experiment(
        made_at=datetime(2026, 11, 2, 23, tzinfo=KST),
        days=days,
        folds=[],
        arms=compared(records),
        by_volatility={},
        by_slot=split(records, lambda r: "09:00", ["09:00"]),
        with_sigma=0,
        gain=dict.fromkeys((SYMBOL_ARM, PRIOR_ARM, BOTH_ARM)),
        records=records,
    )

    assert (result.better(SYMBOL_ARM), result.better(PRIOR_ARM), result.better(BOTH_ARM)) == (
        False,
        True,
        False,
    )
    assert result.lower_bound_up(PRIOR_ARM) is True
    assert result.candidates == [PRIOR_ARM]
    text = "\n".join(lines(result, Path("r.json")))
    assert (
        "#448 rule, (d): over (b) above 0: yes; lower bound by trading day over the climate above (b)'s: yes"
        in text
    )
    assert "-> a candidate" in text and "#448 rule, (c): over (b) above 0: no" in text
    assert "candidates: (d) turing + prior; none goes into turing from this" in text
    by_day = result.arms[PRIOR_ARM].over_climate
    assert by_day is not None and by_day.by_day is not None and by_day.by_day.low == pytest.approx(0.5)


def test_the_command_says_when_no_day_was_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KRX_SYMBOLS", "005930")

    assert main(["turing-prior", "--days", "3"]) == 1

    assert "no recorded day of 005930" in capsys.readouterr().err
    assert not (tmp_path / "state" / turing_prior.REPORT).exists()
