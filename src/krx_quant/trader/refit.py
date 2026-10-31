"""The nightly re-fit of the trader's policy (#399, #393 "정책과 자기 개선"): ``krx-quant
refit-policy``, run every trading day's night after the nightly training by the scheduled job
``trader-report`` (#402: ``--report`` prints the day's report above this output). It first
judges the challengers that ran against the champion and promotes one that beat it (#400,
``promotion.promote``: after the market only), then writes a new policy
version (``policy.py``): the first one becomes the champion, every later one a challenger, which
takes a free one of the three running slots (it runs in paper beside the champion from the next
trading day) or waits for one. A fit with the values of the champion or of a running challenger is
not kept (``policy.same_as``). At the night's end the slots freed are filled, the newest waiting
first (``policy.admit``).

**Which cells.** Tonight's cost map of 005930 (``GET /v1/cost-map``, as the trader reads it at
08:30, ``selection.pick``) decides what may trade: the cells passing at a market order's round
trip, ranked by their hit rate's lower bound less p*. The policy keeps that order; the fit sets
each cell's threshold θ and Kelly fraction λ, or leaves the cell out.

**From what.** Each cell's stored predictions of 005930 on the cost map's days for its kind (the
row's ``days``: the latest 20 with predictions) and their answers, read from the engine's
database read-only, as the cost map counts them: made, their answer known and no special
situation in their window; an algorithm's by models trained on the answers they are scored on
(``stats.on_target``); the direction's on its touch label. Nothing is predicted again.

**The trades simulated** on the answers' paths, by the trader's rules (``paper.plan``) and its
costs, one position at a time, of the cells in their rank:

- a prediction buys when ``paper.plan`` would at the cell's θ (its most likely class the rise and
  its probability at least θ, its reference known, the entry window from 09:05 to a time barrier
  by 15:15, no special window touching it, a final consensus the engine did not block), nothing is
  held, and no cell ranked above it buys on the same bar close;
- it buys at the bar close's best ask (the path's ``ask_start``), nothing when that is at or above
  the target already (``target_reached``), ⌊min(capital, fraction × capital) ÷ ask⌋ shares with the
  fraction λ × the cell's f* cut to 0–1 (``paper.shares``), nothing below one share;
- its P&L per share is its kind's research P&L, the rule the hit rates are read by: a barrier's
  ``barriers.barrier_profit`` of a long (sold at the target when the upper barrier was reached
  first, at the best bid when the lower one was, else at the last best bid), the direction's
  ``stats.touch_profit`` of an up call (at the target when reached, else at the last best bid);
  less the round trip on the reference price (the sell tax and both commissions,
  ``KRX_FEE_PERCENT``); in won, × the shares × the tick;
- it is held until a barrier was reached (the path's time) or the time barrier: a later bar
  close buys from then on.

What the paths keep makes these differ from the paper trader's fills: the buy is at the bar
close's best ask, not the first quote after the decision; a barrier's stop is reached when a
best ask reaches it (the answer's rule), the trader's when a best bid does; the direction's touch
P&L has no stop (its path keeps no best bid at the stop).

**The fit** (``fit``): the cells one at a time in rank order, each at every point of the grid
(``Grid``: θ in ``THRESHOLDS`` × λ in ``KELLY_FRACTIONS``) with the cells kept before it as they
were kept. The point whose simulated days grow the capital most (Σ log(1 + net ÷ capital) over the
trades, the Kelly criterion: the capital does not compound in the trader, but λ is a Kelly
fraction) is kept when the cell made at least ``MIN_TRADES`` trades there and it grows the capital
more than leaving the cell out. Of points that grow it alike, the one nearest the trader's
defaults (θ ``KRX_TRADER_THRESHOLD``, λ ``KELLY_FRACTION``) is kept, then the higher θ, then the
lower λ. A cell kept at no point is left out, and why: ``few_trades`` (fewer than ``MIN_TRADES``
at every point), ``no_gain`` (no point grows the capital more). The same predictions, answers,
cost map, calendar and settings make the same policy.

**Limits.** None is fitted nor kept: the capital sizes the simulated trades as it sizes the
trader's (``KRX_TRADER_CAPITAL``), and the fit only writes ``policies`` and ``champions``.

**Memory** (the server has 2 GB; pre-validation replays have been killed for it): one cell's
predictions are read at a time on the engine database's indexes, each row parsed as it comes and
kept as a small ``Candidate`` (its rises only: a prediction whose most likely class is not the
rise never buys); the scores and the paths are not kept. The process's peak resident memory over
the fit (``footprint.Meter``) is printed, logged and kept in the policy's ``fit.run``.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import time as clock
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from itertools import product
from operator import itemgetter
from pathlib import Path
from typing import Any

import aiohttp

from krx_quant.barriers import DOWN_FIRST, UP_FIRST, barrier_profit
from krx_quant.config import Settings
from krx_quant.feeds.base import KST, kst_day
from krx_quant.footprint import Meter
from krx_quant.kinds import DIRECTION
from krx_quant.market_calendar import Session, calendar_store
from krx_quant.predictions import DB_NAME as ENGINE_DB
from krx_quant.predictions import KIND_ANSWER_JOIN, KIND_SCORED, SCORED, TOUCH_JOIN
from krx_quant.pricing import Direction, tick_size
from krx_quant.special import Window
from krx_quant.stats import Prices, touch_profit
from krx_quant.trader import KELLY_FRACTION, SYMBOL, THRESHOLD, promotion
from krx_quant.trader import policy as policies
from krx_quant.trader.cells import Cell, Signal, open_engine_db, special_windows, traded_kind
from krx_quant.trader.paper import Plan, Rules, plan, shares
from krx_quant.trader.selection import CostMapError, Pick, choose, read_cost_map
from krx_quant.trader.settings import TraderSettings
from krx_quant.trader.store import DB_NAME as TRADER_DB

log = logging.getLogger(__name__)

# The grid (#399: small): the thresholds θ and the Kelly fractions λ tried for each cell.
THRESHOLDS = (0.4, 0.5, 0.6, 0.7, 0.8)
KELLY_FRACTIONS = (0.25, 0.5, 1.0)
# A cell is kept at a point only if it made this many simulated trades there: one a trading day of
# the cost map's 20 on average, so a θ is not picked for a few lucky trades.
MIN_TRADES = 20
# Why a cell the cost map passed is left out.
FEW_TRADES, NO_GAIN = "few_trades", "no_gain"
# The waiting policies the night's output names, newest first.
WAITING_SHOWN = 5
# What the job's exit code says: 0 a policy kept (or the same values running already), 1 none (the
# cost map or the engine's database could not be read).
NO_POLICY_KEPT = 1


@dataclass(frozen=True, slots=True)
class Candidate:
    """A stored rise of a cell: a prediction ``paper.plan`` lets buy at a θ its probability reaches,
    with the trade the answer's path makes of it."""

    bar_close: datetime
    probability: float  # the rise's
    ask: float  # the best ask at the bar close: the buy
    bought: bool  # False: the ask is at or above the target already (target_reached)
    profit: float  # ticks of the reference price per share, less the round trip
    tick: float
    exit_at: datetime  # when the trade ends: a barrier reached, or the time barrier


