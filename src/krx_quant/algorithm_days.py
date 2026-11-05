"""Each algorithm's day by day (#561): its hit rate, its calls and their precision class by class,
its lift over its kind's climate baseline and its log loss, on each trading day reviewed, with the
status it had that day; and the consensus set beside its members. ``GET /v1/leaderboard/days``
serves them (``api.py``).

The user judges an algorithm by how often it calls up, flat and down right. The leaderboard has
today, five days and twenty; a fall shows only in a series of days (turing's after it was made
active showed in a one-off report, #206). Every value here is of one trading day:

- **the predictions counted** are the leaderboard's: scored on their bar close's answer (the
  direction's touch label, a kind's answer), special ones left out, an algorithm's of models trained
  on the target only (``stats.on_target``; the direction's from target version 2, as the
  leaderboard's), the baselines' and the consensus' all;
- **hit rate** (``accuracy``): the class called (the most likely; a tie goes to the middle class,
  then the first) happened; **coverage**: the share calling one of the kind's action classes (the
  direction's up and down); **precision** of each class: of its calls, the share that happened;
- **lift** over the kind's climate baseline (``CLIMATES``): the hit rate gained on the bar closes
  both predicted (``stats.lift``'s value);
- **log loss**: the mean of −ln p(the class that happened), floored as the nightly training's
  (``forecasts.LOSS_FLOOR``): what the nightly swap promotes by (#437);
- **status**: the one it had at the day's close (a review's change counts from the next day). On a
  day its probation's simulation replayed (#298) its values are the simulation's, the day is marked
  so and its status is probation, as the reviews count it.

A day is counted once reviewed (``PredictionEngine.next_review_day``): every bar close of it is
scored. A daily kind's days are those with an answer (its answers come in after the review), as its
reviews count them. Each day is computed once, off the event loop, a day's rows at a time (the
server has 2 GB), and kept (``Kept``) until a simulation is kept, a fill or a rescore rewrites past
scores or, for a daily kind, an answer comes in (``PredictionStore.refill_mark``). A request computes
only the days it lacks: a new bar close changes nothing kept.

Each active algorithm carries a **decay** mark: its weakest lift over its kind's baselines on its
days active among the latest 20 (a daily kind's recent days), as the bench check measures it
(#545), flagged at 0 or below once they hold enough predictions to judge (``stats.enough``). The
bench check judges those days alone only from the tenth day active (``Rules.min_active_days``);
before, the mark shows a fall the rule does not act on yet.

The consensus (the direction's ``consensus-final``) is set beside its members each day: its hit rate
less each member's on the bar closes both predicted, and the weakest of those, against the member
that did best. Its members are the algorithms active at the day's close. Reported only: the
consensus' rule is unchanged.
"""

from __future__ import annotations

import asyncio
import math
from collections import OrderedDict
from collections.abc import Callable, Collection, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import partial
from typing import Any

from krx_quant.algorithms.forecasts import LOSS_FLOOR
from krx_quant.barriers import CLIMATE as BARRIER_CLIMATE
from krx_quant.barriers import NAME as BARRIER
from krx_quant.climate import CLIMATE as DIRECTION_CLIMATE
from krx_quant.feeds.base import KST
from krx_quant.kinds import DIRECTION, Kind, most_likely
from krx_quant.lifecycle import Rules, Simulated
from krx_quant.market_calendar import MarketCalendar
from krx_quant.open_to_close import CLIMATE as OTC_CLIMATE
from krx_quant.open_to_close import NAME as OTC
from krx_quant.open_to_close_5d import CLIMATE as OTC5_CLIMATE
from krx_quant.open_to_close_5d import NAME as OTC5
from krx_quant.ranges import CLIMATE as RANGE_CLIMATE
from krx_quant.ranges import NAME as RANGE
from krx_quant.stats import on_target

LIMIT = 20  # the days a series holds by default
MAX_LIMIT = 60  # and at most
KEPT_DAYS = 512  # days kept across the kinds: every kind's longest series, and more
CONSENSUS = "consensus-final"  # the consensus set beside its members (the signals')
# Each kind's climate baseline: its class shares by symbol and slot (or day), what an algorithm's
# day lift is over. A barrier of a longer horizon has its own (``barrier-climate-<m>m``, #263).
CLIMATES = {
    DIRECTION: DIRECTION_CLIMATE,
    RANGE: RANGE_CLIMATE,
    BARRIER: BARRIER_CLIMATE,
    OTC: OTC_CLIMATE,
    OTC5: OTC5_CLIMATE,
}

