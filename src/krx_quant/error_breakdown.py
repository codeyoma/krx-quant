"""Where an algorithm errs (``krx-quant error-breakdown``, #431): its live predictions over the
latest days broken down by symbol, 30-minute slot, spread and volatility, each cell with its
confusion matrix, the precision of each class it called and its lift over the climate baseline,
as text to paste. A scheduled job runs it once for ``turing`` and posts it on #431
(``scheduled.py``); an agent reads it for where accuracy work could go, the user decides.

It is exploratory, and says so at its head: a cell picked from it is judged on the days after
the period (a holdout), not on these.

It reads the engine DB without writing to it (``predictions.read_only``, as ``signal-review``
and ``diagnose``), over the latest ``days`` days with predictions (as the leaderboard's
``20d``), the scored predictions a day at a time, keeping the algorithm's and the climate
baseline's only. They are the records the leaderboard (``/v1/leaderboard``) is made of: scored
on the touch answer, special ones left out, the algorithm's by models of the engine's target
only (``stats.on_target``).

- **Cells.** All the records; each symbol; each 30-minute slot (``consensus.slot``: the bar
  closes 09:01–09:30 are 09:00's, the climate table's slots); the spread at the bar close, the
  best quotes last received before it (the touch path's ``bid_start`` and ``ask_start``) in ticks
  of the reference price: one tick, two or more, none or less (locked or crossed), no quotes;
  the four-minute volatility σ̂₄ at the bar close (the barrier's, ``barriers.py``: its answer's
  path, in basis points of the mid-point), by tercile of the symbol's bar closes of the period
  that have one (``api.by_volatility``, the cut ``/v1/algorithms/{name}/stats`` splits the
  barrier width by), and the bar closes without one (fewer than 20 bars, then w = k).
- **In each cell**: the scored predictions, the pairs with the climate baseline and the trading
  days; the accuracy; the confusion matrix (the class called × the class that happened), each
  called class's precision (how often it happened) and the classes that happened; the climate
  baseline's accuracy on the pairs; the lift over it (``stats.entry``, as the leaderboard: paired
  by symbol and bar close) with its lower bound, and its lower bound by trading day
  (``stats.day_lift``, the leaderboard's ``day_low``: ``stats.overlap_t_test`` with windows of one
  session is the same Student t over the days). A cell with fewer than ``KRX_MIN_SAMPLE`` scored
  predictions or pairs (``stats.enough``, the leaderboard's rule to rank) says ``표본 부족`` with
  its counts only.

Ratios come with 95% Wilson intervals and the paired lift with a normal one: both count each
prediction as independent, which they are not (a window overlaps the next three, the symbols
move together); the lower bound by trading day does not. Nothing is judged against a threshold.
No price or market value is printed: the spread and volatility cells are named, not cut at
values.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from krx_quant.api import TERCILES, by_volatility
from krx_quant.barriers import ANSWER_VERSION as BARRIER_VERSION
from krx_quant.barriers import NAME as BARRIER
from krx_quant.baselines import baselines_for
from krx_quant.climate import CLIMATE
from krx_quant.config import Settings
from krx_quant.consensus import NAMES, slot
from krx_quant.feeds.base import kst_day
from krx_quant.predictions import days_with_predictions, iso, read_only, scored_rows
from krx_quant.pricing import Direction
from krx_quant.signal_review import CALLED, _points, _ratio, _within
from krx_quant.stats import (
    Entry,
    Interval,
    Lift,
    Scored,
    enough,
    entry,
    on_target,
    scored_by_algorithm,
    wilson,
)
from krx_quant.touch import TARGET_VERSION

DEFAULT_DAYS = 20
DEFAULT_ALGORITHM = "turing"

# The spread cells, in their order.
ONE_TICK, WIDER, LOCKED, NO_QUOTES = "1틱", "2틱 이상", "0틱 이하(잠김·역전)", "호가 없음"
SPREADS = (ONE_TICK, WIDER, LOCKED, NO_QUOTES)
# The volatility cells, in their order: ``api.by_volatility``'s terciles, then the bar closes
# without a σ̂₄.
VOLATILITY = dict(zip(TERCILES, ("낮음", "중간", "높음"), strict=True))
NO_SIGMA = "σ̂₄ 없음"

Key = tuple[str, str]  # (symbol, bar close as stored)


@dataclass(frozen=True)
class Period:
    """What the breakdown reads from the engine DB (``read``)."""

    days: list[date]  # oldest first
    records: list[Scored]  # the algorithm's, as the leaderboard reads them
    climate: list[Scored]  # the climate baseline's
    sigmas: dict[Key, float]  # σ̂₄ at each bar close of the days with one, basis points of the mid-point


def error_breakdown(
    db: Path, settings: Settings, until: date, days: int = DEFAULT_DAYS, algorithm: str = DEFAULT_ALGORITHM
) -> str:
    """The breakdown of ``algorithm`` over the latest ``days`` days with predictions up to ``until``."""
    with read_only(db) as conn:
        data = None if conn is None else read(conn, settings, until, days, algorithm)
    if data is None or not data.days:
        return f"{algorithm} 오류 분석: 데이터 없음"
    lines = [
        f"{algorithm} 오류 분석 {data.days[0]} – {data.days[-1]} ({len(data.days)}일, 예측이 있는 날)",
        "탐색용이다. 여기서 고른 칸을 그대로 판정에 쓰지 않는다. "
        "고른 칸은 이 기간 뒤의 날들(홀드아웃)로 판정한다.",
        f"예측: 실시간, 도달 정답(정답 버전 {TARGET_VERSION}), 특수 상황 제외, {algorithm}은 엔진 목표"
        f"(정답 버전 {settings.target_version})로 학습한 모델의 것만. 순위표가 읽는 예측과 같다.",
        f"기준: {CLIMATE}. 같은 (종목, 봉 마감)끼리 짝지어 정답률 차이가 lift다. 짝 하한은 짝의 95% 구간, "
        "거래일 하한은 거래일마다 낸 lift의 95% t 구간(순위표의 day_low)의 아래 끝이다.",
        f"칸의 채점이나 기후와의 짝이 {settings.min_sample:,}개 미만이면 표본 부족이다(KRX_MIN_SAMPLE). "
        "거래일 하한 말고는 예측을 서로 독립으로 센다(4분 창이 겹쳐 구간이 실제보다 좁다). "
        "가격과 시세 값은 싣지 않는다.",
    ]
    for title, cells in breakdown(data, algorithm, settings.fee_rate, settings.min_sample):
        lines += ["", title, *(f"  {line}" for c in cells for line in cell_lines(c))]
        if not cells:
            lines.append("  없음")
    return "\n".join(lines)


# Reading


def read(conn: sqlite3.Connection, settings: Settings, until: date, days: int, algorithm: str) -> Period:
    """What the breakdown needs of the latest ``days`` days with predictions up to ``until``."""
    conn.row_factory = sqlite3.Row  # as the store's rows (``select_predictions``)
    chosen = sorted(days_with_predictions(conn, until, days))
    if not chosen:
        return Period([], [], [], {})
    scored = _scored(conn, chosen, settings.target_version, algorithm)
    return Period(chosen, scored.get(algorithm, []), scored.get(CLIMATE, []), _sigmas(conn, chosen))


def _scored(
    conn: sqlite3.Connection, days: Sequence[date], target_version: int, algorithm: str
) -> dict[str, list[Scored]]:
    """``algorithm``'s and the climate baseline's scored predictions on ``days`` as the leaderboard
    reads them (special ones left out; an algorithm's by models of the engine's target only), a
    day at a time, as ``signal_review`` reads them."""
    baselines = {b.name for b in baselines_for(target_version)} | set(NAMES)
    wanted = {algorithm, CLIMATE}
    out: dict[str, list[Scored]] = {}
    for day in days:
        rows = [r for r in scored_rows(conn, [day], None, False) if r["algorithm"] in wanted]
        if target_version >= 2:
            rows = on_target(rows, baselines, target_version)
        for name, records in scored_by_algorithm(rows).items():
            out.setdefault(name, []).extend(records)
    return out


def _sigmas(conn: sqlite3.Connection, days: Sequence[date]) -> dict[Key, float]:
    """σ̂₄ at each bar close of ``days`` the barrier answered with one, in basis points of its
    mid-point (σ̂₄ ticks × the tick ÷ the mid-point): in basis points, not ticks, as
    ``PredictionStore.barrier_widths``, because a tick changes size at band edges."""
    on, params = _within(days, "bar_close")
    out: dict[Key, float] = {}
    for symbol, bar_close, path in conn.execute(
        f"SELECT symbol, bar_close, path FROM kind_answers WHERE kind = ? AND answer_version = ? AND {on}",
        [BARRIER, BARRIER_VERSION, *params],
    ):
        try:
            found = json.loads(path or "{}")
        except ValueError:
            continue
        if not isinstance(found, dict):
            continue
        sigma, tick, anchor = found.get("sigma"), found.get("tick"), found.get("anchor")
        if sigma is None or not tick or not anchor or anchor < 0:
            continue
        out[(symbol, bar_close)] = float(sigma) * float(tick) * 1e4 / float(anchor)
    return out


# The cells


def spread(r: Scored) -> str:
    """The spread cell of a record's bar close: its best quotes last received before it (the
    touch path's), in ticks of the reference price."""
    p = r.prices
    if p is None or p.bid_start is None or p.ask_start is None or not p.tick:
        return NO_QUOTES
    ticks = (p.ask_start - p.bid_start) / p.tick
    if ticks <= 0:
        return LOCKED
    return ONE_TICK if ticks < 1.5 else WIDER


