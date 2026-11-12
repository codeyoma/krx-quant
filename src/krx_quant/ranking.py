"""The ranking (#697): each night, the algorithms of each verdict horizon ranked by their skill against
the kind's climate baseline, and the members the verdict pools (``verdict.py``, version 2).

The user's decisions (2026-11-12, #697):

- **Skill against a baseline**, not the raw hit rate, which rewards always saying timeout or flat: an
  algorithm's **log-loss skill** on a trading day is the mean, over the predictions it shares with the
  kind's climate baseline (the same symbol and bar close; ``algorithm_days.climate_of``), of the climate's
  log loss less its own (each floored as the nightly training's, ``forecasts.LOSS_FLOOR``): above 0 it
  forecast the classes better than their shares by symbol and slot (or day) did. The persistence
  baseline puts all on one class, so its log loss is the floor's on every miss: it is not the yardstick.
  The days' means, every day weighing the same, have a two-sided ``LEVEL`` Student t interval
  (``stats.t_test``: the predictions of a day are clustered, the days far closer to independent); a kind
  whose windows run several sessions (the week's, ``Kind.overlap``) takes the Newey–West one
  (``stats.overlap_t_test``, #310). **It is ranked by that interval's lower bound**, highest first.
- Beside it, as shown, not ranked by: its **precision** (the share of its rise and fall calls, the
  kind's ``sides``, that happened), how many **calls** and how often it calls (``call_rate``), and for a
  barrier kind the **net** of its long calls after cost (``barriers.barrier_profit``, ticks a trade).
- **Members** are every ranked algorithm whose lower bound is above 0 (not a share of the ranked). A
  horizon without one says WAIT.
- **Hysteresis**: re-ranked every night, an algorithm **enters or leaves** the members only once the
  same result (above 0 or not, unranked counting as not) held ``NIGHTS`` (3) rankings running. The first
  ranking of a kind that ranks any algorithm (no snapshot before, or none before ranked one: evidence came
  only now, as on the first nights after #697's deploy) takes each result as it is: there is no earlier
  state to keep.

**Evidence**, per algorithm, the first of these with enough (``Window``):

- **live**: its recorded forecasts (``kind_predictions``, every lifecycle: the shadow rows too, #696)
  scored on their answers, special ones left out, of models trained on the kind's answers
  (``stats.on_target``), on the kind's latest days answered before the night: ``RECENT_DAYS`` (20) for a
  bar-close kind, every day for a daily one;
- **walk-forward**: its pre-validation's (``ranking_evidence.py``): a bar-close algorithm's replay of
  its pre-validation (``state/prevalidation-<name>.sqlite3``, its latest ``RECENT_DAYS`` days), a daily
  one's walk-forward over the KRX days (``state/rankings/<name>.json``); only of the code, answers and
  inputs it runs now.

Without enough of either it is **unranked** (``why``): fewer than ``Window.min_days`` days or
``Window.min_calls`` calls (live as it stands, or the walk-forward's when it has one). The minimums:

- a bar-close kind: 10 days (the fewest a pre-validation judges on, ``prevalidation.MIN_DAYS``: a day's
  predictions are one value, and below 10 a Student t over them is too wide to rank by) and 30 calls
  (``stats.MIN_PAIRS_TESTED``: a precision on fewer is noise);
- a daily kind: 250 days (a year of sessions: a day is one prediction a symbol, and the week's windows
  overlap five sessions, so 250 days hold about 50 independent ones) and 100 calls. Its live days reach
  that only after a year: until then it is ranked on its walk-forward, about seven years out of sample.

**The snapshot** of a kind each night (``Snapshot``), stored once and never rewritten
(``predictions.rankings``), with its version (``RANKING_VERSION``: these constants), the night, when it
was made and the latest day its evidence counts (``through``). The live engine makes it at each daily
review (``engine._rank``), after the day's answers, from stored rows only: no model is trained.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from krx_quant.algorithm_days import climate_of
from krx_quant.algorithms.forecasts import LOSS_FLOOR
from krx_quant.barriers import CLASSES as BARRIER_CLASSES
from krx_quant.barriers import barrier_profit
from krx_quant.feeds.base import KST
from krx_quant.kind_stats import scored_of_kind
from krx_quant.kinds import Kind, KindScored
from krx_quant.predictions import iso, kind_reviewed_rows
from krx_quant.stats import Lift, on_target, overlap_t_test, session_index, t_test

if TYPE_CHECKING:
    from krx_quant.algorithms import KindAlgorithm
    from krx_quant.daily import DailyAlgorithm

RANKING_VERSION = 1  # the constants below: changing one is a new version
NIGHTS = 3  # the rankings running a result must hold before an algorithm enters or leaves
LEVEL = 0.95  # the skill's two-sided interval; ranked by its lower bound
RECENT_DAYS = 20  # a bar-close kind's latest days counted (live, and of its pre-validation's replay)
LIVE, WALK_FORWARD = "live", "walk_forward"  # where an algorithm's evidence came from
FEW_DAYS, FEW_CALLS, NO_EVIDENCE = "few_days", "few_calls", "no_evidence"  # why unranked


@dataclass(frozen=True)
class Window:
    """How much evidence a kind's algorithm is ranked on (module doc): its latest ``days`` live (None:
    every one), and at least ``min_days`` days and ``min_calls`` calls."""

    days: int | None
    min_days: int
    min_calls: int


BAR_CLOSE = Window(days=RECENT_DAYS, min_days=10, min_calls=30)
DAILY = Window(days=None, min_days=250, min_calls=100)


@dataclass(frozen=True)
class DayScore:
    """An algorithm's trading day against the kind's climate (module doc): the predictions both made,
    its skill summed over them (the climate's log loss less its own), its rise and fall calls and those
    that happened, and of its long calls those with a P&L and their net after cost (ticks)."""

    day: date
    n: int
    skill: float
    calls: int
    hits: int
    longs: int = 0
    net: float = 0.0

    def as_list(self) -> list[Any]:
        """As a walk-forward's evidence file keeps it (``ranking_evidence.py``)."""
        return [self.day.isoformat(), self.n, self.skill, self.calls, self.hits, self.longs, self.net]

    @classmethod
    def of_list(cls, values: Sequence[Any]) -> DayScore:
        day, n, skill, calls, hits, longs, net = values
        return cls(
            date.fromisoformat(str(day)), int(n), float(skill), int(calls), int(hits), int(longs), float(net)
        )


def _loss(r: KindScored) -> float:
    return -math.log(max(r.probabilities[r.outcome], LOSS_FLOOR))


def score_day(
    day: date, records: Mapping[str, Sequence[KindScored]], climate: str, kind: Kind
) -> dict[str, DayScore]:
    """Each predictor's ``day`` from its scored ``records`` against ``climate``'s among them; the climate
    itself and a predictor sharing no bar close with it are left out."""
    theirs = {(r.symbol, r.bar_close): r for r in records.get(climate, ())}
    sides = kind.sides or ()
    barrier = kind.classes == BARRIER_CLASSES
    out: dict[str, DayScore] = {}
    for name, mine in records.items():
        if name == climate:
            continue
        n = calls = hits = longs = 0
        skill = net = 0.0
        for r in mine:
            c = theirs.get((r.symbol, r.bar_close))
            if c is None:
                continue
            n += 1
            skill += _loss(c) - _loss(r)
            called = kind.classes[r.predicted]
            if called in sides:
                calls += 1
                hits += r.predicted == r.outcome
            if barrier and sides and called == sides[0]:
                profit = barrier_profit(called, kind.classes[r.outcome], r.path)
                if profit is not None:
                    longs += 1
                    net += profit
        if n:
            out[name] = DayScore(day, n, skill, calls, hits, longs, net)
    return out


def answered_days(db: sqlite3.Connection, kind: Kind, before: date, limit: int | None) -> list[date]:
    """The latest ``limit`` (None: every) days before ``before`` with a predicted window of ``kind``
    answered with a class, latest first."""
    rows = db.execute(
        "SELECT DISTINCT substr(a.bar_close, 1, 10) AS day FROM kind_answers a WHERE a.kind = ?"
        " AND a.answer_version = ? AND a.class IS NOT NULL AND a.bar_close < ? AND EXISTS (SELECT 1"
        " FROM kind_predictions p WHERE p.kind = a.kind AND p.symbol = a.symbol"
        " AND p.bar_close = a.bar_close)"
        " ORDER BY day DESC" + ("" if limit is None else " LIMIT ?"),
        (kind.name, kind.answer_version, iso(datetime.combine(before, datetime.min.time(), KST)))
        + (() if limit is None else (limit,)),
    )
    return [date.fromisoformat(r[0]) for r in rows]


def scores_of(
    db: sqlite3.Connection, kind: Kind, days: Sequence[date], names: Collection[str]
) -> dict[str, list[DayScore]]:
    """Each of ``names``' days of ``days`` (oldest first) against the kind's climate, from ``db``'s rows
    (the engine's, or a pre-validation's replay), a day's rows at a time: every lifecycle, special ones
    left out, an algorithm's of models trained on the kind's answers only. Empty without a climate."""
    climate = climate_of(kind, kind.baseline_names())
    if climate is None:
        return {}
    out: dict[str, list[DayScore]] = {}
    for day in sorted(days):
        rows = kind_reviewed_rows(db, kind.name, kind.answer_version, [day], False, shadow=True)
        rows = [
            r for r in on_target(rows, {climate}, kind.answer_version) if r["algorithm"] in {*names, climate}
        ]
        for name, score in score_day(day, scored_of_kind(rows, kind), climate, kind).items():
            out.setdefault(name, []).append(score)
    return out


@dataclass(frozen=True)
class Evidence:
    """An algorithm's days (oldest first) and where they came from (``LIVE``, ``WALK_FORWARD``)."""

    source: str
    days: tuple[DayScore, ...]

    @property
    def calls(self) -> int:
        return sum(d.calls for d in self.days)

    def short_of(self, window: Window) -> str | None:
        """Why it is too little to rank on (``FEW_DAYS``, ``FEW_CALLS``); None if it is enough."""
        if len(self.days) < window.min_days:
            return FEW_DAYS
        return FEW_CALLS if self.calls < window.min_calls else None


def chosen(live: Evidence | None, walked: Evidence | None, window: Window) -> Evidence | None:
    """The evidence an algorithm is ranked on (module doc): live if enough, else the walk-forward's if
    any, else live as it is (to say why it is unranked)."""
    if live is not None and live.short_of(window) is None:
        return live
    return walked if walked is not None else live


# An algorithm's walk-forward evidence before a night (``ranking_evidence.WalkForwardEvidence``): None
# without current evidence.
WalkForwardOf = Callable[["KindAlgorithm | DailyAlgorithm", Kind, date], "Evidence | None"]


@dataclass(frozen=True)
class Carried:
    """An algorithm's state in a kind's last snapshot: a member or not, the rankings it has held that
    state, and the rankings running whose result was the other."""

    member: bool
    nights: int
    pending: int


@dataclass(frozen=True)
class Placed:
    """An algorithm in a night's ranking of a kind (module doc)."""

    algorithm: str
    status: str | None  # its lifecycle at the night: its status, or shadow (#696); None: none recorded
    source: str | None  # LIVE, WALK_FORWARD; None without evidence
    days: int
    n: int  # the predictions paired with the climate's
    skill: float | None  # the days' mean skill; None unranked
    low: float | None
    high: float | None
    calls: int
    precision: float | None
    call_rate: float | None
    longs: int
    net: float | None  # a long call's mean net after cost, ticks; None without one
    why: str | None  # why unranked; None ranked
    qualifies: bool  # ranked and its lower bound above 0, tonight
    member: bool
    nights: int
    pending: int
    rank: int | None = None

    @property
    def ranked(self) -> bool:
        return self.why is None

    def as_json(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "algorithm": self.algorithm,
            "status": self.status,
            "source": self.source,
            "days": self.days,
            "n": self.n,
            "skill": self.skill,
            "skill_low": self.low,
            "skill_high": self.high,
            "precision": self.precision,
            "calls": self.calls,
            "call_rate": self.call_rate,
            "longs": self.longs,
            "net_ticks": self.net,
            "ranked": self.ranked,
            "why": self.why,
            "qualifies": self.qualifies,
            "member": self.member,
            "nights": self.nights,
            "pending": self.pending,
        }

    @classmethod
    def of_json(cls, d: Mapping[str, Any]) -> Placed:
        return cls(
            algorithm=d["algorithm"],
            status=d.get("status"),
            source=d.get("source"),
            days=int(d.get("days") or 0),
            n=int(d.get("n") or 0),
            skill=d.get("skill"),
            low=d.get("skill_low"),
            high=d.get("skill_high"),
            calls=int(d.get("calls") or 0),
            precision=d.get("precision"),
            call_rate=d.get("call_rate"),
            longs=int(d.get("longs") or 0),
            net=d.get("net_ticks"),
            why=d.get("why"),
            qualifies=bool(d.get("qualifies")),
            member=bool(d.get("member")),
            nights=int(d.get("nights") or 0),
            pending=int(d.get("pending") or 0),
            rank=d.get("rank"),
        )


def skill_interval(evidence: Evidence, overlap: int = 1) -> Lift | None:
    """The days' mean skill with its ``LEVEL`` interval (module doc); None from fewer than two days."""
    days = [d for d in evidence.days if d.n]
    values = [d.skill / d.n for d in days]
    if overlap <= 1:
        return t_test(values, LEVEL)
    places = session_index(d.day for d in days)
    return overlap_t_test(values, [places[d.day] for d in days], overlap, LEVEL)


def carry(before: Carried | None, qualifies: bool, first: bool) -> Carried:
    """Tonight's state from the last snapshot's (``before``; None: not in it) and tonight's result
    (module doc). ``first``: the kind ranked none before, so the result is taken as it is."""
    if first:
        kept = before is not None and before.member == qualifies
        return Carried(qualifies, before.nights + 1 if before is not None and kept else 1, 0)
    if before is None:
        before = Carried(False, 0, 0)
    if qualifies == before.member:
        return Carried(before.member, before.nights + 1, 0)
    pending = before.pending + 1
    if pending >= NIGHTS:
        return Carried(qualifies, 1, 0)
    return Carried(before.member, before.nights + 1, pending)


def place(
    name: str,
    status: str | None,
    evidence: Evidence | None,
    window: Window,
    overlap: int,
    before: Carried | None,
    first: bool,
) -> Placed:
    """``name``'s place tonight, before its rank is given (``rank``)."""
    days = () if evidence is None else evidence.days
    n = sum(d.n for d in days)
    calls = sum(d.calls for d in days)
    longs = sum(d.longs for d in days)
    why = NO_EVIDENCE if evidence is None else evidence.short_of(window)
    interval = None if evidence is None or why is not None else skill_interval(evidence, overlap)
    if why is None and (interval is None or interval.low is None):
        why = FEW_DAYS
    qualifies = why is None and interval is not None and interval.low is not None and interval.low > 0
    state = carry(before, qualifies, first)
    ranked = why is None and interval is not None
    return Placed(
        algorithm=name,
        status=status,
        source=None if evidence is None else evidence.source,
        days=len(days),
        n=n,
        skill=interval.value if ranked and interval is not None else None,
        low=interval.low if ranked and interval is not None else None,
        high=interval.high if ranked and interval is not None else None,
        calls=calls,
        precision=sum(d.hits for d in days) / calls if calls else None,
        call_rate=calls / n if n else None,
        longs=longs,
        net=sum(d.net for d in days) / longs if longs else None,
        why=why,
        qualifies=qualifies,
        member=state.member,
        nights=state.nights,
        pending=state.pending,
    )


@dataclass(frozen=True)
class Snapshot:
    """A night's ranking of a kind (module doc)."""

    kind: str
    night: date
    made_at: datetime
    through: date | None  # the latest day any of its evidence counts
    rows: tuple[Placed, ...]
    version: int = RANKING_VERSION
    window: Window = BAR_CLOSE
    nights: int = NIGHTS
    # Whether this kind ever ranked an algorithm (this snapshot or one before): from then on every
    # ranking carries its states (module doc, the hysteresis), a night none could be ranked included.
    settled: bool = False

    @property
    def members(self) -> frozenset[str]:
        return frozenset(p.algorithm for p in self.rows if p.member)

    def carried(self) -> dict[str, Carried]:
        return {p.algorithm: Carried(p.member, p.nights, p.pending) for p in self.rows}

    def as_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "night": self.night.isoformat(),
            "version": self.version,
            "made_at": self.made_at.astimezone(KST).isoformat(),
            "through": None if self.through is None else self.through.isoformat(),
            "rules": {
                "level": LEVEL,
                "nights": self.nights,
                "window_days": self.window.days,
                "min_days": self.window.min_days,
                "min_calls": self.window.min_calls,
            },
            "settled": self.settled,
            "members": sorted(self.members),
            "rows": [p.as_json() for p in self.rows],
        }

    @classmethod
    def of_json(cls, d: Mapping[str, Any]) -> Snapshot:
        rules = d.get("rules") or {}
        return cls(
            kind=d["kind"],
            night=date.fromisoformat(d["night"]),
            made_at=datetime.fromisoformat(d["made_at"]),
            through=None if d.get("through") is None else date.fromisoformat(d["through"]),
            rows=tuple(Placed.of_json(r) for r in d.get("rows", ())),
            version=int(d.get("version", RANKING_VERSION)),
            window=Window(
                rules.get("window_days"), int(rules.get("min_days", 0)), int(rules.get("min_calls", 0))
            ),
            nights=int(rules.get("nights", NIGHTS)),
            settled=bool(d.get("settled", any(r.get("ranked") for r in d.get("rows", ())))),
        )


