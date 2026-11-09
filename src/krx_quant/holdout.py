"""Pre-registered hypotheses judged on a fixed holdout (``krx-quant error-holdout``, #447).

#431's report (``error_breakdown.py``, over 2026-10-12–10-30) showed where turing seemed to win
and lose against the climate baseline, but its cells were picked from the same days, and every
cell's lower bound by trading day was below 0. So four hypotheses were fixed in #447 before the
days after that period, and they are judged on those days only:

- **H1, quiet, it loses:** the bar closes of σ̂₄'s lowest tercile, lift < 0;
- **H2, moving, it wins:** σ̂₄'s highest tercile, lift > 0;
- **H3, early, it wins:** the slots 09:00, 09:30, 10:00 and 10:30 together, lift > 0;
- **H4, lunch, it loses:** the slots 12:00 and 12:30 together, lift < 0.

The lift is turing's accuracy gained over ``baseline-climate``, paired by symbol and bar close
(``stats.lift``); the slots are ``consensus.slot``'s (the bar closes 09:01–09:30 are 09:00's).

- **The window** (``window_days``): the first ``DAYS`` (10) days after ``AFTER`` (2026-10-30)
  with turing's predictions as #431's report reads them: live, scored on the touch answer, special
  ones left out, by models trained on answer version ``TARGET`` (3). It is fixed, not the latest N
  days: a later run reads the same days.
- **σ̂₄'s cuts** (``Holdout.cuts``): each symbol's tercile cuts of σ̂₄ at every bar close of the
  discovery period ``DISCOVERY`` (2026-10-12–10-30) with one (``api.volatility_cuts``, as #431's
  report cut them), read from the DB at each run, never stored. A window's bar close goes by them
  (``Holdout.tercile``); one without σ̂₄, or of a symbol without cuts, is in no tercile.
- **The verdict** (``judge``, ``verdict``): the lift taken a trading day at a time
  (``stats.day_lift``: each day's mean gain one value, a Student t interval over the days) at
  ``LEVEL``, 98.75% (Bonferroni over the four hypotheses: 1 − 0.05 ÷ 4). A positive hypothesis
  stands when the interval's lower end is above 0, a negative one when its upper end is below 0;
  else it does not stand. A cell with fewer than ``MIN_SCORED`` (200) scored predictions, or with
  pairs on fewer than ``DAYS`` trading days, has too few (``표본 부족``) and is not judged. The
  paired interval, at the same level, counts each prediction as independent (the four-minute
  windows overlap, the symbols move together): it only describes.

Nothing is judged before the window is full. A scheduled job runs it once, on the first night it
is (``scheduled.ERROR_HOLDOUT``, ``days_in_window``), and posts it on #447. It reads the engine DB
without writing to it (``predictions.read_only``): turing's and the climate baseline's scored
predictions a day at a time (``read_scored``) and σ̂₄ (``read_sigmas``). No price,
market value or σ̂₄ cut is printed.

#449's regime gate (``climate_gate.py``) reuses the window, the cuts and the verdict:
``read_holdout``, ``Holdout.tercile`` and ``judge``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Literal

from krx_quant.api import tercile, volatility_cuts
from krx_quant.barriers import ANSWER_VERSION as BARRIER_VERSION
from krx_quant.barriers import NAME as BARRIER
from krx_quant.baselines import baselines_for
from krx_quant.climate import CLIMATE
from krx_quant.consensus import NAMES, slot
from krx_quant.feeds.base import KST
from krx_quant.predictions import SCORED, TOUCH_JOIN, iso, read_only, scored_rows, within_days
from krx_quant.stats import (
    Lift,
    Scored,
    day_lift,
    gains_by_day,
    lift,
    on_target,
    points_text,
    scored_by_algorithm,
)
from krx_quant.touch import TARGET_VERSION

log = logging.getLogger(__name__)

# Fixed in #447 before the window's days: none of these is tuned.
ALGORITHM = "turing"
TARGET = 3  # the answer version turing's models were trained on, as in #431's report
DISCOVERY = (date(2026, 10, 12), date(2026, 10, 30))  # #431's report's days: σ̂₄'s cuts come from them
AFTER = DISCOVERY[1]  # the window's days are after it
DAYS = 10  # the window's trading days, and those a cell's pairs must cover
MIN_SCORED = 200  # a cell's scored predictions
LEVEL = 0.9875  # each interval's: Bonferroni over the four hypotheses, 1 − 0.05 ÷ 4

STANDS, FAILS, TOO_FEW = "섰다", "서지 않음", "표본 부족"
Sign = Literal[1, -1]  # the lift a hypothesis claims: above 0, or below
Key = tuple[str, str]  # (symbol, bar close as stored)


# The window


def window_days(
    conn: sqlite3.Connection,
    algorithm: str = ALGORITHM,
    after: date = AFTER,
    days: int = DAYS,
    target: int = TARGET,
) -> list[date]:
    """The first ``days`` days after ``after`` with ``algorithm``'s predictions as the leaderboard
    reads them (scored on the touch answer, special ones left out, by models trained on
    ``target``), oldest first; fewer while the window fills. Steps from one day with the
    algorithm's predictions to the next on its index (``predictions_by_algorithm``), asking each
    for one such prediction: a few small queries, cheap enough for a night's conditions."""
    found: list[date] = []
    since = after + timedelta(days=1)
    while len(found) < days:
        first = conn.execute(
            "SELECT MIN(bar_close) FROM predictions WHERE algorithm = ? AND bar_close >= ?",
            (algorithm, iso(_midnight(since))),
        ).fetchone()[0]
        if first is None:
            break
        day = date.fromisoformat(first[:10])  # stored in KST
        if _scored_on(conn, algorithm, day, target):
            found.append(day)
        since = day + timedelta(days=1)
    return found