Row = Mapping[str, Any]  # a scored prediction as the reviews read it (``REVIEWED``, ``KIND_REVIEWED``)


def climate_of(kind: Kind, baselines: Collection[str]) -> str | None:
    """``kind``'s climate baseline among ``baselines`` (a barrier of a longer horizon's, #263); None
    without one (the direction's before the touch target, #169)."""
    name = CLIMATES.get(kind.name)
    if name is None and kind.name.startswith(f"{BARRIER}_") and kind.name.endswith("m"):
        name = f"{BARRIER_CLIMATE}-{kind.name.removeprefix(f'{BARRIER}_')}"
    return name if name in baselines else None


@dataclass(frozen=True)
class Call:
    """One scored prediction: its bar close (as stored, what pairs it), its three probabilities in
    the kind's order and the place of the class that happened."""

    symbol: str
    bar_close: str
    probabilities: tuple[float, float, float]
    outcome: int

    @property
    def predicted(self) -> int:
        return most_likely(self.probabilities)

    @property
    def correct(self) -> bool:
        return self.predicted == self.outcome

    @property
    def loss(self) -> float:
        return -math.log(max(self.probabilities[self.outcome], LOSS_FLOOR))


def calls(rows: Iterable[Row], kind: Kind) -> dict[str, list[Call]]:
    """Each predictor's scored predictions of ``rows``: the direction's (live or replayed, scored on
    their touch label) or a kind's (on its answer); those without a class are left out."""
    out: dict[str, list[Call]] = {}
    for r in rows:
        if "p_up" in r:
            label = r.get("touch_class")
            probabilities = (float(r["p_up"]), float(r["p_flat"]), float(r["p_down"]))
        else:
            label, output = r.get("answer_class"), r.get("output")
            if output is None:
                continue
            probabilities = kind.probabilities(output)
        if label is None:
            continue
        out.setdefault(r["algorithm"], []).append(
            Call(r["symbol"], r["bar_close"], probabilities, kind.index(label))
        )
    return out


@dataclass(frozen=True)
class Versus:
    """Two predictors on the bar closes both predicted: how many, and the hits of each."""

    pairs: int
    mine: int
    theirs: int

    @property
    def lift(self) -> float:
        """The hit rate gained: ``stats.lift``'s value."""
        return (self.mine - self.theirs) / self.pairs

    def __add__(self, other: Versus) -> Versus:
        return Versus(self.pairs + other.pairs, self.mine + other.mine, self.theirs + other.theirs)


@dataclass(frozen=True)
class Tally:
    """One predictor's scored predictions of a day, counted: summed over days, any span's values."""

    n: int
    hits: int
    called: tuple[int, int, int]  # its calls of each class, in the kind's order
    right: tuple[int, int, int]  # of those, the ones that happened
    loss: float  # the log losses summed
    vs: Mapping[
        str, Versus
    ]  # against each baseline (a consensus: each algorithm too) it shares a bar close with


NOTHING = Tally(0, 0, (0, 0, 0), (0, 0, 0), 0.0, {})


def tally(mine: Sequence[Call], others: Mapping[str, Sequence[Call]]) -> Tally:
    """``mine`` counted, and set against each of ``others`` on the bar closes both predicted."""
    called, right = [0, 0, 0], [0, 0, 0]
    for c in mine:
        called[c.predicted] += 1
        right[c.predicted] += c.correct
    vs: dict[str, Versus] = {}
    for name, theirs in others.items():
        correct = {(t.symbol, t.bar_close): t.correct for t in theirs}
        shared = [(c.correct, hit) for c in mine if (hit := correct.get((c.symbol, c.bar_close))) is not None]
        if shared:
            vs[name] = Versus(len(shared), sum(m for m, _ in shared), sum(t for _, t in shared))
    return Tally(
        len(mine),
        sum(right),
        (called[0], called[1], called[2]),
        (right[0], right[1], right[2]),
        sum(c.loss for c in mine),
        vs,
    )


