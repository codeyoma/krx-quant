"""Neyman's pre-validation diagnostics (#283): what its report measures besides the checks,
reported and never judged (``contract.Diagnostics``, ``Neyman.diagnostics``). They are the
numbers #283 sets the next changes to Neyman by; nothing here changes how it learns or
forecasts: the fit is Neyman's (``neyman.fitted``), the forecasts its ``_second_opinion``. This
module is outside Neyman's package, so a change to it alone does not ask for a new
pre-validation (``prevalidation.code_digest`` reads the package).

- **Flips.** On each replayed day's bar closes (the samples training reads of that day, with
  their touch labels), with the model that predicted the day: how many of the first model's up
  and down calls the second opinion keeps, turns to flat, or turns to the opposite side (a
  flip: 0.35/0.05/0.60 is an up call, and at m = 0.27 down becomes 0.64), and how often each
  final call matched the answer. An up and down tied above flat (0.4/0.2/0.4) is called up
  (``pricing.most_likely``: a tie goes to flat, then up), so the second model judges it as an
  up call; the ties are counted with where they went.
- **First model.** The distribution of the first model's probabilities the second model learned
  from (out of sample: the fold models, trained on 60–75% of the days) against the final first
  model's on the same samples (in sample, trained on all of them) and on the day it then
  predicted (live: what the second model is applied to), by symbol: the quantiles of
  p_up − p_down and of the largest class's probability, the share of up and down calls, the
  Kolmogorov–Smirnov distance of each from the out-of-sample one, and the mean absolute
  difference of the out-of-sample and in-sample values of the same samples.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np

from krx_quant.algorithms.contract import Model, Sample
from krx_quant.algorithms.forecasts import number
from krx_quant.algorithms.neyman import _normalized, _second_opinion, _trees, fitted
from krx_quant.feeds.base import kst_day
from krx_quant.pricing import Direction, most_likely

ISSUE = 283
OUT_OF_FOLD, IN_SAMPLE, LIVE = "out_of_fold", "in_sample", "live"
SOURCES = (OUT_OF_FOLD, IN_SAMPLE, LIVE)
ALL = "all"
QUANTILES = (0.05, 0.25, 0.5, 0.75, 0.95)
# The bins the distributions are kept in, 0.005 wide: their quantiles and distances are read to
# that, and they take the same room however many days are replayed.
SPREAD_EDGES = np.linspace(-1.0, 1.0, 401)  # p_up − p_down
TOP_EDGES = np.linspace(0.0, 1.0, 201)  # the largest class's probability
_DIGITS = 4


class Measures:
    """Neyman's diagnostics over one pre-validation: each fit's first-model probabilities, then
    each replayed day's calls and probabilities."""

    def __init__(self) -> None:
        self._calls: dict[str, _Calls] = {}
        self._first: dict[str, _First] = {}
        self._fits = 0
        self._days = 0

    def fit(self, samples: Sequence[Sample]) -> Model:
        """Neyman's fit (the same model), keeping its first models' probabilities."""
        found = fitted(samples)
        held = ~np.isnan(found.out_of_fold).any(axis=1)
        if held.any():
            self._fits += 1
        symbols = np.array([s.symbol for s in found.scored])
        for symbol in sorted(set(symbols[held])):
            mine = held & (symbols == symbol)
            self._first_of(symbol).fitted(_rows(found.out_of_fold[mine]), _rows(found.in_sample[mine]))
        return found.model

    def day(self, day: date, model: Model, samples: Sequence[Sample]) -> None:
        """``day``'s bar closes as ``model`` forecast them: its calls, flips and first model."""
        scored = [s for s in samples if s.outcome is not None]
        if model.blob is None or not scored:
            return  # its defaults: no trees, no calls
        trees = _trees(model)
        x = np.array([[number(s.features[n]) for n in trees.feature_name()] for s in scored], dtype=float)
        raw = np.asarray(trees.predict(x, num_threads=1), dtype=float).reshape(-1, 3)
        self._days += 1
        symbols = np.array([s.symbol for s in scored])
        for symbol in sorted(set(symbols)):
            self._first_of(symbol).sources[LIVE].add(_rows(raw[symbols == symbol]))
        for p, sample in zip(raw, scored, strict=True):  # as ``Neyman.predict`` takes them
            self._calls.setdefault(sample.symbol, _Calls()).add(p, sample, model.params)

    def report(self) -> dict[str, Any]:
        symbols = sorted(set(self._calls) | set(self._first))
        calls = {s: self._calls.get(s, _Calls()) for s in symbols}
        first = {s: self._first.get(s, _First()) for s in symbols}
        return {
            "issue": ISSUE,
            "judged": False,
            "flips": {
                "tie_rule": "up",
                "by_symbol": {s: c.as_dict() for s, c in calls.items()},
                ALL: _Calls.merged(calls.values()).as_dict(),
            },
            "first_model": {
                "fits": self._fits,
                "days": self._days,
                "by_symbol": {s: f.as_dict() for s, f in first.items()},
                ALL: _First.merged(first.values()).as_dict(),
            },
        }

    def lines(self) -> list[str]:
        report = self.report()
        flips, first = report["flips"], report["first_model"]
        out = [
            f"neyman diagnostics (#{ISSUE}; reported, not judged): {first['days']} replayed days"
            f" predicted by trees, {first['fits']} fits with out-of-sample probabilities"
        ]
        for symbol, c in [*flips["by_symbol"].items(), (ALL, flips[ALL])]:
            out.append(
                f"  flips {symbol}: {c['calls']} up/down calls of {c['scored']} scored;"
                f" kept {c['kept']} (precision {_text(c['kept_precision'])}), to flat {c['to_flat']},"
                f" to the opposite side {c['flipped']} (precision {_text(c['flipped_precision'])},"
                f" the first side right {_text(c['flipped_first_side_right'])});"
                f" kept on the {c['flip_days']} days with a flip {_text(c['kept_precision_on_flip_days'])};"
                f" up/down ties {c['ties']} (called up; then up {c['ties_up']}, flat {c['ties_flat']},"
                f" down {c['ties_down']})"
            )
        for symbol, f in [*first["by_symbol"].items(), (ALL, first[ALL])]:
            for measure, title in (("up_minus_down", "p_up - p_down"), ("largest", "largest class")):
                m = f[measure]
                spread = ", ".join(f"{_short(source)} {_spread(m[source])}" for source in SOURCES)
                out.append(
                    f"  first model {symbol}, {title} p05/p50/p95: {spread};"
                    f" KS from out of fold: in sample {_text(m['ks_in_sample'])}, live {_text(m['ks_live'])};"
                    f" mean |out of fold - in sample| {_text(m['mean_abs_diff_in_sample'])}"
                )
            shares = ", ".join(f"{_short(source)} {_text(f['call_share'][source])}" for source in SOURCES)
            counts = ", ".join(f"{_short(source)} {f['n'][source]}" for source in SOURCES)
            out.append(f"  first model {symbol}, up/down call share: {shares} (samples: {counts})")
        return out

    def _first_of(self, symbol: str) -> _First:
        return self._first.setdefault(symbol, _First())


