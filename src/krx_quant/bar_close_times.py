"""How long a trading day's bar closes took, against the trading days before it (#589).

``krx-quant bar-close-times`` reads the engine DB and the trader's, read-only, beside the running
engine. The scheduled job ``bar-close-times-589`` runs it on the first trading night after the
deploy that keeps NumPy's BLAS and OpenMP on the calling thread (``Dockerfile``), and posts it to
#589: an agent compares the day with the days before and closes the issue, or opens one.

What it reads:

- Each prediction's ``displayed_at``: live, the clock when the engine stored it. A bar close's first
  record is its baselines' (after the scoring and the inputs); each algorithm's record follows its
  own run in a worker thread, so its time after that first record is its wait for a worker and the
  GIL and its computing; the final consensus waits for every member; the last record ends the bar
  close. The direction's predictions and the bar-close kinds' algorithms' (``KIND_ALGORITHMS``); a
  VI's rows (``vi``) are stored at once and left out. An algorithm past its time limit is stored
  when it was given up: its seconds count, and its bar closes timed out or failed are counted apart.
- The engine's ``loop_held`` runs (``loop_watch.LOOP_HELD``): each bar close that held the event
  loop over a second, the WARNING's ``held the event loop`` line. The engine keeps them from #589
  on; for the days before, Loki has the lines.
- The trader's stalls (``stalls``, #407): each time its quotes stopped for more than 5 s.

Its output holds dates, counts and seconds only, no price. Exit 1 when the day has no predictions.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from krx_quant.config import Settings
from krx_quant.consensus import FINAL
from krx_quant.feeds.base import KST
from krx_quant.loop_watch import LOOP_HELD
from krx_quant.predictions import DB_NAME, read_only
from krx_quant.trader.store import DB_NAME as TRADER_DB_NAME

DAYS_BEFORE = 5  # the trading days before the day it is compared with
VI = "vi"
LATE = ("timed_out", "error")  # statuses counted apart


@dataclass(frozen=True)
class Spread:
    """The median, the 90th percentile and the largest of some seconds."""

    median: float
    p90: float
    largest: float

    @classmethod
    def of(cls, values: Sequence[float]) -> Spread | None:
        if not values:
            return None
        ordered = sorted(values)
        return cls(ordered[(len(ordered) - 1) // 2], ordered[int(0.9 * (len(ordered) - 1))], ordered[-1])

    def text(self) -> str:
        return f"{self.median:.3f}/{self.p90:.3f}/{self.largest:.3f}초"


@dataclass(frozen=True)
class Held:
    """A bar close that held the event loop over a second (a ``loop_held`` run): how long, and the
    seconds of each step in that stretch."""

    seconds: float
    parts: dict[str, float]

    @classmethod
    def of(cls, detail: str) -> Held:
        found = json.loads(detail)
        return cls(float(found["seconds"]), {str(k): float(v) for k, v in found["parts"].items()})


@dataclass
class Day:
    """One trading day's bar closes, as stored."""

    day: date
    bar_closes: int = 0
    first: list[float] = field(default_factory=list)  # each bar close's first record, after it
    final: list[float] = field(default_factory=list)  # its final consensus, after it
    last: list[float] = field(default_factory=list)  # its last record, after it
    algorithms: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    late: Counter[str] = field(default_factory=Counter)  # bar closes it timed out or failed at, by name
    stalls: list[float] = field(default_factory=list)  # the trader's quote gaps, in seconds
    held: list[Held] = field(default_factory=list)


def _between(day: date) -> tuple[str, str]:
    start = datetime.combine(day, time(), KST)
    return start.isoformat(), (start + timedelta(days=1)).isoformat()


def _days(db: sqlite3.Connection, through: date, count: int) -> list[date]:
    """The latest ``count`` days with predictions, through ``through``, oldest first."""
    rows = db.execute(
        "SELECT DISTINCT substr(bar_close, 1, 10) AS d FROM predictions WHERE bar_close < ?"
        " ORDER BY d DESC LIMIT ?",
        (_between(through)[1], count),
    ).fetchall()
    return sorted(date.fromisoformat(d) for (d,) in rows)


def _rows(
    db: sqlite3.Connection, day: date, kind_algorithms: Iterable[str]
) -> list[tuple[str, str, str, str]]:
    """The day's rows: (algorithm, bar close, displayed at, status), the direction's and the bar-close
    kinds' algorithms'."""
    start, end = _between(day)
    names = sorted(kind_algorithms)
    rows = db.execute(
        "SELECT algorithm, bar_close, displayed_at, status FROM predictions"
        " WHERE bar_close >= ? AND bar_close < ?",
        (start, end),
    ).fetchall()
    if names:
        marks = ", ".join("?" * len(names))
        rows += db.execute(
            "SELECT algorithm, bar_close, displayed_at, status FROM kind_predictions"
            f" WHERE bar_close >= ? AND bar_close < ? AND algorithm IN ({marks})",
            (start, end, *names),
        ).fetchall()
    return rows


