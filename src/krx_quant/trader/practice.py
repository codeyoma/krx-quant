"""The mock practice cell, frozen for each review cycle (#474, spec #468 6 "연습", the user's choice
of 2026-11-02).

**What.** With ``KRX_TRADER_MOCK_PRACTICE=on`` (mock mode alone, #427), at a pick that read the cost
map and the champion but found no champion cell to trade, the mock book practises one cell a share at
a time (``desk.Desk``). That cell, and its θ, is chosen once for a review cycle (#469, ``review.py``)
and kept for its 20 trading days whatever the cost map says meanwhile; a restart goes on with it
(``practice_cycles``, ``frozen``). It trades by the champion's rules (the entry at its θ, the
barriers, the risk rules), one share, and is of no policy: no record, promotion or live gate counts
it; the mock execution checks do (#427).

**When it is chosen** (``freeze_now``). By the nightly job ``trader-report`` (``report.run_night``),
after the re-fit and the promotion: when the review cycle in force (``review.cycle_at``: after a day's
close, the next trading day's) has no practice cell kept, it chooses one. On a review night that is
the next cycle's, before its first day; the first time (after the deploy, or the practice turned on
in a cycle) the cycle in force's, traded from the next pick. A night that cannot choose (the cost map
or the engine's database cannot be read, no cell measured well enough) keeps nothing, and the next
night tries again; meanwhile nothing is practised (``NOT_FROZEN``). Once kept, the cycle's cell is
never chosen again.

**Which.** Of the cells the cost map gives that night that may be practised (#427,
``Selection.practisable``: a verdict at a market order's round trip, ``pass`` or ``fail``, and a
sound row), each replayed alone a share at each θ of the re-fit's grid (#470, ``replay.Signals``:
its rises on its kind's days of the cost map, by the paper trader's rules and fills, none of a day a
pick would have held it off, #603, ``buyable.inactive_days``): the cell and θ
whose net per signal in ticks has the greatest day-level 95% lower bound (``Signals.per_signal``),
below 0 too, of those that bought at least ``MIN_TRADES`` times on at least two days; of two alike,
the greater mean, then the kinds' order, the roles', the lower θ.

**Gone.** A pick that finds the cell's predictor gone (``cells.gone``, as for a policy's cell, #469)
keeps when (``gone_at``): no practice for the rest of the cycle, even should the predictor come back,
and no other cell in its place (``GONE``). One that finds it not active (#603, ``cells.inactive``, as
for a policy's cell: on probation, on the bench, retired, a clone) keeps when too (``inactive_at``):
no practice for the rest of the cycle, even should it be active again (``PREDICTOR_INACTIVE``).
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import time as clock
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, fields, replace
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import aiohttp

from krx_quant.config import Settings
from krx_quant.feeds.base import KST
from krx_quant.market_calendar import Session, calendar_store
from krx_quant.predictions import DB_NAME as ENGINE_DB
from krx_quant.stats import Interval
from krx_quant.trader import ROLES, SYMBOL, TRADED_KINDS, review
from krx_quant.trader import policy as policies
from krx_quant.trader.book import Rules
from krx_quant.trader.cells import Cell, open_engine_db, special_windows
from krx_quant.trader.refit import THRESHOLDS, cost_map_days
from krx_quant.trader.replay import GAPPED, NO_RECORDING, Replayer, Signals, Source
from krx_quant.trader.selection import GONE, PREDICTOR_INACTIVE, CostMapError, Pick, choose, read_cost_map
from krx_quant.trader.settings import TraderSettings
from krx_quant.trader.store import DB_NAME as TRADER_DB

log = logging.getLogger(__name__)

# A cell × θ may be practised only if it bought this many times alone on the replayed days: one a
# trading day of the cost map's 20 on average, as the re-fit asks of a θ (``refit.MIN_TRADES``), so
# that no θ is chosen for a few lucky trades, nor one that never buys (its signals all given up net 0).
MIN_TRADES = 20
# Why the mock book does not practise at a pick with the practice on (``GET /v1/trader``'s
# ``practice.why``): a champion cell trades; no cell is kept for the review cycle in force (or the
# cycle cannot be told); the cell's predictor is gone (``selection.GONE``) or not active
# (``selection.PREDICTOR_INACTIVE``, #603); or the pick's own reason (``no_cost_map``, ``no_policy``).
CHAMPION = "champion"
NOT_FROZEN = "not_frozen"
HEAD = "연습 칸 (#474)"


@dataclass(frozen=True)
class Measured:
    """A cell at a θ, its signals replayed alone a share (``replay.Signals``)."""

    pick: Pick
    threshold: float
    signals: Signals

    @property
    def interval(self) -> Interval | None:
        return self.signals.per_signal()

    def eligible(self, min_trades: int = MIN_TRADES) -> bool:
        """Whether it may be chosen (module doc "Which")."""
        found = self.interval
        return (
            self.signals.bought >= min_trades
            and found is not None
            and found.low is not None
            and math.isfinite(found.low)
        )

    def rank(self) -> tuple[float, float, int, int, float]:
        """The order of the eligible (module doc "Which"): the greatest lower bound first."""
        found = self.interval
        assert found is not None and found.low is not None
        p = self.pick
        return (-found.low, -found.value, TRADED_KINDS.index(p.kind), ROLES.index(p.role), self.threshold)

    def as_json(self, min_trades: int = MIN_TRADES) -> dict[str, Any]:
        p = self.pick
        return {
            "kind": p.kind,
            "role": p.role,
            "predictor": p.predictor,
            "threshold": self.threshold,
            "eligible": self.eligible(min_trades),
            **self.signals.as_json(),
        }


def ranked(tried: Iterable[Measured], min_trades: int = MIN_TRADES) -> list[Measured]:
    """``tried`` that may be chosen, best first (module doc "Which"): the first is the cell chosen."""
    return sorted((m for m in tried if m.eligible(min_trades)), key=Measured.rank)


@dataclass(frozen=True)
class Frozen:
    """The practice cell kept for review cycle ``cycle`` of evaluation ``evaluation``: when it was
    chosen, the cell as the cost map gave it that night at its θ (``pick``: one share, practice), every
    cell × θ measured that night, best first (``measured``, each ``Measured.as_json``), and when a pick
    found its predictor gone (``gone_at``) or not active (``inactive_at``, #603)."""

    evaluation: int
    cycle: int
    chosen_at: datetime
    pick: Pick
    measured: tuple[Mapping[str, Any], ...]
    gone_at: datetime | None = None
    inactive_at: datetime | None = None

    @property
    def cell(self) -> Cell:
        return self.pick.cell

    @property
    def rested(self) -> bool:
        """Whether it rests for the rest of the cycle (module doc "Gone")."""
        return self.gone_at is not None or self.inactive_at is not None

    @property
    def threshold(self) -> float:
        assert self.pick.threshold is not None
        return self.pick.threshold

    @property
    def chosen(self) -> Mapping[str, Any]:
        """Its own figures on the replay, as measured that night."""
        return self.measured[0]

    def as_json(self) -> dict[str, Any]:
        """``GET /v1/trader``'s ``practice.frozen``."""
        chosen = self.chosen
        return {
            "evaluation": self.evaluation,
            "cycle": self.cycle,
            "chosen_at": self.chosen_at.astimezone(KST).isoformat(),
            "kind": self.pick.kind,
            "role": self.pick.role,
            "predictor": self.pick.predictor,
            "threshold": self.threshold,
            "per_signal": chosen.get("per_signal"),
            "signals": chosen.get("signals"),
            "bought": chosen.get("bought"),
            "wins": chosen.get("wins"),
            "measured": len(self.measured),
            "eligible": sum(1 for m in self.measured if m.get("eligible")),
            "gone_at": None if self.gone_at is None else self.gone_at.astimezone(KST).isoformat(),
            "inactive_at": None if self.inactive_at is None else self.inactive_at.astimezone(KST).isoformat(),
        }


# The store.


def frozen(db: sqlite3.Connection, evaluation: int, cycle: int) -> Frozen | None:
    """The practice cell kept for ``cycle`` of ``evaluation``; None before one was (or in a store from
    before #474)."""
    table = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'practice_cycles'"
    if db.execute(table).fetchone() is None:
        return None
    have = {r[1] for r in db.execute("PRAGMA table_info(practice_cycles)")}
    inactive = "inactive_at" if "inactive_at" in have else "NULL"  # a store the trader has not opened since
    row = db.execute(
        f"SELECT chosen_at, threshold, pick, measured, gone_at, {inactive} FROM practice_cycles"
        " WHERE evaluation = ? AND cycle = ?",
        (evaluation, cycle),
    ).fetchone()
    if row is None:
        return None
    chosen_at, threshold, pick, measured, gone_at, inactive_at = row
    return Frozen(
        evaluation,
        cycle,
        datetime.fromisoformat(chosen_at),
        replace(pick_of(json.loads(pick)), threshold=float(threshold)),
        tuple(json.loads(measured)),
        None if gone_at is None else datetime.fromisoformat(gone_at),
        None if inactive_at is None else datetime.fromisoformat(inactive_at),
    )


def keep(
    db: sqlite3.Connection,
    evaluation: int,
    cycle: int,
    at: datetime,
    chosen: Measured,
    tried: Sequence[Measured],
    min_trades: int = MIN_TRADES,
) -> Frozen:
    """``chosen`` kept for ``cycle`` of ``evaluation`` at ``at``, with every cell × θ measured
    (``tried``: the eligible ranked first, then the rest), unless a cell was kept for it already: the
    cycle's cell, as kept."""
    pick = replace(chosen.pick, threshold=chosen.threshold)
    first = ranked(tried, min_trades)
    others = sorted(
        (m for m in tried if not m.eligible(min_trades)),
        key=lambda m: (TRADED_KINDS.index(m.pick.kind), ROLES.index(m.pick.role), m.threshold),
    )
    order = [chosen, *(m for m in first if m is not chosen), *others]
    with db:
        db.execute(
            "INSERT OR IGNORE INTO practice_cycles (evaluation, cycle, chosen_at, kind, role, predictor,"
            " threshold, pick, measured) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                evaluation,
                cycle,
                at.astimezone(KST).isoformat(),
                pick.kind,
                pick.role,
                pick.predictor,
                chosen.threshold,
                json.dumps(pick.as_json(), ensure_ascii=False),
                json.dumps([m.as_json(min_trades) for m in order], ensure_ascii=False),
            ),
        )
    kept = frozen(db, evaluation, cycle)
    assert kept is not None
    return kept