@dataclass
class CellRead:
    """A cell's stored predictions as the fit reads them: its rises (``candidates``), and how many
    rows were read and what kept the others out (``counts``: ``paper.plan``'s reasons, ``unpriced``
    for a path without the quotes the P&L needs)."""

    candidates: list[Candidate] = field(default_factory=list)
    counts: Counter[str] = field(default_factory=Counter)


@dataclass(frozen=True)
class Grid:
    thresholds: tuple[float, ...] = THRESHOLDS
    kelly_fractions: tuple[float, ...] = KELLY_FRACTIONS
    threshold: float = THRESHOLD  # the defaults ties go to: the trader's
    kelly_fraction: float = KELLY_FRACTION
    min_trades: int = MIN_TRADES

    def points(self) -> list[tuple[float, float]]:
        """Every (θ, λ), in the order ties are kept by (module doc)."""
        return sorted(
            product(self.thresholds, self.kelly_fractions),
            key=lambda g: (
                round(abs(g[0] - self.threshold), 9),
                -g[0],
                round(abs(g[1] - self.kelly_fraction), 9),
                g[1],
            ),
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "thresholds": list(self.thresholds),
            "kelly_fractions": list(self.kelly_fractions),
            "defaults": {"threshold": self.threshold, "kelly_fraction": self.kelly_fraction},
            "min_trades": self.min_trades,
        }