def read_day(
    db: sqlite3.Connection,
    trader: sqlite3.Connection | None,
    day: date,
    algorithms: Iterable[str],
    kind_algorithms: Iterable[str],
) -> Day:
    """What the stores hold of ``day``'s bar closes."""
    timed = set(algorithms) | set(kind_algorithms)
    found = Day(day)
    by_close: dict[str, list[tuple[str, datetime, str]]] = defaultdict(list)
    for name, bar_close, displayed_at, status in _rows(db, day, kind_algorithms):
        if status != VI:
            by_close[bar_close].append((name, datetime.fromisoformat(displayed_at), status))
    for bar_close, rows in by_close.items():
        at = datetime.fromisoformat(bar_close)
        first = min(shown for _, shown, _ in rows)
        found.bar_closes += 1
        found.first.append((first - at).total_seconds())
        found.last.append((max(shown for _, shown, _ in rows) - at).total_seconds())
        each: dict[str, datetime] = {}  # the latest record of each, over the symbols
        late: set[str] = set()
        for name, shown, status in rows:
            if name == FINAL or name in timed:
                each[name] = max(shown, each.get(name, shown))
            if name in timed and status in LATE:
                late.add(name)
        found.late.update(late)
        for name, shown in each.items():
            if name == FINAL:
                found.final.append((shown - at).total_seconds())
            else:
                found.algorithms[name].append((shown - first).total_seconds())
    start, end = _between(day)
    for (detail,) in db.execute(
        "SELECT detail FROM runs WHERE kind = ? AND at >= ? AND at < ? ORDER BY at", (LOOP_HELD, start, end)
    ):
        found.held.append(Held.of(detail))
    if trader is not None:
        for began, last_quote, ended in trader.execute(
            "SELECT began_at, last_quote_at, ended_at FROM stalls WHERE began_at >= ? AND began_at < ?",
            (start, end),
        ):
            since = datetime.fromisoformat(last_quote or began)
            until = datetime.fromisoformat(ended or began)
            found.stalls.append((until - since).total_seconds())
    return found


def _spread_text(values: Sequence[float]) -> str:
    spread = Spread.of(values)
    return "없음" if spread is None else spread.text()


def _held_text(day: Day) -> str:
    if not day.held:
        return "루프 막힘 0번"
    longest = max(h.seconds for h in day.held)
    return f"루프 막힘 {len(day.held)}번(가장 긴 {longest:.2f}초)"


def _stalls_text(day: Day) -> str:
    if not day.stalls:
        return "호가 끊김 0번"
    return f"호가 끊김 {len(day.stalls)}번(가장 긴 {max(day.stalls):.1f}초)"


def render(day: Day, before: Sequence[Day], algorithms: Sequence[str]) -> str:
    """The report (module doc): dates, counts and seconds."""
    span = f"{before[0].day}–{before[-1].day}" if before else "없음"
    lines = [
        f"봉 마감 시간 (#589) · {day.day}, 앞 거래일 {len(before)}일({span})과 견준다",
        "엔진 DB의 예측이 저장된 시각(displayed_at)으로 잰다. 초는 중앙값/90번째 백분위/최대다.",
        "",
        "1. 날마다: 봉 마감 수 · 첫 저장(봉 마감에서 기준 예측까지) · 최종 합의(봉 마감에서) · "
        "마지막 저장(봉 마감 전체) · trader 호가 끊김(5초 넘게)",
    ]
    for d in [*before, day]:
        lines.append(
            f"- {d.day}: 봉 마감 {d.bar_closes} · 첫 저장 {_spread_text(d.first)} · 최종 합의 "
            f"{_spread_text(d.final)} · 마지막 저장 {_spread_text(d.last)} · {_stalls_text(d)}"
        )
    lines += [
        "",
        f"2. 알고리즘마다: 첫 저장에서 그 알고리즘의 예측이 저장되기까지(작업 스레드와 GIL을 기다린 시간과 "
        f"계산). {day.day} · 앞 {len(before)}일을 모은 것. 시간 초과·오류는 그 봉 마감 수, 그날/앞 날들",
    ]
    for name in algorithms:
        earlier = [s for d in before for s in d.algorithms.get(name, [])]
        if not day.algorithms.get(name) and not earlier:
            continue
        late_before = sum(d.late[name] for d in before)
        lines.append(
            f"- {name}: {_spread_text(day.algorithms.get(name, []))} · {_spread_text(earlier)} · "
            f"시간 초과·오류 {day.late[name]}/{late_before}"
        )
    lines += [
        "",
        f"3. {day.day}의 루프 막힘(봉 마감이 이벤트 루프를 1초 넘게 쥠, `loop_held`): {_held_text(day)}",
    ]
    biggest = Counter(max(h.parts, key=h.parts.__getitem__) for h in day.held if h.parts)
    if biggest:
        lines.append("가장 큰 단계: " + " · ".join(f"{name} {n}번" for name, n in biggest.most_common()))
    return "\n".join(lines)


def bar_close_times(
    settings: Settings,
    now: datetime,
    day: date | None = None,
    days_before: int = DAYS_BEFORE,
) -> tuple[str, int]:
    """The report of ``day`` (default: the latest day with predictions, through today), and the exit
    code: 1 when that day has no predictions."""
    from krx_quant.algorithms import ALGORITHMS, KIND_ALGORITHMS

    names = [a.name for a in ALGORITHMS]
    kind_names = [a.name for a in KIND_ALGORITHMS]
    through = day or now.astimezone(KST).date()
    with read_only(settings.state_dir / DB_NAME) as db:
        if db is None:
            return "봉 마감 시간 (#589): 엔진 DB가 없다", 1
        days = _days(db, through, days_before + 1)
        if not days or (day is not None and days[-1] != day):
            return f"봉 마감 시간 (#589): {through}까지 예측이 없다", 1
        with read_only(settings.state_dir / TRADER_DB_NAME) as trader:
            read = [read_day(db, trader, d, names, kind_names) for d in days]
    return render(read[-1], read[:-1], [*names, *kind_names]), 0