def rest(db: sqlite3.Connection, evaluation: int, cycle: int, at: datetime, why: str = GONE) -> None:
    """The cycle's practice cell found gone at ``at`` (module doc "Gone"), or not active (``why``
    ``PREDICTOR_INACTIVE``, #603), once."""
    column = {GONE: "gone_at", PREDICTOR_INACTIVE: "inactive_at"}[why]
    with db:
        db.execute(
            f"UPDATE practice_cycles SET {column} = ? WHERE evaluation = ? AND cycle = ?"
            f" AND {column} IS NULL",
            (at.astimezone(KST).isoformat(), evaluation, cycle),
        )


def pick_of(data: Mapping[str, Any]) -> Pick:
    """A pick as ``Pick.as_json`` kept it."""
    return Pick(**{f.name: data[f.name] for f in fields(Pick) if f.name in data})


# The night.


def measure(
    picks: Sequence[Pick],
    days_of: Mapping[str, Sequence[date]],
    engine_db: Path,
    data_dir: Path,
    sessions: Callable[[date], Session | None],
    *,
    rules: Rules,
    target_version: int,
    thresholds: Sequence[float] = THRESHOLDS,
    evaluation: review.Evaluation | None = None,
) -> tuple[list[Measured], Replayer | None]:
    """Each of ``picks`` replayed alone a share at each of ``thresholds`` (module doc "Which"), on its
    kind's days (``days_of``), from the engine's database (read-only) and the recordings under
    ``data_dir``, by ``rules``: one pass of the replay for them all (#480: ``Replayer.run`` measures
    several cells alone; a pass a pick before), the days a pick would have held a cell off left out
    (#603: its predictor not active, for the rest of ``evaluation``'s review cycle). With the replayer,
    for its counts."""
    days = [d for p in picks for d in days_of.get(p.kind, ())]
    if not days:
        return [], None
    db = open_engine_db(engine_db)
    try:
        windows = special_windows(db, SYMBOL, datetime.combine(min(days), time(), KST))
        replayer = Replayer(
            db,
            data_dir,
            sessions,
            windows,
            {p.cell: Source(p.role, tuple(days_of.get(p.kind, ()))) for p in picks},
            rules=rules,
            target_version=target_version,
            thresholds=thresholds,
            evaluation=evaluation,
        )
        done = replayer.run([], alone=[p.cell for p in picks])
        tried = [
            Measured(pick, threshold, signals)
            for pick in picks
            for threshold, signals in done.signals[pick.cell].items()
        ]
        return tried, replayer
    finally:
        db.close()


