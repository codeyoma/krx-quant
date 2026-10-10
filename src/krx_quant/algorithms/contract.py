"""Algorithm Contract: what an algorithm module provides.

- a personality card: the scientist, the core logic, its own (unique) features and how
  it learns;
- the features it declares, the only ones it is given;
- ``fit``: training samples in, a versioned model out;
- ``predict``: one symbol's features and the model in, a forecast out.

Algorithms never see each other's output; only the consensus combines them.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol

from krx_quant.baselines import Forecast
from krx_quant.features import FeatureVector
from krx_quant.pricing import Direction

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Card:
    """The personality card."""

    scientist: str
    character: str  # e.g. trend following
    logic: str
    unique_features: tuple[str, ...]
    training: str  # what is learned, and how


@dataclass(frozen=True)
class Model:
    """A versioned model: fitted parameters, or the defaults before any training."""

    version: str
    params: Mapping[str, float]
    trained: str | None = None  # what it was trained on
    trained_until: date | None = None  # the last day of its training data

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "params": dict(self.params),
            "trained": self.trained,
            "trained_until": self.trained_until.isoformat() if self.trained_until else None,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict())

    @classmethod
    def fitted(cls, name: str, params: Mapping[str, float], scored: Sequence[Sample], loss: float) -> Model:
        """A model fitted on ``scored``, versioned by its last day and what went into it."""
        first = min(s.bar_close for s in scored).date()
        last = max(s.bar_close for s in scored).date()
        canonical = json.dumps(dict(params), sort_keys=True)
        digest = hashlib.sha256(f"{canonical}|{first}|{last}|{len(scored)}".encode()).hexdigest()[:6]
        return cls(
            version=f"{name}-{last:%Y%m%d}-{digest}",
            params=dict(params),
            trained=f"{first}..{last}, {len(scored)} samples, log loss {loss:.4f}",
            trained_until=last,
        )

    @classmethod
    def from_json(cls, text: str) -> Model:
        return cls.from_dict(json.loads(text))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Model:
        until = data.get("trained_until")
        return cls(
            str(data["version"]),
            {k: float(v) for k, v in data["params"].items()},
            data.get("trained"),
            date.fromisoformat(until) if until else None,
        )


@dataclass(frozen=True)
class Sample:
    """One bar close of training data, with what happened four minutes later."""

    symbol: str
    bar_close: datetime
    features: FeatureVector
    outcome: Direction | None  # None if the bars do not tell
    change_ticks: float | None  # the move behind the outcome, for algorithms that learn its size


class Algorithm(Protocol):
    name: str
    card: Card
    features: tuple[str, ...]
    training_days: int  # the rolling window it trains on

    def default_model(self) -> Model: ...

    def fit(self, samples: Sequence[Sample]) -> Model: ...

    def predict(self, features: FeatureVector, model: Model) -> Forecast: ...


def model_path(state_dir: Path, name: str) -> Path:
    return state_dir / "models" / f"{name}.json"


def save_model(state_dir: Path, name: str, model: Model) -> Path:
    path = model_path(state_dir, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(model.to_json())
    tmp.replace(path)
    return path


def load_models(state_dir: Path, algorithms: Sequence[Algorithm]) -> dict[str, Model]:
    """Each algorithm's trained model if one is saved, else its defaults."""
    models: dict[str, Model] = {}
    for algorithm in algorithms:
        path = model_path(state_dir, algorithm.name)
        models[algorithm.name] = algorithm.default_model()
        if path.exists():
            try:
                models[algorithm.name] = Model.from_json(path.read_text())
            except (OSError, ValueError, KeyError, TypeError) as e:
                log.error("cannot read %s (%s); %s uses its default model", path, e, algorithm.name)
    return models
