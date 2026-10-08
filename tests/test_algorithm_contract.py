"""The contract every registered algorithm keeps, checked for each one automatically.

- its card is filled in and names features it declared;
- it reads only the features it declared;
- its probabilities are each in [0, 1] and add up to 1, whatever the features;
- training gives a new, versioned model that survives being saved.
"""

from __future__ import annotations

import math
import time
from datetime import date
from pathlib import Path

import pytest

from krx_quant.algorithms import ALGORITHMS, Algorithm, load_models, save_model
from krx_quant.features import FEATURES, FeatureVector, UndeclaredFeature
from krx_quant.market_calendar import MarketCalendar
from krx_quant.training import samples
from tests.algorithm_doubles import FixedAlgorithm
from tests.synthetic_bars import random_walk, trending

FEE = 0.015 / 100
DAYS = [date(2026, 9, 21), date(2026, 9, 22)]


def given(default: float | None, **values: float | None) -> dict[str, float | None]:
    """Every registered feature: ``default``, except those named."""
    return {name: values.get(name, default) for name in FEATURES}


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
    assert card.unique_features
    assert set(card.unique_features) <= set(algorithm.features)
    assert algorithm.training_days > 0


@pytest.mark.parametrize("scenario", SCENARIOS, ids=str)
def test_probabilities_are_a_distribution_from_declared_features_only(
    algorithm: Algorithm, scenario: str
) -> None:
    features = FeatureVector(SCENARIOS[scenario], algorithm.features)  # reading any other raises

    forecast = algorithm.predict(features, algorithm.default_model())

    probabilities = (forecast.p_up, forecast.p_flat, forecast.p_down)
    assert all(0 <= p <= 1 for p in probabilities), probabilities
    assert math.isclose(sum(probabilities), 1, abs_tol=1e-9)
    assert math.isfinite(forecast.expected_ticks)


def test_training_gives_a_new_versioned_model_that_can_be_saved(algorithm: Algorithm, tmp_path: Path) -> None:
    calendar = MarketCalendar(dict.fromkeys(DAYS, True))
    bar_days = {("005930", DAYS[0]): trending(DAYS[0]), ("005930", DAYS[1]): random_walk(DAYS[1])}
    training = samples(bar_days, calendar, algorithm.features, FEE)

    model = algorithm.fit(training)

    default = algorithm.default_model()
    assert model.version != default.version
    assert model.params.keys() == default.params.keys()
    save_model(tmp_path, algorithm.name, model)
    assert load_models(tmp_path, [algorithm])[algorithm.name] == model
    for sample in training[:50]:
        algorithm.predict(sample.features, model)


def test_predicting_takes_a_small_part_of_the_time_budget(algorithm: Algorithm) -> None:
    # The engine allows 30 seconds per bar close for every symbol; one prediction of each
    # scenario, a hundred times over, must take well under a second.
    model = algorithm.default_model()
    vectors = [FeatureVector(values, algorithm.features) for values in SCENARIOS.values()]
    started = time.perf_counter()
    for _ in range(100):
        for features in vectors:
            algorithm.predict(features, model)
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