def rank(
    kind: Kind,
    night: date,
    made_at: datetime,
    evidence: Mapping[str, Evidence | None],
    statuses: Mapping[str, str | None],
    before: Snapshot | None,
    window: Window,
) -> Snapshot:
    """``kind``'s ranking of ``night`` (module doc): each algorithm of ``evidence`` (its chosen evidence,
    or None) with its lifecycle (``statuses``), carried from the kind's last snapshot (``before``)."""
    carried = {} if before is None else before.carried()
    first = before is None or not before.settled
    placed = [
        place(name, statuses.get(name), found, window, kind.overlap, carried.get(name), first)
        for name, found in evidence.items()
    ]
    ranked = sorted(
        (p for p in placed if p.ranked),
        key=lambda p: (-(p.low or 0.0), -(p.skill or 0.0), p.algorithm),
    )
    rows = [
        *(_ranked_as(p, i + 1) for i, p in enumerate(ranked)),
        *sorted((p for p in placed if not p.ranked), key=lambda p: p.algorithm),
    ]
    through = max((d.day for e in evidence.values() if e is not None for d in e.days), default=None)
    settled = (before is not None and before.settled) or any(p.ranked for p in placed)
    return Snapshot(kind.name, night, made_at, through, tuple(rows), window=window, settled=settled)