async def freeze_now(
    settings: Settings, trader: TraderSettings, now: datetime, engine_url: str | None = None
) -> None:
    """The night's step (module doc "When"), in the trader's store, its lines printed for the job's
    comment (cells, θ, counts, ticks and dates: no price). What fails is printed and logged, never
    raised: the next night tries again."""
    if not trader.mock_practice:
        print(f"{HEAD}: 연습이 꺼져 있어(KRX_TRADER_MOCK_PRACTICE=off) 고르지 않는다")
        return
    started = clock.monotonic()
    sessions = calendar_store(settings).calendar.session
    try:
        db = policies.connect(settings.state_dir / TRADER_DB)
    except sqlite3.Error as e:
        print(f"{HEAD}: 저장소를 열지 못해 고르지 않았다: {e}")
        log.error("practice: cannot open the trader's store (%s): no practice cell chosen tonight", e)
        return
    try:
        evaluation = review.current(db)
        if evaluation is None:
            print(f"{HEAD}: 평가 버전이 아직 없어(#469) 고르지 않았다")
            return
        cycle = review.cycle_at(evaluation, now, sessions)
        kept = frozen(db, evaluation.version, cycle.number)
        if kept is not None:
            print(said_kept(kept, cycle))
            return
        try:
            async with aiohttp.ClientSession() as session:
                body = await read_cost_map(session, engine_url or trader.engine_url)
            selection = choose(body)
        except CostMapError as e:
            print(
                f"{HEAD}: 비용 지도를 읽지 못해 주기 {cycle.number}의 칸을 고르지 않았다(다음 밤에 다시): {e}"
            )
            log.error("practice: cannot read the cost map (%s): no practice cell chosen tonight", e)
            return
        engine_db = settings.state_dir / ENGINE_DB
        if not engine_db.exists():
            print(f"{HEAD}: 엔진 DB가 없어 주기 {cycle.number}의 칸을 고르지 않았다(다음 밤에 다시)")
            return
        tried, replayer = measure(
            selection.practisable,
            cost_map_days(body),
            engine_db,
            settings.data_dir,
            sessions,
            rules=Rules.of(trader, settings.fee_rate),
            target_version=settings.target_version,
            evaluation=evaluation,
        )
        best = ranked(tried)
        if not best:
            print(said_none(cycle, selection.practisable, tried))
            log.warning(
                "practice: no cell measured well enough for cycle %s: none chosen tonight", cycle.number
            )
        else:
            kept = keep(db, evaluation.version, cycle.number, now, best[0], tried)
            print(said_chosen(kept, cycle))
            log.info(
                "practice: cycle %s's practice cell is %s %s at θ %g (evaluation %s)",
                cycle.number,
                kept.pick.kind,
                kept.pick.predictor,
                kept.threshold,
                evaluation.version,
            )
        print(said_replay(replayer, clock.monotonic() - started))
    except (sqlite3.Error, ValueError) as e:  # ValueError: a calendar without the cycle's days
        print(f"{HEAD}: 고르지 못했다(다음 밤에 다시): {e}")
        log.error("practice: %s: no practice cell chosen tonight", e)
    finally:
        db.close()


