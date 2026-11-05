"""The queue screen (#599): the waiting bar-close algorithms' walk-forward folds, scored by their
weakest lift by trading day over their kind's baselines, set the order of the pre-validations, and
admit, refuse and skip none.

Input: made-up days of samples (each bar close's label, or a kind's answer), recorded days of swings
(``prevalidation_doubles``), stand-in algorithms, stand-in candidate processes, and nights made by
hand for the scheduler. Output: what each baseline was given at a bar close, the calls scored, the
lifts and score, the order and its seats, the screen's files and lines, and the jobs run.
"""

from __future__ import annotations

import asyncio
import json
import random
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from time import monotonic, sleep
from typing import Any

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from krx_quant import queue_screen
from krx_quant.algorithms import KindSample, Model, Sample
from krx_quant.api import create_app
from krx_quant.barriers import BarrierClimate
from krx_quant.baselines import TOUCH_BASELINES, Forecast, Inputs
from krx_quant.cli import main
from krx_quant.climate import fit_climate
from krx_quant.clock import FakeClock
from krx_quant.config import Settings
from krx_quant.features import FeatureVector, Leaders
from krx_quant.feeds.base import KST, kst_day
from krx_quant.github import GitHub
from krx_quant.kind_registry import kind_named
from krx_quant.kinds import Answer, Kind, KindForecast, KindInputs, most_likely
from krx_quant.lifecycle import Status
from krx_quant.market_calendar import calendar_store
from krx_quant.predictions import DB_NAME, PredictionStore
from krx_quant.prevalidation import MIN_DAYS, _WalkForward
from krx_quant.pricing import Direction
from krx_quant.process_log import Child
from krx_quant.queue_screen import (
    BOUND,
    FIRST_COME,
    HISTORY,
    MAX_PUSHED_NIGHTS,
    QUEUE,
    SCORE,
    Outcome,
    Screened,
    direction_calls,
    kind_calls,
    lines,
    order_queue,
    queue_order,
    run_candidate,
    screen,
    screen_queue,
    stretches,
)
from krx_quant.scheduled import (
    JOBS,
    OK,
    PREVALIDATION_ISSUE,
    QUEUE_SCREEN,
    STATE_FILE,
    Facts,
    JobRecord,
    JobState,
    Night,
    Ran,
    Scheduler,
    in_queue,
    prevalidation_job_name,
    screened_order,
)
from krx_quant.scheduled.prevalidations import prevalidation_tonight, prevalidations_quiet
from krx_quant.stats import Lift
from krx_quant.touch import WINDOW
from krx_quant.training import finished_days
from tests.algorithm_doubles import FixedAlgorithm, stand_in_card, stand_in_meanings
from tests.fake_github import REPO
from tests.prevalidation_doubles import AFTER, DAYS, Contrarian, refuse, swings
from tests.prevalidation_doubles import settings as recorded_settings
from tests.replay_doubles import busy_morning, record
from tests.replay_events import at as replay_at
from tests.replay_events import run
from tests.scheduled_doubles import AT, CALENDAR, COMMIT, FRIDAY, Runs, Told

UP, FLAT, DOWN = Direction.UP, Direction.FLAT, Direction.DOWN
DAY = date(2026, 9, 14)
SYMBOLS = ("005930", "000660")
ALPHA = FixedAlgorithm(
    "alpha", features=("velocity",), unique=("velocity",), forecast=Forecast(0.6, 0.3, 0.1, 1.0)
)
ORDER = ("mandelbrot", "breiman", "kalman", "shannon", "poisson")
# The first pre-validations of those (a changed code's have ``--revision``).
FIRST_ONES = [
    j
    for j in JOBS
    if j.command[0] == "prevalidate" and j.command[1:] and j.command[1] in ORDER and len(j.command) == 2
]


def at(hhmm: str, day: date = DAY) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm), KST)


def features() -> FeatureVector:
    return FeatureVector({"velocity": 0.0}, ("velocity",))


def made_up_day(day: date = DAY, seed: int = 7) -> list[Sample]:
    """Each symbol's bar closes from 09:05 to 10:30, every minute, labelled at random; a few without
    a label (a window that could not be scored)."""
    rng = random.Random(seed)
    out = []
    for minute in range(5, 91):
        when = datetime.combine(day, time(9), KST) + timedelta(minutes=minute)
        for symbol in SYMBOLS:
            label = rng.choice([UP, FLAT, FLAT, DOWN, None] if minute % 17 == 0 else [UP, FLAT, FLAT, DOWN])
            out.append(Sample(symbol, when, features(), label, 0.0))
    return out


