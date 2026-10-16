"""A kind's algorithms learn the kind's answers (#179): training, the nightly training, the
swap and pre-validation, with the test kind (``drift``, ``tests/kind_doubles.py``).

Input: recorded days and days with one-minute bars only, as the collector and the archive
store them; stand-in algorithms of the drift. Output: the samples' answers (the ones the
engine scores on a recorded day, the bar rule's on the others), the models installed with the
drift's answer version, the night's reports, and the pre-validation's report.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from krx_quant.algorithms import Model
from krx_quant.algorithms.contract import KindSample
from krx_quant.clock import FakeClock
from krx_quant.engine import PredictionEngine
from krx_quant.features import FeatureVector, Leaders
from krx_quant.kinds import Answer, KindForecast
from krx_quant.model_shelf import Shelf
from krx_quant.nightly import train_night
from krx_quant.object_store import LocalObjectStore
from krx_quant.predictions import PredictionStore
from krx_quant.prevalidation import admits, prevalidate
from krx_quant.swap import reload_models
from krx_quant.touch import TARGET_VERSION
from krx_quant.training import bar_answers, fit_algorithm, training_day
from tests.algorithm_doubles import stand_in_card, stand_in_meanings
from tests.kind_doubles import DRIFT, TEST_KIND, DriftFrequency
from tests.replay_events import CALENDAR, DAY, FEE, SYMBOL, at, run
from tests.synthetic_bars import day_of_bars
from tests.test_nightly import DAYS as NIGHT_DAYS
from tests.test_nightly import FINISHED, NIGHT, archive_bars, read
from tests.test_prevalidation import AFTER, ALPHA, Contrarian, report, settings
from tests.test_replay import busy_morning
from tests.test_replay_cli import record


async def test_a_recorded_day_trains_a_kind_on_the_answers_the_engine_scores(tmp_path: Path) -> None:
    """Input: a busy morning, recorded, and replayed through the engine with the drift.
    Output: the day's drift samples carry the answers the engine stored for the same windows,
    class and reason alike, for every window over by the replay's end."""
    events = busy_morning()
    data = tmp_path / "data"
    record(data, events)
    engine = await run(
        tmp_path / "engine.db",
        events,
        until=at("10:00:00"),
        target_version=TARGET_VERSION,
        kinds=(TEST_KIND,),
        kind_algorithms=(),  # the range's Mandelbrot needs the range (#181)
    )
    stored = {datetime.fromisoformat(a["bar_close"]): a for a in engine.store.kind_answers(DRIFT, SYMBOL)}
    engine.store.close()

    day = training_day(
        data,
        DAY,
        CALENDAR,
        (SYMBOL,),
        ("velocity",),
        FEE,
        Leaders(),
        target_version=TARGET_VERSION,
        kinds=(TEST_KIND,),
    )

    assert day is not None and day.reference == "mid"
    samples = [s for s in day.of_kind(TEST_KIND, ("velocity",)) if s.bar_close <= at("09:56:00")]
    assert len(samples) == 56
    for s in samples:
        assert s.answer is not None
        assert (s.answer.label, s.answer.unscorable) == (
            stored[s.bar_close]["class"],
            stored[s.bar_close]["unscorable"],
        )
        assert dict(s.answer.path) == stored[s.bar_close]["path"]
        assert s.answer_version == 1 and s.reference == "mid"
    assert {s.label for s in samples} == {"fell", "held", "rose"}


def test_a_day_with_bars_only_is_answered_by_the_kind_s_bar_rule() -> None:
    """Input: bars from 09:00 whose closes go up a tick a minute to 09:10, then stay. Output:
    the drift's bar rule: the last close of the four minutes against the bar close's (rose
    while it climbs, held after), none without the bar ending at the bar close (09:01 has the
    09:00 bar, the first) or without any after it."""
    bars = day_of_bars(DAY, [1.0] * 10 + [0.0] * 20)  # 09:00 to 09:29

    answers = bar_answers({(SYMBOL, DAY): bars}, CALENDAR, FEE, (TEST_KIND,))

    def label(hhmm: str) -> str | None:
        answer = answers[(DRIFT, SYMBOL, at(f"{hhmm}:00"))]
        return None if answer is None else answer.label

    assert label("09:01") == "rose"
    assert label("09:06") == "rose"  # the bar ending 09:06 closed 6 up; 09:09's closes 10 up
    assert label("09:10") == "held"
    assert label("09:30") is None, "no trade after it"


