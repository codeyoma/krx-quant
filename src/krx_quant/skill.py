"""Skill against the class rates (#262): how much a predictor's calls beat what calling the
same classes as often, at random, would get right.

The leaderboard's accuracy and lift already pair a predictor with the baselines. These read
its own table of calls (rows: the class it called; columns: the class that happened), so
a predictor that mostly calls flat on a mostly-flat day cannot look skilled:

- **expected accuracy**: Σ_c P(called c)·P(c happened), what calls independent of the
  outcome would get right at the same rates;
- **Cohen's kappa**: (accuracy − expected) ÷ (1 − expected); 0 for always-flat and for
  random calls, 1 for perfect ones (Cohen 1960);
- **Gerrity score**: the equitable score for ordered classes (down < flat < up, Gerrity
  1992): 0 for any constant call and for random calls, 1 for perfect ones; a call two
  classes off (up for down) costs more than one class off;
- **balanced accuracy**: the mean, over the classes that happened, of how often each was
  called when it happened: 1/3 for always-flat when all three happened;
- **independence test**: Pearson's chi-square on the table, its degrees of freedom and
  p-value. It is Pesaran and Timmermann's (2009) test of dependence between multi-category
  forecasts and outcomes for independent observations. The four-minute windows overlap
  and the symbols move together, so the p-value is too small: it describes, it does not
  judge (as the paired lift's). None when a used cell expects fewer than ``MIN_EXPECTED``
  (Cochran's rule: the chi-square approximation needs them; two right calls of a rare
  class would otherwise look very significant).

Every value is None where it is undefined (no prediction, a class rate of 0 or 1 where the
formula divides by it).

Kappa is also taken **by trading day** (``day_kappa``): each day's kappa is one value, and the
days' mean has a Student t interval and a one-sided p-value, as a lift by trading day
(``stats.day_lift``). The windows within a day overlap; the days are far closer to
independent, so this p-value, unlike the independence test's, is not too small for it.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from krx_quant.feeds.base import kst_day
from krx_quant.pricing import Direction
from krx_quant.stats import Lift, Scored, t_test

ORDER = (Direction.DOWN, Direction.FLAT, Direction.UP)  # the classes' order (Gerrity)
MIN_EXPECTED = 5  # the fewest expected in a used cell for the chi-square test


@dataclass(frozen=True)
class Skill:
    n: int
    accuracy: float
    expected_accuracy: float
    kappa: float | None
    gerrity: float | None
    balanced_accuracy: float
    chi2: float | None
    df: int
    p: float | None


def skill(calls: Sequence[tuple[Direction, Direction]]) -> Skill | None:
    """The skill of ``calls`` (called, happened); None without any."""
    n = len(calls)
    if n == 0:
        return None
    table = [[0] * len(ORDER) for _ in ORDER]
    index = {d: i for i, d in enumerate(ORDER)}
    for called, happened in calls:
        table[index[called]][index[happened]] += 1
    rows = [sum(r) for r in table]
    cols = [sum(table[i][j] for i in range(len(ORDER))) for j in range(len(ORDER))]
    accuracy = sum(table[i][i] for i in range(len(ORDER))) / n
    expected = sum(rows[i] * cols[i] for i in range(len(ORDER))) / n**2
    kappa = None if expected == 1 else (accuracy - expected) / (1 - expected)
    recalls = [table[j][j] / cols[j] for j in range(len(ORDER)) if cols[j]]
    chi2, df = _chi2(table, rows, cols, n)
    return Skill(
        n=n,
        accuracy=accuracy,
        expected_accuracy=expected,
        kappa=kappa,
        gerrity=gerrity(table),
        balanced_accuracy=sum(recalls) / len(recalls),
        chi2=chi2,
        df=df,
        p=None if chi2 is None else chi2_sf(chi2, df),
    )


def calls_by_day(records: Sequence[Scored]) -> dict[date, list[tuple[Direction, Direction]]]:
    """``records``' calls (called, happened) by trading day, for ``day_kappa``."""
    out: dict[date, list[tuple[Direction, Direction]]] = {}
    for r in records:
        out.setdefault(kst_day(r.bar_close), []).append((r.predicted, r.outcome))
    return out


