"""Feature Store: named features, each computed once per symbol per bar close.

A feature is a function of a ``Snapshot``: the day's finished one-minute bars up to the
bar close, the reference price there, when the session opened, the bars of the series
that may lead the symbol (``Leaders``), and the day's macro regime (``macro.py``). Live
and replay build snapshots from the engine's Market View; training builds them from
recorded events through the same view, or from backfilled bars, so a feature means the
same thing in both. A snapshot holds nothing received after its bar close, so no feature
can see the future.

A feature is a number (or None, unknown), or a series of numbers (``SERIES_FEATURES``,
read with ``FeatureVector.series``). Algorithms get a ``FeatureVector`` holding only the
features they declared.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import cached_property

from krx_quant.config import Settings
from krx_quant.contracts import KOSPI200, USD
from krx_quant.feeds.market import us_ticker
from krx_quant.pricing import flat_ticks as flat_band
from krx_quant.pricing import tick_size
from krx_quant.reference import BAR, Bar
from krx_quant.regime import Regime

FeatureValue = float | None
Series = tuple[float, ...]
Value = FeatureValue | Series

# The macro regimes (regime.py) as the numbers a feature holds: shock 0, risk_off 1,
# risk_on 2, neutral 3.
REGIME_CODES = {r.value: float(n) for n, r in enumerate(Regime)}


def regime_code(regime: str) -> float | None:
    """A regime's code; None for a name this engine does not know (it counts as unknown)."""
    return REGIME_CODES.get(regime)


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
    # The day's finished bars of each lead group's members (``Leaders.groups``).
    leaders: Mapping[str, Sequence[Sequence[Bar]]] = field(default_factory=dict)
    regime: str | None = None  # the day's macro regime, decided before the open; None unknown

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
        leaders: Mapping[str, Sequence[Sequence[Bar]]] | None = None,
        regime: str | None = None,
    ) -> Snapshot:
        """A snapshot with the tick and flat band of ``price``."""
        lead = leaders or {}
        if price is None:
            return cls(symbol, bar_close, bars, None, None, None, session_open, lead, regime)
        tick, band = tick_size(price), flat_band(price, fee_rate)
        return cls(symbol, bar_close, bars, price, tick, band, session_open, lead, regime)

    @cached_property
    def lead_moves(self) -> dict[str, list[FeatureValue]]:
        """Each lead group's moves, 1 to 3 minutes back (computed once, for every feature)."""
        return {group: _group_moves(self.leaders.get(group, []), self.bar_close) for group in LEAD_GROUPS}

    @cached_property
    def own_moves(self) -> list[FeatureValue]:
        return _moves(self.bars, self.bar_close) or [None] * len(LEAD_LAGS)


Feature = Callable[[Snapshot], Value]

# Bars the price-shape features are fitted on, and the window volume is compared with.
SHAPE_BARS = 6
RECENT_BARS = 5
VOLUME_BARS = 60
VOLATILITY_BARS = 20
# Fewer one-minute changes than this give no volatility.
MIN_CHANGES = 6
# One-minute moves a series of them keeps: the last hour.
MINUTE_MOVES = 60
# Session bars the VWAP needs before a gap from it means anything.
VWAP_MIN_BARS = 5
# One-minute moves the reversion speed is fitted on, and the fewest it needs.
REVERSION_MOVES = 30
MIN_REVERSION_MOVES = 20
# Per minute: a gap left at 5% a minute later. A price that overshoots the VWAP gets this.
MAX_REVERSION_SPEED = 3.0
# The series whose moves may come before a symbol's (Granger): the other predicted symbols,
# KOSPI200 and USD futures, the market indices, US daytime trades and the KRX-listed US
# ETFs. A group's move is the mean of its members'.
LEAD_GROUPS = ("peers", "kospi200f", "usdf", "indices", "us", "etfs")
LEAD_LAGS = (1, 2, 3)  # minutes back
LEAD_WINDOW = 20  # one-minute moves a series' latest are measured against
LEAD_STALE = timedelta(minutes=10)  # a series silent this long says nothing