def _rows(p: np.ndarray) -> np.ndarray:
    """The first model's probabilities (down, flat, up), each row summing to 1 (``_normalized``)."""
    rows = np.asarray(p, dtype=float).reshape(-1, 3)
    out: np.ndarray = rows / rows.sum(axis=1, keepdims=True)
    return out


@dataclass
class _Histogram:
    edges: np.ndarray
    counts: np.ndarray = field(init=False)

    def __post_init__(self) -> None:
        self.counts = np.zeros(len(self.edges) - 1, dtype=np.int64)

    @property
    def n(self) -> int:
        return int(self.counts.sum())

    def add(self, values: np.ndarray) -> None:
        clipped = np.clip(values, self.edges[0], self.edges[-1])
        self.counts += np.histogram(clipped, self.edges)[0]

    def cdf(self) -> np.ndarray:
        return np.cumsum(self.counts) / self.n

    def quantile(self, q: float) -> float | None:
        """The ``q`` quantile, linear within its bin; None without values."""
        if not self.n:
            return None
        cumulative = np.cumsum(self.counts)
        target = q * self.n
        i = min(int(np.searchsorted(cumulative, target)), len(self.counts) - 1)
        before = float(cumulative[i - 1]) if i else 0.0
        inside = float(self.counts[i])
        share = (target - before) / inside if inside else 0.0
        return float(self.edges[i] + share * (self.edges[i + 1] - self.edges[i]))

    def merged(self, other: _Histogram) -> _Histogram:
        out = _Histogram(self.edges)
        out.counts = self.counts + other.counts
        return out