def total(tallies: Iterable[Tally]) -> Tally:
    """``tallies`` summed: the span's (pairs of the same bar closes never meet across days)."""
    n = hits = 0
    called, right, loss = [0, 0, 0], [0, 0, 0], 0.0
    vs: dict[str, Versus] = {}
    for t in tallies:
        n, hits, loss = n + t.n, hits + t.hits, loss + t.loss
        for i in range(3):
            called[i] += t.called[i]
            right[i] += t.right[i]
        for name, v in t.vs.items():
            vs[name] = vs[name] + v if name in vs else v
    return Tally(n, hits, (called[0], called[1], called[2]), (right[0], right[1], right[2]), loss, vs)


@dataclass(frozen=True)
class Day:
    """A reviewed day's tallies, by predictor; ``simulated``: the algorithms whose tally is their
    probation's simulation's (#298)."""

    day: date
    tallies: Mapping[str, Tally]
    simulated: frozenset[str] = frozenset()


def day_of(
    day: date,
    rows: Iterable[Row],
    kind: Kind,
    baselines: Collection[str],
    consensus: Collection[str] = (),
    simulated: Mapping[str, Callable[[], Iterable[Row]]] | None = None,
) -> Day:
    """``day``'s tallies from its scored ``rows`` (those counted): each predictor's against each of
    ``baselines``, each of ``consensus`` against every algorithm too. An algorithm of ``simulated``
    is counted from its simulation's rows of the day (read one algorithm at a time: its own and its
    run's baselines', else the live ones) when they hold any of its predictions."""
    live = calls(rows, kind)
    algorithms = [name for name in live if name not in baselines and name not in consensus]
    tallies: dict[str, Tally] = {}
    for name, mine in live.items():
        against = {b: live.get(b, []) for b in baselines if b != name}
        if name in consensus:
            against |= {a: live[a] for a in algorithms}
        tallies[name] = tally(mine, against)
    replayed: set[str] = set()
    for name, replay in (simulated or {}).items():
        sim = calls(replay(), kind)
        if not sim.get(name):
            continue
        tallies[name] = tally(sim[name], {b: sim.get(b) or live.get(b, []) for b in baselines})
        replayed.add(name)
    return Day(day, tallies, frozenset(replayed))


Read = Callable[[Sequence[date]], list[dict[str, Any]]]  # the scored rows of days, special ones left out


