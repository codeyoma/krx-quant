"""The live mode (#408, #393 "실전 관문", "KIS 주문"): what lets the trader place a real order on the
live account, and how large it may be.

**Unlocked** (``readiness``). With ``KRX_TRADER_MODE=live`` the champion's book orders on the live
account (``mock.MockTrader`` of mode ``live``, through ``kis_orders.trader_live_broker``) only when the
live gate and every execution check pass. Until then the trader places no real order: it runs as
paper, with its challengers as ever (``run.py``), and says on Telegram what is missing at its start
and once a day, at the day's pick (08:30). The trader judges it at its start and at each pick: when
it changes, it stops itself at the pick once nothing of the champion's book is open, and Docker
starts it again in the other mode (``run.py``).

- **The live gate** (``promotion.gate_of``): the champion passed at a checkpoint of the trader's
  paper history (#408: α spent as 1/k², ``promotion.py``).
- **The execution checks**, read from the mock days (#405: the champion's KIS mock orders). They are
  judged on the mock period counted (``mock_period``): the latest mock trading days before today, as
  few as hold ``MOCK_DAYS`` (20) days and ``MOCK_TRIPS`` (30) completed round trips (all of them while
  there are fewer):

  - ``mock_days``, ``mock_trips``: those two at least;
  - ``order_checks``: each of its days with orders compared ``same`` against KIS's day order inquiry
    (``order_checks``, the KIS book's own comparison after each session). A day not compared (KIS
    could not be read) is no pass;
  - ``order_failed``: no day with a failed order (``failures``);
  - ``reconcile``: no reconcile that paused new entries (``reconciles`` ``held``, #406): it pauses only
    on what neither the ledger nor the order probe explains;
  - ``unresolved``: nothing of the mode left open from before today: an order, a buy sent whose
    answer was lost, a trade;
  - ``flat``: every trade of the period sold before its day's closing auction;
  - ``gap``: the adverse gap of each leg against the paper shadow's fill of the same prediction
    (``gap.pairs``), in ticks of the trade's tick: a buy's (fill − shadow), a sale's (shadow − fill),
    plus ``MOCK_CORRECTION`` (0.5) a leg for the mock server's fill half a tick better than the quote
    (#403). Its mean at most ``GAP_MEAN`` (1) and its 95th percentile (nearest rank) at most
    ``GAP_P95`` (2);
  - ``stressed_gate``: the champion passed a checkpoint with one extra adverse tick on each leg too
    (``promotion.Checkpoint.stressed_passed``).

**The first live period** (``ramp``, ``step``). The live book sizes on ``RAMP_CAP`` (250,000 won) in
place of the capital, its shares rounded down (an order the cap cannot pay one share of is not
placed), until the period holds ``RAMP_DAYS`` (20) live trading days and ``RAMP_TRIPS`` (30) completed
live round trips, all before today. The period begins at the first live start. Then, at a pick, the
live gap (the live fills against the paper shadow's, ``gap``'s legs without the correction) is
judged with the same failure, reconcile, open and flat checks over the period: all passing lifts the
cap to the capital for good (``LIFT``). A check that fails, at any pick of the period (the gap once
the period is long enough), is a breach (``BREACH``): new entries pause (a pause of source ``ramp``)
until the server reviews it and resumes (``krx-quant trade resume``), which begins a new period at
the cap from then. The loss limits stay the capital's (#396).

Nothing here sends an order; ``step`` writes the lift, the breach and its pause into the store, the
rest reads it.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

from krx_quant.feeds.base import KST, kst_day
from krx_quant.market_calendar import Session
from krx_quant.trader import LIVE, MOCK, promotion
from krx_quant.trader import policy as policies
from krx_quant.trader.gap import pairs
from krx_quant.trader.store import (
    BREACH,
    DIFFERENT,
    HELD,
    LIFT,
    PAUSE,
    RAMP,
    SAME,
    TraderStore,
    champion_of,
    iso,
)

# The execution checks (#408), on the mock days.
MOCK_DAYS = 20
MOCK_TRIPS = 30
# The mock server fills half a tick better than the quote (#403): a mock leg's gap is corrected by
# this much, so that the check judges what a live fill at the quote would have been.
MOCK_CORRECTION = 0.5
GAP_MEAN = 1.0  # ticks
GAP_P95 = 2.0
# The first live period (#408).
RAMP_CAP = 250_000
RAMP_DAYS = 20
RAMP_TRIPS = 30
# A day's closing auction when the calendar does not say (KRX's: 15:20).
AUCTION = time(15, 20)

Sessions = Callable[[date], Session | None]


@dataclass(frozen=True)
class Check:
    """One check of the live mode: its name, whether it passes, what it found against what it needs
    (``value``, ``required``), and that in Korean (``said``)."""

    name: str
    passed: bool
    value: float | None
    required: float | None
    said: str

    def as_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "value": self.value,
            "required": self.required,
            "said": self.said,
        }


def _has(db: sqlite3.Connection, table: str) -> bool:
    found = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
    return found is not None


def kis_days(db: sqlite3.Connection, mode: str) -> list[date]:
    """The trading days the trader ran in ``mode`` (its ledger, ``days``), oldest first."""
    if not _has(db, "days"):
        return []
    return [
        date.fromisoformat(r[0])
        for r in db.execute("SELECT day FROM days WHERE mode = ? ORDER BY day", (mode,))
    ]


def trips(db: sqlite3.Connection, mode: str, days: Iterable[date], since: datetime | None = None) -> int:
    """The champion's completed round trips of ``mode`` entered on ``days`` (at or after ``since``)."""
    wanted = sorted({d.isoformat() for d in days})
    if not wanted or not _has(db, "trades"):
        return 0
    marks = ",".join("?" * len(wanted))
    where, params = ("AND t.entry_at >= ?", [iso(since)]) if since is not None else ("", [])
    row = db.execute(
        f"SELECT count(*) FROM trades t WHERE t.mode = ? AND t.exit_at IS NOT NULL AND {champion_of(db)}"
        f" AND substr(t.entry_at, 1, 10) IN ({marks}) {where}",
        (mode, *wanted, *params),
    ).fetchone()
    return int(row[0])


