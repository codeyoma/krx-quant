"""The contract every registered algorithm keeps, checked for each one automatically.

- its card is filled in (how it makes its probabilities and when it fails, too) and names
  features it declared;
- every parameter its model can hold, and every registered feature, has a meaning (#164);
- it reads only the features it declared;
- its probabilities are each in [0, 1] and add up to 1, whatever the features, unless it
  declines the symbol with a reason (``NotPredicted``: the book missing);
- training gives a new, versioned model that survives being saved: on backfilled bars, or
  on recorded days for an algorithm that reads the book or the trades' sides.

An algorithm of a kind besides the direction (#179, ``KIND_ALGORITHMS``) keeps the same
contract on its kind: its forecasts keep the kind's (``Kind.invalid``), it trains on the
kind's answers.
"""

from __future__ import annotations

import math
import time
from datetime import date
from pathlib import Path

import pytest

from krx_quant.algorithms import (
    ALGORITHMS,
    KIND_ALGORITHMS,
    Abstention,
    Algorithm,
    KindAlgorithm,
    KindSample,
    Meaning,
    NotPredicted,
    load_models,
    predicted,
    save_model,
)
from krx_quant.baselines import BASELINES, TOUCH_BASELINES
from krx_quant.consensus import CARDS, NAMES
from krx_quant.features import (
    BOOK_FEATURES,
    FEATURE_MEANINGS,
    FEATURES,
    SERIES_FEATURES,
    TAPE_FEATURES,
    FeatureVector,
    Leaders,
    UndeclaredFeature,
    Value,
)
from krx_quant.kind_registry import kind_named
from krx_quant.market_calendar import MarketCalendar
from krx_quant.training import bar_answers, recorded_samples, samples
from tests.algorithm_doubles import FixedAlgorithm
from tests.replay_events import with_sides
from tests.synthetic_bars import random_walk, trending
from tests.test_replay import busy_morning

FEE = 0.015 / 100
DAYS = [date(2026, 9, 21), date(2026, 9, 22)]


def given(default: float | None, **values: float | None) -> dict[str, Value]:
    """Every registered feature: ``default``, except those named; a series feature an hour
    of the value (none of it for None)."""
    out: dict[str, Value] = {}
    for name in FEATURES:
        value = values.get(name, default)
        out[name] = (() if value is None else (value,) * 60) if name in SERIES_FEATURES else value
    return out


SCENARIOS = {
    "nothing known": given(None),
    "quiet": given(0.0, relative_volume=1.0, flat_ticks=2.0),
    "strong rise": given(1.0, velocity=3.0, acceleration=1.0, relative_volume=2.5, flat_ticks=2.0),
    "strong fall": given(1.0, velocity=-3.0, acceleration=-1.0, relative_volume=2.5, flat_ticks=2.0),
    "extreme": given(1e6, velocity=1e6, acceleration=-1e6, volatility=1e-9, flat_ticks=1.0),
    "no volume": given(1.0, relative_volume=0.0),
}


@pytest.fixture(params=ALGORITHMS, ids=lambda a: a.name)
def algorithm(request: pytest.FixtureRequest) -> Algorithm:
    chosen: Algorithm = request.param
    return chosen


def test_the_card_is_filled_in(algorithm: Algorithm) -> None:
    card = algorithm.card
    assert card.scientist and card.character and card.logic and card.training
    assert card.forecasting and card.failure, "how it makes its probabilities, and when it fails"
    assert card.unique_features
    assert set(card.unique_features) <= set(algorithm.features)
    assert algorithm.training_days > 0


def filled(meaning: Meaning) -> bool:
    return bool(meaning.text.strip() and meaning.unit.strip() and meaning.known.strip())


def test_every_parameter_of_its_default_model_has_a_meaning(algorithm: Algorithm) -> None:
    # Fitted models are checked in the training test below (and Markov's IV bounds, only
    # fitted with enough recorded days, in test_markov).
    missing = set(algorithm.default_model().params) - set(algorithm.param_meanings)

    assert not missing, f"{algorithm.name}: parameters without a meaning: {sorted(missing)}"
    assert all(filled(m) for m in algorithm.param_meanings.values())


def test_every_registered_feature_has_a_meaning() -> None:
    assert FEATURE_MEANINGS.keys() == FEATURES.keys(), "a feature without a meaning, or a stale meaning"
    assert all(filled(m) for m in FEATURE_MEANINGS.values())


def test_the_baselines_and_the_consensus_have_cards_too() -> None:
    cards = [b.card for b in BASELINES] + [CARDS[name] for name in NAMES]

    for card in cards:
        assert card.character and card.logic and card.training and card.forecasting and card.failure
        assert card.unique_features == ()
    assert all(b.card.scientist is None for b in BASELINES), "named after no one"
    assert all(CARDS[name].scientist for name in NAMES)


def test_the_touch_targets_baselines_have_cards_too() -> None:
    # With KRX_TARGET_VERSION=2 (#169): the climate is new, persist and random say the new label.
    assert [b.name for b in TOUCH_BASELINES] == [
        "baseline-flat",
        "baseline-persist",
        "baseline-random",
        "baseline-climate",
    ]
    for b in TOUCH_BASELINES:
        card = b.card
        assert card.character and card.logic and card.training and card.forecasting and card.failure
        assert card.unique_features == () and card.scientist is None
    persist, random = TOUCH_BASELINES[1], TOUCH_BASELINES[2]
    assert "도달 정답" in persist.card.logic and "도달 정답" in random.card.logic
    assert (persist.version, random.version) == ("v2", "v2")


