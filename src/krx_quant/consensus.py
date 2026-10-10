"""Condorcet, the consensus: the active algorithms' probabilities, pooled by weight.

At each bar close two consensus predictions are made, each stored and scored like an
algorithm's:

- **fast** (``consensus-fast``): from the active algorithms finished 2 seconds after the
  bar close;
- **final** (``consensus-final``): once every active algorithm has finished or run out of
  its time budget (30 seconds at most).

The pool is linear: each probability (and the expected move) is the weighted mean of the
members', the weights of those present scaled to add up to 1. Algorithms on probation,
on the bench, retired or clones are never members. With no member, the consensus is
recorded empty (``no_members``).

**Weights**, per symbol and 30-minute slot, are refitted after each day's close:

1. For the last 5 and the last 20 days with predictions, the weights whose pool would
   have had the lowest log loss on the bar closes every member predicted (basis B,
   special predictions left out), found by EM.
2. Only a weight steady across both spans moves: above equal in both, it takes the
   smaller; below in both, the larger; otherwise equal. Then they are scaled to add up
   to 1. An algorithm good only lately gains nothing.
3. Shrunk toward equal weights by the evidence: with n bar closes in the 20 days, the
   fitted weights count n / (n + 200).

Before the first fit, or for a member the fit did not know, the members count equally.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from krx_quant.baselines import Forecast
from krx_quant.feeds.base import KST

FAST = "consensus-fast"
FINAL = "consensus-final"
NAMES = (FAST, FINAL)
EQUAL = "condorcet-equal"  # the version before any fit
PERIODS = (5, 20)  # days with predictions
SHRINK = 200  # bar closes' worth of belief in equal weights
_EM_ROUNDS = 200
_EM_TOLERANCE = 1e-9
_FLOOR = 1e-6  # the smallest probability counted, as in training

Cell = tuple[str, str]  # (symbol, slot)


@dataclass(frozen=True)
class Timing:
    fast_seconds: float = 2.0  # after the bar close


@dataclass(frozen=True)
class Weights:
    version: str
    computed_at: datetime | None
    cells: Mapping[Cell, Mapping[str, float]] = field(default_factory=dict)

    def of(self, symbol: str, bar_close: datetime, present: Sequence[str]) -> dict[str, float]:
        """The weights of the ``present`` members at ``symbol``'s bar close, adding up to 1.

        A member the fit did not know counts as one of equals.
        """
        fitted = self.cells.get((symbol, slot(bar_close)), {})
        equal = 1 / len(present)
        raw = {name: fitted.get(name, equal) for name in present}
        total = sum(raw.values())
        return {name: w / total for name, w in raw.items()} if total > 0 else dict.fromkeys(present, equal)


NO_WEIGHTS = Weights(EQUAL, None)


def slot(bar_close: datetime) -> str:
    """The 30-minute slot of the bar a prediction was made at the close of: ``"09:00"`` holds
    the bar closes 09:01–09:30.
    """
    start = (bar_close - timedelta(minutes=1)).astimezone(KST)
    return f"{start.hour:02d}:{start.minute - start.minute % 30:02d}"


def pool(forecasts: Mapping[str, Forecast], weights: Mapping[str, float]) -> Forecast:
    """The weighted mean of ``forecasts`` (whose ``weights`` add up to 1)."""
    return Forecast(
        p_up=sum(weights[n] * f.p_up for n, f in forecasts.items()),
        p_flat=sum(weights[n] * f.p_flat for n, f in forecasts.items()),
        p_down=sum(weights[n] * f.p_down for n, f in forecasts.items()),
        expected_ticks=sum(weights[n] * f.expected_ticks for n, f in forecasts.items()),
    )


def fit(
    rows: Sequence[Mapping[str, Any]], members: Sequence[str], days: Sequence[date], at: datetime
) -> Weights:
    """Weights for ``members`` from scored predictions (``rows``) on ``days`` (latest first)."""
    version = f"condorcet-{at.astimezone(KST):%Y%m%d}"
    if not members:
        return Weights(version, at)
    # Each bar close's probability of what happened, by member, in each cell.
    seen: dict[Cell, dict[tuple[str, str], dict[str, float]]] = {}
    for r in rows:
        if r["algorithm"] not in members or r["class_b"] is None:
            continue
        p = {"up": r["p_up"], "flat": r["p_flat"], "down": r["p_down"]}[r["class_b"]]
        close = datetime.fromisoformat(r["bar_close"])
        cell = (r["symbol"], slot(close))
        seen.setdefault(cell, {}).setdefault((r["bar_close"], r["symbol"]), {})[r["algorithm"]] = max(
            p, _FLOOR
        )
    spans = [set(days[:n]) for n in PERIODS]
    cells: dict[Cell, dict[str, float]] = {}
    for cell, closes in seen.items():
        complete = [
            (date.fromisoformat(key[0][:10]), probs)
            for key, probs in closes.items()
            if len(probs) == len(members)
        ]
        if not complete:
            continue
        fitted = [
            _em([[probs[m] for m in members] for day, probs in complete if day in span], len(members))
            for span in spans
        ]
        kept = [_steady([w[i] for w in fitted], 1 / len(members)) for i in range(len(members))]
        total = sum(kept)
        kept = [k / total for k in kept]
        n = sum(1 for day, _ in complete if day in spans[-1])
        trust = n / (n + SHRINK)
        equal = 1 / len(members)
        cells[cell] = {m: trust * k + (1 - trust) * equal for m, k in zip(members, kept, strict=True)}
    return Weights(version, at, cells)


def _steady(spans: Sequence[float], equal: float) -> float:
    """A member's weight over the spans: above equal only if above it in every span (then the
    least of them), below only if below in every span (the most), else equal.
    """
    if all(w > equal for w in spans):
        return min(spans)
    if all(w < equal for w in spans):
        return max(spans)
    return equal


def _em(observations: Sequence[Sequence[float]], k: int) -> list[float]:
    """Mixture weights maximizing the pooled likelihood of ``observations`` (each the members'
    probabilities of what happened); equal without any.
    """
    weights = [1 / k] * k
    if not observations:
        return weights
    for _ in range(_EM_ROUNDS):
        shares = [0.0] * k
        for probs in observations:
            mixed = sum(w * p for w, p in zip(weights, probs, strict=True))
            for i in range(k):
                shares[i] += weights[i] * probs[i] / mixed
        updated = [s / len(observations) for s in shares]
        moved = max(abs(a - b) for a, b in zip(updated, weights, strict=True))
        weights = updated
        if moved < _EM_TOLERANCE:
            break
    return weights
