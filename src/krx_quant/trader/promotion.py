"""The challengers against the champion, the promotion and the live gate (#400, #393 "정책과 자기
개선", "실전 관문").

**Records.** A policy's paper record is every trading day it ran in the trader (``policy_days``,
which the trader writes while the day's session is open: its champion's book and each running
challenger's, ``desk.py``), each with the net P&L of the policy's trades entered that day, 0 on a
day it made none. A trade ends on the day it is entered (by 15:15; later only when the trader was
down at its time barrier), so a day's trades are all of that day. A policy's days as a challenger
and as the champion are one record: a challenger promoted brings its own. Always paper's
(``records_of``, #405), whatever the trader's mode: every policy, the champion and the challengers
alike, on one fill model, so that a switch from paper to mock to live never starts the history
again. In paper mode the champion's record is its book's, whose trades the limits hold (#396); in
mock mode it is its paper shadow's (``desk.Desk.shadow``, held by nothing, as a challenger's book
is), never its KIS fills; a challenger's trades are held by none (``limits.Unlimited``). The
records are simulated evidence: what a policy would have made filled in paper, not what an
execution makes (rejects, partial fills, the account's reconciliation, the fill gap between KIS
and the shadow, ``gap.py``), which the live mode must check apart (#408).

**Promotion** (``judge``, ``promote``): a challenger becomes the champion when, over the trading
days both ran (``common``), at least ``PROMOTION_DAYS`` of them, the day-level lower bound of
(challenger − champion) net P&L is above 0. The lower bound is the engine's trading-day statistic
(``stats.overlap_t_test`` with an overlap of one session: a Student t over the days), two-sided at
``LEVEL`` with Bonferroni over the challengers running (``policy.challenger_versions``, at most three):
1 − 0.05 ÷ m. A day the champion was stopped (a pause or a loss limit at some time in the session)
is not a common day: its trades were cut short by what is not its policy, the challenger's were
not. Of several passing, the greatest lower bound is promoted (then the newest). A challenger is
judged every night from its tenth common day; one not promoted by its ``RETIRE_DAYS``-th (20) is
dropped (``policy.drop``), which frees its slot for the newest policy waiting (``policy.admit``).
The promotion is judged and written only after the market (``in_market``: from the day's pick at
08:30 to the end of the closing auction it waits), so the champion never changes during a session:
the trader reads it at each pick (``desk.Desk.select``), and a change written before 08:30 holds
the whole day.

**The live gate** (#408, ``check_gate``, ``gate_of``): judged at checkpoints, not every night. The
trader's paper history is every trading day a champion ran in it (``history``: ``policy_days`` with
``champion`` 1, whichever policy), and a checkpoint falls on each ``CHECKPOINT_DAYS``-th (20) of
them: day 20, 40, 60 and so on. At checkpoint k the night's job (``promote``, after the market)
takes the champion of that day (``champion_on``) and its paper record up to that day, at least
``GATE_DAYS`` (20) of its days, and tests the mean of the daily net P&L (no-trade days 0) one-sided at
α_k = ``GATE_ALPHA`` × 6 ÷ (π² k²) (``alpha``): the day-level lower bound two-sided at 1 − 2α_k
(``checkpoint_level``, the engine's ``stats`` statistic with an overlap of one, as before) above 0.
Over every checkpoint the α spent is at most ``GATE_ALPHA`` (Σ 6 ÷ (π² k²) = 1), so a champion
without an edge passes by chance at most 2.5% of the time over the trader's whole life (#408's
simulation, ``tests/test_trader_promotion.py``). k counts the trader's checkpoints, never a
champion's: a new champion gets no fresh α. Each checkpoint is evaluated once and kept
(``gate_checks``), so that a trade that ends later never changes what it found; a night that missed
one evaluates it on the record up to its day. A champion that passed stays passed (``Gate.passed``);
a new one must pass at a later checkpoint, on a day it was the champion. The same checkpoint is also
judged with ``STRESS_TICKS`` (1) extra adverse tick on each leg of each trade (``stressed_low``: the
daily nets less 2 × the trades' tick × shares, the costs already in them not charged again), one of
the live mode's execution checks (``live.py``).

> **바뀜** (2026-10-31, #PRLIVE): the gate was looked at every night: the champion's record of 20 days
> at least, its 95% lower bound above 0 → checkpoints with α spent as 1/k². 이유: with no edge, a
> gate looked at nightly passed by chance 12% of the time by day 120 and 15% by day 250 (#408's
> simulation, t4 daily P&L); the checkpoints keep it at most 2.5% at any horizon.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any

from krx_quant.feeds.base import KST, kst_day
from krx_quant.market_calendar import Session
from krx_quant.stats import overlap_t_test
from krx_quant.trader import PAPER, PICK_AT
from krx_quant.trader import policy as policies

# A challenger is judged on this many common trading days at least (#393).
PROMOTION_DAYS = 10
# One not promoted by this many common days is dropped, freeing its slot (#400, #416).
RETIRE_DAYS = 20
# The live gate (#393, #408): at a checkpoint, the champion's paper record of this many trading days
# at least.
GATE_DAYS = 20
# A checkpoint falls on every this-many-th trading day of the trader's paper history (#408).
CHECKPOINT_DAYS = 20
# The one-sided error the gate may spend over the trader's whole life (#408): α_k = this × 6 ÷ (π² k²).
GATE_ALPHA = 0.025
# The extra adverse ticks a leg of the stressed gate (#408, an execution check of the live mode).
STRESS_TICKS = 1
# The lower bounds' level, two-sided (the engine's "95% lower bound"); a promotion's is divided
# by the challengers (Bonferroni).
LEVEL = 0.95
# The sessions a day's trades span: one (a trade ends on the day it is entered), so the days are
# taken as independent (``stats.overlap_t_test`` with H = 1 is ``stats.t_test``).
OVERLAP = 1
# Why a challenger is not promoted.
FEW_DAYS, NOT_ABOVE = "few_days", "not_above"
# Why no promotion was judged.
MARKET_OPEN, NO_CHAMPION = "market_open", "no_champion"


@dataclass(frozen=True)
class PolicyDay:
    """A trading day a policy ran: its trades' net P&L (0 without one), how many, whether it was
    the champion then and, if so, stopped at some time in the session; and its trades' tick times
    their shares, summed (``ticks``, won: what one tick on every leg's every share costs a leg,
    #408)."""

    day: date
    net: float
    trades: int
    champion: bool
    stopped: bool
    ticks: float = 0.0

    def stressed(self, extra: int = STRESS_TICKS) -> float:
        """The day's net P&L with ``extra`` adverse ticks on each leg of each trade (#408): a buy that
        many ticks dearer, a sale that many cheaper. The costs in ``net`` are not charged again."""
        return self.net - 2 * extra * self.ticks


@dataclass(frozen=True)
class Record:
    """A policy's paper record, oldest day first."""

    version: int
    days: tuple[PolicyDay, ...]

    @property
    def trades(self) -> int:
        return sum(d.trades for d in self.days)

    @property
    def net(self) -> float:
        return sum(d.net for d in self.days)


def records_of(mode: str | None) -> str:
    """The mode whose records ``mode``'s promotion, live gate and report read: paper's for every mode
    (#405), mock and live included: one fill model for every policy, one history across the modes."""
    return PAPER


def mode_at(db: sqlite3.Connection, at: datetime | None = None) -> str:
    """The mode the trader last started in, at or before ``at`` (all its starts if None): the mode
    whose trades and ledger the report shows (#405), not the records it reads (``records_of``).
    ``paper`` before the first start."""
    if not _has_table(db, "runs"):
        return PAPER
    where, params = "", []
    if at is not None:
        where, params = "WHERE at <= ?", [at.astimezone(KST).isoformat()]
    row = db.execute(f"SELECT mode FROM runs {where} ORDER BY at DESC, id DESC LIMIT 1", params).fetchone()
    return PAPER if row is None else str(row[0])


def records(
    db: sqlite3.Connection, mode: str | None = None, versions: Iterable[int] | None = None
) -> dict[int, Record]:
    """The records of ``versions`` (all if None), paper's whatever ``mode`` (``records_of``), read
    from the trader's store; none from a store the trader has not kept them in yet."""
    if not _has_table(db, "policy_days"):
        return {}
    mode = records_of(mode)
    wanted = None if versions is None else set(versions)
    ran: dict[int, list[tuple[date, bool, bool]]] = {}
    for version, day, champion, stopped in db.execute(
        "SELECT policy_id, day, champion, stopped FROM policy_days WHERE mode = ? ORDER BY policy_id, day",
        (mode,),
    ):
        if wanted is None or version in wanted:
            ran.setdefault(version, []).append((date.fromisoformat(day), bool(champion), bool(stopped)))
    made: dict[tuple[int, date], tuple[float, int, float]] = {}
    if ran:
        for version, day, net, count, ticks in db.execute(
            "SELECT policy_id, substr(entry_at, 1, 10), coalesce(sum(net), 0), count(*),"
            " coalesce(sum(tick * quantity), 0) FROM trades"
            " WHERE mode = ? AND policy_id IS NOT NULL AND exit_at IS NOT NULL"
            " GROUP BY policy_id, substr(entry_at, 1, 10)",
            (mode,),
        ):
            if version in ran:
                made[(version, date.fromisoformat(day))] = (float(net), int(count), float(ticks))
    out = {}
    for version, days in ran.items():
        kept = []
        for day, champion, stopped in days:
            net, count, ticks = made.get((version, day), (0.0, 0, 0.0))
            kept.append(PolicyDay(day, net, count, champion, stopped, ticks))
        out[version] = Record(version, tuple(kept))
    return out


def lower_bound(values: Sequence[float], level: float = LEVEL) -> float | None:
    """The day-level lower bound of the mean of ``values`` (one a trading day), two-sided at
    ``level`` (``stats.overlap_t_test``, an overlap of one session); None from fewer than two."""
    found = overlap_t_test(values, list(range(len(values))), OVERLAP, level)
    return None if found is None else found.low


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def paper(record: Record | None) -> dict[str, Any]:
    """A policy's paper record as the Engine API gives it: its first and last day, the days, the
    trades, the net P&L in won, its mean a day and that mean's lower bound (``LEVEL``)."""
    days = () if record is None else record.days
    nets = [d.net for d in days]
    return {
        "since": days[0].day.isoformat() if days else None,
        "last": days[-1].day.isoformat() if days else None,
        "days": len(days),
        "trades": sum(d.trades for d in days),
        "net": sum(nets),
        "mean": _mean(nets),
        "low": lower_bound(nets),
    }


def alpha(k: int) -> float:
    """The one-sided α the live gate spends at its ``k``-th checkpoint (#408): ``GATE_ALPHA`` × 6 ÷
    (π² k²), summing to ``GATE_ALPHA`` over every checkpoint."""
    return GATE_ALPHA * 6 / (math.pi**2 * k**2)


def checkpoint_level(k: int) -> float:
    """The two-sided level of the lower bound at checkpoint ``k``: 1 − 2α_k (its one side at α_k)."""
    return 1 - 2 * alpha(k)


def passes(values: Sequence[float], k: int) -> bool:
    """Whether a record of daily net P&L ``values`` (no-trade days 0) passes the gate at checkpoint
    ``k``: ``GATE_DAYS`` days at least, its lower bound at ``checkpoint_level(k)`` above 0."""
    low = lower_bound(values, checkpoint_level(k))
    return len(values) >= GATE_DAYS and low is not None and low > 0


def history(db: sqlite3.Connection, until: date | None = None) -> list[date]:
    """The trader's paper history (#408): each trading day a champion ran in it (``policy_days``,
    paper, ``champion`` 1), oldest first, to ``until`` if given. The checkpoints count these."""
    if not _has_table(db, "policy_days"):
        return []
    rows = db.execute(
        "SELECT DISTINCT day FROM policy_days WHERE mode = ? AND champion = 1 ORDER BY day", (PAPER,)
    )
    days = [date.fromisoformat(r[0]) for r in rows]
    return [d for d in days if until is None or d <= until]


def champion_on(db: sqlite3.Connection, day: date) -> int | None:
    """The champion that ran on ``day`` (its ``policy_days`` row as the champion); None if none."""
    if not _has_table(db, "policy_days"):
        return None
    row = db.execute(
        "SELECT policy_id FROM policy_days WHERE mode = ? AND champion = 1 AND day = ?"
        " ORDER BY updated_at DESC, policy_id DESC LIMIT 1",
        (PAPER, day.isoformat()),
    ).fetchone()
    return None if row is None else int(row[0])


@dataclass(frozen=True)
class Checkpoint:
    """The live gate at checkpoint ``k`` (#408): on ``day``, the trader's ``k`` × 20th paper day, the
    champion then (``policy``) and its paper record up to it (``days``, ``mean``), its lower bound at
    ``level`` and the same with ``STRESS_TICKS`` extra adverse ticks a leg (``stressed_low``); when it
    was evaluated (``at``)."""

    k: int
    day: date
    policy: int | None
    days: int
    mean: float | None
    low: float | None
    stressed_low: float | None
    at: datetime | None = None

    @property
    def alpha(self) -> float:
        return alpha(self.k)

    @property
    def level(self) -> float:
        return checkpoint_level(self.k)

    @property
    def passed(self) -> bool:
        return self.policy is not None and self.days >= GATE_DAYS and self.low is not None and self.low > 0

    @property
    def stressed_passed(self) -> bool:
        return self.passed and self.stressed_low is not None and self.stressed_low > 0

    def as_json(self) -> dict[str, Any]:
        return {
            "k": self.k,
            "day": self.day.isoformat(),
            "policy": self.policy,
            "days": self.days,
            "mean": self.mean,
            "low": self.low,
            "stressed_low": self.stressed_low,
            "alpha": self.alpha,
            "level": self.level,
            "passed": self.passed,
            "stressed_passed": self.stressed_passed,
            "at": None if self.at is None else self.at.astimezone(KST).isoformat(),
        }


def evaluate(db: sqlite3.Connection, k: int, day: date) -> Checkpoint:
    """Checkpoint ``k`` on ``day`` (module doc): the champion of that day and its record up to it."""
    version = champion_on(db, day)
    if version is None:
        return Checkpoint(k, day, None, 0, None, None, None)
    found = records(db, PAPER, [version]).get(version)
    days = [d for d in (() if found is None else found.days) if d.day <= day]
    nets = [d.net for d in days]
    level = checkpoint_level(k)
    return Checkpoint(
        k,
        day,
        version,
        len(nets),
        _mean(nets),
        lower_bound(nets, level),
        lower_bound([d.stressed() for d in days], level),
    )


def checkpoints(db: sqlite3.Connection) -> list[Checkpoint]:
    """The checkpoints evaluated and kept (``gate_checks``), the first first."""
    if not _has_table(db, "gate_checks"):
        return []
    return [
        Checkpoint(
            int(r[0]),
            date.fromisoformat(r[1]),
            None if r[2] is None else int(r[2]),
            int(r[3]),
            r[4],
            r[5],
            r[6],
            datetime.fromisoformat(r[7]),
        )
        for r in db.execute(
            "SELECT k, day, policy_id, days, mean, low, stressed_low, at FROM gate_checks ORDER BY k"
        )
    ]


def check_gate(db: sqlite3.Connection, now: datetime) -> list[Checkpoint]:
    """The checkpoints due by the trader's paper history and not evaluated yet, evaluated at ``now``
    and kept, in order (a night that missed one evaluates it on the record up to its day). The
    night's job alone writes them, after the market (``promote``)."""
    days = history(db)
    done = {c.k for c in checkpoints(db)}
    out = []
    for k in range(1, len(days) // CHECKPOINT_DAYS + 1):
        if k in done:
            continue
        point = replace(evaluate(db, k, days[k * CHECKPOINT_DAYS - 1]), at=now)
        with db:
            db.execute(
                "INSERT OR IGNORE INTO gate_checks (k, day, policy_id, days, mean, low, stressed_low, alpha,"
                " level, passed, stressed_passed, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    point.k,
                    point.day.isoformat(),
                    point.policy,
                    point.days,
                    point.mean,
                    point.low,
                    point.stressed_low,
                    point.alpha,
                    point.level,
                    int(point.passed),
                    int(point.stressed_passed),
                    now.astimezone(KST).isoformat(),
                ),
            )
        out.append(point)
    return out


@dataclass(frozen=True)
class Gate:
    """The live gate (#393 "실전 관문", #408) on the champion ``policy``: its paper record now
    (``days``, ``mean``, and the lower bound ``low`` at ``level``: the next checkpoint's, or the one it
    passed at), the trader's paper history (``history`` days), the next checkpoint's number
    (``next``), the checkpoint it passed at (``passed_at``) and the first it passed with the extra
    ticks (``stressed``), and the latest checkpoint evaluated (``last``)."""

    policy: int | None
    days: int
    mean: float | None
    low: float | None
    level: float
    history: int
    next: int
    passed_at: Checkpoint | None = None
    stressed: Checkpoint | None = None
    last: Checkpoint | None = None
    required: int = GATE_DAYS

    @property
    def passed(self) -> bool:
        return self.passed_at is not None

    @property
    def next_day(self) -> int:
        """The next checkpoint's day of the trader's paper history."""
        return self.next * CHECKPOINT_DAYS

    def as_json(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "days": self.days,
            "required": self.required,
            "mean": self.mean,
            "low": self.low,
            "level": self.level,
            "passed": self.passed,
            "history": self.history,
            "checkpoint": {
                "k": self.next,
                "day": self.next_day,
                "every": CHECKPOINT_DAYS,
                "alpha": alpha(self.next),
                "level": checkpoint_level(self.next),
            },
            "passed_at": None if self.passed_at is None else self.passed_at.as_json(),
            "stressed": self.stressed is not None,
            "last": None if self.last is None else self.last.as_json(),
        }


def no_gate() -> Gate:
    """The gate before the first champion: no record, the first checkpoint next."""
    return Gate(None, 0, None, None, checkpoint_level(1), 0, 1)


def gate_of(
    db: sqlite3.Connection,
    version: int | None,
    until: date | None = None,
    checked_by: datetime | None = None,
) -> Gate:
    """The live gate on the champion ``version`` from the store ``db``: its record and the trader's
    history up to ``until``, the checkpoints evaluated by ``checked_by`` (all if None)."""
    points = [c for c in checkpoints(db) if checked_by is None or (c.at is not None and c.at <= checked_by)]
    days = len(history(db, until))
    if version is None:
        return replace(no_gate(), history=days, next=len(points) + 1, last=points[-1] if points else None)
    found = records(db, PAPER, [version]).get(version)
    nets = [d.net for d in (() if found is None else found.days) if until is None or d.day <= until]
    passed = next((c for c in points if c.policy == version and c.passed), None)
    stressed = next((c for c in points if c.policy == version and c.stressed_passed), None)
    nxt = len(points) + 1
    level = passed.level if passed is not None else checkpoint_level(nxt)
    return Gate(
        version,
        len(nets),
        _mean(nets),
        lower_bound(nets, level),
        level,
        days,
        nxt,
        passed,
        stressed,
        points[-1] if points else None,
    )


@dataclass(frozen=True)
class Judgment:
    """A challenger against the champion: their common days, the mean a day of (challenger −
    champion) net P&L and its lower bound at ``level`` (Bonferroni over ``challengers``)."""

    challenger: int
    champion: int
    days: int
    mean: float | None
    low: float | None
    level: float
    challengers: int
    required: int = PROMOTION_DAYS
    until: int = RETIRE_DAYS

    @property
    def retires(self) -> bool:
        """Whether it is dropped tonight: its ``until``-th common day reached, not promoted."""
        return not self.passed and self.days >= self.until

    @property
    def why(self) -> str | None:
        """Why it is not promoted (``FEW_DAYS``, ``NOT_ABOVE``); None when it passes."""
        if self.days < self.required:
            return FEW_DAYS
        if self.low is None or self.low <= 0:
            return NOT_ABOVE
        return None

    @property
    def passed(self) -> bool:
        return self.why is None

    def as_json(self) -> dict[str, Any]:
        return {
            "champion": self.champion,
            "days": self.days,
            "required": self.required,
            "until": self.until,
            "mean": self.mean,
            "low": self.low,
            "level": self.level,
            "challengers": self.challengers,
            "passed": self.passed,
            "why": self.why,
        }


def common(champion: Record | None, challenger: Record | None) -> list[tuple[date, float]]:
    """The trading days both ran, oldest first, but those the champion was stopped on, with each
    day's (challenger − champion) net P&L."""
    if champion is None or challenger is None:
        return []
    theirs = {d.day: d for d in champion.days}
    out = []
    for mine in challenger.days:
        other = theirs.get(mine.day)
        if other is not None and not (other.stopped or mine.stopped):
            out.append((mine.day, mine.net - other.net))
    return out


def bonferroni(challengers: int) -> float:
    """The two-sided level of each challenger's lower bound when ``challengers`` run at once."""
    return 1 - (1 - LEVEL) / max(1, challengers)


def judge(champion: int, records_: Mapping[int, Record], challengers: Sequence[int]) -> list[Judgment]:
    """Each of ``challengers`` (the versions running) against the ``champion`` (module doc)."""
    level = bonferroni(len(challengers))
    out = []
    for version in challengers:
        diffs = [d for _, d in common(records_.get(champion), records_.get(version))]
        out.append(
            Judgment(
                version,
                champion,
                len(diffs),
                _mean(diffs),
                lower_bound(diffs, level),
                level,
                len(challengers),
            )
        )
    return out


def in_market(now: datetime, sessions: Callable[[date], Session | None]) -> bool:
    """Whether ``now`` is in a trading day's market: from its pick (``PICK_AT``, when the trader
    reads the day's champion) to the end of its closing auction. No promotion is written then."""
    day = kst_day(now)
    session = sessions(day)
    return session is not None and datetime.combine(day, PICK_AT, KST) <= now < session.close


@dataclass(frozen=True)
class Promotion:
    """A night's judgment: the champion before, each challenger's, the one promoted if any, why
    none was judged (``MARKET_OPEN``, ``NO_CHAMPION``), the challengers dropped (``RETIRE_DAYS``
    reached unpromoted), the live gate on the champion after, and the gate's checkpoints evaluated
    that night (#408)."""

    at: datetime
    champion: int | None
    judgments: tuple[Judgment, ...]
    promoted: int | None
    deferred: str | None
    gate: Gate
    dropped: tuple[int, ...] = ()
    checked: tuple[Checkpoint, ...] = ()


def promote(
    db: sqlite3.Connection,
    now: datetime,
    sessions: Callable[[date], Session | None],
    mode: str | None = None,
) -> Promotion:
    """Judge the challengers running against the champion on the records in the store ``db``
    (``policy.connect``) and, after the market, make the one that passes the champion
    (``policy.crown``) and drop those that reached ``RETIRE_DAYS`` unpromoted (``policy.drop``;
    the slots they free are filled at the night's end, ``policy.admit``). During a trading day's
    market nothing is judged or written. The records are paper's whatever ``mode`` (``records_of``,
    #405)."""
    mode = records_of(mode)
    current = policies.champion_version(db)
    if in_market(now, sessions):
        return Promotion(now, current, (), None, MARKET_OPEN, gate_of(db, current))
    # The live gate's checkpoints due (#408): on the champion of each checkpoint's day, whatever
    # tonight's judgment makes the champion from tomorrow.
    checked = tuple(check_gate(db, now))
    if current is None:
        return Promotion(now, None, (), None, NO_CHAMPION, gate_of(db, None), checked=checked)
    running = policies.challenger_versions(db)
    kept = records(db, mode, [current, *running])
    judgments = tuple(judge(current, kept, running))
    passing = [j for j in judgments if j.passed]
    promoted = None
    if passing:
        best = max(passing, key=lambda j: (j.low or 0.0, j.challenger))
        if policies.crown(db, now, best.challenger, _reason(best), _detail(best), replacing=current):
            promoted = best.challenger
    dropped = tuple(j.challenger for j in judgments if j.retires and j.challenger != promoted)
    policies.drop(db, now, dropped)
    return Promotion(
        now, current, judgments, promoted, None, gate_of(db, promoted or current), dropped, checked
    )


def _reason(j: Judgment) -> str:
    return (
        f"도전 정책 {j.challenger}이 챔피언 정책 {j.champion}을 모의로 이겼다(#400): "
        f"함께 돈 거래일 {j.days}일, "
        f"날마다 (도전 − 챔피언) 순손익 평균 {_won(j.mean)}, 하한 {_won(j.low)}"
        f"(신뢰수준 {j.level:.2%}, 도전 정책 {j.challengers}개로 Bonferroni 보정)"
    )


def _detail(j: Judgment) -> dict[str, Any]:
    return {
        "from": j.champion,
        **{k: v for k, v in j.as_json().items() if k not in ("champion", "until", "passed", "why")},
    }


def _won(value: float | None) -> str:
    return "없음" if value is None else f"{value:+,.0f}원"


def progress(db: sqlite3.Connection, mode: str | None = None) -> dict[str, Any]:
    """``GET /v1/trader``'s policies (#400): the champion's version, the live gate on its record,
    and the champion and each challenger running with their records and, for a challenger, where it
    stands against the champion; the records ``mode``'s (None: the mode the trader last started in,
    #405). Read-only; empty from a store without policies."""
    if not _has_table(db, "policies") or not _has_table(db, "champions"):
        return {"champion": None, "gate": no_gate().as_json(), "policies": []}
    current = policies.champion_version(db)
    running = policies.challenger_versions(db)
    kept = records(db, mode, [*([] if current is None else [current]), *running])
    judged = {j.challenger: j for j in judge(current, kept, running)} if current is not None else {}
    listed: list[dict[str, Any]] = []
    if current is not None:
        listed.append(
            {
                "version": current,
                "status": policies.CHAMPION,
                "paper": paper(kept.get(current)),
                "promotion": None,
            }
        )
    for version in running:
        found = judged.get(version)
        listed.append(
            {
                "version": version,
                "status": policies.CHALLENGER,
                "paper": paper(kept.get(version)),
                "promotion": None if found is None else found.as_json(),
            }
        )
    return {
        "champion": current,
        "gate": gate_of(db, current).as_json(),
        "policies": listed,
    }


def report(done: Promotion) -> str:
    """The night's judgment for the job's output (#400): won and counts, no price."""
    lines = ["도전 정책 판정 (#400)"]
    if done.deferred == MARKET_OPEN:
        lines.append("장중이라 판정하지 않았다: 챔피언은 장이 끝난 뒤에만 바뀐다.")
    elif done.deferred == NO_CHAMPION:
        lines.append("챔피언이 아직 없다(첫 밤 정책 맞추기 전).")
    elif not done.judgments:
        lines.append(f"챔피언 정책 {done.champion}: 도는 도전 정책이 없다.")
    else:
        level = done.judgments[0].level
        lines.append(
            f"챔피언 정책 {done.champion} · 도전 정책 {len(done.judgments)}개 · "
            f"하한의 신뢰수준 {level:.2%}(Bonferroni) · 함께 돈 거래일 {PROMOTION_DAYS}일부터 판정, "
            f"{RETIRE_DAYS}일까지 못 이기면 내린다"
        )
        for j in done.judgments:
            verdict = {None: "통과", FEW_DAYS: "날 수 부족", NOT_ABOVE: "하한 ≤ 0"}[j.why]
            if j.retires:
                verdict += f", {j.until}일을 채워 내림"
            lines.append(
                f"- 정책 {j.challenger}: 함께 돈 거래일 {j.days}일, 날마다 (도전 − 챔피언) 순손익 평균 "
                f"{_won(j.mean)}, 하한 {_won(j.low)} → {verdict}"
            )
        lines.append(
            f"→ 정책 {done.promoted}이 챔피언이 됐다(trader가 다음 08:30 고르기부터 쓴다)."
            if done.promoted is not None
            else "→ 챔피언은 그대로다."
        )
    if done.dropped:
        lines.append(
            f"내린 도전 정책({RETIRE_DAYS}일 동안 이기지 못함, 자리를 비운다): "
            + ", ".join(f"정책 {v}" for v in done.dropped)
        )
    for c in done.checked:
        lines.append(checkpoint_line(c))
    g = done.gate
    if g.policy is not None:
        lines.append(gate_line(g))
    return "\n".join(lines)


def checkpoint_line(c: Checkpoint) -> str:
    """A checkpoint evaluated (#408), for the night's output: no price."""
    head = f"실전 관문 판정 {c.k}번째(모의 이력 {c.k * CHECKPOINT_DAYS}거래일째, {c.day}, α {c.alpha:.3%})"
    if c.policy is None:
        return f"{head}: 그날 챔피언이 없다 → 넘지 못함"
    verdict = "넘었다" if c.passed else ("기록 부족" if c.days < GATE_DAYS else "하한 ≤ 0")
    return (
        f"{head}: 챔피언 정책 {c.policy}의 모의 기록 {c.days}일, 날마다 순손익 평균 {_won(c.mean)}, "
        f"하한 {_won(c.low)}(신뢰수준 {c.level:.2%}), 다리마다 {STRESS_TICKS}틱 더 불리하면 하한 "
        f"{_won(c.stressed_low)} → {verdict}"
    )


def gate_line(g: Gate) -> str:
    """The live gate on the champion now (#408), for the night's output: no price."""
    head = f"실전 관문: 챔피언 정책 {g.policy}의 모의 기록 {g.days}일(이력 {g.history}거래일)"
    if g.passed_at is not None:
        c = g.passed_at
        return f"{head} → 넘었다({c.k}번째 판정, {c.day}, 하한 {_won(c.low)}, 신뢰수준 {c.level:.2%})"
    return (
        f"{head}, 날마다 순손익 평균 {_won(g.mean)}, 다음 판정({g.next}번째, 이력 {g.next_day}거래일째, "
        f"α {alpha(g.next):.3%})의 신뢰수준 {g.level:.2%}로 지금 하한 {_won(g.low)} → 아직"
    )


def _has_table(db: sqlite3.Connection, name: str) -> bool:
    found = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone()
    return found is not None
