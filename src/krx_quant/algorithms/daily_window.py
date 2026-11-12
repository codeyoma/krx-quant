"""The training window and its validation split of the daily candidates (#698: Hurst, Lorenz,
Friedman and their five-session twins), as Cox and Cox-5d take them (``algorithms.cox``,
``algorithms.cox5d``).

**Window.** The labelled samples of the latest ``training_days`` trading days, every symbol pooled,
ordered by day and symbol. Fewer than ``MIN_SAMPLES`` (250): nothing to learn.

**Split.** A chronological 80/20 split of those days, for the one setting each candidate chooses
(Hurst's lookback, Lorenz's number of neighbours, Friedman's number of rounds): the held-out part
is the samples from the first day of the last 20% (``cut``) on; the fitted part, those whose
window ended before it (#310: with windows of H sessions, the H − 1 days before ``cut`` are left
out of both; with one session, every day before it). The setting chosen, the model is fitted
again on the whole window.

Each candidate's code digest holds this module (``code_from``): a change here asks for a new
verdict of every one of them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np

from krx_quant.algorithms.contract import NothingToLearn
from krx_quant.daily import DailySample

MIN_SAMPLES = 250
VALIDATION_SHARE = 0.2


@dataclass(frozen=True)
class Window:
    """A candidate's training window and its split (module doc)."""

    samples: list[DailySample]  # labelled, by (day, symbol)
    days: list[date]  # the trading days, oldest first
    cut: date  # the held-out part's first day
    early: np.ndarray  # bool per sample: the fitted part (ended before ``cut``)
    late: np.ndarray  # bool per sample: the held-out part (from ``cut``)

    @property
    def labels(self) -> list[str]:
        return [s.label for s in self.samples if s.label is not None]

    def splits(self, at_least: int) -> bool:
        """Whether the split has at least ``at_least`` fitted samples and one held out."""
        return int(self.early.sum()) >= at_least and bool(self.late.any())


def window(samples: Sequence[DailySample], training_days: int) -> Window:
    """The window of ``samples`` (module doc); raises NothingToLearn with fewer than
    ``MIN_SAMPLES`` labelled samples."""
    labelled = [s for s in samples if s.label is not None]
    days = sorted({s.day for s in labelled})[-training_days:]
    kept = set(days)
    labelled = sorted((s for s in labelled if s.day in kept), key=lambda s: (s.day, s.symbol))
    if len(labelled) < MIN_SAMPLES:
        raise NothingToLearn(f"{len(labelled)} labelled days, fewer than {MIN_SAMPLES}")
    cut = days[int(len(days) * (1 - VALIDATION_SHARE))]
    early = np.array([s.ended_before(cut) for s in labelled])
    late = np.array([s.day >= cut for s in labelled])
    return Window(labelled, days, cut, early, late)


def log_loss(p: np.ndarray, y: np.ndarray) -> float:
    """The mean log loss of the rows of probabilities ``p`` on the classes ``y``."""
    return float(-np.mean(np.log(np.maximum(p[np.arange(len(y)), y], 1e-12))))