# What is printed (the job's comment) and logged.


def _cell(kind: str, predictor: str, role: str | None) -> str:
    return f"{kind} {predictor}" + (f"({role})" if role else "")


def _ticks(value: float | None) -> str:
    return "?" if value is None else f"{value:+.2f}틱"


def figures(chosen: Mapping[str, Any]) -> str:
    """A cell × θ's figures on the replay (``Measured.as_json``), as the report says them."""
    interval = chosen.get("per_signal") or {}
    return (
        f"1주씩 따로 잰 신호당 순손익 {_ticks(interval.get('value'))}(날 단위 하한 "
        f"{_ticks(interval.get('low'))}, {interval.get('days', 0)}일) · 신호 {chosen.get('signals', 0)}, "
        f"체결 {chosen.get('bought', 0)}(이익 {chosen.get('wins', 0)})"
    )


def _span(cycle: review.Cycle) -> str:
    return f"주기 {cycle.number}({cycle.first}–{cycle.review})"


def said_chosen(kept: Frozen, cycle: review.Cycle) -> str:
    eligible = sum(1 for m in kept.measured if m.get("eligible"))
    return (
        f"{HEAD}: {_span(cycle)}의 연습 칸을 골랐다(주기 동안 바꾸지 않는다): "
        f"{_cell(kept.pick.kind, kept.pick.predictor, kept.pick.role)} · θ {kept.threshold:g} · "
        f"{figures(kept.chosen)} · 잰 칸 × θ {len(kept.measured)}개 가운데 체결 {MIN_TRADES}건 이상 "
        f"{eligible}개의 하한 1위"
    )


