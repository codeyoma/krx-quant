"""Stand-in algorithms for engine tests: fixed forecasts, chosen features, chosen slowness."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence

from krx_quant.algorithms import Card, Meaning, Model, Sample
from krx_quant.baselines import Forecast
from krx_quant.features import SERIES_FEATURES, FeatureVector, Value
from krx_quant.pricing import Direction

CAUTIOUS = Forecast(0.2, 0.6, 0.2, 0.0)


def stand_in_card(name: str, logic: str, unique: tuple[str, ...], training: str) -> Card:
    """A stand-in's card: made-up text in every field."""
    return Card(f"Test {name}", "test", logic, unique, training, "as it is told", "never")


def stand_in_meanings(*params: str) -> Mapping[str, Meaning]:
    """Made-up meanings of a stand-in's parameters."""
    return {p: Meaning(f"test {p}", "none", "always") for p in params}


class FixedAlgorithm:
    """Predicts ``forecast``, after reading ``reads`` (default: what it declared) and
    sleeping ``seconds``.
    """

    training_days = 20

    def __init__(
        self,
        name: str,
        *,
        features: tuple[str, ...],
        unique: tuple[str, ...],
        forecast: Forecast = CAUTIOUS,
        seconds: float = 0.0,
        reads: tuple[str, ...] | None = None,
    ) -> None:
        self.name = name
        self.features = features
        self.card = stand_in_card(name, "a fixed forecast", unique, "nothing")
        self.param_meanings = stand_in_meanings("samples")
        self._forecast = forecast
        self._seconds = seconds
        self._reads = features if reads is None else reads
        self.seen: list[dict[str, Value]] = []

    def default_model(self) -> Model:
        return Model(f"{self.name}-default", {})

    def fit(self, samples: Sequence[Sample]) -> Model:
        return Model(f"{self.name}-fitted", {"samples": float(len(samples))})

    def predict(self, features: FeatureVector, model: Model) -> Forecast:
        self.seen.append(
            {
                name: features.series(name) if name in SERIES_FEATURES else features[name]
                for name in self._reads
            }
        )
        if self._seconds:
            time.sleep(self._seconds)
        return self._forecast


class FrequencyAlgorithm:
    """Forecasts the class frequencies it was trained on, or, ``inverted``, the up and down
    frequencies swapped: a model that learns the wrong way round.
    """

    training_days = 4

    def __init__(self, name: str, feature: str, *, inverted: bool = False) -> None:
        self.name = name
        self.features: tuple[str, ...] = (feature,)
        self.card = stand_in_card(name, "class frequencies", (feature,), "counting")
        self.param_meanings = stand_in_meanings("up", "flat", "down")
        self._inverted = inverted
        self.fitted_on: list[int] = []  # samples per fit

    def default_model(self) -> Model:
        return Model(f"{self.name}-default", {"up": 1 / 3, "flat": 1 / 3, "down": 1 / 3})

    def fit(self, samples: Sequence[Sample]) -> Model:
        scored = [s for s in samples if s.outcome is not None]
        self.fitted_on.append(len(scored))
        count = {d: sum(s.outcome is d for s in scored) / len(scored) for d in Direction}
        up, down = count[Direction.UP], count[Direction.DOWN]
        if self._inverted:
            up, down = down, up
        params = {"up": up, "flat": count[Direction.FLAT], "down": down}
        return Model.fitted(self.name, params, scored, 0.0)

    def predict(self, features: FeatureVector, model: Model) -> Forecast:
        floor = 0.01  # never sure
        p = [max(model.params[k], floor) for k in ("up", "flat", "down")]
        return Forecast(p[0] / sum(p), p[1] / sum(p), p[2] / sum(p), 0.0)