class Recording:
    """A baseline that predicts as ``inner`` does and keeps what it was given at each bar close, and
    what it said."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.name, self.version, self.card = inner.name, inner.version, inner.card
        self.given: dict[tuple[str, datetime], tuple[Any, Any]] = {}

    def predict(self, inputs: Any) -> Any:
        said = self.inner.predict(inputs)
        self.given[(inputs.symbol, inputs.bar_close)] = (inputs, said)
        return said


def _other(d: Direction | None) -> Direction | None:
    return None if d is None else {UP: DOWN, FLAT: UP, DOWN: FLAT}[d]


# The baselines at a bar close know only the windows over by then


def test_the_direction_s_baselines_know_only_the_windows_over_at_each_bar_close() -> None:
    day = made_up_day()
    climate = fit_climate(made_up_day(DAY - timedelta(days=3), seed=3))  # a table of a day before
    cut = at("09:40")
    # Every window still open at the cut ends otherwise; those over by then stay.
    flipped = [replace(s, outcome=_other(s.outcome)) if s.bar_close + WINDOW > cut else s for s in day]
    assert Counter(s.outcome for s in day) != Counter(s.outcome for s in flipped), "the day's shares differ"

    given = []
    for samples in (day, flipped):
        baselines = [Recording(b) for b in TOUCH_BASELINES]
        calls, errors = direction_calls(ALPHA, ALPHA.default_model(), climate, samples, baselines)
        assert errors == 0 and set(calls) == {"alpha", *(b.name for b in TOUCH_BASELINES)}
        given.append({b.name: {k: v for k, v in b.given.items() if k[1] <= cut} for b in baselines})

    assert given[0] == given[1], "what each baseline was given and said up to the cut"
    labels = {(s.symbol, s.bar_close): s.outcome for s in day}
    inputs: Inputs = given[0]["baseline-persist"][("005930", cut)][0]
    assert inputs.previous == labels[("005930", cut - WINDOW)], "the window that ended at the cut"
    assert inputs.day_classes == Counter(
        s.outcome for s in day if s.symbol == "005930" and s.bar_close + WINDOW <= cut and s.outcome
    ), "the classes of the windows over by the cut"
    assert inputs.mid is None and inputs.climate is not None


def test_the_candidate_and_every_baseline_are_scored_on_the_bar_closes_with_a_label() -> None:
    day = made_up_day()
    calls, _ = direction_calls(ALPHA, ALPHA.default_model(), fit_climate([]), day, TOUCH_BASELINES)

    labelled = sorted((s for s in day if s.outcome is not None), key=lambda s: (s.bar_close, s.symbol))
    assert [c.correct for c in calls["alpha"]] == [s.outcome is UP for s in labelled], "it calls up"
    assert [c.correct for c in calls["baseline-flat"]] == [s.outcome is FLAT for s in labelled]
    # The persistence does not predict the day's first four minutes (no window ended yet) nor after
    # a window with no label.
    persisted = {(c.symbol, c.bar_close) for c in calls["baseline-persist"]}
    assert ("005930", at("09:05")) not in persisted and ("005930", at("09:09")) in persisted
    assert len(persisted) < len(labelled)


def barrier_day(kind: Kind, day: date = DAY, seed: int = 11) -> list[KindSample]:
    """A day of a barrier's answers at random, every minute from 09:05 to 11:00; every 13th without a
    class (a special situation over its horizon, or unscorable)."""
    rng = random.Random(seed)
    out = []
    for minute in range(5, 121):
        when = datetime.combine(day, time(9), KST) + timedelta(minutes=minute)
        for symbol in SYMBOLS:
            label = None if minute % 13 == 0 else rng.choice(kind.classes)
            answer = Answer(label, None if label else "special", {"k": 2})
            out.append(KindSample(symbol, when, features(), answer, kind.answer_version, "mid"))
    return out


class SteadyBarrier:
    """A barrier candidate of ``kind`` calling the upper barrier first, always."""

    training_days = 20
    features: tuple[str, ...] = ("velocity",)

    def __init__(self, name: str, kind: str) -> None:
        self.name, self.kind = name, kind
        self.card = stand_in_card(name, "always the upper barrier", ("velocity",), "nothing")
        self.param_meanings = stand_in_meanings()
        self.fitted: list[list[date]] = []

    def default_model(self) -> Model:
        return Model(f"{self.name}-default", {})

    def fit(self, samples: Sequence[KindSample]) -> Model:
        self.fitted.append(sorted({kst_day(s.bar_close) for s in samples}))
        labelled = [s for s in samples if s.label is not None]
        return Model.fitted(self.name, {}, labelled, 0.0)

    def predict(self, features: FeatureVector, model: Model) -> KindForecast:
        return KindForecast((0.6, 0.3, 0.1))


def test_a_barrier_candidate_is_scored_on_its_kind_s_answers_against_its_kind_s_baselines() -> None:
    kind = kind_named("barrier_15m")
    assert kind is not None and kind.horizon == timedelta(minutes=15)
    day = barrier_day(kind)
    table = BarrierClimate(minutes=15).fit(barrier_day(kind, DAY - timedelta(days=3), seed=5))
    candidate = SteadyBarrier("steady", kind.name)

    calls, errors = kind_calls(kind, candidate, candidate.default_model(), {table_name(kind): table}, day)

    assert errors == 0
    assert set(calls) == {"steady", "barrier-climate-15m", "barrier-persist-15m"}, "its kind's own"
    labelled = sorted((s for s in day if s.label is not None), key=lambda s: (s.bar_close, s.symbol))
    assert len(labelled) < len(day), "a window with no class is not scored"
    assert [c.correct for c in calls["steady"]] == [s.label == "up_first" for s in labelled]
    persisted = {(c.symbol, c.bar_close) for c in calls["barrier-persist-15m"]}
    assert ("005930", at("09:19")) not in persisted, "no 15-minute window ended yet"
    assert ("005930", at("09:20")) in persisted, "the window of 09:05 ended at 09:20"
    assert len(calls["barrier-climate-15m"]) == len(labelled), "the table predicts every one"


def table_name(kind: Kind) -> str:
    return next(b.name for b in kind.baselines if b.name.startswith("barrier-climate"))


def test_a_barrier_s_baselines_know_only_its_windows_over_at_each_bar_close() -> None:
    kind = kind_named("barrier_15m")
    assert kind is not None
    day = barrier_day(kind)
    cut = at("10:00")
    flipped = [
        replace(s, answer=Answer(kind.classes[(kind.classes.index(s.label) + 1) % 3], None, {"k": 2}))
        if s.label is not None and s.bar_close + kind.horizon > cut
        else s
        for s in day
    ]
    given = []
    for samples in (day, flipped):
        recorded = replace(kind, baselines=tuple(Recording(b) for b in kind.baselines))
        candidate = SteadyBarrier("steady", kind.name)
        kind_calls(recorded, candidate, candidate.default_model(), {}, samples)
        given.append(
            {b.name: {k: v for k, v in b.given.items() if k[1] <= cut} for b in recorded.baselines}  # type: ignore[attr-defined]
        )

    assert given[0] == given[1], "what each baseline was given and said up to the cut"
    inputs: KindInputs = given[0]["barrier-persist-15m"][("005930", cut)][0]
    assert inputs.previous == next(
        s.answer for s in day if (s.symbol, s.bar_close) == ("005930", at("09:45"))
    )
    now = next(s.answer for s in day if (s.symbol, s.bar_close) == ("005930", cut))
    assert now is not None and inputs.flat_ticks == now.path["k"], "the bar close's band, as live"
    assert inputs.mid is None and inputs.tick is None, "its path has no reference mid-point nor tick"


def test_the_range_s_persistence_classes_the_last_range_by_the_bar_close_s_band() -> None:
    """As live (``engine._predict_kinds``): a range of 3 ticks under the last window's k = 2 is medium,
    under this bar close's k = 4 small, and that is what the persistence calls."""
    kind = kind_named("range")
    assert kind is not None

    def answered(hhmm: str, label: str, r: float, k: int) -> KindSample:
        path = {"r": r, "k": k, "tick": 5, "anchor": 1000.0}
        answer = Answer(label, None, path)
        return KindSample("005930", at(hhmm), features(), answer, kind.answer_version, "mid")

    day = [answered("09:05", "medium", 3, 2), answered("09:09", "small", 1, 4)]
    recorded = replace(kind, baselines=tuple(Recording(b) for b in kind.baselines))
    candidate = SteadyBarrier("steady", kind.name)

    calls, _ = kind_calls(recorded, candidate, candidate.default_model(), {}, day)

    persist = next(b for b in recorded.baselines if b.name == "range-persist")
    inputs, said = persist.given[("005930", at("09:09"))]  # type: ignore[attr-defined]
    assert (inputs.flat_ticks, inputs.tick, inputs.mid) == (4, 5.0, 1000.0)
    assert inputs.previous == day[0].answer and kind.classes[most_likely(said.probabilities)] == "small"
    assert [(c.bar_close, c.correct) for c in calls["range-persist"]] == [(at("09:09"), True)]