def volatility(records: Sequence[Scored], sigmas: Mapping[Key, float]) -> dict[str, list[Scored]]:
    """``records`` by tercile of σ̂₄ at their bar close (``api.by_volatility``: each symbol's cuts
    from its bar closes of the period with one), and those without one."""
    terciles, _ = by_volatility(records, sigmas)
    groups = {VOLATILITY[t]: terciles[t] for t in TERCILES}
    groups[NO_SIGMA] = [r for r in records if (r.symbol, iso(r.bar_close)) not in sigmas]
    return groups


@dataclass(frozen=True)
class Cell:
    """A cell's records judged: the leaderboard's entry of them against the climate baseline, their
    confusion matrix, the climate's accuracy on the pairs, their trading days."""

    name: str
    entry: Entry
    confusion: Mapping[Direction, Mapping[Direction, int]]  # called -> happened -> count
    climate: Interval | None
    days: int
    enough: bool  # the leaderboard's rule to rank (``stats.enough``); else 표본 부족

    @property
    def lift(self) -> Lift:
        return self.entry.lifts[CLIMATE].lift

    @property
    def by_day(self) -> Lift | None:
        return self.entry.lifts[CLIMATE].by_day

    def precision(self, called: Direction) -> Interval | None:
        """How often ``called`` happened when it was called."""
        row = self.confusion[called]
        return wilson(row[called], sum(row.values()))