def mock_period(db: sqlite3.Connection, today: date) -> list[date]:
    """The mock period counted (module doc): the latest mock trading days before ``today``, as few as
    hold ``MOCK_DAYS`` days and ``MOCK_TRIPS`` round trips; all of them while there are fewer."""
    days = [d for d in kis_days(db, MOCK) if d < today]
    out: list[date] = []
    for day in reversed(days):
        out.append(day)
        if len(out) >= MOCK_DAYS and trips(db, MOCK, out) >= MOCK_TRIPS:
            break
    return sorted(out)


def _ordered_days(db: sqlite3.Connection, mode: str) -> set[date]:
    """The days the champion's ``mode`` book placed an order KIS took (a trade's, or a buy that filled
    nothing), as the day's comparison counts them (``mock_check.ledger_orders``)."""
    found: set[date] = set()
    if _has(db, "orders"):
        rows = db.execute(
            "SELECT DISTINCT substr(o.placed_at, 1, 10) FROM orders o JOIN trades t ON t.id = o.trade_id"
            f" WHERE o.mode = ? AND o.broker_id IS NOT NULL AND {champion_of(db)}",
            (mode,),
        )
        found |= {date.fromisoformat(r[0]) for r in rows}
    if _has(db, "unfilled_buys"):
        rows = db.execute(
            "SELECT DISTINCT substr(placed_at, 1, 10) FROM unfilled_buys WHERE mode = ?"
            " AND status IN ('open', 'cancelled') AND broker_id != ''",
            (mode,),
        )
        found |= {date.fromisoformat(r[0]) for r in rows}
    return found


def _verdicts(db: sqlite3.Connection, mode: str) -> dict[date, str]:
    if not _has(db, "order_checks"):
        return {}
    rows = db.execute("SELECT day, verdict FROM order_checks WHERE mode = ?", (mode,))
    return {date.fromisoformat(r[0]): str(r[1]) for r in rows}


