"""Feature Store: named features, each computed once per symbol per bar close.

A feature is a function of a ``Snapshot``: the day's finished one-minute bars up to the
bar close and the reference price there. Live and replay build snapshots from the
engine's bars; training builds them from backfilled bars with the same code, so a
feature means the same thing in both. A snapshot holds nothing received after its bar
close, so no feature can see the future.

Algorithms get a ``FeatureVector`` holding only the features they declared.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from krx_quant.pricing import flat_ticks as flat_band
from krx_quant.pricing import tick_size
from krx_quant.reference import Bar

FeatureValue = float | None


@dataclass(frozen=True)
class Snapshot:
    symbol: str
    bar_close: datetime
    bars: Sequence[Bar]  # the day's finished bars, oldest first
    price: float | None  # the reference price at the bar close
    tick: int | None  # the tick at that price
    flat_ticks: int | None  # k: smaller moves are flat

    @classmethod
    def at(
        cls, symbol: str, bar_close: datetime, bars: Sequence[Bar], price: float | None, fee_rate: float
    ) -> Snapshot:
        """A snapshot with the tick and flat band of ``price``."""
        if price is None:
            return cls(symbol, bar_close, bars, None, None, None)
        return cls(symbol, bar_close, bars, price, tick_size(price), flat_band(price, fee_rate))


Feature = Callable[[Snapshot], FeatureValue]

# Bars the price-shape features are fitted on, and the window volume is compared with.
SHAPE_BARS = 6
RECENT_BARS = 5
VOLUME_BARS = 60
VOLATILITY_BARS = 20
# Fewer one-minute changes than this give no volatility.
MIN_CHANGES = 6


def _closes_in_ticks(s: Snapshot, n: int) -> tuple[list[float], list[float]] | None:
    """Minutes before the last bar and closes in ticks above it, for the last ``n`` bars."""
    if s.tick is None or n < 2 or len(s.bars) < n:
        return None
    bars = s.bars[-n:]
    last = bars[-1]
    minutes = [(b.start - last.start).total_seconds() / 60 for b in bars]
    ticks = [(b.close - last.close) / s.tick for b in bars]
    return minutes, ticks


def _quadratic(s: Snapshot) -> tuple[float, float] | None:
    """Slope and curvature at the last bar of a least-squares parabola through the closes:
    x(t) = c + v·t + a·t²/2, t in minutes from the last bar.
    """
    points = _closes_in_ticks(s, SHAPE_BARS)
    if points is None:
        return None
    ts, xs = points
    # Normal equations for (c, v, a/2).
    sums = [sum(t**p for t in ts) for p in range(5)]
    rhs = [sum(x * t**p for t, x in zip(ts, xs, strict=True)) for p in range(3)]
    matrix = [[sums[i + j] for j in range(3)] for i in range(3)]
    solution = _solve3(matrix, rhs)
    if solution is None:
        return None
    _, v, half_a = solution
    return v, 2 * half_a


def _solve3(m: list[list[float]], b: list[float]) -> list[float] | None:
    """Gaussian elimination for a 3×3 system; None if it is singular."""
    a = [row[:] + [rhs] for row, rhs in zip(m, b, strict=True)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            return None
        a[col], a[pivot] = a[pivot], a[col]
        for r in range(3):
            if r != col:
                factor = a[r][col] / a[col][col]
                a[r] = [x - factor * y for x, y in zip(a[r], a[col], strict=True)]
    return [a[i][3] / a[i][i] for i in range(3)]


def velocity(s: Snapshot) -> FeatureValue:
    """How fast the price moves at the bar close, in ticks per minute."""
    shape = _quadratic(s)
    return None if shape is None else shape[0]


def acceleration(s: Snapshot) -> FeatureValue:
    """How fast that speed changes, in ticks per minute squared."""
    shape = _quadratic(s)
    return None if shape is None else shape[1]


def relative_volume(s: Snapshot) -> FeatureValue:
    """Volume per bar of the last few bars over that of the last hour: 1 is usual."""
    if len(s.bars) < 2 * RECENT_BARS:
        return None
    hour = s.bars[-VOLUME_BARS:]
    usual = sum(b.volume for b in hour) / len(hour)
    if usual <= 0:
        return None
    return sum(b.volume for b in s.bars[-RECENT_BARS:]) / RECENT_BARS / usual


def volatility(s: Snapshot) -> FeatureValue:
    """Standard deviation of one-minute close changes, in ticks."""
    points = _closes_in_ticks(s, min(len(s.bars), VOLATILITY_BARS + 1))
    if points is None or len(points[1]) < MIN_CHANGES + 1:
        return None
    changes = [b - a for a, b in zip(points[1], points[1][1:], strict=False)]
    mean = sum(changes) / len(changes)
    return math.sqrt(sum((c - mean) ** 2 for c in changes) / (len(changes) - 1))


def flat_ticks(s: Snapshot) -> FeatureValue:
    return None if s.flat_ticks is None else float(s.flat_ticks)


FEATURES: dict[str, Feature] = {
    "velocity": velocity,
    "acceleration": acceleration,
    "relative_volume": relative_volume,
    "volatility": volatility,
    "flat_ticks": flat_ticks,
}


class UndeclaredFeature(LookupError):
    """Not a KeyError: ``get`` and ``in`` must not quietly answer for an undeclared feature."""


class FeatureVector(Mapping[str, FeatureValue]):
    """The features an algorithm declared; asking for any other raises UndeclaredFeature."""

    def __init__(self, values: Mapping[str, FeatureValue], declared: Iterable[str]) -> None:
        self._declared = frozenset(declared)
        self._values = {name: values[name] for name in self._declared}

    def __getitem__(self, name: str) -> FeatureValue:
        if name not in self._declared:
            raise UndeclaredFeature(f"feature {name!r} was not declared")
        return self._values[name]

    def __contains__(self, name: object) -> bool:
        if name not in self._declared:
            raise UndeclaredFeature(f"feature {name!r} was not declared")
        return True

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


class FeatureStore:
    def __init__(self, features: Mapping[str, Feature] = FEATURES) -> None:
        self._features = features

    def compute(self, snapshot: Snapshot, names: Iterable[str]) -> dict[str, FeatureValue]:
        """Each named feature once, for every algorithm that declared it."""
        return {name: self._features[name](snapshot) for name in set(names)}
