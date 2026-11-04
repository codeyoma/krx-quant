"""The reviewed refactor of #518 (``revisions.SAME_REVISION``): Turing's and the Breimans' code
changed only to leave the stochastic processes' new features out of theirs, so each learns and
predicts as it did.

Input: the samples Turing's contract test and Breiman's own tests train on. Output: the same
features in the same order, and the same model and forecasts, from the code as it is and from the
rule before #518 run on the Feature Store before it (the stochastic processes' features came last).
"""

from __future__ import annotations

from datetime import date

from krx_quant.algorithms.breiman import Breiman
from krx_quant.algorithms.turing import Turing
from krx_quant.features import (
    BARRIER_FEATURES,
    FEATURES,
    RANGE_FEATURES,
    SERIES_FEATURES,
    STOCHASTIC_FEATURES,
)
from krx_quant.market_calendar import MarketCalendar
from krx_quant.prevalidation import code_digest
from krx_quant.revisions import SAME_REVISION
from krx_quant.training import samples
from tests.synthetic_bars import random_walk, trending
from tests.test_breiman import by_the_book, vector

FEE = 0.015 / 100
DAYS = [date(2026, 9, 21), date(2026, 9, 22)]
# The Feature Store before #518: the same registry without the features it added, which came last.
BEFORE = [n for n in FEATURES if n not in STOCHASTIC_FEATURES]
assert list(FEATURES)[: len(BEFORE)] == BEFORE


class TuringBefore(Turing):
    features = tuple(n for n in BEFORE if n not in SERIES_FEATURES | RANGE_FEATURES | BARRIER_FEATURES)


class BreimanBefore(Breiman):
    features = tuple(n for n in BEFORE if n not in SERIES_FEATURES | RANGE_FEATURES)


def test_their_features_are_those_before_518_in_the_same_order() -> None:
    assert Turing.features == TuringBefore.features
    assert Breiman.features == BreimanBefore.features
    assert not STOCHASTIC_FEATURES & set(Turing.features + Breiman.features)


def test_turing_learns_and_predicts_as_before() -> None:
    calendar = MarketCalendar(dict.fromkeys(DAYS, True))
    bar_days = {("005930", DAYS[0]): trending(DAYS[0]), ("005930", DAYS[1]): random_walk(DAYS[1])}
    training = samples(bar_days, calendar, Turing.features, FEE, target_version=3)

    now, before = Turing().fit(training), TuringBefore().fit(training)

    assert (now.version, now.blob, now.params) == (before.version, before.blob, before.params)
    for s in training[::25]:
        assert Turing().predict(s.features, now) == TuringBefore().predict(s.features, before)


def test_breiman_learns_and_predicts_as_before() -> None:
    training = by_the_book(1500)

    now, before = Breiman().fit(training), BreimanBefore().fit(training)

    assert (now.version, now.blob, now.params) == (before.version, before.blob, before.params)
    for balance in (-0.8, 0.0, 0.8):
        assert Breiman().predict(vector(top_imbalance=balance), now) == BreimanBefore().predict(
            vector(top_imbalance=balance), before
        )


def test_the_map_names_the_code_518_left() -> None:
    """Each entry maps the code before #518 to the code it left; the Breimans share one module.
    A later change of Turing's or Breiman's code is a revision of its own (#454): it drops that
    algorithm from this check, and the entry stays as the record of #518's."""
    entries = {r.algorithm: r for r in SAME_REVISION if r.evidence.startswith("#518")}
    assert entries.keys() == {"turing", "breiman", "breiman-15m", "breiman-30m", "breiman-60m"}
    assert entries["turing"].new == code_digest(Turing())
    assert {r.new for name, r in entries.items() if name != "turing"} == {code_digest(Breiman())}