def _closing_auction(sessions: Sessions | None, day: date) -> datetime:
    session = sessions(day) if sessions is not None else None
    if session is not None:
        return session.closing_auction
    return datetime.combine(day, AUCTION, KST)


def clean_checks(
    db: sqlite3.Connection,
    mode: str,
    days: Sequence[date],
    today: date,
    sessions: Sessions | None,
    since: datetime | None = None,
) -> list[Check]:
    """The failure, reconcile, open and flat checks of ``mode`` over ``days`` (and from ``since``)
    (module doc), and the day comparisons'."""
    period = set(days)
    first = datetime.combine(min(days), time(), KST) if days else None
    start = max(t for t in (first, since) if t is not None) if first or since else None
    last = max(days) if days else None

    def inside(at: datetime) -> bool:
        return start is not None and last is not None and start <= at and kst_day(at) <= last

    ordered = _ordered_days(db, mode) & period
    verdicts = _verdicts(db, mode)
    compared = {d for d in ordered if verdicts.get(d) == SAME}
    differ = {d for d in period if verdicts.get(d) == DIFFERENT} | (ordered - compared)
    out = [
        Check(
            "order_checks",
            not differ,
            float(len(differ)),
            0.0,
            f"KIS 대조: 주문이 있던 {len(ordered)}일 가운데 장부와 KIS 주문 체결 조회가 같은 날 "
            f"{len(compared)}일" + (f", 다르거나 견주지 못한 날 {len(differ)}일" if differ else ""),
        )
    ]
    failed: set[date] = set()
    if _has(db, "failures"):
        for day, at in db.execute("SELECT day, at FROM failures WHERE mode = ?", (mode,)):
            if date.fromisoformat(day) in period and (since is None or datetime.fromisoformat(at) >= since):
                failed.add(date.fromisoformat(day))
    out.append(
        Check("order_failed", not failed, float(len(failed)), 0.0, f"주문 실패가 있던 날 {len(failed)}일")
    )
    held = 0
    if _has(db, "reconciles"):
        for (at,) in db.execute(
            "SELECT at FROM reconciles WHERE mode = ? AND status = ? AND control_id IS NOT NULL", (mode, HELD)
        ):
            held += inside(datetime.fromisoformat(at))
    out.append(Check("reconcile", held == 0, float(held), 0.0, f"계좌 맞추기가 새 진입을 멈춘 번 {held}번"))
    midnight = datetime.combine(today, time(), KST).isoformat()
    left = 0
    if _has(db, "orders"):
        left += db.execute(
            "SELECT count(*) FROM orders WHERE mode = ? AND status = 'open' AND placed_at < ?",
            (mode, midnight),
        ).fetchone()[0]
    if _has(db, "unfilled_buys"):
        left += db.execute(
            "SELECT count(*) FROM unfilled_buys WHERE mode = ? AND status IN ('sent', 'open')"
            " AND placed_at < ?",
            (mode, midnight),
        ).fetchone()[0]
    if _has(db, "trades"):
        left += db.execute(
            f"SELECT count(*) FROM trades t WHERE t.mode = ? AND t.exit_at IS NULL AND {champion_of(db)}"
            " AND t.entry_at < ?",
            (mode, midnight),
        ).fetchone()[0]
    out.append(Check("unresolved", left == 0, float(left), 0.0, f"끝나지 않은 주문·포지션 {left}개"))
    late = 0
    if days and _has(db, "trades"):
        marks = ",".join("?" * len(period))
        for entry_at, exit_at in db.execute(
            f"SELECT t.entry_at, t.exit_at FROM trades t WHERE t.mode = ? AND {champion_of(db)}"
            f" AND substr(t.entry_at, 1, 10) IN ({marks})",
            (mode, *sorted(d.isoformat() for d in period)),
        ):
            entered = datetime.fromisoformat(entry_at)
            if since is not None and entered < since:
                continue
            day = kst_day(entered)
            if exit_at is not None and datetime.fromisoformat(exit_at) >= _closing_auction(sessions, day):
                late += 1
    out.append(Check("flat", late == 0, float(late), 0.0, f"종가 단일가 전에 팔지 못한 매매 {late}건"))
    return out


