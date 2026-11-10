"""A pre-registered claim judged on a fixed holdout, by trading day (#447): the verdict the SMT
experiment's holdout (``smt_experiment.py``, #523) judges its claims by.

- **The verdict** (``judge``, ``verdict``): the lift of a cell's records over a baseline's, paired by
  symbol and bar close (``stats.lift``), taken a trading day at a time (``stats.day_lift``: each
  day's mean gain one value, a Student t interval over the days) at the level asked (``LEVEL``,
  98.75%: Bonferroni over four claims, 1 − 0.05 ÷ 4). A positive claim stands when the interval's
  lower end is above 0, a negative one when its upper end is below 0; else it does not stand. A cell
  with fewer than ``MIN_SCORED`` (200) scored records, or with pairs on fewer than ``DAYS`` (10)
  trading days (the minimum asked), has too few (``표본 부족``) and is not judged. The paired interval,
  at the same level, counts each record as independent (the four-minute windows overlap, the symbols
  move together): it only describes.

> **삭제됨** (2026-11-10, #NNN): turing's four hypotheses of #431's report judged on the first 10
> trading days after 2026-10-30 (``krx-quant error-holdout``, the job ``error-holdout-447``): the
> window (``window_days``, ``days_in_window``), its reading (``read_holdout``, ``read_scored``,
> ``read_sigmas``, σ̂₄'s cuts from the discovery period) and the hypotheses (``HYPOTHESES``), and the
> regime gate that reused them (``climate_gate.py``, #449). 이유: the user closed #447 and #449 on
> 2026-11-10: the verdict (#676) trades barrier horizons, not the four-minute direction, and the
> climate gate was never to go into the engine (#689). 대신: the verdict and its replay
> (``verdict-replay-679``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from krx_quant.stats import Lift, Scored, day_lift, gains_by_day, lift, points_text

# Fixed in #447 before its window's days; the SMT experiment passes its own minimums.
DAYS = 10  # the trading days a cell's pairs must cover
MIN_SCORED = 200  # a cell's scored records
LEVEL = 0.9875  # each interval's: Bonferroni over four claims, 1 − 0.05 ÷ 4

STANDS, FAILS, TOO_FEW = "섰다", "서지 않음", "표본 부족"
Sign = Literal[1, -1]  # the lift a claim makes: above 0, or below


# The verdict


@dataclass(frozen=True)
class Judged:
    """A cell's records judged against a baseline (``judge``)."""

    scored: int
    days: int  # the trading days with pairs
    accuracy: float | None
    baseline: float | None  # the baseline's accuracy on the pairs
    lift: Lift  # paired by symbol and bar close, at the level asked
    by_day: Lift | None  # by trading day, at the level asked: what the verdict goes by
    verdict: str  # STANDS, FAILS or TOO_FEW


def verdict(
    scored: int, by_day: Lift | None, sign: Sign, min_scored: int = MIN_SCORED, min_days: int = DAYS
) -> str:
    """A claim of a lift of ``sign`` stands only on the interval by trading day: a
    positive one when its lower end is above 0, a negative one when its upper end is below 0.
    Too few scored predictions, or pairs on too few days: not judged."""
    if scored < min_scored or by_day is None or by_day.n < min_days:
        return TOO_FEW
    bound = by_day.low if sign > 0 else by_day.high
    if bound is None:
        return FAILS
    return STANDS if bound * sign > 0 else FAILS


def judge(
    records: Sequence[Scored],
    baseline: Sequence[Scored],
    sign: Sign,
    level: float = LEVEL,
    min_scored: int = MIN_SCORED,
    min_days: int = DAYS,
) -> Judged:
    """``records`` (a cell's) against ``baseline``, paired by symbol and bar close (the cell is a
    property of the bar close: the baseline's other records pair with none), and the verdict on a
    lift of ``sign``."""
    theirs = {(b.symbol, b.bar_close): b.correct for b in baseline}
    on_pairs = [theirs[k] for r in records if (k := (r.symbol, r.bar_close)) in theirs]
    by_day = day_lift(records, baseline, level)
    return Judged(
        len(records),
        len(gains_by_day(records, baseline)),
        sum(r.correct for r in records) / len(records) if records else None,
        sum(on_pairs) / len(on_pairs) if on_pairs else None,
        lift(records, baseline, level),
        by_day,
        verdict(len(records), by_day, sign, min_scored, min_days),
    )


def _ratio(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def _span(i: Lift | None) -> str:
    if i is None or i.low is None or i.high is None:
        return "—"
    return f"[{points_text(i.low)}, {points_text(i.high)}]"