@dataclass(frozen=True)
class Leaders:
    """Which series lead, besides the other predicted symbols and the two futures."""

    indices: tuple[str, ...] = ()  # index codes
    us: tuple[str, ...] = ()  # US daytime tickers
    etfs: tuple[str, ...] = ()  # KRX-listed ETF codes

    @classmethod
    def of(cls, settings: Settings) -> Leaders:
        us = tuple(us_ticker(symbol) for symbol in settings.us_symbols)
        etfs = tuple(e for e in settings.etf_symbols if e not in settings.symbols)
        return cls(settings.index_codes, us, etfs)

    @property
    def series(self) -> tuple[str, ...]:
        """The leading series' names, other than the predicted symbols."""
        return (KOSPI200.series, USD.series, *self.indices, *self.us, *self.etfs)

    def groups(
        self, symbol: str, stocks: Mapping[str, Sequence[Bar]], series: Mapping[str, Sequence[Bar]]
    ) -> dict[str, list[Sequence[Bar]]]:
        """The lead groups of ``symbol``: the bars of each member, by group."""
        return {
            "peers": [bars for s, bars in stocks.items() if s != symbol],
            "kospi200f": [series.get(KOSPI200.series, [])],
            "usdf": [series.get(USD.series, [])],
            "indices": [series.get(code, []) for code in self.indices],
            "us": [series.get(ticker, []) for ticker in self.us],
            "etfs": [series.get(etf, []) for etf in self.etfs],
        }


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
    solution = solve(matrix, rhs)
    if solution is None:
        return None
    _, v, half_a = solution
    return v, 2 * half_a