def _ranked_as(p: Placed, rank_: int) -> Placed:
    return Placed(**{**p.__dict__, "rank": rank_})


def rank_kinds(
    db: sqlite3.Connection | None,
    todo: Sequence[tuple[Kind, Window, Sequence[Any]]],
    night: date,
    until: date,
    made_at: datetime,
    statuses: Mapping[str, str | None],
    before: Mapping[str, Snapshot],
    walk_forward: WalkForwardOf | None,
) -> list[Snapshot]:
    """Each kind of ``todo`` (with its window and algorithms) ranked for ``night`` (module doc): its
    evidence of the days before ``until``, live from ``db`` (an engine's; None: none), else the
    walk-forward's, carried from the kind's last snapshot in ``before``."""
    made = []
    for kind, window, algorithms in todo:
        found = (
            {}
            if db is None
            else scores_of(
                db, kind, answered_days(db, kind, until, window.days), {a.name for a in algorithms}
            )
        )
        evidence: dict[str, Evidence | None] = {}
        for a in algorithms:
            live = Evidence(LIVE, tuple(found[a.name])) if found.get(a.name) else None
            walked = None
            if walk_forward is not None and (live is None or live.short_of(window) is not None):
                walked = walk_forward(a, kind, until)
            evidence[a.name] = chosen(live, walked, window)
        made.append(rank(kind, night, made_at, evidence, statuses, before.get(kind.name), window))
    return made


def text(snapshot: Snapshot) -> str:
    """A snapshot in one log line (``docs/operations.md``, Loki): its kind, night, members, and each
    ranked algorithm's skill bound, then the unranked and why."""
    members = ", ".join(sorted(snapshot.members)) or "none (WAIT)"
    ranked = "; ".join(
        f"{p.rank}. {p.algorithm} {p.low:+.4f} ({p.source}, {p.days}d, {p.calls} calls"
        f"{', member' if p.member else ''}{f', pending {p.pending}' if p.pending else ''})"
        for p in snapshot.rows
        if p.ranked and p.low is not None
    )
    unranked = ", ".join(f"{p.algorithm} ({p.why}, {p.days}d)" for p in snapshot.rows if not p.ranked)
    return (
        f"ranking {snapshot.kind} {snapshot.night}: members {members}"
        + (f" | ranked {ranked}" if ranked else "")
        + (f" | unranked {unranked}" if unranked else "")
    )