def cell(
    name: str,
    algorithm: str,
    records: Sequence[Scored],
    climate: Sequence[Scored],
    fee_rate: float,
    min_sample: int,
) -> Cell:
    """``records`` (a cell's) against the climate baseline's ``climate``, paired by symbol and bar
    close (the pairs are the cell's: the cell is a property of the bar close)."""
    judged = entry(algorithm, records, {CLIMATE: list(climate)}, fee_rate)
    confusion = {called: dict.fromkeys(Direction, 0) for called in Direction}
    for r in records:
        confusion[r.predicted][r.outcome] += 1
    theirs = {(c.symbol, c.bar_close): c.correct for c in climate}
    paired = [theirs[k] for r in records if (k := (r.symbol, r.bar_close)) in theirs]
    return Cell(
        name,
        judged,
        confusion,
        wilson(sum(paired), len(paired)),
        len({kst_day(r.bar_close) for r in records}),
        enough(judged, min_sample),
    )


def breakdown(data: Period, algorithm: str, fee_rate: float, min_sample: int) -> list[tuple[str, list[Cell]]]:
    """The sections: each a title and its cells, in order (a cell without records is left out)."""

    def cells(groups: Mapping[str, Sequence[Scored]]) -> list[Cell]:
        return [cell(n, algorithm, g, data.climate, fee_rate, min_sample) for n, g in groups.items() if g]

    def by(key: Callable[[Scored], str], order: Sequence[str] | None = None) -> dict[str, list[Scored]]:
        groups: dict[str, list[Scored]] = {k: [] for k in order or ()}
        for r in data.records:
            groups.setdefault(key(r), []).append(r)
        return groups if order else dict(sorted(groups.items()))

    return [
        ("1. 전체", cells({"전체": data.records})),
        ("2. 종목별", cells(by(lambda r: r.symbol))),
        ("3. 30분 시간대별(봉 마감 09:01–09:30은 09:00 칸)", cells(by(lambda r: slot(r.bar_close)))),
        (
            "4. 스프레드 구간별(봉 마감 직전에 받은 통합 1호가, 기준가의 호가단위 틱)",
            cells(by(spread, SPREADS)),
        ),
        (
            "5. 변동성 구간별(봉 마감의 4분 변동성 σ̂₄ 삼분위: 종목마다 기간 안 봉 마감의 σ̂₄로 자른다)",
            cells(volatility(data.records, data.sigmas)),
        ),
    ]


# Writing


def cell_lines(c: Cell) -> list[str]:
    s = c.entry.summary
    head = f"{c.name}: 채점 {s.n:,} · 기후와 짝 {c.lift.n:,} · {c.days}일"
    if not c.enough:
        return [f"{head} · 표본 부족"]
    by_day = c.by_day
    lines = [
        f"{head} · 정답률 {_ratio(s.accuracy)} · 기후 정답률(짝) {_ratio(c.climate)} · "
        f"lift {_points(c.lift.value)} (짝 하한 {_points(c.lift.low)} · "
        f"거래일 하한 {_points(None if by_day is None else by_day.low)})"
    ]
    for called in Direction:
        row = c.confusion[called]
        happened = " · ".join(f"{CALLED[d]} {row[d]:,}" for d in Direction)
        precision = _ratio(c.precision(called))
        lines.append(f"  {CALLED[called]} 예측 {sum(row.values()):,}: 정답 {happened} · 정밀도 {precision}")
    totals = {d: sum(c.confusion[called][d] for called in Direction) for d in Direction}
    lines.append(
        "  정답: " + " · ".join(f"{CALLED[d]} {totals[d]:,} ({totals[d] / s.n:.1%})" for d in Direction)
    )
    return lines
