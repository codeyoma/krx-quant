"""A kind of prediction for tests (#179), and stand-in algorithms of it.

``drift``: where the mid-point stood at the end of a bar close's four minutes against the bar
close's: below (``fell``), level (``held``) or above (``rose``). Its answer reads the last
usable quote the Market View followed in the window; on a day with one-minute bars only, the
last close of the four minutes against the bar close's. Its output has one field besides the
probabilities, ``drift_ticks``, within ±100 ticks. Its action classes are the moves, and its
own statistics the share of rises answered.

Its baselines: always level (``drift-held``) and the last window's answer goes on
(``drift-persist``, not predicted when that window has none).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from krx_quant.algorithms import Card, Meaning, Model, NothingToLearn
from krx_quant.algorithms.contract import KindSample
from krx_quant.features import FeatureVector, Value, Width
from krx_quant.kinds import Answer, Followed, Kind, KindForecast, KindInputs, KindScored
from krx_quant.reference import Bar
from tests.algorithm_doubles import spend, stand_in_card, stand_in_meanings

DRIFT = "drift"
CLASSES = ("fell", "held", "rose")


def drift_of(window: Followed) -> Answer:
    """``fell``, ``held`` or ``rose``: the last usable quote's mid-point against the bar close's."""
    if window.anchor is None or not window.quotes or not window.tick:
        return Answer(None, "no quotes", {"quotes": len(window.quotes)})
    last = window.quotes[-1]
    mid = (last.bid + last.ask) / 2
    label = "rose" if mid > window.anchor else "fell" if mid < window.anchor else "held"
    ticks = (mid - window.anchor) / window.tick
    return Answer(
        label, None, {"anchor": window.anchor, "last_mid": mid, "ticks": ticks, "quotes": len(window.quotes)}
    )


def drift_of_bars(
    closing: Bar, after: Sequence[Bar], fee_rate: float, *, width: Width | None = None
) -> Answer | None:
    """The last close of the four minutes against the bar close's; None without a trade then."""
    if not after:
        return None
    last = after[-1].close
    label = "rose" if last > closing.close else "fell" if last < closing.close else "held"
    return Answer(label, None, {"close": closing.close, "last_close": last})


def within_a_hundred(output: Mapping[str, float]) -> str | None:
    return None if abs(output["drift_ticks"]) <= 100 else f"drift_ticks {output['drift_ticks']} beyond ±100"


def share_of_rises(
    algorithm: str, records: Sequence[KindScored], baselines: Mapping[str, Sequence[KindScored]]
) -> dict[str, Any]:
    rises = sum(r.outcome == CLASSES.index("rose") for r in records)
    return {"rises": rises, "rise_share": rises / len(records) if records else None}


def _baseline_card(character: str) -> Card:
    return Card(None, character, "test", (), "nothing", "certain", "always")


class AlwaysHeld:
    name = "drift-held"
    version = "v1"
    card = _baseline_card("시험 기준: 늘 그대로")

    def predict(self, inputs: KindInputs) -> KindForecast:
        return KindForecast((0.0, 1.0, 0.0), {"drift_ticks": 0.0})


class LastDrift:
    name = "drift-persist"
    version = "v1"
    card = _baseline_card("시험 기준: 직전 4분")

    def predict(self, inputs: KindInputs) -> KindForecast | None:
        if inputs.previous is None or inputs.previous.label is None:
            return None
        sure = tuple(1.0 if c == inputs.previous.label else 0.0 for c in CLASSES)
        return KindForecast(
            (sure[0], sure[1], sure[2]), {"drift_ticks": float(inputs.previous.path["ticks"])}
        )


TEST_KIND = Kind(
    name=DRIFT,
    title="시험: 4분 끝 중간가",
    classes=CLASSES,
    class_titles=("내림", "그대로", "오름"),
    action=("fell", "rose"),
    answer_version=1,
    answer=drift_of,
    bar_answer=drift_of_bars,
    baselines=(AlwaysHeld(), LastDrift()),
    fields={"drift_ticks": Meaning("4분 끝 중간가 변화의 기대값", "틱", "봉 마감에 낸다")},
    check=within_a_hundred,
    stats=share_of_rises,
)

SURE_RISE = KindForecast((0.1, 0.2, 0.7), {"drift_ticks": 1.0})


class FixedKindAlgorithm:
    """Predicts ``forecast`` of the test kind after spending ``seconds`` (``spend``)."""

    kind = DRIFT
    training_days = 20

    def __init__(
        self,
        name: str,
        *,
        features: tuple[str, ...],
        unique: tuple[str, ...],
        forecast: KindForecast = SURE_RISE,
        seconds: float = 0.0,
    ) -> None:
        self.name = name
        self.features = features
        self.card = stand_in_card(name, "a fixed forecast of the drift", unique, "nothing")
        self.param_meanings = stand_in_meanings("samples")
        self._forecast = forecast
        self._seconds = seconds
        self.seen: list[dict[str, Value]] = []

    def default_model(self) -> Model:
        return Model(f"{self.name}-default", {})

    def fit(self, samples: Sequence[KindSample]) -> Model:
        labelled = [s for s in samples if s.label is not None]
        return Model.fitted(self.name, {"samples": float(len(labelled))}, labelled, 0.0)

    def predict(self, features: FeatureVector, model: Model) -> KindForecast:
        self.seen.append({name: features[name] for name in self.features})
        if self._seconds:
            spend(self._seconds)
        return self._forecast


class DriftFrequency:
    """Forecasts the drift classes' frequencies it was trained on (a floor keeps it unsure)."""

    kind = DRIFT
    training_days = 4

    def __init__(self, name: str, feature: str) -> None:
        self.name = name
        self.features: tuple[str, ...] = (feature,)
        self.card = stand_in_card(name, "the drift's class frequencies", (feature,), "counting")
        self.param_meanings = stand_in_meanings(*CLASSES)
        self.fitted_on: list[int] = []  # labelled samples per fit

    def default_model(self) -> Model:
        return Model(f"{self.name}-default", dict.fromkeys(CLASSES, 1 / 3))

    def fit(self, samples: Sequence[KindSample]) -> Model:
        labelled = [s for s in samples if s.label is not None]
        self.fitted_on.append(len(labelled))
        if not labelled:
            raise NothingToLearn("no answers")
        params = {c: sum(s.label == c for s in labelled) / len(labelled) for c in CLASSES}
        loss = -sum(math.log(max(params[s.label], 1e-6)) for s in labelled if s.label) / len(labelled)
        return Model.fitted(self.name, params, labelled, loss)

    def predict(self, features: FeatureVector, model: Model) -> KindForecast:
        p = [max(model.params[c], 0.01) for c in CLASSES]
        total = sum(p)
        return KindForecast((p[0] / total, p[1] / total, p[2] / total), {"drift_ticks": 0.0})