def day_kappa(
    calls_by_day: Mapping[date, Sequence[tuple[Direction, Direction]]], level: float = 0.95
) -> Lift | None:
    """Cohen's kappa taken a trading day at a time (#262): each day's kappa of its ``calls``
    (called, happened) is one value, every day weighing the same, with a two-sided ``level``
    Student t interval over the days and the one-sided p-value of "no better than the class
    rates" (``stats.t_test``). Its ``n`` is the days with a kappa: a day whose calls and
    outcomes were all one class has none and is left out. None from fewer than two."""
    days = [skill(calls) for calls in calls_by_day.values()]
    return t_test([s.kappa for s in days if s is not None and s.kappa is not None], level)


def gerrity(table: Sequence[Sequence[int]]) -> float | None:
    """The Gerrity score of a K×K table (rows called, columns happened, in ``ORDER``); None
    when a cumulative class rate of the outcomes is 0 or 1 (the weights divide by it)."""
    k = len(table)
    n = sum(sum(r) for r in table)
    if n == 0:
        return None
    happened = [sum(table[i][j] for i in range(k)) / n for j in range(k)]
    a: list[float] = []
    cumulative = 0.0
    for r in range(k - 1):
        cumulative += happened[r]
        if cumulative <= 0 or cumulative >= 1:
            return None
        a.append((1 - cumulative) / cumulative)

    def weight(i: int, j: int) -> float:  # 0-based; symmetric
        i, j = min(i, j), max(i, j)
        below = sum(1 / a[r] for r in range(i))
        above = sum(a[r] for r in range(j, k - 1))
        return (below - (j - i) + above) / (k - 1)

    return sum(table[i][j] / n * weight(i, j) for i in range(k) for j in range(k))


def _chi2(
    table: Sequence[Sequence[int]], rows: Sequence[int], cols: Sequence[int], n: int
) -> tuple[float | None, int]:
    """Pearson's chi-square over the rows and columns with any count, and its degrees of
    freedom; None (0 degrees) when either has fewer than two, None when a cell expects fewer
    than ``MIN_EXPECTED``."""
    used_rows = [i for i, r in enumerate(rows) if r]
    used_cols = [j for j, c in enumerate(cols) if c]
    df = (len(used_rows) - 1) * (len(used_cols) - 1)
    if df <= 0:
        return None, 0
    total = 0.0
    for i in used_rows:
        for j in used_cols:
            expected = rows[i] * cols[j] / n
            if expected < MIN_EXPECTED:
                return None, df
            total += (table[i][j] - expected) ** 2 / expected
    return total, df


def chi2_sf(x: float, df: int) -> float:
    """P(χ²(df) ≥ x): the regularized upper incomplete gamma Q(df/2, x/2)."""
    if x <= 0:
        return 1.0
    return _gamma_q(df / 2, x / 2)


def _gamma_q(a: float, x: float) -> float:
    """Q(a, x) by its series below a + 1 and its continued fraction above (Numerical Recipes)."""
    log_front = a * math.log(x) - x - math.lgamma(a)
    if x < a + 1:
        term = total = 1 / a
        ap = a
        for _ in range(1000):
            ap += 1
            term *= x / ap
            total += term
            if abs(term) < abs(total) * 1e-15:
                break
        return max(0.0, 1 - total * math.exp(log_front))
    tiny = 1e-300
    b = x + 1 - a
    c = 1 / tiny
    d = 1 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = tiny if abs(d) < tiny else d
        c = b + an / c
        c = tiny if abs(c) < tiny else c
        d = 1 / d
        step = d * c
        h *= step
        if abs(step - 1) < 1e-15:
            break
    return min(1.0, math.exp(log_front) * h)