class Turner:
    """Calls the drift undone: after a rise over the last four minutes, a fall; right on a
    market that swings back. Its own feature is ``minute_ticks``."""

    kind = DRIFT
    training_days = 2

    def __init__(self, name: str = "turner") -> None:
        self.name = name
        self.features: tuple[str, ...] = ("minute_ticks",)
        self.card = stand_in_card(name, "the last minutes undone", ("minute_ticks",), "nothing")
        self.param_meanings = stand_in_meanings("samples")
        self.fitted: list[int] = []  # labelled samples per fit

    def default_model(self) -> Model:
        return Model(f"{self.name}-default", {})

    def fit(self, samples: Sequence[KindSample]) -> Model:
        labelled = [s for s in samples if s.label is not None]
        self.fitted.append(len(labelled))
        return Model.fitted(self.name, {"samples": float(len(labelled))}, labelled, 0.0)

    def predict(self, features: FeatureVector, model: Model) -> KindForecast:
        change = sum(features.series("minute_ticks")[-4:])
        if change >= 2:
            return KindForecast((0.7, 0.2, 0.1), {"drift_ticks": -change})
        if change <= -2:
            return KindForecast((0.1, 0.2, 0.7), {"drift_ticks": -change})
        return KindForecast((0.2, 0.6, 0.2), {"drift_ticks": 0.0})


def test_a_kind_s_algorithm_is_fitted_on_its_answers_and_the_engine_switches_to_it(tmp_path: Path) -> None:
    """Input: ten recorded days of a swinging market. Output: ``fit`` trains the drift's
    algorithm on its four latest days' answers, installs a model of the drift's answer version,
    and a running engine switches to it as it does to a direction algorithm's."""
    s = settings(tmp_path)
    counter = DriftFrequency("counter", "velocity")

    fitted = fit_algorithm(s, "counter", now=AFTER, kind_algorithms=[counter], kinds=(TEST_KIND,))

    assert fitted.days == 4 and len(fitted.recorded) == 4
    assert fitted.samples == counter.fitted_on[-1] > 0
    assert fitted.model.target_version == TEST_KIND.answer_version
    assert sum(fitted.model.params.values()) == 1.0
    assert (
        json.loads((s.state_dir / "models" / "counter.json").read_text())["version"] == fitted.model.version
    )
    engine = PredictionEngine(
        PredictionStore(tmp_path / "engine.db"),
        CALENDAR,
        (SYMBOL,),
        FEE,
        FakeClock(at("08:00:00")),
        kinds=(TEST_KIND,),
        kind_algorithms=[counter],
    )
    assert reload_models(engine, Shelf(s.state_dir))
    assert engine.models["counter"].version == fitted.model.version
    engine.store.close()