@dataclass(frozen=True)
class Point:
    """A cell at a point of the grid."""

    pick: Pick
    threshold: float
    kelly_fraction: float

    @property
    def fraction(self) -> float:
        """The share of the capital it buys: λ × f*, cut to 0–1 (``policy.apply``'s)."""
        return min(1.0, max(0.0, self.kelly_fraction * (self.pick.kelly or 0.0)))


@dataclass
class Tally:
    """Simulated trades: how many, those that gained, the net in won and the capital's log growth."""

    trades: int = 0
    wins: int = 0
    net: float = 0.0
    growth: float = 0.0

    def add(self, net: float, capital: float) -> None:
        self.trades += 1
        self.wins += net > 0
        self.net += net
        self.growth += math.log1p(net / capital) if net > -capital else -math.inf

    def as_json(self) -> dict[str, Any]:
        growth = self.growth if math.isfinite(self.growth) else None  # a trade that lost the capital
        return {"trades": self.trades, "wins": self.wins, "net": round(self.net, 3), "growth": growth}


@dataclass(frozen=True)
class Result:
    """A policy's simulated days: all its trades, each cell's, and the net of each day traded."""

    total: Tally
    cells: Mapping[Cell, Tally]
    days: Mapping[date, float]


def simulate(
    points: Sequence[Point], candidates: Mapping[Cell, Sequence[Candidate]], capital: float
) -> Result:
    """The trades of ``points`` (cells at their θ and λ, first ranked first) on their candidates
    (module doc: the first ranked rise of a bar close is decided, if nothing is held; it buys
    nothing when its ask reached the target or its size is below one share)."""
    eligible = [
        (c.bar_close, rank, c)
        for rank, point in enumerate(points)
        for c in candidates.get(point.pick.cell, ())
        if c.probability >= point.threshold
    ]
    eligible.sort(key=itemgetter(0, 1))  # one prediction a cell a bar close: never a tie
    total = Tally()
    cells = {p.pick.cell: Tally() for p in points}
    days: dict[date, float] = {}
    free_from: datetime | None = None
    decided: datetime | None = None
    for close, rank, c in eligible:
        if close == decided or (free_from is not None and close < free_from):
            continue  # outranked, or something is held
        decided = close
        point = points[rank]
        quantity = shares(point.fraction, capital, c.ask)
        if not c.bought or quantity < 1:
            continue  # target_reached, no_shares: nothing bought, nothing held
        net = quantity * c.profit * c.tick
        total.add(net, capital)
        cells[point.pick.cell].add(net, capital)
        day = kst_day(close)
        days[day] = days.get(day, 0.0) + net
        free_from = c.exit_at
    return Result(total, cells, days)


@dataclass(frozen=True)
class Fitted:
    """The fit's outcome: the points kept (the policy's cells), the cells left out and why, and the
    kept policy's simulated days."""

    kept: tuple[Point, ...]
    left_out: tuple[dict[str, Any], ...]
    result: Result


def fit(
    picks: Sequence[Pick], candidates: Mapping[Cell, Sequence[Candidate]], capital: float, grid: Grid
) -> Fitted:
    """The cells' θ and λ (module doc "The fit")."""
    kept: list[Point] = []
    left_out: list[dict[str, Any]] = []
    current = simulate(kept, candidates, capital)
    for pick in picks:
        best: tuple[Point, Result] | None = None
        tried: tuple[Point, Result] | None = None  # the best with enough trades, kept or not
        most = 0
        for threshold, fraction in grid.points():
            point = Point(pick, threshold, fraction)
            result = simulate([*kept, point], candidates, capital)
            made = result.cells[pick.cell].trades
            most = max(most, made)
            if made < grid.min_trades:
                continue
            if tried is None or result.total.growth > tried[1].total.growth:
                tried = (point, result)
            if result.total.growth > (current if best is None else best[1]).total.growth:
                best = (point, result)
        if best is not None:
            kept.append(best[0])
            current = best[1]
            continue
        out: dict[str, Any] = {"kind": pick.kind, "role": pick.role, "predictor": pick.predictor}
        out |= {"p_star": pick.p_star, "low": pick.low, "kelly": pick.kelly}
        if tried is None:
            out |= {"why": FEW_TRADES, "most_trades": most}
        else:
            point, result = tried
            best_point = {"threshold": point.threshold, "kelly_fraction": point.kelly_fraction}
            out |= {"why": NO_GAIN, "best": best_point | result.cells[pick.cell].as_json()}
        left_out.append(out)
    return Fitted(tuple(kept), tuple(left_out), current)


# Reading the stored predictions.