def _scored_on(conn: sqlite3.Connection, algorithm: str, day: date, target: int) -> bool:
    """Whether ``algorithm`` has a prediction on ``day`` that ``read_scored`` reads:
    scored on its touch answer (``predictions.SCORED``), not special, by a model of ``target``."""
    row = conn.execute(
        f"SELECT 1 FROM predictions p JOIN outcomes o ON o.prediction_id = p.id {TOUCH_JOIN}"
        " JOIN model_targets m ON m.algorithm = p.algorithm AND m.version = p.model_version"
        f" WHERE p.algorithm = ? AND p.bar_close >= ? AND p.bar_close < ? AND {SCORED}"
        " AND o.special = 0 AND m.target_version = ? LIMIT 1",
        (algorithm, iso(_midnight(day)), iso(_midnight(day + timedelta(days=1))), target),
    ).fetchone()
    return row is not None


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time(), KST)


def days_in_window(db: Path) -> int:
    """How many of the window's days are in (``scheduled.research.read_holdout_days``): 0 without the
    DB, or when it cannot be read (logged; the jobs' other conditions are read all the same)."""
    try:
        with read_only(db) as conn:
            return 0 if conn is None else len(window_days(conn))
    except sqlite3.Error:
        log.exception("could not count the holdout window's days (#447) in the engine DB")
        return 0


# Reading (``error_breakdown`` reads the same, #431)


def read_scored(
    conn: sqlite3.Connection, days: Sequence[date], target_version: int, algorithm: str
) -> dict[str, list[Scored]]:
    """``algorithm``'s and the climate baseline's scored predictions on ``days`` as the leaderboard
    reads them (special ones left out; an algorithm's by models of ``target_version`` only: the
    engine's in ``error_breakdown``, the pre-registered one in the holdout), a day at a time, as
    ``signal_review`` reads them."""
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