def legs(
    db: sqlite3.Connection, mode: str, days: Iterable[date], correction: float, since: datetime | None = None
) -> list[float]:
    """Each leg's adverse gap in ticks (module doc): of the champion's ``mode`` trades entered on
    ``days`` (at or after ``since``) against their paper shadow's, plus ``correction`` a leg."""
    out: list[float] = []
    for kis, shadow in pairs(db, mode, days):
        if since is not None and datetime.fromisoformat(kis["entry_at"]) < since:
            continue
        tick = kis["tick"] or shadow["tick"]
        if not tick:
            continue
        out.append((kis["entry_price"] - shadow["entry_price"]) / tick + correction)
        if kis["exit_price"] is not None and shadow["exit_price"] is not None:
            out.append((shadow["exit_price"] - kis["exit_price"]) / tick + correction)
    return out


def p95(values: Sequence[float]) -> float | None:
    """The 95th percentile of ``values`` by the nearest rank: the ⌈0.95 n⌉-th smallest."""
    if not values:
        return None
    ranked = sorted(values)
    return ranked[max(0, math.ceil(0.95 * len(ranked)) - 1)]


def gap_check(name: str, values: Sequence[float], corrected: bool) -> Check:
    """The legs' gap: its mean at most ``GAP_MEAN`` and its 95th percentile at most ``GAP_P95``; no
    leg is no pass."""
    if not values:
        return Check(name, False, None, GAP_MEAN, "그림자 장부와 짝지은 매매가 없어 체결 차이를 재지 못했다")
    mean = sum(values) / len(values)
    top = p95(values)
    assert top is not None
    passed = mean <= GAP_MEAN and top <= GAP_P95
    note = f", 모의 서버의 반 틱 보정 {MOCK_CORRECTION:+g}틱 포함" if corrected else ""
    return Check(
        name,
        passed,
        mean,
        GAP_MEAN,
        f"다리 {len(values)}개의 불리한 체결 차이 평균 {mean:+.2f}틱(≤ {GAP_MEAN:g}), 95% {top:+.2f}틱"
        f"(≤ {GAP_P95:g}){note}",
    )


@dataclass(frozen=True)
class Readiness:
    """What the live mode needs (module doc): the gate on the champion, the execution checks, and the
    mock period they are judged on."""

    gate: promotion.Gate
    checks: tuple[Check, ...]
    period: tuple[date, ...] = ()

    @property
    def unlocked(self) -> bool:
        return self.gate.passed and all(c.passed for c in self.checks)

    @property
    def missing(self) -> list[str]:
        """The names of what does not pass: ``gate`` first, then the checks."""
        return (["gate"] if not self.gate.passed else []) + [c.name for c in self.checks if not c.passed]

    def gate_said(self) -> str:
        g = self.gate
        if g.policy is None:
            return "실전 관문: 챔피언이 아직 없다(첫 밤 정책 맞추기 전)"
        if g.passed:
            assert g.passed_at is not None
            return (
                f"실전 관문: 챔피언 정책 {g.policy}이 {g.passed_at.k}번째 판정({g.passed_at.day})에서 넘었다"
            )
        low = "없음" if g.low is None else f"{g.low:+,.0f}원"
        return (
            f"실전 관문: 챔피언 정책 {g.policy}의 모의 기록 {g.days}일, 다음 판정은 모의 이력 "
            f"{g.next_day}거래일째(지금 {g.history}일, α {promotion.alpha(g.next):.3%}), 지금 하한 {low}"
        )

    def said(self) -> list[str]:
        """What is missing, in Korean, one a line."""
        out = [] if self.gate.passed else [self.gate_said()]
        return out + [c.said for c in self.checks if not c.passed]

    def notice(self) -> str:
        """The Telegram notice of a live trader that places no real order yet (#408)."""
        return (
            "자동 매매(live): 실전 모드(KRX_TRADER_MODE=live)지만 아직 실제 주문을 내지 않고 모의(paper)로 "
            "돕니다. 모자란 것: "
            + "; ".join(self.said())
            + ". 모두 채우면 그날 08:30 고르기에서 스스로 다시 시작해 실전 계좌에 주문합니다."
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "unlocked": self.unlocked,
            "missing": self.missing,
            "gate": self.gate_said(),
            "checks": [c.as_json() for c in self.checks],
            "period": {
                "since": self.period[0].isoformat() if self.period else None,
                "until": self.period[-1].isoformat() if self.period else None,
                "days": len(self.period),
            },
        }