def ks(a: _Histogram, b: _Histogram) -> float | None:
    """The Kolmogorov–Smirnov distance of two distributions, at their bins' edges; None if
    either is empty."""
    if not a.n or not b.n:
        return None
    return float(np.max(np.abs(a.cdf() - b.cdf())))


@dataclass
class _Source:
    """One source's first-model probabilities: their spread, largest class and calls."""

    spread: _Histogram = field(default_factory=lambda: _Histogram(SPREAD_EDGES))
    top: _Histogram = field(default_factory=lambda: _Histogram(TOP_EDGES))
    calls: int = 0

    def add(self, p: np.ndarray) -> None:
        self.spread.add(p[:, 2] - p[:, 0])
        self.top.add(p.max(axis=1))
        self.calls += int(np.sum(np.maximum(p[:, 0], p[:, 2]) > p[:, 1]))  # flat ties stay flat

    def merged(self, other: _Source) -> _Source:
        return _Source(self.spread.merged(other.spread), self.top.merged(other.top), self.calls + other.calls)


@dataclass
class _First:
    """A symbol's first-model probabilities, by source, and the out-of-sample and in-sample
    values' differences on the same samples."""

    sources: dict[str, _Source] = field(default_factory=lambda: {s: _Source() for s in SOURCES})
    paired: int = 0
    spread_gap: float = 0.0  # the sums of |out of fold − in sample|
    top_gap: float = 0.0

    def fitted(self, out_of_fold: np.ndarray, in_sample: np.ndarray) -> None:
        self.sources[OUT_OF_FOLD].add(out_of_fold)
        self.sources[IN_SAMPLE].add(in_sample)
        self.paired += len(out_of_fold)
        spread = (out_of_fold[:, 2] - out_of_fold[:, 0]) - (in_sample[:, 2] - in_sample[:, 0])
        self.spread_gap += float(np.abs(spread).sum())
        self.top_gap += float(np.abs(out_of_fold.max(axis=1) - in_sample.max(axis=1)).sum())

    @classmethod
    def merged(cls, symbols: Iterable[_First]) -> _First:
        out = cls()
        for f in symbols:
            out.sources = {s: out.sources[s].merged(f.sources[s]) for s in SOURCES}
            out.paired += f.paired
            out.spread_gap += f.spread_gap
            out.top_gap += f.top_gap
        return out

    def as_dict(self) -> dict[str, Any]:
        spread = {s: self.sources[s].spread for s in SOURCES}
        top = {s: self.sources[s].top for s in SOURCES}
        return {
            "n": {s: spread[s].n for s in SOURCES},
            "call_share": {s: _share(self.sources[s].calls, spread[s].n) for s in SOURCES},
            "up_minus_down": _compared(spread, self.spread_gap, self.paired),
            "largest": _compared(top, self.top_gap, self.paired),
        }


def _compared(by_source: Mapping[str, _Histogram], gap: float, paired: int) -> dict[str, Any]:
    """A measure's quantiles by source, and how far the in-sample and live ones are from the
    out-of-sample one (``gap``: the sum of the paired differences over ``paired`` samples)."""
    reference = by_source[OUT_OF_FOLD]
    return {
        **{s: _quantiles(by_source[s]) for s in SOURCES},
        "ks_in_sample": _round(ks(reference, by_source[IN_SAMPLE])),
        "ks_live": _round(ks(reference, by_source[LIVE])),
        "mean_abs_diff_in_sample": _share(gap, paired),
    }


