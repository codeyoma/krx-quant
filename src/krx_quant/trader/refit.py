"""The nightly re-fit of the trader's policy (#399, #393 "정책과 자기 개선"): ``krx-quant
refit-policy``, run every trading day's night after the nightly training by the scheduled job
``trader-report`` (#402: ``--report`` prints the day's report above this output). It first
judges, on a review night alone, the cohort of challengers that ran against the champion for the
cycle and promotes one that beat it (#400, #473, ``promotion.promote``: after the market only), then
writes a new policy version (``policy.py``), which waits for the next cohort. A fit with the values of
the champion, of a running challenger or of a policy waiting is not kept (``policy.same_as``). At the
night's end the next cycle's cohort is formed from the policies waiting, the newest first, when its
first day is the next trading day (#473, ``cohort.form_tonight``): on a review night.

> **바뀜** (2026-11-03, #481): a new policy took a free one of three running slots (it ran from the
> next trading day) or waited for one, and the slots freed were filled at the night's end
> (``policy.admit``) → it waits for the next cohort, formed on a review night. 이유: spec #468 5
> "도전 정책".

**Which cells** (#472). Every cell tonight's cost map of 005930 names (``GET /v1/cost-map``, as the
trader reads it at 08:30, ``selection.pick``: ``Selection.named``), whatever its verdict: the cost
map's hit rates describe, and the replayed one-share net decides. Each is tried at every θ of the
grid; the fit keeps a cell at one θ, or leaves it out with why.

> **바뀜** (2026-11-03, #480): the cells passing the cost map at a market order's round trip alone
> were fitted, in its rank (the hit rate's lower bound less p*) → every cell it names, ranked by the
> fit. 이유: spec #468 3 "진입": the cost map's pass is no longer a condition.

**From what** (#470): the recorded days, replayed by the paper trader's own rules
(``replay.py``): each cell's predictions of 005930 on the cost map's days for its kind (the row's
``days``: the latest 20 with predictions), read from the engine's database read-only as the trader
reads them (an algorithm's by models trained on the answers they are scored on,
``stats.on_target``; none of a day a pick would have held its cell off, its predictor's status as the
engine kept it then not active, for the rest of the trader's review cycle: ``predictor_inactive``,
#603), and each day's quotes as the collector recorded them and streamed them to the trader. Nothing
is predicted again, and no answer is read.

**The entry** (#472, spec #468 3 "진입", ``judge``): each cell's rises, each measured alone a share by
the paper trader's rules (``replay.Signals``: decided at its read as if nothing were held, bought at
the first best ask after it, sold by the barriers; one not bought nets 0; one whose data is missing
left out), tallied at each θ its probability reaches. A cell at a θ is judged when it has the
evidence (``Grid``):

- at least ``MIN_DAYS`` (20) of its kind's days recorded: the day's recording of the book was read
  (``replay.RECORDED`` or ``GAPPED``). A recorded gap leaves out the signals it touches, not the day
  (spec #468 2: they are not evidence, and never a net of 0); a day without its recording is not
  counted (``FEW_DAYS``);
- at least ``MIN_TRADES`` (20) of its signals bought, on at least ``MIN_TRADE_DAYS`` (10) days
  (``FEW_TRADES`` when no θ has them).

Judged, its net per signal in ticks, each day's mean one value, has a day-level interval
(``stats.day_mean``, a Student t over the days) two-sided at 1 − ``ALPHA`` ÷ m, m the cell × θ points
judged tonight (Bonferroni over all of them: the error spent by the night's fit stays at most
``ALPHA`` ÷ 2 a side). The cell is kept at the lowest θ whose lower bound is above 0; at none, left
out (``NO_EDGE``, with the point whose bound is the highest). The probability is only a score: a
predictor whose rises are always 1.000 has every θ's signals alike, and its replayed net alone
decides. The cells kept are ranked by their lower bound, the highest first (then the kinds' order,
then the roles'): of one bar close's rises the first ranked buys.

**The calibration** (#475, ``replay.Calibration``): beside each cell's signals alone, its rises in
the grid's five bins of probability, with their answers' classes, the class right, the upper target
reached first and those that netted above 0 bought alone; kept in the cell's ``fitted`` (and a cell
left out's) and printed under it (``calibrated``). A description: the entry above decides, and
nothing changes a θ by it.

**The whole policy** (#472): the cells kept, in their rank at their θ, replayed together one position
at a time as the paper book would have traded them (``replay.Policy``: the rank waits, sized by the
trader's loss budget, ``paper.size``, held by its risk rules and loss limits, #471). Its daily net in
won over the days its cells were recorded (a day without a trade 0) must have a day-level lower bound
(``LEVEL``, 95%: one policy) above 0: else the policy is not kept, and the night's output and log say
why (``Refitted.rejected``). A fit keeping no cell keeps an empty policy, as before: it trades
nothing (the first night's on a store without a champion is the empty champion that every later fit
challenges, #399).

**Development evidence.** The cells and θ are chosen on the days they are judged on, so a policy kept
is a challenger alone: it becomes the champion only by its forward shadow record (``promotion.py``).
On a store without a champion, the fit of a policy with cells keeps an empty one first (``BOOTSTRAP``,
#473): the empty policy is the first champion, which trades nothing, and the fit waits for a cohort,
as every later one does: no policy trades before a review crowned it. A fit with no cell is that
empty champion itself.

> **바뀜** (2026-11-03, #481): on a store without a champion the first policy kept was the champion,
> cells and all (#399) → a policy with cells waits for a cohort, an empty champion kept before it.
> 이유: spec #468 1 and 3: every change of the policy that trades goes through a review, and a fit is
> development evidence.

> **바뀜** (2026-11-03, #480): the cells were fitted one at a time in the cost map's rank, each at
> the point of the grid whose replayed policy grew the capital most (Σ log(1 + net ÷ capital)), kept
> when it made ``MIN_TRADES`` replayed trades there and grew it more than leaving it out
> (``few_trades``, ``no_gain``), ties going to the trader's default θ then the higher; a pass a cell →
> one pass of the cells alone, each kept at its lowest θ whose one-share net's lower bound is above 0,
> and one pass of the policy kept. 이유: spec #468 3 "진입".

**Limits.** None is fitted nor kept: the capital, the loss budget, the risk rules and the loss limits
hold the replayed trades as they hold the trader's (``KRX_TRADER_CAPITAL``, #471's settings), and the
fit only writes ``policies`` and ``champions``.

**Memory** (the server has 2 GB; pre-validation replays have been killed for it): two passes of the
replay, the cells alone together, then the policy kept (none when no cell is); a pass reads one day
at a time, its recording once, and lets it go before the next (``replay.Replayer``). Each day's peak
resident memory (``footprint.Meter``, the highest of its passes) is kept in the policy's
``fit.run.days``, the highest printed and logged.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import time as clock
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import aiohttp

from krx_quant.config import Settings
from krx_quant.feeds.base import KST, kst_day
from krx_quant.footprint import Meter
from krx_quant.market_calendar import Session, calendar_store
from krx_quant.predictions import DB_NAME as ENGINE_DB
from krx_quant.stats import Interval, day_mean
from krx_quant.trader import ROLES, SYMBOL, TRADED_KINDS, cohort, promotion, review
from krx_quant.trader import policy as policies
from krx_quant.trader.cells import Cell, open_engine_db, special_windows
from krx_quant.trader.db import reading
from krx_quant.trader.paper import NO_QUOTE, Rules
from krx_quant.trader.replay import (
    GAP,
    GAPPED,
    LEVEL,
    NO_EXIT_QUOTE,
    NO_RECORDING,
    Calibration,
    Ranked,
    Replayer,
    Result,
    Signals,
    Source,
    Tally,
)
from krx_quant.trader.selection import CostMapError, Pick, choose, read_cost_map
from krx_quant.trader.settings import TraderSettings
from krx_quant.trader.store import DB_NAME as TRADER_DB

log = logging.getLogger(__name__)

# The grid (#399: small): the thresholds θ tried for each cell (#471: no Kelly fraction λ).
THRESHOLDS = (0.4, 0.5, 0.6, 0.7, 0.8)
# The evidence a cell at a θ needs to be judged (#472, spec #468 3, module doc): its kind's days
# recorded, its signals bought alone a share, and the days they were bought on.
MIN_DAYS = 20
MIN_TRADES = 20
MIN_TRADE_DAYS = 10
# The error the night's lower bounds spend, two-sided (the engine's "95%"), shared by the cell × θ
# points judged (Bonferroni).
ALPHA = 1 - LEVEL
# Why a cell the cost map names is left out (#472).
FEW_DAYS, FEW_TRADES, NO_EDGE = "few_days", "few_trades", "no_edge"
# Why no cell came in before #472, as policies of then keep it: it did not grow the replayed capital.
NO_GAIN = "no_gain"
# Why a fit is not kept (#472): its whole policy's daily net has no lower bound above 0.
NOT_ABOVE = "policy_not_above"
# The waiting policies the night's output names, newest first.
WAITING_SHOWN = 5
# Why the empty policy kept before the first fit with cells is the champion (#473).
BOOTSTRAP = "빈 챔피언(#473): 맞춘 정책은 검토 날의 묶음 판정으로만 챔피언이 된다"
# What the job's exit code says: 0 a policy kept (or the same values running already, or one its
# whole replay did not keep), 1 none (the cost map or the engine's database could not be read).
NO_POLICY_KEPT = 1


@dataclass(frozen=True)
class Grid:
    """The points tried and the evidence a point needs (module doc)."""

    thresholds: tuple[float, ...] = THRESHOLDS
    min_days: int = MIN_DAYS
    min_trades: int = MIN_TRADES
    min_trade_days: int = MIN_TRADE_DAYS
    alpha: float = ALPHA

    def enough(self, signals: Signals | None) -> bool:
        """Whether a cell's signals at a θ are enough to judge (its days are the caller's)."""
        return (
            signals is not None
            and signals.bought >= self.min_trades
            and signals.days_bought >= self.min_trade_days
        )

    def level(self, judged: int) -> float:
        """The two-sided level of each lower bound when ``judged`` points are judged (Bonferroni)."""
        return 1 - self.alpha / max(1, judged)

    def as_json(self) -> dict[str, Any]:
        return {
            "thresholds": list(self.thresholds),
            "min_days": self.min_days,
            "min_trades": self.min_trades,
            "min_trade_days": self.min_trade_days,
            "alpha": self.alpha,
        }


@dataclass(frozen=True)
class Point:
    """A cell kept at a θ (#472): its net per signal's day-level interval there, at the fit's
    level, in ticks."""

    pick: Pick
    threshold: float
    bound: Interval

    def ranked(self) -> Ranked:
        """As the replay trades it."""
        return Ranked(self.pick.cell, self.threshold)


@dataclass(frozen=True)
class Judged:
    """The night's entry (``judge``): the points kept, ranked; the cells left out and why, in the cost
    map's order; the cell × θ points judged and each lower bound's level."""

    kept: tuple[Point, ...]
    left_out: tuple[dict[str, Any], ...]
    judged: int
    level: float


def bound_json(interval: Interval | None, level: float) -> dict[str, Any] | None:
    """A net per signal's day-level interval (ticks) and its level."""
    if interval is None:
        return None
    return {
        "value": interval.value,
        "low": interval.low,
        "high": interval.high,
        "days": interval.n,
        "level": level,
    }


def judge(
    picks: Sequence[Pick],
    signals: Mapping[Cell, Mapping[float, Signals]],
    recorded: Mapping[Cell, int],
    grid: Grid,
) -> Judged:
    """The cells of ``picks`` entered on their ``signals`` each measured alone (module doc "The
    entry"), with each cell's ``recorded`` days: the same replay, the same points kept."""
    eligible: dict[Cell, list[float]] = {}
    left: dict[Cell, dict[str, Any]] = {}
    for pick in picks:
        days = recorded.get(pick.cell, 0)
        measured = signals.get(pick.cell, {})
        if days < grid.min_days:
            left[pick.cell] = {"why": FEW_DAYS, "recorded_days": days}
            continue
        ok = [t for t in sorted(grid.thresholds) if grid.enough(measured.get(t))]
        if ok:
            eligible[pick.cell] = ok
            continue
        left[pick.cell] = {
            "why": FEW_TRADES,
            "recorded_days": days,
            "most_trades": max((s.bought for s in measured.values()), default=0),
            "most_days": max((s.days_bought for s in measured.values()), default=0),
        }
    judged = sum(len(ts) for ts in eligible.values())
    level = grid.level(judged)
    kept: list[Point] = []
    for pick in picks:
        thresholds = eligible.get(pick.cell)
        if thresholds is None:
            continue
        bounds = [(t, signals[pick.cell][t].per_signal(level)) for t in thresholds]  # the lowest θ first
        found = next((Point(pick, t, i) for t, i in bounds if i is not None and _low(i) > 0), None)
        if found is not None:
            kept.append(found)
            continue
        threshold, interval = max(bounds, key=lambda b: _low(b[1]))  # the first of the highest
        left[pick.cell] = {
            "why": NO_EDGE,
            "recorded_days": recorded.get(pick.cell, 0),
            "best": {"threshold": threshold, "bound": bound_json(interval, level)},
        }
    kept.sort(key=_rank)
    out = tuple({**_named(p), **left[p.cell]} for p in picks if p.cell in left)
    return Judged(tuple(kept), out, judged, level)


def _low(interval: Interval | None) -> float:
    """An interval's lower bound; −∞ without one."""
    return -math.inf if interval is None or interval.low is None else interval.low


def _rank(p: Point) -> tuple[float, int, int]:
    """The kept cells' order (module doc): the highest lower bound first, then the kinds', then the
    roles'."""
    return (-_low(p.bound), TRADED_KINDS.index(p.pick.kind), ROLES.index(p.pick.role))


def _named(pick: Pick) -> dict[str, Any]:
    """A cell as the policy names it, with the cost map's figures (a diagnosis, #472)."""
    return {
        "kind": pick.kind,
        "role": pick.role,
        "predictor": pick.predictor,
        "p_star": pick.p_star,
        "low": pick.low,
        "kelly": pick.kelly,
    }


def checked(result: Result, days: Sequence[date]) -> Interval | None:
    """The whole policy's daily net in won over ``days`` (a day without a trade 0), with its day-level
    interval at ``LEVEL`` (module doc "The whole policy"); None from fewer than two days."""
    return day_mean({d: [result.days.get(d, 0.0)] for d in days}, LEVEL)


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

    version: int | None  # None: not kept (``rejected``)
    champion: bool  # it became the champion (the first)
    champion_version: int | None  # the champion's now
    cells: tuple[policies.PolicyCell, ...]
    left_out: tuple[dict[str, Any], ...]
    fit: Mapping[str, Any]
    named: int  # the cells the cost map named, each tried (#472)
    # #400: the champion, running challenger or policy waiting it equals (then it is not kept:
    # ``version`` is that one's), the challengers running after it (oldest first) and the policies
    # waiting (newest first).
    same_as: int | None = None
    challengers: tuple[int, ...] = ()
    waiting: tuple[int, ...] = ()
    # #472: why the policy fitted was not kept (``NOT_ABOVE``: its whole replay's daily net has no
    # lower bound above 0); None when kept.
    rejected: str | None = None
    # #473: the empty champion kept before it, on a store without a champion (``BOOTSTRAP``).
    bootstrap: int | None = None


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
    at ``trader_db`` when its whole replay passes. ``CostMapError`` when ``body`` is not the cost map
    asked for."""
    meter = meter or Meter()
    started = clock.monotonic()
    meter.reset()
    start_mb = meter.rss_mb()
    selection = choose(body)
    days_of = cost_map_days(body)
    picks = selection.named  # every cell the cost map names (#472)
    verdicts: dict[Cell, str | None] = {}  # the cost map's at a market order's round trip, a record
    for seen in selection.considered:
        if seen.get("predictor"):  # the first of a predictor named twice, as ``named`` keeps it
            verdicts.setdefault(Cell(seen["kind"], seen["predictor"]), seen.get("verdict"))
    alone: Mapping[Cell, Mapping[float, Signals]] = {}
    counts: Mapping[Cell, Mapping[str, int]] = {}
    calibration: Mapping[Cell, Calibration] = {}
    recorded: dict[Cell, int] = {}
    replayer: Replayer | None = None
    judged = Judged((), (), 0, grid.level(0))
    result = Result(Tally(), {}, {})  # no cell kept: no trade
    policy_days: list[date] = []
    if picks:
        with reading(trader_db) as store:  # #603: its review cycles hold a cell off not active
            evaluation = None if store is None else review.current(store)
        db = open_engine_db(engine_db)
        try:
            first = min((min(d) for d in days_of.values() if d), default=kst_day(now))
            windows = special_windows(db, SYMBOL, datetime.combine(first, time(), KST))
            sources = {p.cell: Source(p.role, days_of.get(p.kind, ())) for p in picks}
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
                evaluation=evaluation,
            )
            measured = replayer.run([], alone=[p.cell for p in picks])
            alone, counts, calibration = measured.signals, measured.counts, measured.calibration
            found = replayer.recordings
            recorded = {
                p.cell: sum(found.get(d, NO_RECORDING) != NO_RECORDING for d in sources[p.cell].days)
                for p in picks
            }
            judged = judge(picks, alone, recorded, grid)
            if judged.kept:
                result = replayer.run([[p.ranked() for p in judged.kept]]).results[0]
                policy_days = sorted(
                    {
                        d
                        for p in judged.kept
                        for d in sources[p.pick.cell].days
                        if found.get(d, NO_RECORDING) != NO_RECORDING
                    }
                )
        finally:
            db.close()

    def measured_json(cell: Cell) -> dict[str, Any]:
        calibrated = calibration.get(cell)
        return {
            "verdict": verdicts.get(cell),
            "read": dict(counts.get(cell, {})),
            "signals": {f"{t:g}": s.as_json() for t, s in alone.get(cell, {}).items()},
            **({} if calibrated is None else {"calibration": calibrated.as_json()}),  # #475
        }

    cells = tuple(
        policies.PolicyCell(
            kind=p.pick.kind,
            role=p.pick.role,
            predictor=p.pick.predictor,
            threshold=p.threshold,
            fitted={
                **{k: v for k, v in _named(p.pick).items() if k in ("p_star", "low", "kelly")},
                "days": [d.isoformat() for d in days_of.get(p.pick.kind, ())],
                "recorded_days": recorded.get(p.pick.cell, 0),
                **measured_json(p.pick.cell),
                "bound": bound_json(p.bound, judged.level),
                **result.cells[p.pick.cell].as_json(),
            },
        )
        for p in judged.kept
    )
    left_out = tuple({**x, **measured_json(Cell(x["kind"], x["predictor"]))} for x in judged.left_out)
    check = checked(result, policy_days) if cells else None
    rejected = None if not cells or _low(check) > 0 else NOT_ABOVE
    peaks = {} if replayer is None else replayer.peaks
    recordings = {} if replayer is None else replayer.recordings
    day_peak = max(((mb, d) for d, mb in peaks.items() if mb is not None), default=None)
    fit_json: dict[str, Any] = {
        "evaluation": review.EVALUATION,  # #469: the rules it is fitted and judged under
        "cost_map": {
            "as_of": selection.as_of,
            "period": body.get("period"),
            "named": len(picks),  # #472: every one tried
            "passed": len(selection.picks),  # its verdict at a market order's round trip: a diagnosis
        },
        "grid": grid.as_json(),
        "judged": {"points": judged.judged, "level": judged.level},  # #472: Bonferroni over them
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
            # #472: the daily net over the days its cells were recorded, and its day-level interval
            "days": len(policy_days),
            "per_day": None
            if check is None
            else {"value": check.value, "low": check.low, "high": check.high, "level": LEVEL},
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
        f"{night} 밤 맞추기: 비용 지도({selection.as_of})의 {len(picks)}칸 가운데 실행 손익 하한을 "
        f"넘은 {len(cells)}칸"
    )
    if rejected is not None:
        champion, challengers, waiting = _running(trader_db)
        return Refitted(
            version=None,
            champion=False,
            champion_version=champion,
            cells=cells,
            left_out=left_out,
            fit=fit_json,
            named=len(picks),
            challengers=challengers,
            waiting=waiting,
            rejected=rejected,
        )
    store = policies.connect(trader_db)
    try:
        try:
            same = policies.same_as(store, cells)
        except policies.PolicyError as e:  # one it would be compared with cannot be read: kept
            log.warning("refit: cannot compare with the policies running (%s): kept all the same", e)
            same = None
        bootstrap = None
        if same is None:
            if cells and policies.champion_version(store) is None:  # #473: an empty champion first
                bootstrap, _ = policies.add(
                    store, now, BOOTSTRAP, [], [], {"evaluation": review.EVALUATION}, first_reason=BOOTSTRAP
                )
            version, first_champion = policies.add(store, now, reason, cells, left_out, fit_json)
        else:
            version, first_champion = same, False
        champion = policies.champion_version(store)
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
        named=len(picks),
        same_as=same,
        challengers=challengers,
        waiting=waiting,
        bootstrap=bootstrap,
    )


def _running(trader_db: Path) -> tuple[int | None, tuple[int, ...], tuple[int, ...]]:
    """The champion, the challengers running and the policies waiting, for a night that keeps none
    (#472); none without a store (it is not made for that)."""
    if not trader_db.exists():
        return None, (), ()
    store = policies.connect(trader_db)
    try:
        return (
            policies.champion_version(store),
            tuple(policies.challenger_versions(store)),
            tuple(policies.waiting_versions(store)),
        )
    finally:
        store.close()


def report(done: Refitted) -> str:
    """The job's output, for the issue (no price: cells, values, counts, ticks and won)."""
    fit_json = done.fit
    cost_map = fit_json["cost_map"]
    total = fit_json["result"]
    if done.rejected is not None:
        head = (
            f"새 정책 없음 · 실행 손익 하한을 넘은 {len(done.cells)}칸으로 짠 정책 전체의 재생에서 날마다 "
            f"순손익의 95% 하한이 0을 넘지 않아 저장하지 않았다(#472, {done.rejected})"
        )
    elif done.champion:
        head = f"정책 {done.version} · 챔피언(처음 맞춘 정책)"
    elif done.same_as is not None:
        whose = (
            "챔피언"
            if done.same_as == done.champion_version
            else "도는 도전 정책"
            if done.same_as in done.challengers
            else "기다리는 정책"
        )
        head = f"새 정책 없음 · 맞춘 값이 {whose} 정책 {done.same_as}과 같아 저장하지 않았다"
    else:
        head = (
            f"정책 {done.version} · 기다리는 정책(#473: 다음 묶음을 기다린다. 검토 날 밤에 기다리는 정책 "
            "가운데 새것부터 셋까지 다음 주기의 도전 정책으로 들어간다)"
        )
        if done.bootstrap is not None:
            head += f" · 챔피언이 없어 빈 정책 {done.bootstrap}을 챔피언으로 먼저 두었다"
    grid = fit_json["grid"]
    judged = fit_json["judged"]
    lines = [
        f"정책 맞추기 (#399): {head}",
        f"비용 지도: {SYMBOL} {cost_map['period']}, as_of {cost_map['as_of']}, 이름이 있는 칸 "
        f"{done.named}칸(판정과 상관없이 모두 시험한다. 시장가 왕복 통과 {cost_map['passed']}칸은 참고만)",
        "진입 (#472): θ "
        + " ".join(f"{t:g}" for t in grid["thresholds"])
        + f" 가운데 1주씩 따로 잰 신호당 순손익의 날 단위 하한이 0을 넘는 가장 낮은 θ · 녹화한 날 "
        f"{grid['min_days']}일 이상, 체결 {grid['min_trades']}건 이상이 {grid['min_trade_days']}일 이상에 · "
        f"하한의 수준 {_percent(judged['level'])}(판정한 칸 × θ {judged['points']}개에 Bonferroni)",
        _replayed(fit_json.get("replay")),
        "",
    ]
    if not done.cells:
        lines.append("매매할 칸: 없음" + (" (비용 지도에 이름이 있는 칸이 없다)" if not done.named else ""))
    for i, c in enumerate(done.cells, 1):
        f = c.fitted
        lines.append(
            f"{i}. {c.kind} {c.predictor} ({c.role}): θ {c.threshold:g} · "
            + _bounded(f["signals"].get(f"{c.threshold:g}") or {}, f["bound"])
            + f" · 정책 재생 모의 {f['trades']}건(이익 {f['wins']}) · 순손익 {f['net']:+,.0f}원 · "
            f"읽은 예측 {f['read'].get('read', 0)}, 오름 {f['read'].get('rises', 0)}"
            + _verdict(f.get("verdict"))
        )
        lines += _alone(f.get("signals") or {})
        lines += calibrated(f.get("calibration"))
    for x in done.left_out:
        why = x["why"]
        if why == FEW_DAYS:
            detail = f"녹화한 날이 {grid['min_days']}일에 못 미침({x['recorded_days']}일)"
        elif why == FEW_TRADES:
            detail = (
                f"1주씩 따로 잰 체결이 {grid['min_trades']}건·{grid['min_trade_days']}일에 못 미침"
                f"(가장 많을 때 {x['most_trades']}건, {x['most_days']}일)"
            )
        else:
            best = x["best"]
            bound = best.get("bound") or {}
            detail = (
                f"신호당 순손익의 하한이 0을 넘는 θ가 없음(가장 높은 하한 θ {best['threshold']:g}: "
                + _ticks(bound.get("low"))
                + f", 평균 {_ticks(bound.get('value'))})"
            )
        lines.append(
            f"뺀 칸: {x['kind']} {x['predictor']} ({x['role']}): {why}, {detail}" + _verdict(x.get("verdict"))
        )
        lines += _alone(x.get("signals") or {})
        lines += calibrated(x.get("calibration"))
    if done.challengers:
        lines.append(
            "모의로 도는 도전 정책(묶음, 셋까지): " + ", ".join(f"정책 {v}" for v in done.challengers)
        )
    if done.waiting:
        shown = ", ".join(f"정책 {v}" for v in done.waiting[:WAITING_SHOWN])
        more = f" 외 {len(done.waiting) - WAITING_SHOWN}개" if len(done.waiting) > WAITING_SHOWN else ""
        lines.append(f"다음 묶음을 기다리는 정책(새것부터): {shown}{more}")
    run = fit_json["run"]
    missing = total.get("missing") or {}
    left = (
        f" · 데이터가 빠져 뺀 신호 {sum(missing.values())}({_counted(missing, MISSING_NAMES)})"
        if missing
        else ""
    )
    per_day = total.get("per_day")
    daily = (
        ""
        if per_day is None
        else f", 날마다 평균 {per_day['value']:+,.0f}원(95% 하한 "
        + ("?" if per_day["low"] is None else f"{per_day['low']:+,.0f}원")
        + f", {total['days']}일)"
    )
    day_peak = run.get("day_peak_rss_mb")
    lines += [
        "",
        f"정책 전체: 모의 {total['trades']}건(이익 {total['wins']}), 매매한 날 {total['days_traded']}일, "
        f"순손익 {total['net']:+,.0f}원{daily}, 로그 성장 {_growth(total['growth'])}{left}",
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


def _bounded(signals: Mapping[str, Any], bound: Mapping[str, Any] | None) -> str:
    """A kept cell's evidence at its θ (#472): its signals, those bought and on how many days, and its
    net per signal with the lower bound it entered on."""
    bought = f"체결 {signals.get('bought', 0)}({signals.get('days_bought', 0)}일)"
    head = f"신호 {signals.get('signals', 0)}, {bought}"
    if not bound:
        return head
    return (
        f"{head} · 신호당 {_ticks(bound.get('value'))}(하한 {_ticks(bound.get('low'))}, "
        f"{bound.get('days')}일)"
    )


def _verdict(verdict: str | None) -> str:
    """The cost map's verdict at a market order's round trip, for the record (#472: no condition)."""
    return "" if verdict is None else f" · 비용 지도 판정 {verdict}"


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
            part += f"(날 단위 95% 하한 {interval['low']:+.2f}, {interval['days']}일)"
        if s["missing"]:
            part += f", 빠짐 {sum(s['missing'].values())}"
        parts.append(part)
    return ["   1주씩 따로 잰 신호: " + " · ".join(parts)]


def calibrated(calibration: Mapping[str, Any] | None) -> list[str]:
    """A cell's calibration (#475, ``replay.Calibration``), a line a bin with rises: its probability's
    span, the rises measured, their answers' classes (상승: the kind's rise; 보합; 하락), the class right,
    the upper target reached first, and those bought alone a share that netted above 0; counts and
    shares, no price. A description: nothing changes a θ by it."""
    if not calibration:
        return []
    bins = [b for b in calibration["bins"] if b["signals"] or b["missing"]]
    below = calibration.get("below") or 0
    head = "   보정 (#475, 점수 구간마다 오름 신호, 1주씩 따로 잰 것: 설명일 뿐 θ를 바꾸지 않는다)"
    under = f" · 첫 경계 아래 {below}(어느 θ도 사지 않는다)" if below else ""
    if not bins:
        return [f"{head}: 구간 안 신호 없음{under}"]
    return [head + under, *(_bin_line(b) for b in bins)]


def _bin_line(b: Mapping[str, Any]) -> str:
    """A calibration's bin (``calibrated``)."""
    span = f"[{b['from']:g}, 1]" if b["to"] >= 1 else f"[{b['from']:g}, {b['to']:g})"
    parts = [f"   - {span}: 신호 {b['signals']}"]
    answered, out = b["answered"], b["outcomes"]
    if answered:
        parts += [
            f"결과 상승 {_share(out['up'], answered)}, 보합 {_share(out['flat'], answered)}, "
            f"하락 {_share(out['down'], answered)}(답 {answered})",
            f"클래스 맞음 {_share(b['hits'], answered)}",
            f"목표 먼저 {_share(b['first'], answered)}",
        ]
    else:
        parts.append("답 없음")
    bought = b["bought"]
    parts.append(f"순이익 {_share(b['profitable'], bought)}(체결 {bought})" if bought else "체결 없음")
    if b["missing"]:
        parts.append(f"데이터가 빠져 뺌 {b['missing']}")
    return " · ".join(parts)


def _share(n: int, of: int) -> str:
    return f"{n / of:.0%}" if of else "?"


def _ticks(value: float | None) -> str:
    return "?" if value is None else f"{value:+.2f}틱"


def _percent(level: float) -> str:
    return f"{level * 100:.4g}%"


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
    settings: Settings,
    trader: TraderSettings,
    now: datetime,
    engine_url: str | None = None,
    fitted: list[Refitted] | None = None,
) -> int:
    """``krx-quant refit-policy``: first the challengers' cohort judged on a review night and one
    promoted if it beat the champion (#400, #473, ``promotion.promote``), then tonight's cost map from
    the Engine API (``engine_url``, else the trader's ``KRX_TRADER_ENGINE_URL``), the fit, the policy
    kept, and at the end the next cycle's cohort formed on a review night (``form_now``); each printed.
    0 when a policy is kept, ``NO_POLICY_KEPT`` when the cost map or the engine's database cannot be
    read (the promotion is judged and the cohort formed all the same). The fit, kept or not, goes into
    ``fitted`` when given (#560: the night's report says what it left out and why)."""
    url = engine_url or trader.engine_url
    calendar = calendar_store(settings).calendar
    promote_now(settings.state_dir / TRADER_DB, calendar.session, now)
    try:
        return await _refit(settings, trader, url, calendar.session, now, fitted)
    finally:
        form_now(settings.state_dir / TRADER_DB, calendar.session, now)


def form_now(trader_db: Path, sessions: Callable[[date], Session | None], now: datetime) -> None:
    """The night's end (#473, ``cohort.form_tonight``): the next cycle's cohort formed from the
    policies waiting on a review night, its lines printed (and why not, when the store cannot be
    written). Without a store there is no champion yet, and none is made for it."""
    if not trader_db.exists():
        return
    try:
        db = policies.connect(trader_db)
        try:
            tonight = cohort.form_tonight(db, now, sessions)
        finally:
            db.close()
    except (sqlite3.Error, ValueError) as e:  # ValueError: a calendar without the cycle's days
        print(f"\n{COHORT_HEAD}: 저장소를 쓰지 못해 묶음을 만들지 못했다: {e}")
        log.error("cohort: %s", e)
        return
    if tonight.formed and tonight.cohort is not None:
        log.info(
            "cohort: cycle %s's challengers are %s, against policy %s, judged on its review night (%s)",
            tonight.cohort.cycle,
            ", ".join(map(str, tonight.cohort.members)) or "none",
            tonight.cohort.champion,
            tonight.cohort.review,
        )
    print("\n" + said_tonight(tonight))


COHORT_HEAD = "도전 정책 묶음 (#473)"


def said_tonight(tonight: cohort.Tonight) -> str:
    """The night's end for the job's output (``form_now``): counts, versions and dates, no price."""
    c, found = tonight.cycle, tonight.cohort
    waiting = f"다음 묶음을 기다리는 정책 {len(tonight.waiting)}개"
    span = f"주기 {c.number}({c.first}–{c.review})"
    if tonight.why == cohort.NO_CHAMPION:
        return f"{COHORT_HEAD}: 챔피언이 아직 없어 {span}의 묶음을 만들지 않았다 · {waiting}"
    if found is None:
        return (
            f"{COHORT_HEAD}: {span}은 묶음 없이 시작했다(주기 중간에는 넣지 않는다) · {waiting}: "
            f"검토 날 {c.review} 밤에 새것부터 셋까지 들어간다"
        )
    members = ", ".join(f"정책 {v}" for v in found.members) or "없음"
    if not tonight.formed:
        return (
            f"{COHORT_HEAD}: {span}의 묶음 {members} · 챔피언 정책 {found.champion}과 견준다 · "
            f"검토 날 {found.review} 밤에 한 번 판정 · {waiting}"
        )
    lines = [
        f"{COHORT_HEAD}: {span}의 묶음을 만들었다: {members}(새것부터) · 비교할 챔피언 정책 {found.champion}"
        f"(고정) · 주기 동안 모의로 돌고 검토 날 {found.review} 밤에 한 번 판정한다"
    ]
    if not found.members:
        lines[0] = (
            f"{COHORT_HEAD}: {span}의 묶음: 기다리는 정책이 없어 비었다(이 주기에는 도전 정책이 돌지 않는다)"
        )
    if found.passed_over:
        lines.append(
            "묶음에 들지 못해 내린 정책: "
            + ", ".join(
                f"정책 {p['version']}({PASSED_OVER.get(p['why'], p['why'])})" for p in found.passed_over
            )
        )
    return "\n".join(lines)


# How the night's output names why a policy waiting is passed over (``cohort.choose``).
PASSED_OVER = {
    cohort.OLDER: "새것 셋에 밀림",
    cohort.CHAMPION_VALUES: "챔피언과 같은 값",
    cohort.ALIKE: "더 새것과 같은 값",
    cohort.UNREADABLE: "읽지 못함",
    cohort.EARLIER_EVALUATION: "평가 버전 전에 맞춤",
}


async def _refit(
    settings: Settings,
    trader: TraderSettings,
    url: str,
    sessions: Callable[[date], Session | None],
    now: datetime,
    fitted: list[Refitted] | None = None,
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
            grid=Grid(),
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
        "none" if done.version is None else done.version,
        f"its whole replay's daily net has no lower bound above 0: {done.rejected}"
        if done.rejected is not None
        else "the champion"
        if done.champion
        else f"the same values as policy {done.same_as}: not kept again"
        if done.same_as is not None
        else f"a challenger waiting for the next cohort, after an empty champion, policy {done.bootstrap}"
        if done.bootstrap is not None
        else "a challenger waiting for the next cohort",
        len(done.cells),
        done.named,
        done.fit["replay"]["days"],
        done.fit["replay"]["passes"],
        _mb(run["peak_rss_mb"]),
        "fit" if run["peak_since"] == "step" else "process started",
        _mb(run["day_peak_rss_mb"]),
        run["day_peak_on"],
        run["seconds"],
    )
    print(report(done))
    if fitted is not None:
        fitted.append(done)
    return 0
