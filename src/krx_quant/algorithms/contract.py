"""Algorithm Contract: what an algorithm module provides.

- a personality card: the scientist, the core logic, its own (unique) features, how it
  learns, how it makes its three probabilities and the market it tends to fail in
  (``cards.Card``);
- the features it declares, the only ones it is given (their meanings beside the Feature
  Store's registry);
- the meaning of every parameter its model can hold (``param_meanings``);
- ``fit``: training samples in, a versioned model out;
- ``predict``: one symbol's features and the model in, a forecast out; or, when the data
  it needs is missing at the bar close, ``NotPredicted`` with why (stored as the
  prediction's status, unscored).

Algorithms never see each other's output; only the consensus combines them.
"""

from __future__ import annotations

import enum
import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol

from krx_quant.baselines import Forecast
from krx_quant.cards import Card, Meaning
from krx_quant.features import FeatureVector
from krx_quant.pricing import Direction
from krx_quant.touch import TARGET_VERSION

log = logging.getLogger(__name__)


class Abstention(enum.Enum):
    """Why an algorithm did not predict a symbol at a bar close: the prediction's status."""

    NO_BOOK = "no_book"  # the book was missing at the bar close


class NotPredicted(Exception):
    """Raised by ``predict`` for a symbol it does not predict at this bar close."""

    def __init__(self, why: Abstention) -> None:
        super().__init__(why.value)
        self.why = why


class NothingToLearn(ValueError):
    """Raised by ``fit`` when its samples hold nothing it can learn from (no outcomes, no
    book): a gap in the data, not a defect. Any other error from ``fit`` is a failure."""


# Where a day's labels came from (#165), by the reference price its samples were made with
# (``training.TrainingDay``): a recorded day's mid-points, a backfilled day's closes.
LABEL_SOURCES = {"mid": "recorded", "close": "bars"}


@dataclass(frozen=True)
class TrainedOn:
    """What a fitted model was trained on (#165): ``Model.trained``'s text, as values."""

    first_day: date
    last_day: date
    samples: int  # with an outcome
    loss: float  # its mean log loss on those samples: the training data's, not a validation's
    # The days by where their labels came from: ``recorded`` (mid-points) and ``bars``
    # (one-minute closes). None when the samples did not say (made by hand, not by training).
    days: Mapping[str, int] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "first_day": self.first_day.isoformat(),
            "last_day": self.last_day.isoformat(),
            "samples": self.samples,
            "loss": self.loss,
            "days": None if self.days is None else dict(self.days),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TrainedOn:
        days = data.get("days")
        return cls(
            date.fromisoformat(data["first_day"]),
            date.fromisoformat(data["last_day"]),
            int(data["samples"]),
            float(data["loss"]),
            None if days is None else {str(k): int(v) for k, v in days.items()},
        )


