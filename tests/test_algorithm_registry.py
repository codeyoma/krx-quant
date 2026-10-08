"""Registering algorithms: each needs a feature set and features of its own."""

from __future__ import annotations

import pytest

from krx_quant.algorithms import RegistrationError, register
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