def _on_days(days: Sequence[date]) -> tuple[str, list[Any]]:
    """A condition on predictions ``p`` of 005930 made on ``days``, as the stats ask it
    (``predictions._made_on``): a time range for the index, then the days themselves."""
    marks = ", ".join("?" for _ in days)
    where = (
        f"p.symbol = ? AND p.bar_close >= ? AND p.bar_close < ? AND substr(p.bar_close, 1, 10) IN ({marks})"
    )
    params: list[Any] = [
        SYMBOL,
        datetime.combine(min(days), time(), KST).isoformat(),
        datetime.combine(max(days) + timedelta(days=1), time(), KST).isoformat(),
        *(d.isoformat() for d in days),
    ]
    return where, params


def read_cell(
    db: sqlite3.Connection,
    pick: Pick,
    days: Sequence[date],
    sessions: Callable[[date], Session | None],
    windows: Sequence[Window],
    fee_rate: float,
    target_version: int,
) -> CellRead:
    """``pick``'s stored predictions of ``days`` and their answers, as the cost map counts them, read
    a row at a time (module doc "From what")."""
    read = CellRead()
    if not days:
        return read
    kind = traded_kind(pick.kind)
    rules = Rules(threshold=0.0, fee_rate=fee_rate)  # every rise: θ is the simulation's
    rows = _direction_rows(db, pick, days) if kind.name == DIRECTION else _kind_rows(db, pick, days)
    # An algorithm's predictions by models trained on the answers they are scored on, as the
    # statistics keep them (``stats.on_target``); the direction's only from touch target 2 on.
    target = kind.answer_version if kind.name != DIRECTION else target_version
    # Every one of a baseline's and a consensus's.
    every = pick.role != "top" or (kind.name == DIRECTION and target_version < 2)
    for row, signal, path, label in rows:
        read.counts["read"] += 1
        if not every and row["model_target"] != target:
            read.counts["off_target"] += 1
            continue
        planned = plan(signal, rules, sessions(kst_day(signal.bar_close)), signal.displayed_at, windows)
        if not isinstance(planned, Plan):
            read.counts[planned] += 1
            continue
        candidate = _candidate(signal, planned, path, label, fee_rate)
        if candidate is None:
            read.counts["unpriced"] += 1
            continue
        read.counts["rises"] += 1
        read.candidates.append(candidate)
    return read


Row = tuple[sqlite3.Row, Signal, Mapping[str, Any], str]


def _direction_rows(db: sqlite3.Connection, pick: Pick, days: Sequence[date]) -> Iterator[Row]:
    on, params = _on_days(days)
    cursor = db.execute(
        "SELECT p.id, p.bar_close, p.displayed_at, p.status, p.p_up, p.p_flat, p.p_down, o.mid_close,"
        " o.flat_ticks, b.reason AS blocked, t.class AS label, t.path AS path,"
        " m.target_version AS model_target"
        " FROM predictions p JOIN outcomes o ON o.prediction_id = p.id"
        f" LEFT JOIN blocks b ON b.prediction_id = p.id {TOUCH_JOIN}"
        " LEFT JOIN model_targets m ON m.algorithm = p.algorithm AND m.version = p.model_version"
        f" WHERE p.algorithm = ? AND {on} AND {SCORED} AND o.special = 0 ORDER BY p.bar_close, p.id",
        (pick.predictor, *params),
    )
    kind = traded_kind(DIRECTION)
    for r in cursor:
        anchor = r["mid_close"]
        signal = Signal(
            prediction_id=r["id"],
            kind=DIRECTION,
            predictor=pick.predictor,
            symbol=SYMBOL,
            bar_close=datetime.fromisoformat(r["bar_close"]),
            displayed_at=datetime.fromisoformat(r["displayed_at"]),
            status=r["status"],
            probabilities=(r["p_up"], r["p_flat"], r["p_down"]),
            rise=kind.index(kind.sides[0]) if kind.sides else 0,
            anchor=anchor,
            tick=None if anchor is None else float(tick_size(anchor)),
            width=r["flat_ticks"],
            horizon=kind.horizon,
            blocked=r["blocked"],
        )
        yield r, signal, json.loads(r["path"]), r["label"]