def champion(db: sqlite3.Connection) -> int | None:
    if not _has(db, "champions") or not _has(db, "policies"):
        return None
    return policies.champion_version(db)


def readiness(db: sqlite3.Connection, sessions: Sessions | None, today: date) -> Readiness:
    """The live gate on the champion and the execution checks on the mock period before ``today``
    (module doc). Read-only."""
    g = promotion.gate_of(db, champion(db))
    period = mock_period(db, today)
    made = trips(db, MOCK, period)
    checks = [
        Check(
            "mock_days",
            len(period) >= MOCK_DAYS,
            float(len(period)),
            float(MOCK_DAYS),
            f"KIS 모의 거래일 {len(period)}/{MOCK_DAYS}일",
        ),
        Check(
            "mock_trips",
            made >= MOCK_TRIPS,
            float(made),
            float(MOCK_TRIPS),
            f"KIS 모의 왕복 매매 {made}/{MOCK_TRIPS}건",
        ),
        *clean_checks(db, MOCK, period, today, sessions),
        gap_check("gap", legs(db, MOCK, period, MOCK_CORRECTION), corrected=True),
        Check(
            "stressed_gate",
            g.stressed is not None,
            None,
            None,
            "다리마다 1틱 더 불리하게 쳐도 관문을 넘는가: "
            + ("넘었다" if g.stressed is not None else "아직(챔피언이 그렇게 넘은 판정이 없다)"),
        ),
    ]
    return Readiness(g, tuple(checks), tuple(period))


@dataclass(frozen=True)
class Ramp:
    """The live book's cap now (module doc): the capital once lifted (``lifted_at``), else
    ``RAMP_CAP``; the period since ``since`` (its days before today and round trips), its checks, and
    a breach whose pause holds (``breached_at``)."""

    cap: int
    lifted_at: datetime | None = None
    since: datetime | None = None
    days: tuple[date, ...] = ()
    trips: int = 0
    checks: tuple[Check, ...] = field(default_factory=tuple)
    breached_at: datetime | None = None

    @property
    def long_enough(self) -> bool:
        return len(self.days) >= RAMP_DAYS and self.trips >= RAMP_TRIPS

    @property
    def outcome(self) -> str | None:
        """What a step makes of it: ``LIFT``, ``BREACH``, or None (the period goes on, or is over)."""
        if self.lifted_at is not None or self.breached_at is not None or self.since is None:
            return None
        clean = [c for c in self.checks if c.name not in ("live_days", "live_trips", "live_gap")]
        if not all(c.passed for c in clean):
            return BREACH
        if not self.long_enough:
            return None
        gap = next(c for c in self.checks if c.name == "live_gap")
        return LIFT if gap.passed else BREACH

    def failing(self) -> list[str]:
        return [c.said for c in self.checks if not c.passed and c.name not in ("live_days", "live_trips")]

    def as_json(self) -> dict[str, Any]:
        return {
            "cap": self.cap,
            "lifted_at": None if self.lifted_at is None else iso(self.lifted_at),
            "since": None if self.since is None else iso(self.since),
            "days": len(self.days),
            "required_days": RAMP_DAYS,
            "trips": self.trips,
            "required_trips": RAMP_TRIPS,
            "checks": [c.as_json() for c in self.checks],
            "breached_at": None if self.breached_at is None else iso(self.breached_at),
        }


