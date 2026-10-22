"""Algorithm Registry: the algorithms the engine runs, checked for a personality of their own.

An algorithm is refused when its feature set is the same as a registered one's, when it
names no feature of its own (unique), or when one it calls its own is already used by a
registered algorithm. Later algorithms may share an earlier one's own features (Turing
uses them all), so the check runs in registration order. Order is also priority: when
the CPU budget runs short, the last registered is skipped first, whatever the ranking.

An algorithm of a kind besides the direction (#179, ``kinds.py``) is registered in
``KIND_ALGORITHMS``, after every direction algorithm: the CPU budget skips them first. Its
name is its own among every algorithm's; the feature checks above look only within its
kind (a range algorithm may call its own a feature a direction algorithm uses).

An algorithm of a daily kind (#264, ``daily.py``) is registered in ``DAILY_ALGORITHMS``, its
features of the daily features' registry (``daily_features.DAILY_FEATURES``).
"""

from __future__ import annotations

from collections.abc import Collection, Sequence

from krx_quant.algorithms.breiman import Breiman
from krx_quant.algorithms.contract import (
    Abstention,
    Algorithm,
    Diagnostics,
    KindAlgorithm,
    KindSample,
    Model,
    NothingToLearn,
    NotPredicted,
    Sample,
    kind_of,
    kind_predicted,
    load_models,
    model_path,
    predicted,
    save_model,
)
from krx_quant.algorithms.cox import Cox
from krx_quant.algorithms.cox5d import Cox5d
from krx_quant.algorithms.gauss import Gauss
from krx_quant.algorithms.granger import Granger
from krx_quant.algorithms.kalman import Kalman
from krx_quant.algorithms.mandelbrot import Mandelbrot
from krx_quant.algorithms.markov import Markov
from krx_quant.algorithms.newton import Newton
from krx_quant.algorithms.neyman import Neyman
from krx_quant.algorithms.poisson import Poisson
from krx_quant.algorithms.shannon import Shannon
from krx_quant.algorithms.turing import Turing
from krx_quant.barriers import BARRIER_LADDER
from krx_quant.cards import Card, Meaning
from krx_quant.daily import DailyAlgorithm, register_daily_algorithms
from krx_quant.daily_features import DAILY_FEATURES
from krx_quant.features import FEATURES
from krx_quant.kind_registry import DAILY_KINDS, KINDS
from krx_quant.kinds import DIRECTION, Kind


class RegistrationError(ValueError):
    pass


def register(algorithms: Sequence[Algorithm]) -> tuple[Algorithm, ...]:
    """Check ``algorithms`` in order; raises RegistrationError on the first refused."""
    _check(algorithms)
    return tuple(algorithms)


def register_kind_algorithms(
    algorithms: Sequence[KindAlgorithm], kinds: Sequence[Kind] = KINDS, taken: Collection[str] = ()
) -> tuple[KindAlgorithm, ...]:
    """Check the algorithms of the kinds besides the direction (#179), in order; raises
    RegistrationError on the first refused: a kind not among ``kinds`` (or the direction), a
    name taken (``taken``: the direction's algorithms', the baselines'), or, within its kind,
    ``register``'s checks."""
    known = {k.name for k in kinds if k.name != DIRECTION}
    names = set(taken)
    by_kind: dict[str, list[KindAlgorithm]] = {}
    for a in algorithms:
        if a.kind not in known:
            raise RegistrationError(
                f"{a.name}: kind {a.kind!r} is not a registered kind besides the direction"
            )
        if a.name in names:
            raise RegistrationError(f"{a.name}: already registered")
        names.add(a.name)
        by_kind.setdefault(a.kind, []).append(a)
    for group in by_kind.values():
        _check(group)
    return tuple(algorithms)


def _check(algorithms: Sequence[Algorithm | KindAlgorithm]) -> None:
    """The registration checks of algorithms of one kind, in order."""
    registered: list[Algorithm | KindAlgorithm] = []
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


# Turing reads every feature in the Feature Store but the series', the range's and the
# barrier's, so it comes last but one: an algorithm's own features must be added before it,
# or Turing's taking them refuses it. Neyman (#266) reads Turing's and the barrier's two, its
# own, after it; being last, it is the first skipped when the CPU budget runs short. Kalman
# (#25), Shannon (#26), Poisson (#27) and Neyman were registered after pre-validation existed:
# the engine starts each once it has passed.
ALGORITHMS: tuple[Algorithm, ...] = register(
    [Newton(), Gauss(), Granger(), Markov(), Kalman(), Shannon(), Poisson(), Turing(), Neyman()]
)

# The algorithms of the kinds besides the direction (#179), in the kinds' order, all after
# the direction's: the range's Mandelbrot (#181), the barrier's Breiman (#184), then a Breiman
# for each barrier of the horizon ladder (#263, breiman-15m, -30m, -60m), all registered after
# pre-validation existed (each runs once it has passed). The last is skipped first when the
# CPU budget runs short.
KIND_ALGORITHMS: tuple[KindAlgorithm, ...] = register_kind_algorithms(
    [Mandelbrot(), Breiman(), *(Breiman(k) for k in BARRIER_LADDER)], taken={a.name for a in ALGORITHMS}
)

# The algorithms of the daily kinds (#264), predicted once a trading day: open_to_close's Cox,
# then open_to_close_5d's Cox-5d (#310). Registered after pre-validation existed: each runs once it
# has passed (its walk-forward over the KRX days, ``daily_prevalidation``).
DAILY_ALGORITHMS: tuple[DailyAlgorithm, ...] = register_daily_algorithms(
    [Cox(), Cox5d()],
    DAILY_KINDS,
    taken={a.name for a in ALGORITHMS}
    | {a.name for a in KIND_ALGORITHMS}
    | {b.name for k in KINDS for b in k.baselines}
    | {b.name for k in DAILY_KINDS for b in k.daily_baselines},
    features=DAILY_FEATURES,
)

__all__ = [
    "DAILY_ALGORITHMS",
    "ALGORITHMS",
    "KIND_ALGORITHMS",
    "Abstention",
    "Algorithm",
    "Card",
    "Diagnostics",
    "KindAlgorithm",
    "KindSample",
    "Meaning",
    "Model",
    "NothingToLearn",
    "NotPredicted",
    "RegistrationError",
    "Sample",
    "kind_of",
    "kind_predicted",
    "load_models",
    "model_path",
    "predicted",
    "register",
    "register_kind_algorithms",
    "save_model",
]
