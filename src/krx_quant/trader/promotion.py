"""The challengers against the champion, the promotion and the live gate (#400, #393 "정책과 자기
개선", "실전 관문").

**Records.** A policy's paper record is every trading day it ran in the trader (``policy_days``,
which the trader writes while the day's session is open: its champion's book and each running
challenger's, ``desk.py``), each with the net P&L of the policy's trades entered that day, 0 on a
day it made none. A trade ends on the day it is entered (by 15:15; later only when the trader was
down at its time barrier), so a day's trades are all of that day. A policy's days as a challenger
and as the champion are one record: a challenger promoted brings its own. Paper only (mode
``paper``): the champion's paper trades are the ones the limits hold (#396), a challenger's are
held by none (``limits.Unlimited``).

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

**The live gate** (``gate``): the champion's paper record of at least ``GATE_DAYS`` trading days,
whose day-level lower bound of the daily net P&L (no-trade days 0, ``LEVEL`` with an overlap of
one) is above 0. It is measured here (``GET /v1/trader``, the nightly job's output); the live mode
that orders only past it is #408's.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
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
# The live gate: the champion's paper record of this many trading days at least (#393).
GATE_DAYS = 20
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
    the champion then and, if so, stopped at some time in the session."""

    day: date
    net: float
    trades: int
    champion: bool
    stopped: bool


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


def records(
    db: sqlite3.Connection, mode: str = PAPER, versions: Iterable[int] | None = None
) -> dict[int, Record]:
    """The paper records of ``versions`` (all if None) in ``mode``, read from the trader's store;
    none from a store the trader has not kept them in yet."""
    if not _has_table(db, "policy_days"):
        return {}
    wanted = None if versions is None else set(versions)
    ran: dict[int, list[tuple[date, bool, bool]]] = {}
    for version, day, champion, stopped in db.execute(
        "SELECT policy_id, day, champion, stopped FROM policy_days WHERE mode = ? ORDER BY policy_id, day",
        (mode,),
    ):
        if wanted is None or version in wanted:
            ran.setdefault(version, []).append((date.fromisoformat(day), bool(champion), bool(stopped)))
    made: dict[tuple[int, date], tuple[float, int]] = {}
    if ran:
        for version, day, net, count in db.execute(
            "SELECT policy_id, substr(entry_at, 1, 10), coalesce(sum(net), 0), count(*) FROM trades"
            " WHERE mode = ? AND policy_id IS NOT NULL AND exit_at IS NOT NULL"
            " GROUP BY policy_id, substr(entry_at, 1, 10)",
            (mode,),
        ):
            if version in ran:
                made[(version, date.fromisoformat(day))] = (float(net), int(count))
    return {
        version: Record(
            version,
            tuple(
                PolicyDay(day, *made.get((version, day), (0.0, 0)), champion, stopped)
                for day, champion, stopped in days
            ),
        )
        for version, days in ran.items()
    }


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


@dataclass(frozen=True)
class Gate:
    """The live gate (#393 "실전 관문") on the champion's paper record."""

    policy: int | None
    days: int
    mean: float | None
    low: float | None
    required: int = GATE_DAYS
    level: float = LEVEL

    @property
    def passed(self) -> bool:
        return self.days >= self.required and self.low is not None and self.low > 0

    def as_json(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "days": self.days,
            "required": self.required,
            "mean": self.mean,
            "low": self.low,
            "level": self.level,
            "passed": self.passed,
        }


def gate(version: int | None, record: Record | None) -> Gate:
    """The live gate on the champion ``version``'s record: every day it ran, a day without a trade
    0; none before the first champion."""
    nets = [] if record is None else [d.net for d in record.days]
    return Gate(version, len(nets), _mean(nets), lower_bound(nets))


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
    reached unpromoted), and the live gate on the champion after."""

    at: datetime
    champion: int | None
    judgments: tuple[Judgment, ...]
    promoted: int | None
    deferred: str | None
    gate: Gate
    dropped: tuple[int, ...] = ()


def promote(
    db: sqlite3.Connection,
    now: datetime,
    sessions: Callable[[date], Session | None],
    mode: str = PAPER,
) -> Promotion:
    """Judge the challengers running against the champion on the records in the store ``db``
    (``policy.connect``) and, after the market, make the one that passes the champion
    (``policy.crown``) and drop those that reached ``RETIRE_DAYS`` unpromoted (``policy.drop``;
    the slots they free are filled at the night's end, ``policy.admit``). During a trading day's
    market nothing is judged or written."""
    current = policies.champion_version(db)
    if in_market(now, sessions):
        return Promotion(now, current, (), None, MARKET_OPEN, _gate(db, current, mode))
    if current is None:
        return Promotion(now, None, (), None, NO_CHAMPION, _gate(db, None, mode))
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
    return Promotion(now, current, judgments, promoted, None, _gate(db, promoted or current, mode), dropped)


def _gate(db: sqlite3.Connection, version: int | None, mode: str) -> Gate:
    if version is None:
        return gate(None, None)
    return gate(version, records(db, mode, [version]).get(version))


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


def progress(db: sqlite3.Connection, mode: str = PAPER) -> dict[str, Any]:
    """``GET /v1/trader``'s policies (#400): the champion's version, the live gate on its paper
    record, and the champion and each challenger running with their paper records and, for a
    challenger, where it stands against the champion. Read-only; empty from a store without
    policies."""
    if not _has_table(db, "policies") or not _has_table(db, "champions"):
        return {"champion": None, "gate": gate(None, None).as_json(), "policies": []}
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
        "gate": gate(current, kept.get(current) if current is not None else None).as_json(),
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
    g = done.gate
    if g.policy is not None:
        lines.append(
            f"실전 관문: 챔피언 정책 {g.policy}의 모의 기록 {g.days}/{g.required}거래일, 날마다 순손익 평균 "
            f"{_won(g.mean)}, 하한 {_won(g.low)} → {'넘었다' if g.passed else '아직'}"
        )
    return "\n".join(lines)


def _has_table(db: sqlite3.Connection, name: str) -> bool:
    found = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone()
    return found is not None