def read_sigmas(conn: sqlite3.Connection, days: Sequence[date]) -> dict[Key, float]:
    """σ̂₄ at each bar close of ``days`` the barrier answered with one, in basis points of its
    mid-point (σ̂₄ ticks × the tick ÷ the mid-point): in basis points, not ticks, as
    ``PredictionStore.barrier_widths``, because a tick changes size at band edges."""
    on, params = within_days(days, "bar_close")
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


def discovery_days() -> list[date]:
    """Every day of ``DISCOVERY``, both ends in."""
    first, last = DISCOVERY
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


@dataclass(frozen=True)
class Holdout:
    """The window as read (``read_holdout``): its days, the algorithm's and the climate baseline's
    records on them, σ̂₄ at their bar closes, and σ̂₄'s cuts from the discovery period."""

    days: list[date]  # oldest first; ``DAYS`` once full
    records: list[Scored]  # the algorithm's
    climate: list[Scored]  # the climate baseline's
    sigmas: dict[Key, float]  # σ̂₄ at the window's bar closes, basis points of the mid-point
    cuts: dict[str, list[float]]  # each symbol's two cuts, from the discovery period's σ̂₄

    @property
    def full(self) -> bool:
        return len(self.days) >= DAYS

    def tercile(self, r: Scored) -> str | None:
        """The tercile (``api.TERCILES``) of σ̂₄ at ``r``'s bar close by its symbol's discovery
        cuts; None without σ̂₄ there, or without cuts for the symbol."""
        sigma = self.sigmas.get((r.symbol, iso(r.bar_close)))
        cuts = self.cuts.get(r.symbol)
        return None if sigma is None or cuts is None else tercile(sigma, cuts)


def read_holdout(conn: sqlite3.Connection, algorithm: str = ALGORITHM) -> Holdout:
    """The window of ``algorithm`` and what judging it needs."""
    conn.row_factory = sqlite3.Row  # as the store's rows (``select_predictions``)
    cuts = volatility_cuts(read_sigmas(conn, discovery_days()))
    days = window_days(conn, algorithm)
    if not days:
        return Holdout([], [], [], {}, cuts)
    scored = read_scored(conn, days, TARGET, algorithm)
    return Holdout(days, scored.get(algorithm, []), scored.get(CLIMATE, []), read_sigmas(conn, days), cuts)


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
    """A hypothesis claiming a lift of ``sign`` stands only on the interval by trading day: a
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


# The hypotheses of #447


@dataclass(frozen=True)
class Hypothesis:
    """A pre-registered hypothesis: its cell of the window's bar closes, and the lift it claims there."""

    name: str
    claim: str
    cell: str
    sign: Sign
    selects: Callable[[Scored, Holdout], bool]  # whether a window's record is in its cell


EARLY = ("09:00", "09:30", "10:00", "10:30")
LUNCH = ("12:00", "12:30")

HYPOTHESES = (
    Hypothesis("H1", "조용할 때 진다", "σ̂₄ 하위 삼분위", -1, lambda r, h: h.tercile(r) == "low"),
    Hypothesis("H2", "움직일 때 이긴다", "σ̂₄ 상위 삼분위", 1, lambda r, h: h.tercile(r) == "high"),
    Hypothesis("H3", "장 초반에 이긴다", "·".join(EARLY) + " 칸", 1, lambda r, h: slot(r.bar_close) in EARLY),
    Hypothesis("H4", "점심에 진다", "·".join(LUNCH) + " 칸", -1, lambda r, h: slot(r.bar_close) in LUNCH),
)


def judged(data: Holdout) -> list[tuple[Hypothesis, Judged]]:
    """Each hypothesis's cell of the window judged against the climate baseline."""
    return [
        (h, judge([r for r in data.records if h.selects(r, data)], data.climate, h.sign)) for h in HYPOTHESES
    ]


# Writing


