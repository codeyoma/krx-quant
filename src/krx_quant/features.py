"""Feature Store: named features, each computed once per symbol per bar close.

A feature is a function of a ``Snapshot``: the day's finished one-minute bars up to the
bar close, the reference price there, and when the session opened. Live and replay build
snapshots from the engine's bars; training builds them from backfilled bars with the same
code, so a feature means the same thing in both. A snapshot holds nothing received after its bar
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
from krx_quant.reference import BAR, Bar

FeatureValue = float | None


@dataclass(frozen=True)
class Snapshot:
    symbol: str
    bar_close: datetime
    bars: Sequence[Bar]  # the day's finished bars, oldest first
    price: float | None  # the reference price at the bar close
    tick: int | None  # the tick at that price
    flat_ticks: int | None  # k: smaller moves are flat
    # Continuous trading started: the bars before it are the NXT pre-market's. None only if
    # the day has no session (the calendar changed under the engine): then every bar counts.
    session_open: datetime | None

    @classmethod
    def at(
        cls,
        symbol: str,
        bar_close: datetime,
        bars: Sequence[Bar],
        price: float | None,
        fee_rate: float,
        *,
        session_open: datetime | None,
    ) -> Snapshot:
        """A snapshot with the tick and flat band of ``price``."""
        if price is None:
            return cls(symbol, bar_close, bars, None, None, None, session_open)
        return cls(symbol, bar_close, bars, price, tick_size(price), flat_band(price, fee_rate), session_open)


Feature = Callable[[Snapshot], FeatureValue]

# Bars the price-shape features are fitted on, and the window volume is compared with.
SHAPE_BARS = 6
RECENT_BARS = 5
VOLUME_BARS = 60
VOLATILITY_BARS = 20
# Fewer one-minute changes than this give no volatility.
MIN_CHANGES = 6
# Session bars the VWAP needs before a gap from it means anything.
VWAP_MIN_BARS = 5
# One-minute moves the reversion speed is fitted on, and the fewest it needs.
REVERSION_MOVES = 30
MIN_REVERSION_MOVES = 20
# Per minute: a gap left at 5% a minute later. A price that overshoots the VWAP gets this.
MAX_REVERSION_SPEED = 3.0


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


def _session_bars(s: Snapshot) -> Sequence[Bar]:
    """The bars from the open: the pre-market's thin trades do not set the day's level."""
    if s.session_open is None:
        return s.bars
    return [b for b in s.bars if b.start >= s.session_open]


def _typical(b: Bar) -> float:
    return (b.high + b.low + b.close) / 3


def _running_vwap(bars: Sequence[Bar]) -> list[tuple[Bar, float]]:
    """Each bar with the VWAP (of typical prices) as it stood at its close; bars before the
    first volume are left out.
    """
    out: list[tuple[Bar, float]] = []
    value = volume = 0.0
    for b in bars:
        value += _typical(b) * b.volume
        volume += b.volume
        if volume > 0:
            out.append((b, value / volume))
    return out


def _vwap_band(s: Snapshot) -> tuple[float, float] | None:
    """The session's VWAP, and the volume-weighted standard deviation of the traded prices
    around it, in won.

    A bar's trades are taken to spread evenly over its range: each adds its typical price's
    distance from the VWAP and its range's own spread, (high − low)² / 12.
    """
    bars = _session_bars(s)
    running = _running_vwap(bars)
    if len(bars) < VWAP_MIN_BARS or not running:
        return None
    vwap = running[-1][1]
    volume = sum(b.volume for b in bars)
    variance = sum(b.volume * ((_typical(b) - vwap) ** 2 + (b.high - b.low) ** 2 / 12) for b in bars) / volume
    return vwap, math.sqrt(variance)


def vwap_gap(s: Snapshot) -> FeatureValue:
    """How far the price stands from the session's VWAP, in ticks (above is positive)."""
    band = _vwap_band(s)
    if band is None or s.price is None or s.tick is None:
        return None
    vwap, _ = band
    return (s.price - vwap) / s.tick


def vwap_z(s: Snapshot) -> FeatureValue:
    """That gap in standard deviations of the session's prices around the VWAP. None while
    they have spread less than half a tick: the session has not moved yet.
    """
    band = _vwap_band(s)
    if band is None or s.price is None or s.tick is None:
        return None
    vwap, deviation = band
    return None if deviation < s.tick / 2 else (s.price - vwap) / deviation


def ou_speed(pairs: Sequence[tuple[float, float]], minutes: float) -> float:
    """The Ornstein–Uhlenbeck speed κ (per minute) fitted by least squares to pairs of a gap
    from the mean and the price's move over the next ``minutes``: move = −gap·(1 − e^(−κ·minutes)).

    0 when the moves widened the gaps on the whole (or there are none); at most
    MAX_REVERSION_SPEED, when they overshot the mean.
    """
    squares = sum(gap * gap for gap, _ in pairs)
    if squares <= 0:
        return 0.0
    shrink = -sum(gap * move for gap, move in pairs) / squares  # 1 − e^(−κ·minutes)
    if shrink <= 0:
        return 0.0
    shrink = min(shrink, 1 - math.exp(-MAX_REVERSION_SPEED * minutes))
    return -math.log1p(-shrink) / minutes


def reversion_speed(s: Snapshot) -> FeatureValue:
    """How fast the price has closed its gaps from the VWAP today, per minute: the OU speed
    fitted to the last 30 one-minute moves of the close, each against its gap from the VWAP
    as it stood a minute before.

    Only the price's moves count: the VWAP catching up with the price is not reversion. A
    move across minutes without a bar (no trades, or a feed gap) is not a one-minute move
    and is left out. None with fewer than 20 moves, or while the closes have not strayed
    half a tick from the VWAP.
    """
    if s.tick is None:
        return None
    running = _running_vwap(_session_bars(s))
    pairs = [
        (b.close - vwap, later.close - b.close)
        for (b, vwap), (later, _) in zip(running, running[1:], strict=False)
        if later.start - b.start == BAR
    ][-REVERSION_MOVES:]
    if len(pairs) < MIN_REVERSION_MOVES or max(abs(gap) for gap, _ in pairs) < s.tick / 2:
        return None
    return ou_speed(pairs, 1.0)


def flat_ticks(s: Snapshot) -> FeatureValue:
    return None if s.flat_ticks is None else float(s.flat_ticks)


FEATURES: dict[str, Feature] = {
    "velocity": velocity,
    "acceleration": acceleration,
    "relative_volume": relative_volume,
    "volatility": volatility,
    "flat_ticks": flat_ticks,
    "vwap_gap": vwap_gap,
    "vwap_z": vwap_z,
    "reversion_speed": reversion_speed,
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