def ramp(db: sqlite3.Connection, sessions: Sessions | None, now: datetime, capital: int) -> Ramp:
    """The live book's first period at ``now`` (module doc). Read-only."""
    lifted: datetime | None = None
    breached: datetime | None = None
    resumed: list[datetime] = []
    if _has(db, "ramp"):
        for at, event, resumed_at in db.execute(
            "SELECT at, event, resumed_at FROM ramp WHERE mode = ? ORDER BY id", (LIVE,)
        ):
            if event == LIFT and lifted is None:
                lifted = datetime.fromisoformat(at)
            elif event == BREACH:
                if resumed_at is None:
                    breached = datetime.fromisoformat(at)
                else:
                    resumed.append(datetime.fromisoformat(resumed_at))
    if lifted is not None:
        return Ramp(capital, lifted_at=lifted)
    cap = min(RAMP_CAP, capital)
    first = None
    if _has(db, "runs"):
        row = db.execute("SELECT min(at) FROM runs WHERE mode = ?", (LIVE,)).fetchone()
        first = None if row[0] is None else datetime.fromisoformat(row[0])
    since = max([t for t in (first, *resumed) if t is not None], default=None)
    if since is None:
        return Ramp(cap)
    today = kst_day(now)
    days = tuple(d for d in kis_days(db, LIVE) if kst_day(since) <= d < today)
    made = trips(db, LIVE, days, since)
    checks = (
        Check(
            "live_days",
            len(days) >= RAMP_DAYS,
            float(len(days)),
            float(RAMP_DAYS),
            f"실전 거래일 {len(days)}/{RAMP_DAYS}일",
        ),
        Check(
            "live_trips",
            made >= RAMP_TRIPS,
            float(made),
            float(RAMP_TRIPS),
            f"실전 왕복 매매 {made}/{RAMP_TRIPS}건",
        ),
        *clean_checks(db, LIVE, days, today, sessions, since),
        gap_check("live_gap", legs(db, LIVE, days, 0.0, since), corrected=False),
    )
    return Ramp(cap, None, since, days, made, checks, breached)


def step(
    store: TraderStore,
    sessions: Sessions | None,
    now: datetime,
    capital: int,
    notify: Callable[[str], None],
) -> Ramp:
    """The first period judged at a pick (module doc): a lift or a breach written (a breach's pause of
    new entries with it) and told; the ramp as it then stands."""
    found = ramp(store.db, sessions, now, capital)
    outcome = found.outcome
    if outcome == LIFT:
        store.add_ramp(now, LIVE, LIFT, found.as_json())
        notify(
            f"자동 매매(live): 실전 첫 단계({len(found.days)}거래일, 왕복 매매 {found.trips}건)를 점검대로 "
            f"마쳤습니다. 운용 상한을 {RAMP_CAP:,}원에서 {capital:,}원으로 올립니다."
        )
        return Ramp(capital, lifted_at=now)
    if outcome == BREACH:
        control = store.add_control(now, PAUSE, RAMP, sell_now=False)
        store.add_ramp(now, LIVE, BREACH, found.as_json(), control.id)
        notify(
            "자동 매매(live): 실전 첫 단계의 점검을 벗어나 새 진입을 멈췄습니다: "
            + "; ".join(found.failing())
            + ". 들고 있는 것은 원래 규칙대로 팝니다. 서버에서 살펴본 뒤 krx-quant trade resume으로 "
            f"다시 켜면 운용 상한 {found.cap:,}원의 첫 단계를 그때부터 다시 셉니다."
        )
        return Ramp(found.cap, None, found.since, found.days, found.trips, found.checks, now)
    return found


def live_state(readiness_: Readiness, configured: str, ordering: bool, ramp_: Ramp | None) -> dict[str, Any]:
    """``GET /v1/trader``'s ``live`` (#408): whether ``KRX_TRADER_MODE`` is live, whether the champion's
    book orders on the live account now, what is missing, the checks, and the first period's ramp."""
    return {
        "configured": configured == LIVE,
        "ordering": ordering,
        **readiness_.as_json(),
        "ramp": None if ramp_ is None else ramp_.as_json(),
    }
