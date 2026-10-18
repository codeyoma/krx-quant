"""Kind Registry: every kind the engine predicts (#179, #180, #183), in registration order.

The direction comes first, as it always ran (``kinds.DIRECTION_KIND``); then the range
(``ranges.RANGE``, #180); then the barrier (``barriers.BARRIER``, #183); then the barrier at
15, 30 and 60 minutes (``barriers.BARRIER_LADDER``, #263). A kind added later
goes after them, and its algorithms after theirs: the CPU budget skips them first. The kinds'
framework is ``kinds.py``; each kind's rules are its own module, built on it, so the list is
kept here.
"""

from __future__ import annotations

from collections.abc import Sequence

from krx_quant.barriers import BARRIER, BARRIER_LADDER
from krx_quant.kinds import DIRECTION_KIND, Kind, register_kinds
from krx_quant.ranges import RANGE

# The barrier's horizon ladder (#263: 15, 30 and 60 minutes) after the four-minute barrier.
KINDS: tuple[Kind, ...] = register_kinds([DIRECTION_KIND, RANGE, BARRIER, *BARRIER_LADDER])


def kind_named(name: str, kinds: Sequence[Kind] = KINDS) -> Kind | None:
    return next((k for k in kinds if k.name == name), None)
