"""Registering algorithms: each needs a feature set and features of its own."""

from __future__ import annotations

import pytest

from krx_quant.algorithms import ALGORITHMS, RegistrationError, register
from krx_quant.algorithms.gauss import Gauss
from krx_quant.algorithms.granger import Granger
from krx_quant.algorithms.kalman import Kalman
from krx_quant.algorithms.markov import Markov
from krx_quant.algorithms.newton import Newton
from krx_quant.algorithms.poisson import Poisson
from krx_quant.algorithms.shannon import Shannon
from krx_quant.algorithms.turing import Turing
from krx_quant.features import BOOK_FEATURES, OPTION_FEATURES, SERIES_FEATURES, TAPE_FEATURES
from tests.algorithm_doubles import FixedAlgorithm


def test_algorithms_with_features_of_their_own_register_in_order() -> None:
    first = FixedAlgorithm("first", features=("velocity", "volatility"), unique=("velocity",))
    second = FixedAlgorithm("second", features=("acceleration", "volatility"), unique=("acceleration",))

    assert register([first, second]) == (first, second)


def test_an_algorithm_with_the_feature_set_of_a_registered_one_is_refused() -> None:
    first = FixedAlgorithm("first", features=("velocity", "volatility"), unique=("velocity",))
    twin = FixedAlgorithm("twin", features=("volatility", "velocity"), unique=("volatility",))

    with pytest.raises(RegistrationError, match="twin.*same features as first"):
        register([first, twin])


def test_an_algorithm_without_a_feature_of_its_own_is_refused() -> None:
    first = FixedAlgorithm("first", features=("velocity", "volatility"), unique=("velocity",))
    borrower = FixedAlgorithm("borrower", features=("velocity", "flat_ticks"), unique=("velocity",))
    nothing = FixedAlgorithm("nothing", features=("flat_ticks",), unique=())

    with pytest.raises(RegistrationError, match="borrower.*velocity.*first"):
        register([first, borrower])
    with pytest.raises(RegistrationError, match="nothing.*no feature of its own"):
        register([nothing])


def test_an_algorithm_must_declare_known_features_and_a_new_name() -> None:
    unknown = FixedAlgorithm("unknown", features=("telepathy",), unique=("telepathy",))
    first = FixedAlgorithm("first", features=("velocity",), unique=("velocity",))
    again = FixedAlgorithm("first", features=("acceleration",), unique=("acceleration",))
    stray = FixedAlgorithm("stray", features=("velocity",), unique=("acceleration",))

    with pytest.raises(RegistrationError, match="unknown.*telepathy"):
        register([unknown])
    with pytest.raises(RegistrationError, match="first.*already registered"):
        register([first, again])
    with pytest.raises(RegistrationError, match="stray.*acceleration.*not among its features"):
        register([stray])


def test_each_registered_algorithm_has_a_personality_of_its_own() -> None:
    assert [a.name for a in ALGORITHMS] == [
        "newton",
        "gauss",
        "granger",
        "markov",
        "kalman",
        "shannon",
        "poisson",
        "turing",
    ]
    assert not set(Gauss.card.unique_features) & set(Newton.features)
    assert not set(Granger.card.unique_features) & (set(Newton.features) | set(Gauss.features))
    earlier = set(Newton.features) | set(Gauss.features) | set(Granger.features)
    assert not set(Markov.card.unique_features) & earlier
    assert not set(Kalman.card.unique_features) & (earlier | set(Markov.features))
    before_shannon = earlier | set(Markov.features) | set(Kalman.features)
    assert not set(Shannon.card.unique_features) & before_shannon
    assert not set(Poisson.card.unique_features) & (before_shannon | set(Shannon.features))
    # Turing reads every number the others do (not their series, nor the book's, the
    # tape's and the options' until #55), and its own on top.
    others = before_shannon | set(Shannon.features) | set(Poisson.features)
    unread = SERIES_FEATURES | BOOK_FEATURES | TAPE_FEATURES | OPTION_FEATURES
    assert set(Turing.features) == (others - unread) | set(Turing.card.unique_features)