# The folds: trained on the days before each, the day's own answers never


# Four recorded mornings, each made into samples by the Market View: a few seconds on a laptop, past
# the 30 s default on a loaded CI runner.
@pytest.mark.timeout(180)
def test_a_direction_candidate_s_folds_are_fitted_on_the_days_before_each(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    days = DAYS[:4]
    s = recorded_settings(tmp_path, days)
    contrarian = Contrarian("contrarian")
    climates: list[tuple[date, Model]] = []
    original = queue_screen.direction_calls

    def watched(candidate: Any, model: Model, climate: Model, samples: Sequence[Sample], b: Any) -> Any:
        climates.append((kst_day(samples[0].bar_close), climate))
        return original(candidate, model, climate, samples, b)

    monkeypatch.setattr(queue_screen, "direction_calls", watched)

    result = screen(s, "contrarian", algorithms=[contrarian], kind_algorithms=[], kinds=[], now=AFTER)

    assert result.days == tuple(days)
    window = Contrarian.training_days
    assert contrarian.fitted == [days[max(i - window, 0) : i] for i in range(1, len(days))], "never its own"
    assert [d for d, _ in climates] == days
    assert climates[0][1].version == "baseline-climate-default", "nothing before the first day"
    assert all(m.trained_until is not None and m.trained_until < d for d, m in climates[1:]), "before each"
    assert set(result.lifts) == {b.name for b in TOUCH_BASELINES}
    assert result.calls > 100 and result.errors == 0
    lifts = {b: x.value for b, x in result.lifts.items() if x is not None and x.value is not None}
    assert len(lifts) == 4 and result.score == min(lifts.values())
    assert result.weakest == min(lifts.items(), key=lambda pair: (pair[1], pair[0]))
    assert all(x is not None and x.n == len(days) for x in result.lifts.values()), "by trading day"


@pytest.mark.timeout(180)
def test_a_barrier_candidate_s_folds_learn_and_score_its_kind_s_answers(tmp_path: Path) -> None:
    days = DAYS[:3]
    s = recorded_settings(tmp_path, days)
    kind = kind_named("barrier_15m")
    assert kind is not None
    steady = SteadyBarrier("steady", kind.name)

    result = screen(s, "steady", algorithms=[], kind_algorithms=[steady], kinds=[kind], now=AFTER)

    assert result.kind == "barrier_15m"
    assert set(result.lifts) == {"barrier-climate-15m", "barrier-persist-15m"}, "its kind's baselines only"
    assert steady.fitted == [days[:i] for i in range(1, len(days))], "the days before each"
    # Scored on its kind's answers with a class, as the folds read them.
    available = finished_days(s.data_dir, s.symbols, AFTER)
    trainer = _WalkForward(s, steady, available, calendar_store(s).calendar, Leaders.of(s), kind)
    answered = sum(1 for d in days for x in trainer.kind_samples_on(d) if x.label is not None)
    directions = sum(1 for d in days for x in trainer.samples_on(d) if x.outcome is not None)
    assert result.calls == answered and answered != directions, "its answers, not the touch labels"


def test_on_the_four_minute_move_a_direction_candidate_s_score_is_unknown(tmp_path: Path) -> None:
    """``KRX_TARGET_VERSION=1``: its persistence reads mid-points the samples lack; given none it would
    be a second always flat, so the screen leaves the score unknown rather than wrong."""
    s = replace(quiet_settings(tmp_path), target_version=1)

    result = screen(s, "alpha", algorithms=[ALPHA], kind_algorithms=[], kinds=[], now=AFTER)

    assert (result.score, result.calls, result.lifts) == (None, 0, {})
    assert result.why == queue_screen.TOUCH_ONLY
    assert Screened.from_dict(result.as_dict()) == result


# The order


def test_a_score_s_seat_and_an_arrival_s_seat_by_turns_the_bound_first() -> None:
    first_come = ["a", "b", "c", "d", "e"]
    scores = {"a": 0.01, "b": None, "c": 0.05, "d": -0.02, "e": 0.05}

    assert order_queue(first_come, scores, {}) == [
        ("c", SCORE),  # the highest; e ties it and came later
        ("a", FIRST_COME),
        ("e", SCORE),
        ("b", FIRST_COME),  # unknown: never a score's seat, still its arrival's
        ("d", SCORE),
    ]
    # Passed over on three nights: first, by arrival among them.
    assert order_queue(first_come, scores, {"d": MAX_PUSHED_NIGHTS, "b": MAX_PUSHED_NIGHTS, "a": 2}) == [
        ("b", BOUND),
        ("d", BOUND),
        ("c", SCORE),
        ("a", FIRST_COME),
        ("e", SCORE),
    ]
    # No score known (every screen failed): the arrival's order, as before the screen.
    assert [n for n, _ in order_queue(first_come, dict.fromkeys(first_come), {})] == first_come


def test_arrival_and_the_nights_passed_over_are_of_each_one_s_latest_stretch() -> None:
    def line(night: str, first: list[str], order: list[str]) -> dict[str, Any]:
        return {"night": night, "first_come": first, "order": order}

    history = [
        line("2026-11-01", ["a", "b"], ["b", "a"]),
        line("2026-11-02", ["a", "b"], ["a", "b"]),  # run again that night: the last line counts
        line("2026-11-02", ["a", "b", "c"], ["c", "b", "a"]),
        line("2026-11-03", ["b", "c"], ["c", "b"]),  # a left (it was pre-validated)
        line("2026-11-04", ["b", "c", "a"], ["b", "a", "c"]),  # a waits again: a new stretch
        {"night": "not a date"},
        line("2026-11-05", ["x"], ["x"]),  # tonight's own: not counted
    ]

    since, pushed = stretches(history, date(2026, 11, 5))

    assert since == {"b": date(2026, 11, 1), "c": date(2026, 11, 2), "a": date(2026, 11, 4)}
    # a was passed over on 11-01 and 11-02, then left: its new stretch starts afresh. b behind c on
    # 11-03, c behind a on 11-04.
    assert pushed == {"b": 1, "c": 1}


# The screen of the night: every one waiting, each in a process of its own


STAND_INS = [
    FixedAlgorithm(name, features=(feature,), unique=(feature,))
    for name, feature in (
        ("alpha", "velocity"),
        ("beta", "acceleration"),
        ("gamma", "vwap_z"),
        ("delta", "vwap_gap"),
    )
]
SCORES: dict[str, float | None] = {"alpha": 0.01, "beta": None, "gamma": 0.05, "delta": -0.02}


class Candidates:
    """Stand-in candidate processes: each screen of a candidate is counted, and gives its made-up
    score, or (beta) stops at a limit."""

    def __init__(self) -> None:
        self.ran: list[tuple[str, timedelta]] = []

    def __call__(self, name: str, days: int, timeout: timedelta, memory_mb: float) -> Outcome:
        self.ran.append((name, timeout))
        score = SCORES[name]
        if score is None:
            return Outcome(None, "time limit (10 min)")
        lift = Lift(3, score, score - 0.01, score + 0.01, 0.2)
        return Outcome(Screened(name, "direction", tuple(DAYS[:3]), 500, 0, {"baseline-climate": lift}))


def quiet_settings(tmp_path: Path, days: Sequence[date] = DAYS[:3]) -> Settings:
    """Recorded days (a few minutes of swings each: the folds are stand-ins here)."""
    data = tmp_path / "data"
    for day in days:
        record(data, swings(day, end="09:02:00"), day=day)
    return Settings(
        kis_app_key="",
        kis_app_secret="",
        symbols=("005930",),
        etf_symbols=(),
        index_codes=(),
        us_symbols=(),
        data_dir=data,
        state_dir=tmp_path / "state",
    )


def night_after(n: int) -> datetime:
    return AFTER.replace(hour=23) + timedelta(days=n)


def test_the_night_s_screen_orders_every_one_waiting_and_keeps_it(tmp_path: Path) -> None:
    s = quiet_settings(tmp_path)
    runs = Candidates()

    queue = screen_queue(
        s,
        ["alpha", "beta", "gamma", "delta"],
        now=night_after(0),
        run=runs,
        algorithms=STAND_INS,
        kind_algorithms=[],
        kinds=[],
    )

    assert queue is not None
    assert [name for name, _ in runs.ran] == ["alpha", "beta", "gamma", "delta"], "by arrival"
    assert queue.order == (("gamma", SCORE), ("alpha", FIRST_COME), ("delta", SCORE), ("beta", FIRST_COME))
    saved = json.loads((s.state_dir / QUEUE).read_text())
    assert saved["order"] == ["gamma", "alpha", "delta", "beta"]
    assert saved["first_come"] == ["alpha", "beta", "gamma", "delta"]
    assert saved["candidates"]["beta"]["why"] == "time limit (10 min)"
    assert saved["candidates"]["gamma"]["score"] == 0.05
    assert queue_order(s.state_dir, night_after(0)) == ("gamma", "alpha", "delta", "beta")
    assert queue_order(s.state_dir, night_after(queue_screen.QUEUE_KEPT_DAYS + 1)) == (), "too old"
    [line] = [json.loads(x) for x in (s.state_dir / HISTORY).read_text().splitlines()]
    assert line["scores"] == {"alpha": 0.01, "beta": None, "gamma": 0.05, "delta": -0.02}
    text = "\n".join(lines(queue, s.state_dir / QUEUE))
    assert (
        "  1. gamma        seat: score      score +0.0500 (over baseline-climate, 500 calls on 3 days)"
        in text
    )
    assert "  4. beta         seat: first come unknown: time limit (10 min);" in text
    assert "it orders the queue only" in text


def test_a_score_of_the_same_key_is_kept_and_one_passed_over_three_nights_goes_first(tmp_path: Path) -> None:
    s = quiet_settings(tmp_path)
    turn = ["alpha", "beta", "gamma", "delta"]
    runs = Candidates()
    orders = []
    for n in range(MAX_PUSHED_NIGHTS + 1):
        queue = screen_queue(
            s, turn, now=night_after(n), run=runs, algorithms=STAND_INS, kind_algorithms=[], kinds=[]
        )
        assert queue is not None
        orders.append(queue.order)
        if n == 0:
            runs.ran.clear()

    # The same code and days: the known scores are kept; the one a limit stopped is screened again.
    assert [name for name, _ in runs.ran] == ["beta"] * MAX_PUSHED_NIGHTS
    assert all(o == orders[0] for o in orders[:MAX_PUSHED_NIGHTS])
    # alpha and beta stood behind their arrival's place on three nights: first, by arrival.
    assert orders[-1] == (("alpha", BOUND), ("beta", BOUND), ("gamma", SCORE), ("delta", FIRST_COME))
    assert queue is not None and all(c.cached == (c.name != "beta") for c in queue.candidates)
    # A new recorded day: every score's key changes.
    record(s.data_dir, swings(DAYS[3], end="09:02:00"), day=DAYS[3])
    runs.ran.clear()
    screen_queue(s, turn, now=night_after(9), run=runs, algorithms=STAND_INS, kind_algorithms=[], kinds=[])
    assert sorted(name for name, _ in runs.ran) == sorted(turn)


def test_the_night_s_budget_leaves_the_rest_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = quiet_settings(tmp_path)
    clock = [0.0]
    monkeypatch.setattr(queue_screen, "monotonic", lambda: clock[0])
    monkeypatch.setattr(queue_screen, "SCREEN_BUDGET", timedelta(minutes=5))

    class Slow(Candidates):
        def __call__(self, name: str, days: int, timeout: timedelta, memory_mb: float) -> Outcome:
            clock[0] += 180  # three minutes each
            return super().__call__(name, days, timeout, memory_mb)

    runs = Slow()
    queue = screen_queue(
        s,
        ["gamma", "alpha", "delta"],
        now=night_after(0),
        run=runs,
        algorithms=STAND_INS[::-1],
        kind_algorithms=[],
        kinds=[],
    )

    assert queue is not None
    assert runs.ran == [("gamma", timedelta(minutes=5)), ("alpha", timedelta(minutes=2))], "what is left"
    why = {c.name: c.why for c in queue.candidates}
    assert why["delta"] == why["beta"] == "not screened: tonight's budget (5 min) was spent"
    assert queue.order[0] == ("gamma", SCORE)


def test_only_the_algorithms_the_engine_waits_for_are_screened(tmp_path: Path) -> None:
    s = quiet_settings(tmp_path)
    refuse(s.state_dir, STAND_INS[1])  # beta: refused on the code it has
    store = PredictionStore(s.state_dir / DB_NAME)
    store.add_status("delta", AFTER, None, Status.PROBATION, "registered")  # enrolled
    store.close()
    newton = FixedAlgorithm("newton", features=("velocity",), unique=("velocity",))  # before pre-validation
    runs = Candidates()

    queue = screen_queue(
        s,
        ["alpha", "gamma"],
        now=night_after(0),
        run=runs,
        algorithms=[newton, *STAND_INS],
        kind_algorithms=[],
        kinds=[],
    )

    assert queue is not None and [c.name for c in queue.candidates] == ["alpha", "gamma"]
    for name in ("alpha", "gamma"):
        refuse(s.state_dir, next(a for a in STAND_INS if a.name == name))
    assert (
        screen_queue(s, [], now=night_after(1), run=runs, algorithms=STAND_INS, kind_algorithms=[], kinds=[])
        is None
    )


# A candidate's process


SCREENED = Screened(
    "alpha", "direction", tuple(DAYS[:2]), 9, 0, {"baseline-climate": Lift(2, 0.1, 0.0, 0.2, 0.1)}
)


def program(script: str) -> list[str]:
    """A stand-in for ``krx-quant``: Python running ``script``, given the candidate's arguments."""
    return [sys.executable, "-c", script]


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(queue_screen, "POLL_SECONDS", 0.05)


def test_a_candidate_s_process_gives_its_folds_in_a_file(quick: None) -> None:
    text = json.dumps(json.dumps(SCREENED.as_dict()))
    writes = f"import sys, pathlib; pathlib.Path(sys.argv[-1]).write_text({text})"

    outcome = run_candidate("alpha", 2, timedelta(minutes=1), 10_000, program=program(writes))

    assert outcome == Outcome(SCREENED)
    failed = run_candidate("alpha", 2, timedelta(minutes=1), 10_000, program=program("raise SystemExit(3)"))
    assert failed == Outcome(None, "failed: exit 3")
    silent = run_candidate("alpha", 2, timedelta(minutes=1), 10_000, program=program("pass"))
    assert silent == Outcome(None, "no result: FileNotFoundError")


def test_a_candidate_s_process_is_stopped_at_its_time_limit(quick: None) -> None:
    waits = "import time\nwhile True: time.sleep(1)"

    outcome = run_candidate("alpha", 2, timedelta(seconds=0.2), 10_000, program=program(waits))

    assert outcome == Outcome(None, "time limit (0.00333333 min)")


@pytest.mark.skipif(not Path("/proc/self/status").exists(), reason="the resident memory is read from /proc")
def test_a_candidate_s_process_is_stopped_at_its_memory_limit(quick: None) -> None:
    grows = "import time\nheld = b'x' * (300 << 20)\nwhile True: time.sleep(1)"

    outcome = run_candidate("alpha", 2, timedelta(minutes=1), 100, program=program(grows))

    assert outcome.screened is None and outcome.why is not None
    assert outcome.why.startswith("memory limit (") and outcome.why.endswith(" MB resident, at most 100)")


def _until(condition: Callable[[], bool], timeout: float = 20.0) -> None:
    """Return once ``condition()`` holds, asking it every 20 ms; TimeoutError after ``timeout`` s."""
    deadline = monotonic() + timeout
    while not condition():
        if monotonic() > deadline:
            raise TimeoutError
        sleep(0.02)


def _gone(pid: int) -> bool:
    """Whether process ``pid`` has ended (reaped, or a zombie no one reaped yet)."""
    try:
        status = Path(f"/proc/{pid}/status").read_text()
    except FileNotFoundError:
        return True
    return "\nState:\tZ" in status


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG is Linux's")
def test_a_candidate_s_process_ends_with_the_screen_s(tmp_path: Path) -> None:
    pid = tmp_path / "candidate.pid"
    candidate = (
        "import os, time\n"
        f"open({str(pid) + '.tmp'!r}, 'w').write(str(os.getpid()))\n"
        f"os.replace({str(pid) + '.tmp'!r}, {str(pid)!r})\n"
        "while True: time.sleep(1)"
    )
    screens = (
        "import sys\nfrom datetime import timedelta\nfrom krx_quant.queue_screen import run_candidate\n"
        f"run_candidate('alpha', 2, timedelta(minutes=5), 1e9, program=[sys.executable, '-c', {candidate!r}])"
    )
    screen = subprocess.Popen([sys.executable, "-c", screens])
    try:
        _until(pid.exists)
        child = int(pid.read_text())
        assert not _gone(child)
    finally:
        screen.kill()  # as the scheduler stops a job at its time limit
        screen.wait()

    _until(lambda: _gone(child))


def test_the_command_says_when_no_day_was_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KRX_SYMBOLS", "005930")

    assert main(["screen-queue", "--candidate", "kalman", "--out", str(tmp_path / "out.json")]) == 1
    assert "no recorded day of 005930" in capsys.readouterr().err
    assert main(["screen-queue"]) == 1
    assert "no recorded day of 005930" in capsys.readouterr().err
    assert not (tmp_path / "state" / QUEUE).exists()


# The night's pre-validations go by it


def night(
    *, queue: Sequence[str] = (), recorded: int = MIN_DAYS, runs: dict[str, JobRecord] | None = None
) -> Night:
    facts = Facts(recorded_days=recorded, waiting=ORDER, queue=tuple(queue))
    records = {prevalidation_job_name(n): r for n, r in (runs or {}).items()}
    return Night(FRIDAY, True, COMMIT, None, records, facts)


def test_the_pre_validations_go_by_the_latest_screen_s_order() -> None:
    assert screened_order(("kalman", "gone", "mandelbrot"), ORDER) == (
        "kalman",
        "mandelbrot",
        "breiman",
        "shannon",
        "poisson",
    ), "an algorithm it did not order (a new one) after those it did; one not waiting is no matter"
    due = [j.command[1] for j in FIRST_ONES if j.due(night(queue=("poisson", "kalman")))]
    assert due == ["poisson"], "the score's seat first"
    ran = JobRecord(FRIDAY, OK, datetime.combine(FRIDAY, time(22, 30), KST), COMMIT, 0, 60.0)
    after = night(queue=("poisson", "kalman"), runs={"poisson": ran})
    quiet = ("kalman", "mandelbrot", "breiman", "shannon")
    assert prevalidations_quiet(after, screened_order(after.facts.queue, ORDER)) == quiet
    # The quiet turn asks them in the screen's order: the first that fits runs (the scheduler's).
    asked = [j.command[1] for j in in_queue(FIRST_ONES, after.facts.queue) if j.quiet and j.quiet(after)]
    assert asked == list(quiet)
    revisions = [j for j in JOBS if "--revision" in j.command]
    assert in_queue([*revisions, *FIRST_ONES], ("poisson",))[: len(revisions)] == revisions, "theirs first"
    assert prevalidation_tonight(night(), ORDER) == "mandelbrot", "no screen: the registries' turn"


def test_the_screen_runs_once_a_night_two_or_more_have_a_turn_and_posts_on_204() -> None:
    assert QUEUE_SCREEN.command == ("screen-queue",)
    assert QUEUE_SCREEN.refreshes and QUEUE_SCREEN.gives_way_to is not None
    assert [t.issue for t in QUEUE_SCREEN.targets] == [PREVALIDATION_ISSUE]
    assert "순서만 정한다" in QUEUE_SCREEN.targets[0].note
    assert [a.path for a in QUEUE_SCREEN.attach] == [QUEUE]
    assert QUEUE_SCREEN.timeout == queue_screen.SCREEN_BUDGET + queue_screen.CANDIDATE_TIMEOUT + timedelta(
        minutes=5
    )
    assert QUEUE_SCREEN.due(night())
    assert not QUEUE_SCREEN.due(night(recorded=MIN_DAYS - 1))
    ran = JobRecord(FRIDAY, OK, datetime.combine(FRIDAY, time(22, 30), KST), COMMIT, 0, 60.0)
    assert not QUEUE_SCREEN.due(replace(night(), record=ran)), "once a night"
    four = {n: replace(ran, commit=COMMIT) for n in ORDER[:4]}  # run on this code without a verdict
    assert not QUEUE_SCREEN.due(night(runs=four)), "one with a turn: no order to set"
    assert QUEUE_SCREEN.label_if is not None
    assert not QUEUE_SCREEN.label_if(Ran(0, "", "")) and QUEUE_SCREEN.label_if(Ran(1, "", ""))


class Screens(Runs):
    """Stand-in job processes: the screen sets its order as it ends; the others are recorded."""

    def __init__(self, clock: FakeClock, queue: list[str]) -> None:
        super().__init__(clock)
        self.queue = queue
        self.commands: list[str] = []

    async def __call__(self, args: Sequence[str], timeout: timedelta, child: Child | None = None) -> Ran:
        self.commands.append(" ".join(args))
        if args[0] == "screen-queue":
            self.queue[:] = ["poisson", "kalman"]
        return await super().__call__(args, timeout, child)


async def test_the_night_s_pre_validations_go_by_the_order_its_screen_set_that_night(tmp_path: Path) -> None:
    """The whole night, to its end (its quiet turn too): the screen first, then the first in the
    order it set (without the facts read again, mandelbrot's), then the quiet night's second in that
    order (in the list's, mandelbrot's again)."""
    queue: list[str] = []
    clock = FakeClock(datetime.combine(FRIDAY, time(22, 30), KST))
    runs = Screens(clock, queue)

    async def facts() -> Facts:
        return Facts(recorded_days=MIN_DAYS, waiting=ORDER, queue=tuple(queue))

    async with aiohttp.ClientSession() as session:
        scheduler = Scheduler(
            [QUEUE_SCREEN, *FIRST_ONES],
            AT,
            CALENDAR,
            JobState.load(tmp_path / STATE_FILE),
            GitHub(session, None, REPO, "http://127.0.0.1:9"),
            Told(),
            clock,
            commit=COMMIT,
            facts=facts,
            runner=runs,
        )
        await asyncio.wait_for(scheduler.run_night(FRIDAY), 25)

    assert runs.commands == ["screen-queue", "prevalidate poisson", "prevalidate kalman"]


# The Engine API's place in the queue


async def test_the_engine_api_s_queue_is_the_screen_s_order(tmp_path: Path) -> None:
    first = FixedAlgorithm("first", features=("vwap_z",), unique=("vwap_z",))
    second = FixedAlgorithm("second", features=("vwap_gap",), unique=("vwap_gap",))
    engine = await run(
        tmp_path / "live.db",
        busy_morning(end="09:05:00"),
        until=replay_at("09:05:00"),
        algorithms=[first, second],
        kind_algorithms=(),
        admits=lambda a: False,
    )
    state = tmp_path / "state"
    path = state / QUEUE
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"night": replay_at("09:05:00").date().isoformat(), "order": ["second", "first"]})
    )

    async with TestClient(TestServer(create_app(engine, state_dir=state))) as client:
        response = await client.get("/v1/algorithms")
        body = await response.json()

    places = {a["name"]: a["prevalidation"]["queue"] for a in body["algorithms"]}
    assert places == {"first": 2, "second": 1}
