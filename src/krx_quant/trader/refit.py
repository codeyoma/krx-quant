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
each cell's threshold θ, or leaves the cell out.

**From what** (#470): the recorded days, replayed by the paper trader's own rules
(``replay.py``): each cell's predictions of 005930 on the cost map's days for its kind (the row's
``days``: the latest 20 with predictions), read from the engine's database read-only as the trader
reads them (an algorithm's by models trained on the answers they are scored on,
``stats.on_target``), and each day's quotes as the collector recorded them and streamed them to the
trader. Nothing is predicted again, and no answer is read.

**The trades replayed** (``replay.Policy``): the policy's cells in their rank at their θ, one
position at a time, as the paper book would have traded them on those quotes: a rise decided as
``paper.plan`` and ``paper.settle`` decide it (the entry rules at the cell's θ, with the special
windows known then; of a bar close's rises the first ranked buys, a rise waiting for the cells
ranked above it; the risk rules, ``paper.risk_held``, #471), bought at the best ask of the first quote
after the decision, as many shares as the trader's loss budget pays (``paper.size``, #471: within the
cash, the capital plus the replay's net so far), sold at the target once a best bid reaches it, at the
first best bid at or below the stop (the direction's too), or at the time barrier, or at once by a
loss limit simulated (``limits.Simulated``); its net in won after the commission on both fills and
the sell tax. A signal in a gap of the recording, or of a day without one, is left out and counted,
not traded.

**The fit** (``fit``): the cells one at a time in rank order, each at every point of the grid
(``Grid``: θ in ``THRESHOLDS``) with the cells kept before it as they were kept. The point whose
replayed days grow the capital most (Σ log(1 + net ÷ capital) over the trades) is kept when the cell
made at least ``MIN_TRADES`` trades there and it grows the capital more than leaving the cell out. Of
points that grow it alike, the one nearest the trader's default θ (``KRX_TRADER_THRESHOLD``) is
kept, then the higher. A cell kept at no point is left out, and why: ``few_trades`` (fewer than
``MIN_TRADES`` at every point), ``no_gain`` (no point grows the capital more). The same predictions,
recordings, cost map, calendar and settings make the same policy.

> **바뀜** (2026-11-03, #478): the grid was θ × λ (``KELLY_FRACTIONS`` 0.25, 0.5, 1), its ties going to
> the λ nearest ``KELLY_FRACTION`` then the lower → θ alone. 이유: #471 took λ out of the policy (spec
> #468 4 "크기"); the change is that alone, the fit's target is #472's.

**Each signal alone** (#470, ``replay.Signals``): each cell's rises, each measured alone a share at
each θ of the grid, kept in its policy cell (``fitted.signals``) or in its entry of the cells left
out, and shown in the report. The fit does not use them yet (#472).

**Limits.** None is fitted nor kept: the capital, the loss budget, the risk rules and the loss limits
hold the replayed trades as they hold the trader's (``KRX_TRADER_CAPITAL``, #471's settings), and the
fit only writes ``policies`` and ``champions``.

**Memory** (the server has 2 GB; pre-validation replays have been killed for it): a pass of the
replay for each cell the cost map passes, its points of the grid replayed together with the cells
kept before it; a pass reads one day at a time, its recording once, and lets it go before the next
(``replay.Replayer``). Each day's peak resident memory (``footprint.Meter``, the highest of its
passes) is kept in the policy's ``fit.run.days``, the highest printed and logged.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time as clock
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import aiohttp

from krx_quant.config import Settings
from krx_quant.feeds.base import KST, kst_day
from krx_quant.footprint import Meter
from krx_quant.kinds import DIRECTION
from krx_quant.market_calendar import Session, calendar_store
from krx_quant.predictions import DB_NAME as ENGINE_DB
from krx_quant.predictions import KIND_ANSWER_JOIN, KIND_SCORED, SCORED, TOUCH_JOIN
from krx_quant.pricing import tick_size
from krx_quant.special import Window
from krx_quant.trader import SYMBOL, THRESHOLD, promotion, review
from krx_quant.trader import policy as policies
from krx_quant.trader.cells import Cell, Signal, open_engine_db, special_windows, traded_kind
from krx_quant.trader.paper import NO_QUOTE, Plan, Rules, plan
from krx_quant.trader.replay import (
    GAP,
    GAPPED,
    NO_EXIT_QUOTE,
    NO_RECORDING,
    OFF_TARGET,
    Pass,
    Ranked,
    Replayer,
    Result,
    Source,
    Tally,
    target_of,
)
from krx_quant.trader.selection import CostMapError, Pick, choose, read_cost_map
from krx_quant.trader.settings import TraderSettings
from krx_quant.trader.store import DB_NAME as TRADER_DB

log = logging.getLogger(__name__)

# The grid (#399: small): the thresholds θ tried for each cell (#471: no Kelly fraction λ).
THRESHOLDS = (0.4, 0.5, 0.6, 0.7, 0.8)
# A cell is kept at a point only if it made this many replayed trades there: one a trading day of
# the cost map's 20 on average, so a θ is not picked for a few lucky trades.
MIN_TRADES = 20
# Why a cell the cost map passed is left out.
FEW_TRADES, NO_GAIN = "few_trades", "no_gain"
# The waiting policies the night's output names, newest first.
WAITING_SHOWN = 5
# What the job's exit code says: 0 a policy kept (or the same values running already), 1 none (the
# cost map or the engine's database could not be read).
NO_POLICY_KEPT = 1


@dataclass(frozen=True)
class Grid:
    thresholds: tuple[float, ...] = THRESHOLDS
    threshold: float = THRESHOLD  # the default ties go to: the trader's
    min_trades: int = MIN_TRADES

    def points(self) -> list[float]:
        """Every θ, in the order ties are kept by (module doc)."""
        return sorted(self.thresholds, key=lambda t: (round(abs(t - self.threshold), 9), -t))

    def as_json(self) -> dict[str, Any]:
        return {
            "thresholds": list(self.thresholds),
            "defaults": {"threshold": self.threshold},
            "min_trades": self.min_trades,
        }


@dataclass(frozen=True)
class Point:
    """A cell at a point of the grid."""

    pick: Pick
    threshold: float

    def ranked(self) -> Ranked:
        """As the replay trades it."""
        return Ranked(self.pick.cell, self.threshold)


@dataclass(frozen=True)
class Fitted:
    """The fit's outcome: the points kept (the policy's cells), the cells left out and why, and the
    kept policy's replayed days."""

    kept: tuple[Point, ...]
    left_out: tuple[dict[str, Any], ...]
    result: Result


# A pass of the replay as the fit asks for one (``replay.Replayer.run``): with the pick it fits, the
# policies to replay, each its points in rank order; their results, in that order.
Replay = Callable[[Pick, Sequence[Sequence[Point]]], Sequence[Result]]


def fit(picks: Sequence[Pick], replay: Replay, grid: Grid) -> Fitted:
    """The cells' θ (module doc "The fit"): one pass of ``replay`` a pick, every point of the grid
    replayed in it with the points kept before."""
    kept: list[Point] = []
    left_out: list[dict[str, Any]] = []
    current = Result(Tally(), {}, {})  # no cell: no trade
    for pick in picks:
        points = [Point(pick, threshold) for threshold in grid.points()]
        results = replay(pick, [[*kept, point] for point in points])
        best: tuple[Point, Result] | None = None
        tried: tuple[Point, Result] | None = None  # the best with enough trades, kept or not
        most = 0
        for point, result in zip(points, results, strict=True):
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
            best_point = {"threshold": point.threshold}
            out |= {"why": NO_GAIN, "best": best_point | result.cells[pick.cell].as_json()}
        left_out.append(out)
    return Fitted(tuple(kept), tuple(left_out), current)


# The stored predictions with their answers, as the cost map counts them: the passive entry's
# measurement reads its signals so (#432, ``passive.py``).


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


def rises(
    db: sqlite3.Connection,
    cell: Cell,
    role: str,
    days: Sequence[date],
    sessions: Callable[[date], Session | None],
    windows: Sequence[Window],
    target_version: int,
    counts: Counter[str],
) -> Iterator[tuple[Signal, Plan, Mapping[str, Any], str]]:
    """``cell``'s stored predictions of ``days`` that ``paper.plan`` lets buy at some θ (its rises,
    the entry rules met), each with its plan, its answer's path and its answer's class; read a row
    at a time, as the cost map counts them: made, their answer known and no special situation in
    their window, an algorithm's by models trained on its target. Each row read is counted in
    ``counts`` (``read``), and each kept out by why: ``off_target``, ``paper.plan``'s reasons. The
    passive entry's measurement (#432, ``passive.py``) reads its signals so; the re-fit replays the
    recordings instead (#470, ``replay.day_reads``)."""
    if not days:
        return
    kind = traded_kind(cell.kind)
    rules = Rules(threshold=0.0)  # every rise: θ is the caller's
    rows = _direction_rows(db, cell, days) if kind.name == DIRECTION else _kind_rows(db, cell, days)
    # An algorithm's predictions by models trained on the answers they are scored on, as the
    # statistics keep them (``stats.on_target``); every one of a baseline's and a consensus's.
    target = target_of(cell, role, target_version)
    for row, signal, path, label in rows:
        counts["read"] += 1
        if target is not None and row["model_target"] != target:
            counts[OFF_TARGET] += 1
            continue
        planned = plan(signal, rules, sessions(kst_day(signal.bar_close)), signal.displayed_at, windows)
        if not isinstance(planned, Plan):
            counts[planned] += 1
            continue
        yield signal, planned, path, label


Row = tuple[sqlite3.Row, Signal, Mapping[str, Any], str]


def _direction_rows(db: sqlite3.Connection, cell: Cell, days: Sequence[date]) -> Iterator[Row]:
    on, params = _on_days(days)
    cursor = db.execute(
        "SELECT p.id, p.bar_close, p.displayed_at, p.status, p.p_up, p.p_flat, p.p_down, o.mid_close,"
        " o.flat_ticks, b.reason AS blocked, t.class AS label, t.path AS path,"
        " m.target_version AS model_target"
        " FROM predictions p JOIN outcomes o ON o.prediction_id = p.id"
        f" LEFT JOIN blocks b ON b.prediction_id = p.id {TOUCH_JOIN}"
        " LEFT JOIN model_targets m ON m.algorithm = p.algorithm AND m.version = p.model_version"
        f" WHERE p.algorithm = ? AND {on} AND {SCORED} AND o.special = 0 ORDER BY p.bar_close, p.id",
        (cell.predictor, *params),
    )
    kind = traded_kind(DIRECTION)
    for r in cursor:
        anchor = r["mid_close"]
        signal = Signal(
            prediction_id=r["id"],
            kind=DIRECTION,
            predictor=cell.predictor,
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


def _kind_rows(db: sqlite3.Connection, cell: Cell, days: Sequence[date]) -> Iterator[Row]:
    kind = traded_kind(cell.kind)
    on, params = _on_days(days)
    cursor = db.execute(
        "SELECT p.id, p.bar_close, p.displayed_at, p.status, p.output, p.anchor, p.tick, p.width_ticks,"
        " a.class AS label, a.path AS path, m.target_version AS model_target"
        f" FROM kind_predictions p {KIND_ANSWER_JOIN}"
        " LEFT JOIN model_targets m ON m.algorithm = p.algorithm AND m.version = p.model_version"
        f" WHERE p.kind = ? AND p.algorithm = ? AND {on} AND {KIND_SCORED} AND a.special = 0"
        " ORDER BY p.bar_close, p.id",
        (kind.answer_version, kind.name, cell.predictor, *params),
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
            predictor=cell.predictor,
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
    data_dir: Path,
    sessions: Callable[[date], Session | None],
    *,
    rules: Rules,
    target_version: int,
    grid: Grid,
    now: datetime,
    commit: str = "unknown",
    meter: Meter | None = None,
) -> Refitted:
    """Fit a policy on the cost map ``body``, the engine's database at ``engine_db`` (read-only) and
    the recordings under ``data_dir`` (module doc), the replay held by the trader's ``rules`` (its
    capital, commission, loss budget, risk rules and loss limits), and keep it in the trader's store
    at ``trader_db``. ``CostMapError`` when ``body`` is not the cost map asked for."""
    meter = meter or Meter()
    started = clock.monotonic()
    meter.reset()
    start_mb = meter.rss_mb()
    selection = choose(body)
    days_of = cost_map_days(body)
    alone: dict[Cell, Pass] = {}  # each pick's pass: its signals measured alone
    replayer: Replayer | None = None
    fitted = Fitted((), (), Result(Tally(), {}, {}))  # no pick: nothing replayed
    if selection.picks:
        db = open_engine_db(engine_db)
        try:
            first = min((min(d) for d in days_of.values() if d), default=kst_day(now))
            windows = special_windows(db, SYMBOL, datetime.combine(first, time(), KST))
            sources = {p.cell: Source(p.role, days_of.get(p.kind, ())) for p in selection.picks}
            replayer = Replayer(
                db,
                data_dir,
                sessions,
                windows,
                sources,
                rules=rules,
                target_version=target_version,
                thresholds=grid.thresholds,
                meter=meter,
            )
            run = replayer.run

            def replay(pick: Pick, policies: Sequence[Sequence[Point]]) -> Sequence[Result]:
                done = run([[p.ranked() for p in policy] for policy in policies], alone=pick.cell)
                alone[pick.cell] = done
                return done.results

            fitted = fit(selection.picks, replay, grid)
        finally:
            db.close()

    def measured(cell: Cell) -> dict[str, Any]:
        done = alone.get(cell)
        if done is None:
            return {"read": {}, "signals": {}}
        return {
            "read": dict(done.counts),
            "signals": {f"{t:g}": s.as_json() for t, s in done.signals.items()},
        }

    cells = tuple(
        policies.PolicyCell(
            kind=p.pick.kind,
            role=p.pick.role,
            predictor=p.pick.predictor,
            threshold=p.threshold,
            fitted={
                "p_star": p.pick.p_star,
                "low": p.pick.low,
                "kelly": p.pick.kelly,
                "days": [d.isoformat() for d in days_of.get(p.pick.kind, ())],
                **measured(p.pick.cell),
                **fitted.result.cells[p.pick.cell].as_json(),
            },
        )
        for p in fitted.kept
    )
    left_out = tuple({**x, **measured(Cell(x["kind"], x["predictor"]))} for x in fitted.left_out)
    result = fitted.result
    peaks = {} if replayer is None else replayer.peaks
    recordings = {} if replayer is None else replayer.recordings
    day_peak = max(((mb, d) for d, mb in peaks.items() if mb is not None), default=None)
    fit_json: dict[str, Any] = {
        "evaluation": review.EVALUATION,  # #469: the rules it is fitted and judged under
        "cost_map": {"as_of": selection.as_of, "period": body.get("period"), "passed": len(selection.picks)},
        "grid": grid.as_json(),
        "fee_rate": rules.fee_rate,
        "replay": {  # #470: the recorded days replayed
            "days": len(recordings),
            "no_recording": [d.isoformat() for d, r in sorted(recordings.items()) if r == NO_RECORDING],
            "gaps": [d.isoformat() for d, r in sorted(recordings.items()) if r == GAPPED],
            "passes": 0 if replayer is None else replayer.passes,
            "days_read": 0 if replayer is None else replayer.days_read,
        },
        "result": {
            **result.total.as_json(),
            "days_traded": len(result.days),
            "missing": dict(sorted(result.missing.items())),
        },
        "run": {
            "commit": commit,
            "seconds": round(clock.monotonic() - started, 2),
            "rss_start_mb": start_mb,
            "peak_rss_mb": _highest(meter.peak_mb(), *peaks.values()),
            "rss_mb": meter.rss_mb(),
            "peak_since": meter.peak_since,
            "day_peak_rss_mb": None if day_peak is None else day_peak[0],
            "day_peak_on": None if day_peak is None else day_peak[1].isoformat(),
            "days": {d.isoformat(): mb for d, mb in sorted(peaks.items())},
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
        + f" · 칸마다 모의 매매 {grid['min_trades']}건 이상",
        _replayed(fit_json.get("replay")),
        "",
    ]
    if not done.cells:
        lines.append("매매할 칸: 없음" + (" (비용 지도를 통과한 칸이 없다)" if not done.passed else ""))
    for i, c in enumerate(done.cells, 1):
        f = c.fitted
        lines.append(
            f"{i}. {c.kind} {c.predictor} ({c.role}): θ {c.threshold:g} · 모의 {f['trades']}건(이익 "
            f"{f['wins']}) · 순손익 {f['net']:+,.0f}원 · "
            f"로그 성장 {_growth(f['growth'])} · 읽은 예측 {f['read'].get('read', 0)}, "
            f"오름 {f['read'].get('rises', 0)}"
        )
        lines += _alone(f.get("signals") or {})
    for x in done.left_out:
        why = x["why"]
        if why == FEW_TRADES:
            detail = f"모의 매매가 {grid['min_trades']}건에 못 미침(가장 많을 때 {x['most_trades']}건)"
        else:
            best = x["best"]
            detail = (
                f"넣어도 정책의 로그 성장이 늘지 않음(가장 나은 θ {best['threshold']:g}: 모의 "
                f"{best['trades']}건, 순손익 {best['net']:+,.0f}원)"
            )
        lines.append(f"뺀 칸: {x['kind']} {x['predictor']} ({x['role']}): {why}, {detail}")
        lines += _alone(x.get("signals") or {})
    if done.challengers:
        lines.append("모의로 도는 도전 정책(셋까지): " + ", ".join(f"정책 {v}" for v in done.challengers))
    if done.waiting:
        shown = ", ".join(f"정책 {v}" for v in done.waiting[:WAITING_SHOWN])
        more = f" 외 {len(done.waiting) - WAITING_SHOWN}개" if len(done.waiting) > WAITING_SHOWN else ""
        lines.append(f"자리를 기다리는 정책(새 것부터): {shown}{more}")
    total = fit_json["result"]
    run = fit_json["run"]
    missing = total.get("missing") or {}
    left = (
        f" · 데이터가 빠져 뺀 신호 {sum(missing.values())}({_counted(missing, MISSING_NAMES)})"
        if missing
        else ""
    )
    day_peak = run.get("day_peak_rss_mb")
    lines += [
        "",
        f"정책 전체: 모의 {total['trades']}건(이익 {total['wins']}), 매매한 날 {total['days_traded']}일, "
        f"순손익 {total['net']:+,.0f}원, 로그 성장 {_growth(total['growth'])}{left}",
        f"메모리: 최대 RSS {_mb(run['peak_rss_mb'])} MB("
        + ("맞추기 동안" if run["peak_since"] == "step" else "프로세스 시작부터")
        + ")"
        + ("" if day_peak is None else f", 하루 최대 {_mb(day_peak)} MB({run['day_peak_on']})")
        + f", 시작 {_mb(run['rss_start_mb'])} MB, 끝 {_mb(run['rss_mb'])} MB · {run['seconds']:.1f}초",
    ]
    return "\n".join(lines)


# How the report names why a signal is left out or bought nothing (``replay.py``).
MISSING_NAMES = {
    NO_RECORDING: "녹화 없음",
    GAP: "결측 구간",
    NO_QUOTE: "결정 뒤 호가 없음",
    NO_EXIT_QUOTE: "청산 호가 없음",
}


def _replayed(replay: Mapping[str, Any] | None) -> str:
    """The line on the recorded days replayed (#470)."""
    if not replay or not replay.get("days"):
        return "재생 (#470): 녹화한 날을 trader의 모의 체결 규칙 그대로 다시 돌린다 · 돌린 날 없음"
    absent, gaps = len(replay.get("no_recording") or ()), len(replay.get("gaps") or ())
    return (
        f"재생 (#470): 녹화한 날을 trader의 모의 체결 규칙 그대로 다시 돌린다 · {replay['days']}일"
        f"(녹화 없음 {absent}일, 장중 결측 구간 {gaps}일) · 재생 {replay['passes']}번, 날 읽기 "
        f"{replay['days_read']}번"
    )


def _alone(signals: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """A cell's signals measured alone a share at each θ (#470), one line: signals, those bought, the
    net per signal measured in ticks, and the signals left out for missing data."""
    if not signals:
        return []
    parts = []
    for threshold, s in signals.items():
        part = f"θ {threshold} 신호 {s['signals']}, 체결 {s['bought']}"
        if s["measured"]:
            part += f", 신호당 {s['ticks'] / s['measured']:+.2f}틱"
        interval = s.get("per_signal")
        if interval is not None and interval["low"] is not None:
            part += f"(날 단위 하한 {interval['low']:+.2f}, {interval['days']}일)"
        if s["missing"]:
            part += f", 빠짐 {sum(s['missing'].values())}"
        parts.append(part)
    return ["   1주씩 따로 잰 신호: " + " · ".join(parts)]


def _counted(counts: Mapping[str, int], names: Mapping[str, str]) -> str:
    return ", ".join(f"{names.get(r, r)} {n}" for r, n in counts.items() if n)


def _growth(value: float | None) -> str:
    return "−∞" if value is None else f"{value:+.5f}"  # None: a trade lost the whole capital


def _mb(value: float | None) -> str:
    return "?" if value is None else f"{value:.1f}"


def _highest(*values: float | None) -> float | None:
    known = [v for v in values if v is not None]
    return max(known) if known else None


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
            settings.data_dir,
            sessions,
            rules=Rules.of(trader, settings.fee_rate),
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
        "refit: policy %s kept (%s), %s of %s cell(s); %s recorded day(s) replayed in %s pass(es);"
        " peak RSS %s MB (since the %s), a day's %s MB (%s), %.1fs",
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
        done.fit["replay"]["days"],
        done.fit["replay"]["passes"],
        _mb(run["peak_rss_mb"]),
        "fit" if run["peak_since"] == "step" else "process started",
        _mb(run["day_peak_rss_mb"]),
        run["day_peak_on"],
        run["seconds"],
    )
    print(report(done))
    return 0