@dataclass
class _Calls:
    """A symbol's first-model calls on the replayed days, and what the second opinion made of
    them; by (symbol, day), the kept calls (and those right) and the flips."""

    scored: int = 0
    calls: int = 0
    kept: int = 0
    kept_right: int = 0
    to_flat: int = 0
    flipped: int = 0
    flipped_right: int = 0  # the opposite side matched the answer
    flipped_first_right: int = 0  # the side it turned from did
    ties: int = 0  # up and down tied above flat: called up
    ties_up: int = 0
    ties_flat: int = 0
    ties_down: int = 0
    by_day: dict[tuple[str, date], list[int]] = field(default_factory=dict)  # kept, right, flipped

    def add(self, p: np.ndarray, sample: Sample, params: Mapping[str, float]) -> None:
        """A bar close: the first model's ``p`` (down, flat, up, as the trees give them), and its
        answer."""
        self.scored += 1
        p_down, p_flat, p_up = _normalized(p)  # as ``_second_opinion`` reads them
        call = most_likely(p_up, p_flat, p_down)
        if call is Direction.FLAT:
            return
        self.calls += 1
        forecast = _second_opinion(p, sample.features, params)
        final = most_likely(forecast.p_up, forecast.p_flat, forecast.p_down)
        day = self.by_day.setdefault((sample.symbol, kst_day(sample.bar_close)), [0, 0, 0])
        if final is call:
            self.kept += 1
            self.kept_right += sample.outcome is final
            day[0] += 1
            day[1] += sample.outcome is final
        elif final is Direction.FLAT:
            self.to_flat += 1
        else:
            self.flipped += 1
            self.flipped_right += sample.outcome is final
            self.flipped_first_right += sample.outcome is call
            day[2] += 1
        if p_up == p_down:
            self.ties += 1
            self.ties_up += final is Direction.UP
            self.ties_flat += final is Direction.FLAT
            self.ties_down += final is Direction.DOWN

    @classmethod
    def merged(cls, symbols: Iterable[_Calls]) -> _Calls:
        out = cls()
        for c in symbols:
            for name in (
                "scored",
                "calls",
                "kept",
                "kept_right",
                "to_flat",
                "flipped",
                "flipped_right",
                "flipped_first_right",
                "ties",
                "ties_up",
                "ties_flat",
                "ties_down",
            ):
                setattr(out, name, getattr(out, name) + getattr(c, name))
            out.by_day |= c.by_day
        return out

    def as_dict(self) -> dict[str, Any]:
        flip_days = [day for day in self.by_day.values() if day[2]]
        return {
            "scored": self.scored,
            "calls": self.calls,
            "kept": self.kept,
            "kept_precision": _share(self.kept_right, self.kept),
            "to_flat": self.to_flat,
            "flipped": self.flipped,
            "flipped_precision": _share(self.flipped_right, self.flipped),
            "flipped_first_side_right": _share(self.flipped_first_right, self.flipped),
            "flip_days": len({d for (_, d), day in self.by_day.items() if day[2]}),
            "kept_precision_on_flip_days": _share(sum(d[1] for d in flip_days), sum(d[0] for d in flip_days)),
            "ties": self.ties,
            "ties_up": self.ties_up,
            "ties_flat": self.ties_flat,
            "ties_down": self.ties_down,
        }


def _quantiles(histogram: _Histogram) -> dict[str, float | None]:
    return {f"p{round(q * 100):02d}": _round(histogram.quantile(q)) for q in QUANTILES}


def _share(part: float, whole: float) -> float | None:
    return _round(part / whole) if whole else None


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, _DIGITS)


def _text(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


def _spread(quantiles: Mapping[str, float | None]) -> str:
    return "/".join(_text(quantiles[k]) for k in ("p05", "p50", "p95"))


def _short(source: str) -> str:
    return source.replace("_", " ")