def solve(m: Sequence[Sequence[float]], b: Sequence[float]) -> list[float] | None:
    """Gaussian elimination for a square system; None if it is singular."""
    n = len(b)
    a = [list(row) + [rhs] for row, rhs in zip(m, b, strict=True)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            return None
        a[col], a[pivot] = a[pivot], a[col]
        for r in range(n):
            if r != col:
                factor = a[r][col] / a[col][col]
                a[r] = [x - factor * y for x, y in zip(a[r], a[col], strict=True)]
    return [a[i][n] / a[i][i] for i in range(n)]


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


def minute_ticks(s: Snapshot) -> Series:
    """The session's one-minute close changes in ticks (of the reference price), the last
    hour of them, oldest first. A minute without a trade is a change of 0, up to the bar
    close.
    """
    bars = _session_bars(s)
    if s.tick is None or not bars:
        return ()
    moves: list[float] = []
    for before, after in zip(bars, bars[1:], strict=False):
        quiet = int((after.start - before.start) / BAR) - 1
        moves += [0.0] * quiet + [(after.close - before.close) / s.tick]
    moves += [0.0] * int((s.bar_close - bars[-1].start - BAR) / BAR)  # quiet since the last bar
    return tuple(moves[-MINUTE_MOVES:])


def macro_regime(s: Snapshot) -> FeatureValue:
    """The day's macro regime as a number (``REGIME_CODES``); None when it is unknown."""
    return regime_code(s.regime) if s.regime is not None else None


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


def _moves(bars: Sequence[Bar], at: datetime) -> list[FeatureValue] | None:
    """A series' one-minute log returns 1 to 3 minutes before ``at``, each in standard
    deviations of its last 20; None if it has not traded for 10 minutes, or if its bars
    reach back over fewer than 6 of those minutes.

    A minute without a bar is no move: the last close carries over. Returns too old for the
    series' bars are left out. A series that has not moved at all moved 0.
    """
    if not bars or at - (bars[-1].start + BAR) > LEAD_STALE:
        return None
    closes: list[float | None] = []
    i = len(bars) - 1
    for k in range(LEAD_WINDOW + 1):
        minute = at - k * BAR
        while i >= 0 and bars[i].start + BAR > minute:
            i -= 1
        closes.append(bars[i].close if i >= 0 else None)
    returns = [
        math.log(now / before) if now and before else None
        for now, before in zip(closes, closes[1:], strict=False)
    ]
    known = [r for r in returns if r is not None]
    if len(known) < MIN_CHANGES:
        return None
    mean = sum(known) / len(known)
    deviation = math.sqrt(sum((r - mean) ** 2 for r in known) / (len(known) - 1))
    latest = returns[: len(LEAD_LAGS)]
    if deviation == 0:
        return [None if r is None else 0.0 for r in latest]
    return [None if r is None else r / deviation for r in latest]


def _group_moves(members: Sequence[Sequence[Bar]], at: datetime) -> list[FeatureValue]:
    """The mean move of the members with one, at each lag; None where none has one."""
    each = [m for m in (_moves(bars, at) for bars in members) if m is not None]
    out: list[FeatureValue] = []
    for lag in range(len(LEAD_LAGS)):
        known = [m[lag] for m in each if m[lag] is not None]
        out.append(sum(v for v in known if v is not None) / len(known) if known else None)
    return out


def own_feature(lag: int) -> str:
    return f"return_{lag}"


def lead_feature(group: str, lag: int) -> str:
    return f"lead_{group}_{lag}"


def _lead(group: str, lag: int) -> Feature:
    def feature(s: Snapshot) -> FeatureValue:
        return s.lead_moves[group][lag - 1]

    feature.__doc__ = f"The {group} group's move {lag} minute(s) before the bar close."
    return feature


def _own(lag: int) -> Feature:
    def feature(s: Snapshot) -> FeatureValue:
        return s.own_moves[lag - 1]

    feature.__doc__ = f"The symbol's own move {lag} minute(s) before the bar close."
    return feature


FEATURES: dict[str, Feature] = {
    "velocity": velocity,
    "acceleration": acceleration,
    "relative_volume": relative_volume,
    "volatility": volatility,
    "flat_ticks": flat_ticks,
    "vwap_gap": vwap_gap,
    "vwap_z": vwap_z,
    "reversion_speed": reversion_speed,
    "minute_ticks": minute_ticks,
    "macro_regime": macro_regime,
    **{own_feature(lag): _own(lag) for lag in LEAD_LAGS},
    **{lead_feature(group, lag): _lead(group, lag) for group in LEAD_GROUPS for lag in LEAD_LAGS},
}
LEAD_FEATURES = frozenset(lead_feature(group, lag) for group in LEAD_GROUPS for lag in LEAD_LAGS)
SERIES_FEATURES = frozenset({"minute_ticks"})  # the features that are series of numbers


class UndeclaredFeature(LookupError):
    """Not a KeyError: ``get`` and ``in`` must not quietly answer for an undeclared feature."""


class FeatureVector(Mapping[str, FeatureValue]):
    """The features an algorithm declared; asking for any other raises UndeclaredFeature.
    A number is read by name, a series with ``series``."""

    def __init__(self, values: Mapping[str, Value], declared: Iterable[str]) -> None:
        source: Mapping[str, Value] = values._values if isinstance(values, FeatureVector) else values
        self._declared = frozenset(declared)
        self._values: dict[str, Value] = {name: source[name] for name in self._declared}

    def __getitem__(self, name: str) -> FeatureValue:
        value = self._value(name)
        if isinstance(value, tuple):
            raise TypeError(f"feature {name!r} is a series: read it with series()")
        return value

    def series(self, name: str) -> Series:
        value = self._value(name)
        if not isinstance(value, tuple):
            raise TypeError(f"feature {name!r} is not a series")
        return value

    def as_dict(self) -> dict[str, Value]:
        """Every declared feature's value, numbers and series alike."""
        return dict(self._values)

    def _value(self, name: str) -> Value:
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

    def compute(self, snapshot: Snapshot, names: Iterable[str]) -> dict[str, Value]:
        """Each named feature once, for every algorithm that declared it."""
        return {name: self._features[name](snapshot) for name in set(names)}