def error_holdout(db: Path) -> str:
    """The hypotheses judged on the window, as text to paste; nothing judged before it is full."""
    with read_only(db) as conn:
        data = None if conn is None else read_holdout(conn)
    title = f"{ALGORITHM} 홀드아웃 판정 (#447, 사전 등록 가설)"
    if data is None:
        return f"{title}: 데이터 없음"
    first, last = DISCOVERY
    span = f"{data.days[0]} – {data.days[-1]}, " if data.days else ""
    lines = [
        title,
        f"창: {AFTER} 뒤 {ALGORITHM}의 정답 버전 {TARGET} 예측이 있는 첫 {DAYS}거래일. 고정 창이다"
        f"(최근 N일이 아니다). 지금 {span}{len(data.days)}/{DAYS}일.",
        f"예측: 실시간, 도달 정답(정답 버전 {TARGET_VERSION}), 특수 상황 제외, {ALGORITHM}은 "
        f"정답 버전 {TARGET}으로 학습한 모델의 것만. #431의 리포트와 같다.",
        f"기준: {CLIMATE}. 같은 (종목, 봉 마감)끼리 짝지어 정답률 차이가 lift다. 칸은 consensus.slot이다"
        "(봉 마감 09:01–09:30은 09:00 칸).",
    ]
    if not data.full:
        lines.append(f"창이 아직 차지 않았다. 판정하지 않는다({DAYS}일이 차면 판정한다).")
        return "\n".join(lines)
    unsorted = [r for r in data.records if data.tercile(r) is None]
    lines += [
        f"σ̂₄ 삼분위 경계: 종목마다 발견 기간 {first} – {last}의 봉 마감 σ̂₄로 정했다(종목 {len(data.cuts)}개, "
        f"돌 때마다 DB에서 다시 낸다). 창의 채점 {len(data.records):,}개 중 {len(unsorted):,}개는 σ̂₄나 "
        "종목의 경계가 없어 어느 삼분위에도 들지 않는다.",
        f"판정: 거래일마다 낸 lift의 {LEVEL:.2%} t 구간(가설 넷의 본페로니)으로만 한다. "
        f"양의 가설은 하한 > 0, 음의 가설은 상한 < 0일 때 '{STANDS}', 아니면 '{FAILS}'이다. "
        f"칸의 채점이 {MIN_SCORED}개 미만이거나 짝이 있는 거래일이 {DAYS}일보다 적으면 '{TOO_FEW}'이고 "
        f"판정하지 않는다. 짝 구간(같은 {LEVEL:.2%})은 예측을 서로 독립으로 센다"
        "(4분 창이 겹쳐 실제보다 좁다). 참고용이다.",
        "가격, 시세 값, σ̂₄ 경계 값은 싣지 않는다.",
        "",
    ]
    results = judged(data)
    lines += [hypothesis_line(h, j) for h, j in results]
    by_verdict = {v: [h.name for h, j in results if j.verdict == v] for v in (STANDS, FAILS, TOO_FEW)}
    lines += ["", " · ".join(f"{v}: {', '.join(names) or '없음'}" for v, names in by_verdict.items())]
    return "\n".join(lines)


def hypothesis_line(h: Hypothesis, j: Judged) -> str:
    claim = "lift > 0" if h.sign > 0 else "lift < 0"
    head = f"{h.name} {h.claim}({h.cell}, {claim}): 채점 {j.scored:,} · 기후와 짝 {j.lift.n:,} · {j.days}일"
    if j.verdict == TOO_FEW:
        return f"{head} · {TOO_FEW}"
    return (
        f"{head} · 정답률 {_ratio(j.accuracy)} · 기후 정답률(짝) {_ratio(j.baseline)}"
        f" · lift {points_text(j.lift.value)} · 짝 구간 {_span(j.lift)} · 거래일 구간 {_span(j.by_day)}"
        f" · {j.verdict}"
    )


def _ratio(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def _span(i: Lift | None) -> str:
    if i is None or i.low is None or i.high is None:
        return "—"
    return f"[{points_text(i.low)}, {points_text(i.high)}]"
