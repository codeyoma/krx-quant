"""Algorithm Registry: the algorithms the engine runs, checked for a personality of their own.

An algorithm is refused when its feature set is the same as a registered one's, when it
names no feature of its own (unique), or when one it calls its own is already used by a
registered algorithm. Later algorithms may share an earlier one's own features (Turing
uses them all), so the check runs in registration order. Order is also priority: when
the CPU budget runs short, the last registered is skipped first, whatever the ranking.
"""

from __future__ import annotations

from collections.abc import Sequence

from krx_quant.algorithms.contract import (
    Algorithm,
    Card,
    Model,
    Sample,
    load_models,
    model_path,
    save_model,
)
from krx_quant.algorithms.gauss import Gauss
from krx_quant.algorithms.granger import Granger
from krx_quant.algorithms.markov import Markov
from krx_quant.algorithms.newton import Newton
from krx_quant.features import FEATURES


class RegistrationError(ValueError):
    pass


def register(algorithms: Sequence[Algorithm]) -> tuple[Algorithm, ...]:
    """Check ``algorithms`` in order; raises RegistrationError on the first refused."""
    registered: list[Algorithm] = []
    for a in algorithms:
        unknown = sorted(set(a.features) - FEATURES.keys())
        if unknown:
            raise RegistrationError(f"{a.name}: unknown features {', '.join(unknown)}")
        if any(r.name == a.name for r in registered):
            raise RegistrationError(f"{a.name}: already registered")
        stray = sorted(set(a.card.unique_features) - set(a.features))
        if stray:
            raise RegistrationError(
                f"{a.name}: unique features {', '.join(stray)} are not among its features"
            )
        if not a.card.unique_features:
            raise RegistrationError(f"{a.name}: no feature of its own")
        for r in registered:
            if set(r.features) == set(a.features):
                raise RegistrationError(f"{a.name}: the same features as {r.name}")
            taken = sorted(set(a.card.unique_features) & set(r.features))
            if taken:
                raise RegistrationError(f"{a.name}: its own features {', '.join(taken)} are used by {r.name}")
        registered.append(a)
    return tuple(registered)


ALGORITHMS: tuple[Algorithm, ...] = register([Newton(), Gauss(), Granger(), Markov()])

__all__ = [
    "ALGORITHMS",
    "Algorithm",
    "Card",
    "Model",
    "RegistrationError",
    "Sample",
    "load_models",
    "model_path",
    "register",
    "save_model",
]