def _kind_rows(db: sqlite3.Connection, pick: Pick, days: Sequence[date]) -> Iterator[Row]:
    kind = traded_kind(pick.kind)
    on, params = _on_days(days)
    cursor = db.execute(
        "SELECT p.id, p.bar_close, p.displayed_at, p.status, p.output, p.anchor, p.tick, p.width_ticks,"
        " a.class AS label, a.path AS path, m.target_version AS model_target"
        f" FROM kind_predictions p {KIND_ANSWER_JOIN}"
        " LEFT JOIN model_targets m ON m.algorithm = p.algorithm AND m.version = p.model_version"
        f" WHERE p.kind = ? AND p.algorithm = ? AND {on} AND {KIND_SCORED} AND a.special = 0"
        " ORDER BY p.bar_close, p.id",
        (kind.answer_version, kind.name, pick.predictor, *params),
    )
    rise = kind.index(kind.sides[0]) if kind.sides else 0
    for r in cursor:
        try:
            probabilities = kind.probabilities(json.loads(r["output"]))
        except (ValueError, KeyError, TypeError):
            log.warning("refit: kind prediction %s has an output it cannot read", r["id"])
            continue
        signal = Signal(
            prediction_id=r["id"],
            kind=kind.name,
            predictor=pick.predictor,
            symbol=SYMBOL,
            bar_close=datetime.fromisoformat(r["bar_close"]),
            displayed_at=datetime.fromisoformat(r["displayed_at"]),
            status=r["status"],
            probabilities=probabilities,
            rise=rise,
            anchor=r["anchor"],
            tick=None if r["tick"] is None else float(r["tick"]),
            width=r["width_ticks"],
            horizon=kind.horizon,
        )
        yield r, signal, json.loads(r["path"]), r["label"]


def _candidate(
    signal: Signal, planned: Plan, path: Mapping[str, Any], label: str, fee_rate: float
) -> Candidate | None:
    """The trade a rise makes on its answer's path (module doc); None without the quotes it needs."""
    probability = signal.rise_probability
    assert probability is not None  # planned: made, with its probabilities
    ask: Any
    tick: Any
    if signal.kind == DIRECTION:
        prices = Prices.of(path)
        if prices is None or prices.ask_start is None:
            return None
        profit = touch_profit(Direction.UP, prices, fee_rate)
        ask, tick = prices.ask_start, prices.tick
        reached = path.get("upper_at") if prices.upper_reached else None
    else:
        ask, tick = path.get("ask_start"), path.get("tick")
        profit = barrier_profit(UP_FIRST, label, path)
        reached = {UP_FIRST: path.get("up_at"), DOWN_FIRST: path.get("down_at")}.get(label)
    if profit is None or ask is None or not tick:
        return None
    exit_at = datetime.fromisoformat(reached) if isinstance(reached, str) else planned.time_barrier
    return Candidate(
        bar_close=signal.bar_close,
        probability=probability,
        ask=float(ask),
        bought=float(ask) < planned.target,
        profit=float(profit),
        tick=float(tick),
        exit_at=exit_at,
    )


def cost_map_days(body: Mapping[str, Any]) -> dict[str, tuple[date, ...]]:
    """Each row's days (the cost map's period for its kind), by kind; a row without readable days
    has none."""
    out: dict[str, tuple[date, ...]] = {}
    for row in body.get("rows") or ():
        if not isinstance(row, Mapping) or not isinstance(row.get("kind"), str):
            continue
        try:
            out[row["kind"]] = tuple(sorted(date.fromisoformat(d) for d in row.get("days") or ()))
        except (TypeError, ValueError):
            out[row["kind"]] = ()
    return out


# The job.


@dataclass(frozen=True)
class Refitted:
    """A night's re-fit, as kept and reported."""

    version: int
    champion: bool  # it became the champion (the first)
    champion_version: int | None  # the champion's now
    cells: tuple[policies.PolicyCell, ...]
    left_out: tuple[dict[str, Any], ...]
    fit: Mapping[str, Any]
    passed: int  # the cells the cost map passed
    # #400: the champion or running challenger it equals (then it is not kept: ``version`` is that
    # one's), whether it was admitted to a running slot, the challengers running after it (oldest
    # first) and the policies waiting (newest first).
    same_as: int | None = None
    admitted: bool = False
    challengers: tuple[int, ...] = ()
    waiting: tuple[int, ...] = ()