def test_the_night_trains_a_kind_s_algorithm_on_its_answers(tmp_path: Path) -> None:
    """Input: eight days of rising bars in the store, a drift algorithm that learns the
    answers' frequencies. Output: its report names the drift and its answer version, counts
    the drift's labelled samples (by the bar rule: no recording), and its challenger, which
    learned the rises, beats the defaults; the summary has a line of the drift's samples."""
    root = tmp_path / "store"
    for day in NIGHT_DAYS:
        archive_bars(root, day)
    counter = DriftFrequency("counter", "velocity")

    result = train_night(
        LocalObjectStore(root),
        tmp_path / "cache",
        symbols=(SYMBOL,),
        fee_rate=FEE,
        algorithms=[],
        now=lambda: FINISHED,
        target_version=TARGET_VERSION,
        kind_algorithms=[counter],
        kinds=(TEST_KIND,),
    )

    saved: dict[str, Any] = read(root, f"reports/{NIGHT}/counter.json")
    assert (saved["kind"], saved["target_version"]) == ("drift", 1)
    assert saved["labels"]["samples"]["recorded"] == 0 and saved["labels"]["samples"]["bars"] > 0
    assert saved["promoted"] is True and saved["forced"] is False
    assert saved["challenger"]["loss"] < saved["champion"]["loss"]
    assert result.promoted["counter"] == saved["challenger"]["version"]
    assert f"drift 정답 표본: 녹화 호가 0개, 1분봉 {saved['labels']['samples']['bars']}개" in result.summary()
    assert f"정답 버전 {TARGET_VERSION} 표본" in result.summary(), (
        "the direction's line, from the climate's report"
    )
    model = read(root, f"models/counter/{saved['challenger']['version']}.json")
    assert model["target_version"] == 1


async def test_a_kind_s_algorithm_is_prevalidated_on_its_kind(tmp_path: Path) -> None:
    """Input: ten recorded days of a market swinging up and down every four minutes, the
    drift's turner and the direction's contrarian, both calling the swing back. Output: the
    turner passes on the drift's answers against the drift's baselines, its correlation is
    measured with the drift's algorithms only, its report names the drift and its answer
    version, and the engine admits it on that version."""
    s = settings(tmp_path)
    turner = Turner()

    result = await prevalidate(
        s,
        "turner",
        algorithms=[ALPHA, Contrarian("contrarian")],
        kind_algorithms=[turner],
        kinds=(TEST_KIND,),
        now=AFTER,
    )

    saved = report(s, "turner")
    assert result.passed, saved["checks"]
    assert (saved["kind"], saved["target_version"]) == ("drift", 1)
    assert set(saved["stats"]["lift"]["vs"]) == {"drift-held", "drift-persist"}
    assert saved["stats"]["lift"]["low"] > 0 and "pnl" not in saved["stats"]
    assert saved["correlations"] == {}, "no other drift algorithm: the contrarian is the direction's"
    assert len(turner.fitted) == 9, "retrained before each day but the first (no day before it)"
    # The first day ran the default model, not trained on the drift's answers: it does not count.
    conn = sqlite3.connect(saved["db"])
    try:
        by_day = dict(
            conn.execute(
                "SELECT substr(p.bar_close, 1, 10), COUNT(*) FROM kind_predictions p JOIN kind_answers a"
                " ON a.kind = p.kind AND a.symbol = p.symbol AND a.bar_close = p.bar_close"
                " WHERE p.algorithm = 'turner' AND p.status = 'ok' AND a.class IS NOT NULL AND a.special = 0"
                " GROUP BY 1"
            ).fetchall()
        )
    finally:
        conn.close()
    assert by_day[saved["days"][0]] > 0
    assert saved["stats"]["n"] == sum(by_day.values()) - by_day[saved["days"][0]]
    gate = admits(s.state_dir, 2, (TEST_KIND,))
    assert gate(turner), "on the drift's answer version, not the engine's target"
    assert not admits(s.state_dir, 2)(turner), "an engine without the drift does not know it"


def test_a_model_fitted_on_a_kind_s_samples_learned_its_answer_version() -> None:
    """A model's target (#170) is its kind's answer version when fitted on the kind's samples:
    the same parameters on another version are another model."""
    answered = KindSample(SYMBOL, at("09:31:00"), FeatureVector({}, ()), Answer("rose"), 1)
    later = KindSample(SYMBOL, at("09:31:00") + timedelta(minutes=1), FeatureVector({}, ()), None, 1)
    assert (answered.label, later.label) == ("rose", None)

    first = Model.fitted("x", {"a": 1.0}, [answered], 0.0)
    second = Model.fitted("x", {"a": 1.0}, [replace(answered, answer_version=2)], 0.0)

    assert (first.target_version, second.target_version) == (1, 2)
    assert first.version != second.version
