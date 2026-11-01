"""The climate prior (#429): each symbol and 30-minute slot's class frequencies, as a model keeps
them and as an algorithm reads them at a bar close. Shannon predicts with it since #429's
experiment (``krx_quant.climate_prior``) met its rule; any algorithm may (#448).

**The table** (``PriorTally``): label counts by symbol and slot (``consensus.slot``), and by symbol
over the whole day, of the samples an algorithm is trained on (recorded and backfilled days alike,
as the climate baseline counts them). Smoothed: a symbol's day is its counts plus
``PRIOR_STRENGTH`` pseudo-counts split evenly among the classes; a slot is its counts plus as many
in its symbol's day's frequencies. A slot never seen takes its symbol's day; a symbol never seen,
the even split (``prior_of``).

- **Causal** (``training_priors``). A training sample's prior counts the training window's days
  before its own only: each of their labels was decided before its bar close (a four-minute window
  ends the day it starts). Its own day's labels and the days after it never reach it.
- **Kept with the model.** The prior a model predicts with counts every day of its window, and is
  held in the model's parameters (``prior.<symbol>.<slot>.<class>``, ``prior.<symbol>.all.<class>``):
  never a feature, as a label aggregate depends on the window it was counted on and the feature
  cache (the sample store) is shared by every window and algorithm. So an algorithm that reads it
  predicts with the symbol and the bar close besides its features (``contract.ContextAware``).
- **Its two inputs** (``PRIOR_INPUTS``, ``prior_inputs``): the log-odds of up and of down against
  flat at the bar close's symbol and slot.

The table's parameters have one meaning each, by name (``PriorMeanings``): their symbols are the
settings', not known before training.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import date, datetime, timedelta

from krx_quant.algorithms.contract import Sample
from krx_quant.algorithms.forecasts import CLASSES, param
from krx_quant.cards import CLASS_NAMES, Meaning
from krx_quant.consensus import slot
from krx_quant.feeds.base import kst_day
from krx_quant.pricing import Direction

PRIOR = "prior"  # the table's parameters: prior.<symbol>.<slot>.<class>
# The table's back-off, a symbol's whole day: the climate baseline's word (``climate.ALL_DAY``),
# not imported from it, as ``climate`` imports the algorithms' contract and the algorithms this.
ALL_DAY = "all"
# The prior's inputs: log(p_up / p_flat) and log(p_down / p_flat) at the bar close's symbol and slot.
PRIOR_INPUTS = ("climate_up", "climate_down")
# Pseudo-counts a slot's (and a symbol's day's) frequencies are pulled toward the next coarser one
# with: about a day of a slot's bar closes (30 a symbol).
PRIOR_STRENGTH = 30.0

Prior = tuple[float, float, float]  # p_up, p_flat, p_down
EVEN: Prior = (1 / 3, 1 / 3, 1 / 3)


def _smoothed(counts: Counter[Direction], base: Prior) -> Prior:
    n = sum(counts.values())
    up, flat, down = (
        (counts[c] + PRIOR_STRENGTH * b) / (n + PRIOR_STRENGTH) for c, b in zip(CLASSES, base, strict=True)
    )
    return up, flat, down


def _row(symbol: str, where: str, p: Prior) -> dict[str, float]:
    return {param(PRIOR, symbol, where, c.value): q for c, q in zip(CLASSES, p, strict=True)}


class PriorTally:
    """Label counts by symbol and 30-minute slot, and by symbol over the whole day: the prior's."""

    def __init__(self) -> None:
        self._slots: dict[tuple[str, str], Counter[Direction]] = {}
        self._days: dict[str, Counter[Direction]] = {}

    def add(self, samples: Iterable[Sample]) -> None:
        """Count the labels of ``samples`` (those without one are not counted)."""
        for s in samples:
            if s.outcome is not None:
                self._slots.setdefault((s.symbol, slot(s.bar_close)), Counter())[s.outcome] += 1
                self._days.setdefault(s.symbol, Counter())[s.outcome] += 1

    def table(self) -> dict[str, float]:
        """The smoothed frequencies, as a model keeps them (module doc): each symbol's day,
        ``prior.<symbol>.all.<class>``, and each slot, ``prior.<symbol>.<slot>.<class>``; empty
        before any label."""
        days = {symbol: _smoothed(counts, EVEN) for symbol, counts in self._days.items()}
        out: dict[str, float] = {}
        for symbol, p in sorted(days.items()):
            out |= _row(symbol, ALL_DAY, p)
        for (symbol, where), counts in sorted(self._slots.items()):
            out |= _row(symbol, where, _smoothed(counts, days[symbol]))
        return out


