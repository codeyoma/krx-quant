"""Kind Registry: every kind the engine predicts (#179, #180, #183), in registration order.

The direction comes first, as it always ran (``kinds.DIRECTION_KIND``); then the range
(``ranges.RANGE``, #180); then the barrier (``barriers.BARRIER``, #183); then the barrier at
15, 30 and 60 minutes (``barriers.BARRIER_LADDER``, #263). A kind added later
goes after them, and its algorithms after theirs: the CPU budget skips them first. The kinds'
framework is ``kinds.py``; each kind's rules are its own module, built on it, so the list is
kept here.

The daily kinds (#264, ``daily.py``), predicted once a trading day rather than at each bar
close, are listed apart (``DAILY_KINDS``): the bar-close machinery reads ``KINDS`` only. The
first is ``open_to_close`` (``open_to_close.OPEN_TO_CLOSE``), then ``open_to_close_5d``, its window
of five sessions (#310, ``open_to_close_5d.OPEN_TO_CLOSE_5D``).
"""

from __future__ import annotations

from collections.abc import Sequence

from krx_quant.barriers import BARRIER, BARRIER_LADDER
from krx_quant.daily import DailyKind, register_daily_kinds
from krx_quant.kinds import DIRECTION_KIND, Kind, register_kinds
from krx_quant.open_to_close import OPEN_TO_CLOSE
from krx_quant.open_to_close_5d import OPEN_TO_CLOSE_5D
from krx_quant.ranges import RANGE

# The barrier's horizon ladder (#263: 15, 30 and 60 minutes) after the four-minute barrier.
KINDS: tuple[Kind, ...] = register_kinds([DIRECTION_KIND, RANGE, BARRIER, *BARRIER_LADDER])
# Predicted once a trading day (#264), apart from the bar-close kinds: the day's open to its close,
# then the five sessions' (#310).
DAILY_KINDS: tuple[DailyKind, ...] = register_daily_kinds([OPEN_TO_CLOSE, OPEN_TO_CLOSE_5D], KINDS)


def kind_named(name: str, kinds: Sequence[Kind] = KINDS) -> Kind | None:
    return next((k for k in kinds if k.name == name), None)


def daily_kind_named(name: str, kinds: Sequence[DailyKind] = DAILY_KINDS) -> DailyKind | None:
    return next((k for k in kinds if k.name == name), None)