def refit_from(
    body: Mapping[str, Any],
    engine_db: Path,
    trader_db: Path,
    sessions: Callable[[date], Session | None],
    *,
    capital: float,
    fee_rate: float,
    target_version: int,
    grid: Grid,
    now: datetime,
    commit: str = "unknown",
    meter: Meter | None = None,
) -> Refitted:
    """Fit a policy on the cost map ``body`` and the engine's database at ``engine_db`` (read-only),
    and keep it in the trader's store at ``trader_db``. ``CostMapError`` when ``body`` is not the cost
    map asked for."""
    meter = meter or Meter()
    started = clock.monotonic()
    meter.reset()
    start_mb = meter.rss_mb()
    selection = choose(body)
    days_of = cost_map_days(body)
    candidates: dict[Cell, list[Candidate]] = {}
    seen: dict[Cell, dict[str, int]] = {}
    if selection.picks:
        db = open_engine_db(engine_db)
        try:
            first = min((min(d) for d in days_of.values() if d), default=kst_day(now))
            windows = special_windows(db, SYMBOL, datetime.combine(first, time(), KST))
            for pick in selection.picks:
                days = days_of.get(pick.kind, ())
                read = read_cell(db, pick, days, sessions, windows, fee_rate, target_version)
                candidates[pick.cell] = read.candidates
                seen[pick.cell] = dict(sorted(read.counts.items()))
        finally:
            db.close()
    fitted = fit(selection.picks, candidates, capital, grid)
    cells = tuple(
        policies.PolicyCell(
            kind=p.pick.kind,
            role=p.pick.role,
            predictor=p.pick.predictor,
            threshold=p.threshold,
            kelly_fraction=p.kelly_fraction,
            fitted={
                "p_star": p.pick.p_star,
                "low": p.pick.low,
                "kelly": p.pick.kelly,
                "fraction": p.fraction,
                "days": [d.isoformat() for d in days_of.get(p.pick.kind, ())],
                "read": seen.get(p.pick.cell, {}),
                **fitted.result.cells[p.pick.cell].as_json(),
            },
        )
        for p in fitted.kept
    )
    left_out = tuple({**x, "read": seen.get(Cell(x["kind"], x["predictor"]), {})} for x in fitted.left_out)
    result = fitted.result
    fit_json: dict[str, Any] = {
        "cost_map": {"as_of": selection.as_of, "period": body.get("period"), "passed": len(selection.picks)},
        "grid": grid.as_json(),
        "fee_rate": fee_rate,
        "result": {**result.total.as_json(), "days_traded": len(result.days)},
        "run": {
            "commit": commit,
            "seconds": round(clock.monotonic() - started, 2),
            "rss_start_mb": start_mb,
            "peak_rss_mb": meter.peak_mb(),
            "rss_mb": meter.rss_mb(),
            "peak_since": meter.peak_since,
        },
    }
    night = kst_day(now)
    reason = (
        f"{night} 밤 맞추기: 비용 지도({selection.as_of})가 시장가 왕복으로 통과시킨 "
        f"{len(selection.picks)}칸 가운데 {len(cells)}칸"
    )
    store = policies.connect(trader_db)
    try:
        try:
            same = policies.same_as(store, cells)
        except policies.PolicyError as e:  # one it would be compared with cannot be read: kept
            log.warning("refit: cannot compare with the policies running (%s): kept all the same", e)
            same = None
        if same is None:
            version, first_champion = policies.add(store, now, reason, cells, left_out, fit_json)
        else:
            version, first_champion = same, False
        champion = policies.champion_version(store)
        admitted = version in policies.admitted_at(store, now)
        challengers = tuple(policies.challenger_versions(store))
        waiting = tuple(policies.waiting_versions(store))
    finally:
        store.close()
    return Refitted(
        version=version,
        champion=first_champion,
        champion_version=champion,
        cells=cells,
        left_out=left_out,
        fit=fit_json,
        passed=len(selection.picks),
        same_as=same,
        admitted=admitted,
        challengers=challengers,
        waiting=waiting,
    )