@pytest.mark.parametrize("scenario", SCENARIOS, ids=str)
def test_probabilities_are_a_distribution_from_declared_features_only(
    algorithm: Algorithm, scenario: str
) -> None:
    features = FeatureVector(SCENARIOS[scenario], algorithm.features)  # reading any other raises

    try:
        forecast = algorithm.predict(features, algorithm.default_model())
    except NotPredicted as declined:
        assert isinstance(declined.why, Abstention)
        assert set(algorithm.features) & BOOK_FEATURES, "only for missing data an algorithm reads"
        missing = [SCENARIOS[scenario][n] in ((), None) for n in set(algorithm.features) & BOOK_FEATURES]
        assert all(missing), "only when that data is missing"
        return

    probabilities = (forecast.p_up, forecast.p_flat, forecast.p_down)
    assert all(0 <= p <= 1 for p in probabilities), probabilities
    assert math.isclose(sum(probabilities), 1, abs_tol=1e-9)
    assert math.isfinite(forecast.expected_ticks)


def test_training_gives_a_new_versioned_model_that_can_be_saved(algorithm: Algorithm, tmp_path: Path) -> None:
    calendar = MarketCalendar(dict.fromkeys(DAYS, True))
    if set(algorithm.features) & (BOOK_FEATURES | TAPE_FEATURES):
        training = [
            s
            for day in DAYS
            for s in recorded_samples(
                with_sides(busy_morning(day, end="11:00:00")),
                calendar,
                day,
                ("005930",),
                algorithm.features,
                FEE,
                Leaders(),
            )
        ]
    else:
        bar_days = {("005930", DAYS[0]): trending(DAYS[0]), ("005930", DAYS[1]): random_walk(DAYS[1])}
        training = samples(bar_days, calendar, algorithm.features, FEE)

    model = algorithm.fit(training)

    default = algorithm.default_model()
    assert model.version != default.version
    assert model.params.keys() == default.params.keys()
    assert set(model.params) <= set(algorithm.param_meanings), "every fitted parameter has a meaning"
    save_model(tmp_path, algorithm.name, model)
    assert load_models(tmp_path, [algorithm])[algorithm.name] == model
    for sample in training[:50]:
        predicted(algorithm, sample.features, model)


def test_predicting_takes_a_small_part_of_the_time_budget(algorithm: Algorithm) -> None:
    # The engine allows 10 seconds per bar close for every symbol; one prediction of each
    # scenario, a hundred times over, must take well under a second.
    model = algorithm.default_model()
    vectors = [FeatureVector(values, algorithm.features) for values in SCENARIOS.values()]
    started = time.perf_counter()
    for _ in range(100):
        for features in vectors:
            predicted(algorithm, features, model)
    assert time.perf_counter() - started < 1.0


def test_reading_an_undeclared_feature_breaks_the_contract() -> None:
    peeker = FixedAlgorithm(
        "peeker", features=("velocity",), unique=("velocity",), reads=("velocity", "volatility")
    )
    features = FeatureVector(SCENARIOS["quiet"], peeker.features)

    with pytest.raises(UndeclaredFeature, match="volatility"):
        peeker.predict(features, peeker.default_model())
    with pytest.raises(UndeclaredFeature, match="volatility"):
        features.get("volatility")
    with pytest.raises(UndeclaredFeature, match="volatility"):
        assert "volatility" in features
    assert "velocity" in features


@pytest.fixture(params=KIND_ALGORITHMS, ids=lambda a: a.name)
def kind_algorithm(request: pytest.FixtureRequest) -> KindAlgorithm:
    chosen: KindAlgorithm = request.param
    return chosen


def test_a_kind_s_algorithm_keeps_the_contract_on_its_kind(
    kind_algorithm: KindAlgorithm, tmp_path: Path
) -> None:
    """Its card is filled in; its parameters have meanings; whatever the features, its forecast
    keeps its kind's contract or it declines with a reason; trained on the kind's answers of
    two days of bars, it gives a versioned model of the kind's answer version that can be
    saved, whose every parameter has a meaning."""
    kind = kind_named(kind_algorithm.kind)
    assert kind is not None and kind.name != "direction"
    card = kind_algorithm.card
    assert card.scientist and card.character and card.logic and card.training
    assert card.forecasting and card.failure
    assert card.unique_features and set(card.unique_features) <= set(kind_algorithm.features)
    assert set(kind_algorithm.default_model().params) <= set(kind_algorithm.param_meanings)
    assert all(filled(m) for m in kind_algorithm.param_meanings.values())
    calendar = MarketCalendar(dict.fromkeys(DAYS, True))
    bar_days = {("005930", DAYS[0]): trending(DAYS[0]), ("005930", DAYS[1]): random_walk(DAYS[1])}
    answers = bar_answers(bar_days, calendar, FEE, (kind,))
    training = [
        KindSample(
            s.symbol,
            s.bar_close,
            s.features,
            answers.get((kind.name, s.symbol, s.bar_close)),
            kind.answer_version,
            "close",
        )
        for s in samples(bar_days, calendar, kind_algorithm.features, FEE)
    ]

    model = kind_algorithm.fit(training)

    assert model.version != kind_algorithm.default_model().version
    assert model.target_version == kind.answer_version
    assert set(model.params) <= set(kind_algorithm.param_meanings), "every fitted parameter has a meaning"
    save_model(tmp_path, kind_algorithm.name, model)
    assert load_models(tmp_path, [kind_algorithm])[kind_algorithm.name] == model
    for chosen in (kind_algorithm.default_model(), model):
        for values in SCENARIOS.values():
            try:
                forecast = kind_algorithm.predict(FeatureVector(values, kind_algorithm.features), chosen)
            except NotPredicted as declined:
                assert isinstance(declined.why, Abstention)
                continue
            assert kind.invalid(forecast) is None