@dataclass(frozen=True)
class Model:
    """A versioned model: fitted parameters, or the defaults before any training. A learner
    whose model is more than numbers (Turing's trees) keeps it as text in ``blob``.

    ``target_version`` is the target its labels were of (#170): 2, the touch target, for a
    model fitted on samples that carry its terms (``FeatureVector.touch``); 1, the
    four-minute move, otherwise. None for the defaults (trained on nothing) and for models
    saved before it was recorded (they learned the four-minute move). The statuses and the
    consensus weights count only the predictions of models of the touch target."""

    version: str
    params: Mapping[str, float]
    trained: str | None = None  # what it was trained on
    trained_until: date | None = None  # the last day of its training data
    blob: str | None = field(default=None, repr=False)
    # The same as values (#165); None for the defaults and for models saved before it.
    training: TrainedOn | None = None
    target_version: int | None = None  # the target it learned (#170); None: see above

    def as_dict(self, *, blob: bool = False) -> dict[str, Any]:
        """As the Engine API shows it; with the blob (``blob=True``) as stored."""
        out: dict[str, Any] = {
            "version": self.version,
            "params": dict(self.params),
            "trained": self.trained,
            "trained_until": self.trained_until.isoformat() if self.trained_until else None,
            "training": None if self.training is None else self.training.as_dict(),
            "target_version": self.target_version,
        }
        if blob and self.blob is not None:
            out["blob"] = self.blob
        return out

    def to_json(self) -> str:
        return json.dumps(self.as_dict(blob=True))

    @classmethod
    def fitted(
        cls,
        name: str,
        params: Mapping[str, float],
        scored: Sequence[Sample],
        loss: float,
        blob: str | None = None,
    ) -> Model:
        """A model fitted on ``scored``, versioned by its last day and what went into it; what
        it was trained on, as text and as values, the days by where their labels came from
        where the samples say (``Sample.reference``), and the target of their labels (#170).
        A touch target model's version digests its target too: the same parameters learned on
        another target are another model."""
        first = min(s.bar_close for s in scored).date()
        last = max(s.bar_close for s in scored).date()
        target = target_of(scored)
        canonical = json.dumps(dict(params), sort_keys=True)
        if blob is not None:
            canonical += "|" + hashlib.sha256(blob.encode()).hexdigest()
        marked = f"|target {target}" if target != 1 else ""
        digest = hashlib.sha256(f"{canonical}|{first}|{last}|{len(scored)}{marked}".encode()).hexdigest()[:6]
        return cls(
            version=f"{name}-{last:%Y%m%d}-{digest}",
            params=dict(params),
            trained=f"{first}..{last}, {len(scored)} samples, log loss {loss:.4f}",
            trained_until=last,
            blob=blob,
            training=TrainedOn(first, last, len(scored), loss, _days_by_source(scored)),
            target_version=target,
        )

    @classmethod
    def from_json(cls, text: str) -> Model:
        return cls.from_dict(json.loads(text))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Model:
        """A stored model; one saved before a field was added has it None."""
        until = data.get("trained_until")
        training = data.get("training")
        target = data.get("target_version")
        return cls(
            str(data["version"]),
            {k: float(v) for k, v in data["params"].items()},
            data.get("trained"),
            date.fromisoformat(until) if until else None,
            data.get("blob"),
            None if training is None else TrainedOn.from_dict(training),
            None if target is None else int(target),
        )


def target_of(samples: Sequence[Sample]) -> int:
    """The target the samples' labels are of (#170): the touch target's when their features
    carry its terms (``FeatureVector.touch``, as ``forecasts.forecasting`` reads them), else
    the four-minute move's."""
    return TARGET_VERSION if any(s.features.touch is not None for s in samples) else 1


def _days_by_source(scored: Sequence[Sample]) -> dict[str, int] | None:
    """The days of ``scored`` by where their labels came from; None if no sample says."""
    sources = {s.bar_close.date(): LABEL_SOURCES[s.reference] for s in scored if s.reference in LABEL_SOURCES}
    if not sources:
        return None
    return {source: sum(kind == source for kind in sources.values()) for source in LABEL_SOURCES.values()}


@dataclass(frozen=True)
class Sample:
    """One bar close of training data, with what happened four minutes later."""

    symbol: str
    bar_close: datetime
    features: FeatureVector
    outcome: Direction | None  # None if the bars do not tell
    change_ticks: float | None  # the move behind the outcome, for algorithms that learn its size
    # The reference price its day was made with (#165): "mid" on a recorded day, "close" on a
    # backfilled one (``training.TrainingDay``); None when not made from a day of training data.
    reference: str | None = None


class Algorithm(Protocol):
    name: str
    card: Card
    features: tuple[str, ...]
    training_days: int  # the rolling window it trains on
    # Every parameter its model can hold, default or fitted: what it is, its unit, when known.
    param_meanings: Mapping[str, Meaning]

    def default_model(self) -> Model: ...

    def fit(self, samples: Sequence[Sample]) -> Model: ...

    def predict(self, features: FeatureVector, model: Model) -> Forecast: ...


def predicted(algorithm: Algorithm, features: FeatureVector, model: Model) -> Forecast | None:
    """``algorithm``'s forecast, or None where it does not predict (``NotPredicted``)."""
    try:
        return algorithm.predict(features, model)
    except NotPredicted:
        return None


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