def prior_of(params: Mapping[str, float], symbol: str, bar_close: datetime) -> Prior:
    """The prior ``params`` hold for ``symbol`` at ``bar_close``: its slot's, else its symbol's
    day's, else the even split."""
    for where in (slot(bar_close), ALL_DAY):
        found = [params.get(param(PRIOR, symbol, where, c.value)) for c in CLASSES]
        if all(p is not None for p in found):
            up, flat, down = (float(p) for p in found if p is not None)
            return up, flat, down
    return EVEN


def prior_inputs(p: Prior) -> tuple[float, float]:
    """The prior's two inputs (``PRIOR_INPUTS``): the log-odds of up and of down against flat."""
    up, flat, down = p
    return math.log(up / flat), math.log(down / flat)


def training_priors(window: Sequence[Sample], scored: Sequence[Sample]) -> list[tuple[float, float]]:
    """The prior inputs of each of ``scored``, the training samples of ``window``: the table of
    ``window``'s labels on the days before the sample's own, never its own day's or a later one's
    (module doc, "Causal")."""
    by_day: dict[date, list[Sample]] = {}
    for s in window:
        by_day.setdefault(kst_day(s.bar_close), []).append(s)
    tally = PriorTally()
    tables: dict[date, dict[str, float]] = {}
    for day in sorted({*by_day, *(kst_day(s.bar_close) for s in scored)}):
        tables[day] = tally.table()
        tally.add(by_day.get(day, ()))
    return [prior_inputs(prior_of(tables[kst_day(s.bar_close)], s.symbol, s.bar_close)) for s in scored]


# prior.<symbol>.<slot or all>.<class>: a symbol has no dot, a slot is HH:MM.
_TABLE_KEY = re.compile(rf"{PRIOR}\.([^.]+)\.(\d\d:\d\d|{ALL_DAY})\.({'|'.join(CLASS_NAMES)})")


def _closes(where: str) -> str:
    """The bar closes of a slot (``consensus.slot``): ``09:00`` holds 09:01–09:30."""
    start = datetime.strptime(where, "%H:%M")
    return f"{start + timedelta(minutes=1):%H:%M}–{start + timedelta(minutes=30):%H:%M}"


def table_meaning(name: str, known: str) -> Meaning | None:
    """The meaning of the prior table's parameter ``name`` (module doc); None for another name."""
    match = _TABLE_KEY.fullmatch(name)
    if match is None:
        return None
    symbol, where, c = match.groups()
    cell = "하루 전체" if where == ALL_DAY else f"{where} 칸(봉 마감 {_closes(where)})"
    back_off = (
        "가짜 수 30을 세 클래스에 똑같이 나눠 더한다"
        if where == ALL_DAY
        else "가짜 수 30을 그 종목 하루의 빈도대로 더한다. 본 적 없는 칸은 종목 하루의 것을 쓴다"
    )
    return Meaning(
        f"종목 {symbol}의 {cell}에서 학습 창의 정답이 {CLASS_NAMES[c]}이었던 비율(기후 사전 확률). "
        f"평활했다: {back_off}. 예측할 때 이 확률로 사전 확률 입력(`climate_up`, `climate_down`)을 낸다.",
        "비율",
        known,
    )


class PriorMeanings(Mapping[str, Meaning]):
    """An algorithm's parameter meanings with its prior table's: ``fixed`` are listed; the table's
    (``prior.<symbol>.<slot>.<class>``, ``prior.<symbol>.all.<class>``) are answered by name
    (``table_meaning``), as their symbols are the settings'. The Engine API lists those a model
    holds (``api._algorithm_json``)."""

    def __init__(self, fixed: Mapping[str, Meaning], known: str) -> None:
        self._fixed = dict(fixed)
        self._known = known

    def __getitem__(self, name: str) -> Meaning:
        if name in self._fixed:
            return self._fixed[name]
        meaning = table_meaning(name, self._known)
        if meaning is None:
            raise KeyError(name)
        return meaning

    def __iter__(self) -> Iterator[str]:
        return iter(self._fixed)

    def __len__(self) -> int:
        return len(self._fixed)
