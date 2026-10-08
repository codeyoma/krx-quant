"""Stand-in algorithms for engine tests: fixed forecasts, chosen features, chosen slowness."""

from __future__ import annotations

import time
from collections.abc import Sequence

from krx_quant.algorithms import Card, Model, Sample
from krx_quant.baselines import Forecast
from krx_quant.features import FeatureVector

CAUTIOUS = Forecast(0.2, 0.6, 0.2, 0.0)


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
        self.card = Card(f"Test {name}", "test", "a fixed forecast", unique, "nothing")
        self._forecast = forecast
        self._seconds = seconds
        self._reads = features if reads is None else reads
        self.seen: list[dict[str, float | None]] = []

    def default_model(self) -> Model:
        return Model(f"{self.name}-default", {})

    def fit(self, samples: Sequence[Sample]) -> Model:
        return Model(f"{self.name}-fitted", {"samples": float(len(samples))})

    def predict(self, features: FeatureVector, model: Model) -> Forecast:
        self.seen.append({name: features[name] for name in self._reads})
        if self._seconds:
            time.sleep(self._seconds)
        return self._forecast