def report(done: Refitted) -> str:
    """The job's output, for the issue (no price: cells, values, counts, won and growth)."""
    fit_json = done.fit
    cost_map = fit_json["cost_map"]
    if done.champion:
        head = f"정책 {done.version} · 챔피언(처음 맞춘 정책)"
    elif done.same_as is not None:
        whose = "챔피언" if done.same_as == done.champion_version else "도는 도전 정책"
        head = f"새 정책 없음 · 맞춘 값이 {whose} 정책 {done.same_as}과 같아 저장하지 않았다"
    elif done.admitted:
        head = (
            f"정책 {done.version} · 도전 정책(챔피언은 정책 {done.champion_version}, "
            "다음 거래일부터 모의로 함께 돈다)"
        )
    else:
        head = (
            f"정책 {done.version} · 기다리는 정책"
            "(도전 정책 셋이 돌고 있다: 자리가 나면 가장 새 것부터 들어간다)"
        )
    grid = fit_json["grid"]
    lines = [
        f"정책 맞추기 (#399): {head}",
        f"비용 지도: {SYMBOL} {cost_map['period']}, as_of {cost_map['as_of']}, "
        f"시장가 왕복 통과 {done.passed}칸",
        "격자: θ "
        + " ".join(f"{t:g}" for t in grid["thresholds"])
        + " × λ "
        + " ".join(f"{k:g}" for k in grid["kelly_fractions"])
        + f" · 칸마다 모의 매매 {grid['min_trades']}건 이상",
        "",
    ]
    if not done.cells:
        lines.append("매매할 칸: 없음" + (" (비용 지도를 통과한 칸이 없다)" if not done.passed else ""))
    for i, c in enumerate(done.cells, 1):
        f = c.fitted
        lines.append(
            f"{i}. {c.kind} {c.predictor} ({c.role}): θ {c.threshold:g}, λ {c.kelly_fraction:g} → 비율 "
            f"{f['fraction']:.2f} · 모의 {f['trades']}건(이익 {f['wins']}) · 순손익 {f['net']:+,.0f}원 · "
            f"로그 성장 {_growth(f['growth'])} · 읽은 예측 {f['read'].get('read', 0)}, "
            f"오름 {f['read'].get('rises', 0)}"
        )
    for x in done.left_out:
        why = x["why"]
        if why == FEW_TRADES:
            detail = f"모의 매매가 {grid['min_trades']}건에 못 미침(가장 많을 때 {x['most_trades']}건)"
        else:
            best = x["best"]
            detail = (
                f"넣어도 정책의 로그 성장이 늘지 않음(가장 나은 θ {best['threshold']:g}, λ "
                f"{best['kelly_fraction']:g}: 모의 {best['trades']}건, 순손익 {best['net']:+,.0f}원)"
            )
        lines.append(f"뺀 칸: {x['kind']} {x['predictor']} ({x['role']}): {why}, {detail}")
    if done.challengers:
        lines.append("모의로 도는 도전 정책(셋까지): " + ", ".join(f"정책 {v}" for v in done.challengers))
    if done.waiting:
        shown = ", ".join(f"정책 {v}" for v in done.waiting[:WAITING_SHOWN])
        more = f" 외 {len(done.waiting) - WAITING_SHOWN}개" if len(done.waiting) > WAITING_SHOWN else ""
        lines.append(f"자리를 기다리는 정책(새 것부터): {shown}{more}")
    total = fit_json["result"]
    run = fit_json["run"]
    lines += [
        "",
        f"정책 전체: 모의 {total['trades']}건(이익 {total['wins']}), 매매한 날 {total['days_traded']}일, "
        f"순손익 {total['net']:+,.0f}원, 로그 성장 {_growth(total['growth'])}",
        f"메모리: 최대 RSS {_mb(run['peak_rss_mb'])} MB("
        + ("맞추기 동안" if run["peak_since"] == "step" else "프로세스 시작부터")
        + f"), 시작 {_mb(run['rss_start_mb'])} MB, 끝 {_mb(run['rss_mb'])} MB · {run['seconds']:.1f}초",
    ]
    return "\n".join(lines)


def _growth(value: float | None) -> str:
    return "−∞" if value is None else f"{value:+.5f}"  # None: a trade lost the whole capital


def _mb(value: float | None) -> str:
    return "?" if value is None else f"{value:.1f}"


def promote_now(trader_db: Path, sessions: Callable[[date], Session | None], now: datetime) -> None:
    """The night's promotion (#400) on the trader's store, its report printed (and why not, when
    the store cannot be read or written: the re-fit goes on). Without a store there is no champion
    yet, and none is made for it."""
    if not trader_db.exists():
        print(
            promotion.report(
                promotion.Promotion(now, None, (), None, promotion.NO_CHAMPION, promotion.no_gate())
            )
            + "\n"
        )
        return
    try:
        db = policies.connect(trader_db)
        try:
            done = promotion.promote(db, now, sessions)
        finally:
            db.close()
    except (sqlite3.Error, policies.PolicyError) as e:
        print(f"도전 정책 판정 (#400): 저장소를 읽거나 쓰지 못해 판정하지 않았다: {e}\n")
        log.error("promotion: %s", e)
        return
    if done.promoted is not None:
        log.warning(
            "promotion: policy %s is the champion now, in place of policy %s", done.promoted, done.champion
        )
    else:
        log.info("promotion: policy %s stays the champion (%s)", done.champion, done.deferred or "judged")
    print(promotion.report(done) + "\n")