def make_days(
    days: Sequence[date],
    read: Read,
    kind: Kind,
    baselines: Collection[str],
    target: int | None,
    consensus: Collection[str] = (),
    simulated: Mapping[str, Simulated] | None = None,
) -> list[Day]:
    """Each of ``days`` from its rows (``read``, a day at a time), of models trained on ``target``
    only (None: every model's), the baselines' and the consensus' all; an algorithm's simulated days
    (#298) from its simulation's rows."""
    kept = {*baselines, *consensus}

    def counted(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return rows if target is None else on_target(rows, kept, target)

    def replay(sim: Simulated, day: date) -> list[dict[str, Any]]:
        return counted(sim.rows([day], False))

    out = []
    for day in days:
        replayed = {
            name: partial(replay, sim, day) for name, sim in (simulated or {}).items() if day in sim.days
        }
        out.append(day_of(day, counted(read([day])), kind, baselines, consensus, replayed))
    return out


class Kept:
    """The days computed, by kind and day, each with the mark it was computed under (``mark``, the
    store's ``simulation_mark`` and ``refill_mark``): at most ``size``, the least recently asked let
    go first. ``lock``: one request computes at a time, and the next finds its days kept."""

    def __init__(self, size: int = KEPT_DAYS) -> None:
        self.size = size
        self.lock = asyncio.Lock()
        self._days: OrderedDict[tuple[str, date], tuple[Hashable, Day]] = OrderedDict()

    def get(self, kind: str, day: date, mark: Hashable) -> Day | None:
        found = self._days.get((kind, day))
        if found is None or found[0] != mark:
            return None
        self._days.move_to_end((kind, day))
        return found[1]

    def put(self, kind: str, made: Day, mark: Hashable) -> None:
        self._days[(kind, made.day)] = (mark, made)
        self._days.move_to_end((kind, made.day))
        while len(self._days) > self.size:
            self._days.popitem(last=False)


# The answer, from the days kept.

History = Sequence[Mapping[str, Any]]  # an algorithm's status changes, oldest first (``status_history``)


def close_of(calendar: MarketCalendar, day: date) -> datetime:
    """When ``day``'s session closes; the day's end without one."""
    session = calendar.session(day)
    return session.close if session is not None else datetime.combine(day + timedelta(days=1), time(), KST)


def status_at(history: History, at: datetime) -> str | None:
    """The status in force at ``at``: the latest change's at or before it; None before the first."""
    status = None
    for change in history:
        if datetime.fromisoformat(change["at"]) > at:
            break
        status = change["status"]
    return status


def day_json(
    made: Day, name: str, kind: Kind, climate: str | None, status: str | None
) -> dict[str, Any] | None:
    """``name``'s values of ``made``; None without a scored prediction."""
    t = made.tallies.get(name)
    if t is None or t.n == 0:
        return None
    action = sum(t.called[kind.index(c)] for c in kind.action)
    versus = None if climate is None else t.vs.get(climate)
    simulated = name in made.simulated
    return {
        "day": made.day.isoformat(),
        "status": "probation" if simulated else status,
        "simulated": simulated,
        "n": t.n,
        "accuracy": t.hits / t.n,
        "coverage": action / t.n,
        "precision": {
            c: {"n": t.called[i], "value": t.right[i] / t.called[i] if t.called[i] else None}
            for i, c in enumerate(kind.classes)
        },
        "lift": None if versus is None else {"n": versus.pairs, "value": versus.lift},
        "log_loss": t.loss / t.n,
    }


def decay_json(
    name: str,
    since: datetime,
    days: Sequence[Day],
    statuses: Mapping[date, str | None],
    baselines: Collection[str],
    rules: Rules,
    min_sample: int,
    recent: int,
) -> dict[str, Any]:
    """An active algorithm's decay mark: its weakest lift over ``baselines`` on its days active since
    ``since`` among the latest ``recent`` of ``days`` (latest first), as the bench check takes it."""
    active = [
        d for d in days[:recent] if d.day >= since.astimezone(KST).date() and statuses.get(d.day) == "active"
    ]
    t = total(d.tallies.get(name, NOTHING) for d in active)
    lifts = {b: t.vs.get(b) for b in sorted(baselines)}
    measured = {b: v for b, v in lifts.items() if v is not None}
    weakest: tuple[str, Versus] | None = None
    if lifts and len(measured) == len(lifts):  # a lift over every baseline, as ``weakest_lift``
        weakest = min(measured.items(), key=lambda bv: bv[1].lift)
    enough = (
        weakest is not None and t.n >= min_sample and all(v.pairs >= min_sample for v in measured.values())
    )
    return {
        "days": len(active),
        "rule_days": rules.min_active_days,
        "weakest": None
        if weakest is None
        else {"baseline": weakest[0], "n": weakest[1].pairs, "value": weakest[1].lift},
        "enough": enough,
        "flagged": enough and weakest is not None and weakest[1].lift <= 0,
    }


def members_json(made: Day, members: Sequence[str]) -> dict[str, Any] | None:
    """The consensus set beside ``members`` on ``made``: its hit rate, and on the bar closes it
    shares with each member theirs and the gain; the weakest gain, against the member that did best.
    None without a scored consensus prediction."""
    t = made.tallies.get(CONSENSUS)
    if t is None or t.n == 0:
        return None
    vs = {m: v for m in members if (v := t.vs.get(m)) is not None}
    best = min(vs, key=lambda m: vs[m].lift, default=None)  # the first of the lowest, in their order
    return {
        "day": made.day.isoformat(),
        "consensus": CONSENSUS,
        "members": list(members),
        "n": t.n,
        "accuracy": t.hits / t.n,
        "vs": {
            m: {"n": v.pairs, "accuracy": v.theirs / v.pairs, "consensus": v.mine / v.pairs, "lift": v.lift}
            for m, v in vs.items()
        },
        "best": best,
        "lift": None if best is None else vs[best].lift,
    }
