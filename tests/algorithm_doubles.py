"""Stand-in algorithms for engine tests: fixed forecasts, chosen features, chosen slowness."""

from __future__ import annotations

import asyncio
import contextvars
import time
import weakref
from collections.abc import Mapping, Sequence

from krx_quant.algorithms import Card, Meaning, Model, Sample
from krx_quant.baselines import Forecast
from krx_quant.features import SERIES_FEATURES, FeatureVector, Value
from krx_quant.pricing import Direction

CAUTIOUS = Forecast(0.2, 0.6, 0.2, 0.0)


class _Tally:
    """The seconds the stand-ins declared while one run was timed."""

    def __init__(self) -> None:
        self.seconds = 0.0


_TALLY: contextvars.ContextVar[_Tally] = contextvars.ContextVar("stand_in_tally")


class FixedCosts:
    """The engine's timer (``krx_quant.engine.timer``) in a CPU budget test: a run takes the
    seconds its stand-ins declare (``spend``), never what a loaded machine adds to them (#327).

    The engine times each algorithm's run in an asyncio task of its own, and the run's worker
    thread works in a copy of that task's context. So the first reading in a task opens the
    task's tally, and a stand-in that spends adds to the tally of the run that called it. The
    time limits still count real time: a stand-in over its limit sleeps through it.
    """

    def __init__(self) -> None:
        self._tallies: weakref.WeakKeyDictionary[asyncio.Task[object], _Tally] = weakref.WeakKeyDictionary()

    def perf_counter(self) -> float:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("the engine times its runs in asyncio tasks")
        tally = self._tallies.get(task)
        if tally is None:
            tally = self._tallies[task] = _Tally()
            _TALLY.set(tally)
        return tally.seconds


def spend(seconds: float) -> None:
    """Sleep ``seconds``, and count them to the run that called, when ``FixedCosts`` times it."""
    time.sleep(seconds)
    tally = _TALLY.get(None)
    if tally is not None:
        tally.seconds += seconds


def stand_in_card(name: str, logic: str, unique: tuple[str, ...], training: str) -> Card:
    """A stand-in's card: made-up text in every field."""
    return Card(f"Test {name}", "test", logic, unique, training, "as it is told", "never")


def stand_in_meanings(*params: str) -> Mapping[str, Meaning]:
    """Made-up meanings of a stand-in's parameters."""
    return {p: Meaning(f"test {p}", "none", "always") for p in params}


class FixedAlgorithm:
    """Predicts ``forecast``, after reading ``reads`` (default: what it declared) and
    spending ``seconds`` (``spend``).
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
            spend(self._seconds)
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