async def run_refit(
    settings: Settings, trader: TraderSettings, now: datetime, engine_url: str | None = None
) -> int:
    """``krx-quant refit-policy``: first the challengers judged and one promoted if it beat the
    champion (#400, ``promotion.promote``), then tonight's cost map from the Engine API
    (``engine_url``, else the trader's ``KRX_TRADER_ENGINE_URL``), the fit, the policy kept; both
    reports printed. 0 when a policy is kept, ``NO_POLICY_KEPT`` when the cost map or the engine's
    database cannot be read (the promotion is judged all the same)."""
    url = engine_url or trader.engine_url
    calendar = calendar_store(settings).calendar
    promote_now(settings.state_dir / TRADER_DB, calendar.session, now)
    try:
        return await _refit(settings, trader, url, calendar.session, now)
    finally:
        admit_now(settings.state_dir / TRADER_DB, now)


def admit_now(trader_db: Path, now: datetime) -> None:
    """The night's end (#400): the running slots freed tonight (a promotion, a challenger dropped)
    and not taken by tonight's fit given to the newest policies waiting; printed when any is."""
    if not trader_db.exists():
        return
    try:
        db = policies.connect(trader_db)
        try:
            admitted = policies.admit(db, now)
        finally:
            db.close()
    except sqlite3.Error as e:
        print(f"도전 정책 자리 (#400): 저장소를 쓰지 못해 기다리는 정책을 들이지 못했다: {e}")
        log.error("admission: %s", e)
        return
    if admitted:
        log.info("admission: policy %s take(s) a challenger's slot", ", ".join(map(str, admitted)))
        print(
            "\n도전 정책 자리 (#400): 빈 자리에 들어간 정책(다음 거래일부터 돈다): "
            + ", ".join(f"정책 {v}" for v in admitted)
        )


async def _refit(
    settings: Settings,
    trader: TraderSettings,
    url: str,
    sessions: Callable[[date], Session | None],
    now: datetime,
) -> int:
    try:
        async with aiohttp.ClientSession() as session:
            body = await read_cost_map(session, url)
    except CostMapError as e:
        print(f"정책 맞추기 (#399): 비용 지도를 읽지 못해 정책을 만들지 않았다: {e}")
        log.error("refit: could not read the cost map (%s): no policy kept tonight", e)
        return NO_POLICY_KEPT
    engine_db = settings.state_dir / ENGINE_DB
    if not engine_db.exists():
        print(f"정책 맞추기 (#399): 엔진 DB가 없어({engine_db}) 정책을 만들지 않았다")
        return NO_POLICY_KEPT
    try:
        done = refit_from(
            body,
            engine_db,
            settings.state_dir / TRADER_DB,
            sessions,
            capital=trader.capital,
            fee_rate=settings.fee_rate,
            target_version=settings.target_version,
            grid=Grid(threshold=trader.threshold),
            now=now,
            commit=settings.commit,
        )
    except CostMapError as e:
        print(f"정책 맞추기 (#399): 비용 지도가 아니어서 정책을 만들지 않았다: {e}")
        return NO_POLICY_KEPT
    except sqlite3.Error as e:
        print(f"정책 맞추기 (#399): DB를 읽거나 쓰지 못해 정책을 만들지 않았다: {e}")
        log.error("refit: %s", e)
        return NO_POLICY_KEPT
    run = done.fit["run"]
    log.info(
        "refit: policy %s kept (%s), %s of %s cell(s); peak RSS %s MB (since the %s), %.1fs",
        done.version,
        "the champion"
        if done.champion
        else f"the same values as policy {done.same_as}: not kept again"
        if done.same_as is not None
        else "a challenger running"
        if done.admitted
        else "a challenger waiting",
        len(done.cells),
        done.passed,
        _mb(run["peak_rss_mb"]),
        "fit" if run["peak_since"] == "step" else "process started",
        run["seconds"],
    )
    print(report(done))
    return 0