def resting(kept: Frozen) -> str:
    """Why the cycle's practice cell rests, as the job's comment and the report say it (module doc
    "Gone"): its predictor gone, or not active (#603); empty while it does not."""
    out = ""
    if kept.gone_at is not None:
        out += f" · {kept.gone_at.astimezone(KST):%Y-%m-%d %H:%M}에 예측자가 없어져(gone) 주기 끝까지 쉰다"
    if kept.inactive_at is not None:
        out += (
            f" · {kept.inactive_at.astimezone(KST):%Y-%m-%d %H:%M}에 예측자가 active가 아니어서"
            f"({PREDICTOR_INACTIVE}) 주기 끝까지 쉰다"
        )
    return out


def said_kept(kept: Frozen, cycle: review.Cycle) -> str:
    return (
        f"{HEAD}: {_span(cycle)}의 연습 칸은 {_cell(kept.pick.kind, kept.pick.predictor, kept.pick.role)}"
        f" · θ {kept.threshold:g} 그대로다({kept.chosen_at.astimezone(KST):%Y-%m-%d %H:%M}에 고름)"
        f"{resting(kept)}"
    )


def said_none(cycle: review.Cycle, picks: Sequence[Pick], tried: Sequence[Measured]) -> str:
    if not picks:
        why = "비용 지도에 연습할 수 있는 칸(시장가 왕복의 판정 pass·fail, 온전한 줄)이 없다"
    else:
        most = max((m.signals.bought for m in tried), default=0)
        why = (
            f"{len(picks)}칸 × θ 가운데 이틀 이상에서 체결 {MIN_TRADES}건 이상인 것이 없다"
            f"(가장 많을 때 {most}건)"
        )
    return f"{HEAD}: {_span(cycle)}의 칸을 고르지 않았다(다음 밤에 다시, 그동안 연습하지 않는다): {why}"


def said_replay(replayer: Replayer | None, seconds: float) -> str:
    if replayer is None:
        return f"{HEAD}: 재생한 날 없음 · {seconds:.1f}초"
    recordings = replayer.recordings
    absent = sum(1 for r in recordings.values() if r == NO_RECORDING)
    gaps = sum(1 for r in recordings.values() if r == GAPPED)
    peak = max((mb for mb in replayer.peaks.values() if mb is not None), default=None)
    peaked = "?" if peak is None else f"{peak:.1f}"
    return (
        f"{HEAD}: 재생 (#470) {len(recordings)}일(녹화 없음 {absent}일, 장중 결측 구간 {gaps}일) · "
        f"재생 {replayer.passes}번 · 하루 최대 RSS {peaked} MB · {seconds:.1f}초"
    )
