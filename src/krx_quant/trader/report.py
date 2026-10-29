"""The trader's nightly report (#402, #393 "리포트와 에이전트"): what the trader did on a trading day
and what that night changed, for the report issue, where an agent reads it
(``docs/agents/trader-report.md``).

``krx-quant trader-report [--day]`` prints it, reading ``state/trader.sqlite3`` read-only. The
scheduled job ``trader-report`` runs ``krx-quant refit-policy --report`` every trading day's night
(``run_night``): the night's judgment of the challengers and re-fit first (#399, #400,
``refit.run_refit``), then this report of that day, printed above the re-fit's own output, one
comment a night.

**Which day, which night.** A trading day's night runs from its pick (``PICK_AT``, 08:30) to the
next trading day's pick: a champion crowned, a challenger dropped or admitted, a policy fitted in it
takes effect from the next trading day's pick (``promotion.in_market``, ``policy.running_versions``).
The day reported is the one whose night it is (``report_day``): at the scheduled jobs' night (22:30
to 05:30) the evening's trading day, as the scheduler counts it. The day's champion and challengers
are those in force at its pick; the records are read to the day.

**What it holds** (``render``): the day's ledger and the champion's trades (times, cell,
probability and θ, p* and the hit rate's lower bound, shares, how each ended, the ticks between its
fills, the costs and the net P&L in won), what the champion's book did with the predictions it read
and, when no cell traded, why (the day's pick from the cost map: every cell considered and its
verdict); each policy's paper record to the day and, for a challenger, where it stands against the
champion (``promotion.judge``, as the night's judgment counts it); what the night changed (the
champion, the challengers dropped and admitted, the policies fitted and their cells, the policies
waiting, and whether the re-fit kept none or failed); the live gate (``promotion.gate``); the limits,
the halts reached that day, a cumulative halt still holding, the pause in force and the pauses,
resumes and re-arms asked for; the last trading days' ledger.

**What it never holds**: a price (no fill, reference, target, stop or quote), a broker's order
number, an account, a key. The scheduled job's output is masked besides, as every job's
(``redaction.py``).

**When it calls an agent** (``reasons``; the job labels the issue ``ready-for-agent`` only then,
``calls_agent``): on the week's last trading day (a Friday, or the day before a Friday the market is
closed), when a loss limit was reached that day, when the champion changed that night, when the live
gate passed that night (not passed the evening before under the same champion), or when the night's
re-fit failed. Any other night is a record and changes no label.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from krx_quant.config import Settings
from krx_quant.feeds.base import KST
from krx_quant.market_calendar import MarketCalendar, calendar_store
from krx_quant.trader import PAPER, PICK_AT, promotion
from krx_quant.trader import policy as policies
from krx_quant.trader.limits import Pause, pause_of
from krx_quant.trader.refit import NO_POLICY_KEPT, run_refit
from krx_quant.trader.settings import TraderSettings
from krx_quant.trader.store import CUMULATIVE_LIMIT, DAILY_LIMIT, DB_NAME, Control, champion_of

log = logging.getLogger(__name__)

# The line that says whether the report calls an agent (``calls_agent``), and what it says when not.
AGENT = "에이전트 차례: "
NO_CALL = "아님"
# The exit code when the report cannot be made (the store cannot be read).
REPORT_FAILED = 2
# The last trading days of the ledger shown.
RECENT_DAYS = 5
# The heading of the re-fit's own output, under the report (``run_night``).
REFIT_HEADING = "밤 정책 맞추기의 출력 (krx-quant refit-policy, #399 #400)"

WEEKDAYS = "월화수목금토일"
MODES = {PAPER: "모의(paper)", "mock": "KIS 모의(mock)", "live": "실전(live)"}
STATUSES = {
    "running": "도는 중(running)",
    "entries_paused": "새 진입 정지(entries_paused)",
    "paused": "정지하고 즉시 매도(paused)",
    DAILY_LIMIT: "하루 손실 한도로 멈춤(daily_limit)",
    CUMULATIVE_LIMIT: "누적 손실 한도로 멈춤(cumulative_limit)",
}
EXITS = {
    "take_profit": "익절(take_profit)",
    "stop_loss": "손절(stop_loss)",
    "time": "시간(time)",
    "pause": "정지 매도(pause)",
    DAILY_LIMIT: "하루 한도 매도(daily_limit)",
    CUMULATIVE_LIMIT: "누적 한도 매도(cumulative_limit)",
}
LIMITS = {DAILY_LIMIT: "하루 손실 한도", CUMULATIVE_LIMIT: "누적 손실 한도"}
NO_CELL = {
    "no_pass": "비용 지도에서 시장가 왕복으로 통과한 칸이 없다",
    "no_cost_map": "비용 지도를 읽지 못했다",
    "not_in_policy": "통과한 칸 가운데 챔피언 정책의 칸이 없다",
    "no_policy": "챔피언 정책을 읽지 못했다",
}
POLICY_STATUSES = {
    policies.CHAMPION: "챔피언",
    policies.CHALLENGER: "도전 정책: 다음 거래일부터 모의로 돈다",
    policies.WAITING: "기다리는 정책: 도전 정책 자리 셋이 차 있다",
    policies.DROPPED: "내린 정책",
    policies.FORMER: "전 챔피언",
}
VERDICTS = {
    None: "통과",
    promotion.FEW_DAYS: f"날 수 부족({promotion.PROMOTION_DAYS}일부터 판정)",
    promotion.NOT_ABOVE: "하한 ≤ 0",
}


@dataclass(frozen=True)
class Trade:
    """A trade of the champion's book, as the report shows it: no price."""

    entry_at: datetime
    exit_at: datetime | None
    kind: str
    predictor: str
    role: str | None
    probability: float
    threshold: float
    p_star: float | None
    low: float | None
    quantity: int
    ticks: float | None  # between the buy's fill and the sale's, in the trade's ticks
    reason: str | None  # how it ended; None while open
    cost: float | None
    net: float | None


@dataclass(frozen=True)
class Standing:
    """A policy of the day: the champion or a challenger, its record's day of the report (None if it
    did not run that day), its paper record to the day and, a challenger's, its judgment."""

    version: int
    champion: bool
    today: promotion.PolicyDay | None
    paper: Mapping[str, Any]
    judgment: promotion.Judgment | None = None


@dataclass(frozen=True)
class Crowned:
    """A change of the champion in the night: the policy before (None for the first) and why."""

    at: datetime
    version: int
    replaced: int | None
    reason: str


@dataclass(frozen=True)
class Fitted:
    """A policy fitted in the night, its status at the night's end; ``error`` if it cannot be read."""

    version: int
    status: str
    cells: tuple[policies.PolicyCell, ...] = ()
    left_out: tuple[Mapping[str, Any], ...] = ()
    result: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True)
class Report:
    """A trading day and its night, as read from the trader's store."""

    day: date
    mode: str
    last_of_week: bool  # the week's last trading day
    gate: promotion.Gate  # on the champion at the night's end, its record to the day
    gate_before: promotion.Gate  # on the day's champion, its record to the day before
    found: bool = True  # the store is there
    capital: float | None = None
    champion: int | None = None  # in force on the day
    ledger: Mapping[str, Any] | None = None  # the day's ``days`` row
    recent: tuple[Mapping[str, Any], ...] = ()  # the last ``RECENT_DAYS`` rows, the day's last
    trades: tuple[Trade, ...] = ()
    # What the champion's book did with the predictions it read: (``bought``, None), (``unfilled``,
    # why), (``skip``, why) → how many.
    decisions: Mapping[tuple[str, str | None], int] = field(default_factory=dict)
    pick: Mapping[str, Any] | None = None  # the champion's last pick of the day
    picks: int = 0  # its picks that day
    standings: tuple[Standing, ...] = ()
    crowned: tuple[Crowned, ...] = ()
    dropped: tuple[int, ...] = ()
    admitted: tuple[int, ...] = ()
    fitted: tuple[Fitted, ...] = ()
    waiting: int = 0
    halts: tuple[Mapping[str, Any], ...] = ()  # reached that day
    held: Mapping[str, Any] | None = None  # a cumulative halt reached before the day, not re-armed
    pause: Pause | None = None  # in force at the night's end
    controls: tuple[Control, ...] = ()  # asked from the day's start to the night's end
    refit: int | None = None  # the night's re-fit's exit code (``run_night``); None: not run with it

    @property
    def gate_passed(self) -> bool:
        """The live gate passed in the night: passed at its end, not the evening before under the
        same champion."""
        before = self.gate_before.passed and self.gate_before.policy == self.gate.policy
        return self.gate.passed and not before


def report_day(calendar: MarketCalendar, now: datetime) -> date:
    """The day a report at ``now`` is of: the trading day whose night ``now`` is in (``night_of``),
    the latest whose pick (08:30) is at or before ``now``."""
    day = now.astimezone(KST).date()
    if now < datetime.combine(day, PICK_AT, KST):
        day -= timedelta(days=1)
    while not calendar.is_trading_day(day):
        day -= timedelta(days=1)
    return day


def night_of(calendar: MarketCalendar, day: date) -> tuple[datetime, datetime]:
    """The night of the trading day ``day``: from its pick to the next trading day's."""
    later = day + timedelta(days=1)
    while not calendar.is_trading_day(later):
        later += timedelta(days=1)
    return datetime.combine(day, PICK_AT, KST), datetime.combine(later, PICK_AT, KST)


def last_of_week(calendar: MarketCalendar, day: date) -> bool:
    """Whether no trading day follows ``day`` in its week (to the Sunday)."""
    later = day + timedelta(days=1)
    while later.weekday() != 0:
        if calendar.is_trading_day(later):
            return False
        later += timedelta(days=1)
    return True


def read(
    path: Path,
    day: date,
    calendar: MarketCalendar,
    *,
    mode: str = PAPER,
    refit: int | None = None,
) -> Report:
    """The report of the trading day ``day`` from the trader's store at ``path``, read-only (module
    doc). ``sqlite3.Error`` when it cannot be read."""
    week = last_of_week(calendar, day)
    if not path.exists():
        none = promotion.gate(None, None)
        return Report(day, mode, week, none, none, found=False, refit=refit)
    start, end = night_of(calendar, day)
    session = calendar.session(day)
    with _reading(path) as db:
        tables = _tables(db)
        has_policies = {"policies", "champions"} <= tables
        champion = _champion_at(db, start) if has_policies else None
        at_end = _champion_at(db, end) if has_policies else None
        challengers = _slots_at(db, start) if has_policies else []
        records = promotion.records(db, PAPER, {v for v in (champion, at_end, *challengers) if v is not None})
        to_day = {v: _upto(r, day) for v, r in records.items()}
        before = {v: _upto(r, day - timedelta(days=1)) for v, r in records.items()}
        standings = _standings(day, champion, challengers, to_day)
        crowned: tuple[Crowned, ...] = ()
        dropped: tuple[int, ...] = ()
        admitted: tuple[int, ...] = ()
        fitted: tuple[Fitted, ...] = ()
        waiting = 0
        if has_policies:
            crowned = _crowned(db, start, end)
            dropped, admitted = (
                _changed(db, "dropped_at", start, end),
                _changed(db, "admitted_at", start, end),
            )
            fitted = _fitted(db, start, end)
            waiting = _waiting(db, end)
        pick, picks = _pick(db, mode, day, champion, None if session is None else session.close)
        controls = _controls(db, datetime.combine(day, time(), KST), end) if "controls" in tables else ()
        everything = _controls(db, None, end) if "controls" in tables else ()
        return Report(
            day=day,
            mode=mode,
            last_of_week=week,
            gate=promotion.gate(at_end, to_day.get(at_end) if at_end is not None else None),
            gate_before=promotion.gate(champion, before.get(champion) if champion is not None else None),
            capital=_capital(db, mode, end) if "runs" in tables else None,
            champion=champion,
            ledger=_ledger(db, mode, day) if "days" in tables else None,
            recent=_recent(db, mode, day) if "days" in tables else (),
            trades=_trades(db, mode, day) if "trades" in tables else (),
            decisions=_decisions(db, mode, day, champion) if "decisions" in tables else {},
            pick=pick,
            picks=picks,
            standings=standings,
            crowned=crowned,
            dropped=dropped,
            admitted=admitted,
            fitted=fitted,
            waiting=waiting,
            halts=_halts(db, mode, day) if "halts" in tables else (),
            held=_held(db, mode, day) if "halts" in tables else None,
            pause=pause_of(everything),
            controls=controls,
            refit=refit,
        )


def reasons(report: Report) -> list[str]:
    """Why the report calls an agent (module doc); none on a night that is a record only."""
    out = []
    if report.last_of_week:
        out.append(f"이번 주 마지막 거래일({WEEKDAYS[report.day.weekday()]}요일)")
    for halt in report.halts:
        out.append(f"{LIMITS.get(halt['kind'], halt['kind'])}에 닿음({_hms(halt['at'])})")
    for change in report.crowned:
        if change.replaced is None:
            out.append(f"첫 챔피언(정책 {change.version})")
        else:
            out.append(f"챔피언 교체(정책 {change.replaced} → 정책 {change.version})")
    if report.gate_passed:
        out.append(f"실전 관문을 넘음(챔피언 정책 {report.gate.policy})")
    if report.refit:
        out.append(f"밤 정책 맞추기 실패(종료 코드 {report.refit})")
    return out


def calls_agent(output: str) -> bool:
    """Whether a report's output calls an agent: its ``AGENT`` line names a reason."""
    return any(
        line.startswith(AGENT) and not line.startswith(AGENT + NO_CALL) for line in output.splitlines()
    )


def render(report: Report) -> str:
    """The report's text (module doc): won, counts, ticks, probabilities and times; no price."""
    day = report.day
    lines = [
        f"자동 매매 리포트 (#402) · {day}({WEEKDAYS[day.weekday()]}) · {MODES.get(report.mode, report.mode)}"
    ]
    called = reasons(report)
    lines.append(AGENT + ("; ".join(called) if called else f"{NO_CALL}(밤의 기록만: 라벨을 바꾸지 않는다)"))
    if not report.found:
        lines += ["", "trader의 저장소(state/trader.sqlite3)가 없다: trader가 아직 돌지 않았다."]
        return "\n".join(lines)
    sections = [
        _day_section(report),
        _pick_section(report),
        _standings_section(report),
        _night_section(report),
        _gate_line(report),
        _limits_section(report),
        _recent_section(report),
    ]
    return "\n\n".join(["\n".join(lines), *sections])


def run_report(
    settings: Settings, now: datetime, day: date | None = None, refit: int | None = None, mode: str = PAPER
) -> int:
    """``krx-quant trader-report``: the report of ``day`` (else ``report_day``) printed; 0, or
    ``REPORT_FAILED`` (with a line saying why) when the store cannot be read."""
    calendar = calendar_store(settings).calendar
    day = day or report_day(calendar, now)
    try:
        made = read(settings.state_dir / DB_NAME, day, calendar, mode=mode, refit=refit)
    except (sqlite3.Error, ValueError, KeyError, TypeError) as e:
        log.error("trader report: cannot read the trader's store (%s)", e)
        print(f"자동 매매 리포트 (#402) · {day}: 저장소를 읽지 못해 만들지 못했다: {type(e).__name__}: {e}")
        print(AGENT + "리포트를 만들지 못함")
        return REPORT_FAILED
    print(render(made))
    return 0


async def run_night(
    settings: Settings, trader: TraderSettings, now: datetime, engine_url: str | None = None
) -> int:
    """``krx-quant refit-policy --report``, the scheduled job ``trader-report``'s: the night's re-fit
    (``refit.run_refit``), then the day's report printed above the re-fit's output. The re-fit's exit
    code (one that raised counts 1), else the report's."""
    printed = io.StringIO()
    try:
        with contextlib.redirect_stdout(printed):
            code = await run_refit(settings, trader, now, engine_url)
    except Exception:
        log.exception("trader report: the night's re-fit failed")
        code = NO_POLICY_KEPT
    reported = run_report(settings, now, refit=code)
    print(f"\n{REFIT_HEADING}\n")
    print(printed.getvalue().rstrip("\n") or "(출력 없음)")
    return code or reported


# Reading.


@contextlib.contextmanager
def _reading(path: Path) -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


def _tables(db: sqlite3.Connection) -> set[str]:
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}


def _iso(at: datetime) -> str:
    return at.astimezone(KST).isoformat()


def _champion_at(db: sqlite3.Connection, moment: datetime) -> int | None:
    """The champion before ``moment``: the latest change of it then."""
    row = db.execute(
        "SELECT policy_id FROM champions WHERE at < ? ORDER BY id DESC LIMIT 1", (_iso(moment),)
    ).fetchone()
    return None if row is None else int(row[0])


def _slots_at(db: sqlite3.Connection, moment: datetime) -> list[int]:
    """The challengers in a running slot just before ``moment``, oldest first: admitted before it,
    not dropped nor the champion by then: those the night's judgment judges
    (``policy.challenger_versions``)."""
    columns = _columns(db, "policies")
    if "admitted_at" not in columns:
        return []
    dropped = " AND (p.dropped_at IS NULL OR p.dropped_at >= :at)" if "dropped_at" in columns else ""
    rows = db.execute(
        "SELECT p.id FROM policies p WHERE p.admitted_at IS NOT NULL AND p.admitted_at < :at"
        f"{dropped} AND p.id NOT IN (SELECT policy_id FROM champions WHERE at < :at) ORDER BY p.id",
        {"at": _iso(moment)},
    )
    return [int(r[0]) for r in rows]


def _upto(record: promotion.Record, day: date) -> promotion.Record:
    return promotion.Record(record.version, tuple(d for d in record.days if d.day <= day))


def _standings(
    day: date, champion: int | None, challengers: Sequence[int], records: Mapping[int, promotion.Record]
) -> tuple[Standing, ...]:
    judged = (
        {j.challenger: j for j in promotion.judge(champion, records, challengers)}
        if champion is not None
        else {}
    )
    out = []
    for version in [*([] if champion is None else [champion]), *challengers]:
        record = records.get(version)
        today = None if record is None else next((d for d in record.days if d.day == day), None)
        out.append(
            Standing(version, version == champion, today, promotion.paper(record), judged.get(version))
        )
    return tuple(out)


def _crowned(db: sqlite3.Connection, start: datetime, end: datetime) -> tuple[Crowned, ...]:
    out = []
    before: int | None = None
    for row in db.execute("SELECT at, policy_id, reason FROM champions ORDER BY id"):
        at = datetime.fromisoformat(row["at"])
        if start <= at < end:
            out.append(Crowned(at, int(row["policy_id"]), before, row["reason"]))
        before = int(row["policy_id"])
    return tuple(out)


def _changed(db: sqlite3.Connection, column: str, start: datetime, end: datetime) -> tuple[int, ...]:
    """The policies whose ``column`` (``admitted_at``, ``dropped_at``) is in the night."""
    if column not in _columns(db, "policies"):
        return ()
    rows = db.execute(
        f"SELECT id FROM policies WHERE {column} >= ? AND {column} < ? ORDER BY id", (_iso(start), _iso(end))
    )
    return tuple(int(r[0]) for r in rows)


def _fitted(db: sqlite3.Connection, start: datetime, end: datetime) -> tuple[Fitted, ...]:
    """The policies kept in the night, each with its status at the night's end."""
    columns = _columns(db, "policies")
    champion = _champion_at(db, end)
    once = {int(r[0]) for r in db.execute("SELECT policy_id FROM champions WHERE at < ?", (_iso(end),))}
    out = []
    for row in db.execute(
        "SELECT * FROM policies WHERE created_at >= ? AND created_at < ? ORDER BY id",
        (_iso(start), _iso(end)),
    ):
        version = int(row["id"])
        dropped = "dropped_at" in columns and row["dropped_at"] is not None and row["dropped_at"] < _iso(end)
        admitted = (
            "admitted_at" in columns and row["admitted_at"] is not None and row["admitted_at"] < _iso(end)
        )
        status = policies.status_of(version, champion, once, dropped, admitted)
        try:
            kept = policies.read(db, version)
        except policies.PolicyError as e:
            out.append(Fitted(version, status, error=str(e)))
            continue
        out.append(Fitted(version, status, kept.cells, kept.left_out, dict(kept.fit.get("result") or {})))
    return tuple(out)


def _waiting(db: sqlite3.Connection, end: datetime) -> int:
    """The policies waiting for a slot at the night's end."""
    columns = _columns(db, "policies")
    if not {"admitted_at", "dropped_at"} <= columns:
        return 0
    row = db.execute(
        "SELECT count(*) FROM policies p WHERE p.created_at < :at"
        " AND (p.admitted_at IS NULL OR p.admitted_at >= :at)"
        " AND (p.dropped_at IS NULL OR p.dropped_at >= :at)"
        " AND p.id NOT IN (SELECT policy_id FROM champions WHERE at < :at)",
        {"at": _iso(end)},
    ).fetchone()
    return int(row[0])


def _capital(db: sqlite3.Connection, mode: str, end: datetime) -> float | None:
    """The capital of the trader's last start before the night's end (``runs``)."""
    row = db.execute(
        "SELECT detail FROM runs WHERE mode = ? AND at < ? ORDER BY id DESC LIMIT 1", (mode, _iso(end))
    ).fetchone()
    if row is None:
        return None
    capital = json.loads(row[0]).get("capital")
    return float(capital) if isinstance(capital, int | float) and not isinstance(capital, bool) else None


def _ledger(db: sqlite3.Connection, mode: str, day: date) -> dict[str, Any] | None:
    row = db.execute("SELECT * FROM days WHERE mode = ? AND day = ?", (mode, day.isoformat())).fetchone()
    return None if row is None else dict(row)


def _recent(db: sqlite3.Connection, mode: str, day: date) -> tuple[dict[str, Any], ...]:
    rows = db.execute(
        "SELECT * FROM days WHERE mode = ? AND day <= ? ORDER BY day DESC LIMIT ?",
        (mode, day.isoformat(), RECENT_DAYS),
    ).fetchall()
    return tuple(dict(r) for r in reversed(rows))


def _trades(db: sqlite3.Connection, mode: str, day: date) -> tuple[Trade, ...]:
    """The champion's trades entered on ``day`` (``store.CHAMPION_TRADE``), in their order."""
    rows = db.execute(
        f"SELECT t.* FROM trades t WHERE t.mode = ? AND substr(t.entry_at, 1, 10) = ? AND {champion_of(db)}"
        " ORDER BY t.entry_at, t.id",
        (mode, day.isoformat()),
    )
    out = []
    for row in rows:
        r = dict(row)
        ticks = None
        if r["exit_price"] is not None and r["tick"]:
            ticks = (r["exit_price"] - r["entry_price"]) / r["tick"]
        out.append(
            Trade(
                entry_at=datetime.fromisoformat(r["entry_at"]),
                exit_at=None if r["exit_at"] is None else datetime.fromisoformat(r["exit_at"]),
                kind=r["kind"],
                predictor=r["predictor"],
                role=r.get("role"),
                probability=r["probability"],
                threshold=r["threshold"],
                p_star=r.get("p_star"),
                low=r.get("precision_low"),
                quantity=r["quantity"],
                ticks=ticks,
                reason=r["exit_reason"],
                cost=r["cost"],
                net=r["net"],
            )
        )
    return tuple(out)


def _decisions(
    db: sqlite3.Connection, mode: str, day: date, champion: int | None
) -> dict[tuple[str, str | None], int]:
    """What the champion's book did with the predictions it read on ``day``: its decisions are
    under the champion's policy, or none (before the first, or a champion that could not be read)."""
    whose = ""
    params: list[Any] = [mode, day.isoformat()]
    if "policy_id" in _columns(db, "decisions"):
        whose = " AND (policy_id IS NULL OR policy_id = ?)"
        params.append(-1 if champion is None else champion)
    out: dict[tuple[str, str | None], int] = {}
    for action, reason, filled, count in db.execute(
        "SELECT action, reason, trade_id IS NOT NULL, count(*) FROM decisions"
        f" WHERE mode = ? AND substr(decided_at, 1, 10) = ?{whose} GROUP BY 1, 2, 3",
        params,
    ):
        if action == "buy":
            key: tuple[str, str | None] = ("bought", None) if filled else ("unfilled", reason or "waiting")
        else:
            key = ("skip", reason)
        out[key] = out.get(key, 0) + int(count)
    return out


def _pick(
    db: sqlite3.Connection, mode: str, day: date, champion: int | None, close: datetime | None
) -> tuple[dict[str, Any] | None, int]:
    """The champion's last pick of ``day`` by its close (the one the session traded on), and how
    many it made that day."""
    if "selections" not in _tables(db):
        return None, 0
    whose = ""
    params: list[Any] = [mode, day.isoformat()]
    if "policy_id" in _columns(db, "selections"):
        whose = " AND (policy_id IS NULL OR policy_id = ?)"
        params.append(-1 if champion is None else champion)
    rows = [
        dict(r)
        for r in db.execute(
            f"SELECT * FROM selections WHERE mode = ? AND substr(at, 1, 10) = ?{whose} ORDER BY id", params
        )
    ]
    if not rows:
        return None, 0
    traded = [r for r in rows if close is None or datetime.fromisoformat(r["at"]) <= close] or rows
    last = traded[-1]
    last["picks"] = json.loads(last["picks"])
    last["considered"] = json.loads(last["considered"])
    return last, len(rows)


def _halts(db: sqlite3.Connection, mode: str, day: date) -> tuple[dict[str, Any], ...]:
    rows = db.execute("SELECT * FROM halts WHERE mode = ? AND day = ? ORDER BY id", (mode, day.isoformat()))
    return tuple(dict(r) for r in rows)


def _held(db: sqlite3.Connection, mode: str, day: date) -> dict[str, Any] | None:
    row = db.execute(
        "SELECT * FROM halts WHERE mode = ? AND kind = ? AND lifted_at IS NULL AND day < ?"
        " ORDER BY id LIMIT 1",
        (mode, CUMULATIVE_LIMIT, day.isoformat()),
    ).fetchone()
    return None if row is None else dict(row)


def _controls(db: sqlite3.Connection, start: datetime | None, end: datetime) -> tuple[Control, ...]:
    """The pauses, resumes and re-arms asked for from ``start`` (all if None) to ``end``, in order."""
    where, params = "at < ?", [_iso(end)]
    if start is not None:
        where, params = "at >= ? AND " + where, [_iso(start), *params]
    rows = db.execute(f"SELECT * FROM controls WHERE {where} ORDER BY id", params)
    return tuple(
        Control(
            r["id"],
            datetime.fromisoformat(r["at"]),
            r["action"],
            r["source"],
            None if r["sell_now"] is None else bool(r["sell_now"]),
            r["mode"],
            None if r["applied_at"] is None else datetime.fromisoformat(r["applied_at"]),
        )
        for r in rows
    )


# Writing.


def _won(value: float | None) -> str:
    return "없음" if value is None else f"{value:+,.0f}원"


def _amount(value: float | None) -> str:
    return "?" if value is None else f"{value:,.0f}원"


def _ratio(value: float | None) -> str:
    return "?" if value is None else f"{value:.3f}"


def _hms(at: str | datetime) -> str:
    moment = datetime.fromisoformat(at) if isinstance(at, str) else at
    return moment.astimezone(KST).strftime("%H:%M:%S")


def _when(at: str | datetime, day: date) -> str:
    """A time of ``day`` as ``HH:MM:SS``; of another day with its date."""
    moment = (datetime.fromisoformat(at) if isinstance(at, str) else at).astimezone(KST)
    return moment.strftime("%H:%M:%S") if moment.date() == day else moment.strftime("%Y-%m-%d %H:%M:%S")


def _used(worst: float, limit: float) -> str:
    return f"{max(0.0, -worst) / limit:.0%}" if limit > 0 else "?"


def _cell(kind: str, predictor: str | None, role: str | None) -> str:
    return f"{kind} {predictor or '-'}" + (f"({role})" if role else "")


def _day_section(report: Report) -> str:
    champion = report.champion
    head = (
        f"그날 · 챔피언 정책 {champion}"
        if champion is not None
        else "그날 · 챔피언 없음(첫 정책 전: 비용 지도의 칸을 기본값 θ와 λ로 산다)"
    )
    lines = [head]
    ledger = report.ledger
    if ledger is None:
        lines.append("장부: 그날의 줄이 없다(trader가 그날 장중에 돌지 않았다)")
    else:
        lines.append(
            f"장부: 그날 마지막 상태 {STATUSES.get(ledger['status'], ledger['status'])} · 청산한 매매 "
            f"{ledger['trades']}건, 순손익 {_won(ledger['realized'])} · 그날 최저 {_won(ledger['worst'])}"
            f"(하루 한도 {_amount(ledger['daily_limit'])}의 {_used(ledger['worst'], ledger['daily_limit'])})"
            f" · 누적 {_won(ledger['cumulative'])}, 최저 {_won(ledger['cumulative_worst'])}"
            f"(누적 한도 {_amount(ledger['cumulative_limit'])}의 "
            f"{_used(ledger['cumulative_worst'], ledger['cumulative_limit'])})"
        )
    if report.trades:
        net = sum(t.net or 0.0 for t in report.trades)
        lines.append(f"매매 {len(report.trades)}건(진입 순, 가격은 싣지 않는다), 순손익 합 {_won(net)}:")
        for n, t in enumerate(report.trades, 1):
            exit_ = (
                "열림(아직 청산하지 않았다)"
                if t.exit_at is None
                else f"{_when(t.exit_at, report.day)} {EXITS.get(t.reason or '', t.reason)}"
            )
            moved = "" if t.ticks is None else f" · {t.ticks:+g}틱"
            lines.append(
                f"{n}. {_when(t.entry_at, report.day)} → {exit_} · {_cell(t.kind, t.predictor, t.role)} · "
                f"오름 {t.probability:.2f} ≥ θ {t.threshold:g} · "
                f"p* {_ratio(t.p_star)}, 하한 {_ratio(t.low)} · "
                f"{t.quantity}주{moved} · 비용 {_amount(t.cost)} · 순손익 {_won(t.net)}"
            )
    else:
        lines.append("매매 없음.")
    lines.append(_decisions_line(report.decisions))
    return "\n".join(lines)


def _decisions_line(decisions: Mapping[tuple[str, str | None], int]) -> str:
    if not decisions:
        return "읽은 예측: 없음(챔피언의 장부가 그날 읽은 예측이 없다)"
    total = sum(decisions.values())
    bought = decisions.get(("bought", None), 0)
    parts = [f"읽은 예측 {total}", f"산 것 {bought}"]
    for outcome, title in (("unfilled", "사려 했으나 못 산 것"), ("skip", "사지 않은 것")):
        found = sorted(
            ((reason or "?", n) for (o, reason), n in decisions.items() if o == outcome),
            key=lambda x: (-x[1], x[0]),
        )
        if found:
            parts.append(f"{title}: " + ", ".join(f"{reason} {n}" for reason, n in found))
    return "읽은 예측(챔피언의 장부): " + " · ".join(parts)


def _pick_section(report: Report) -> str:
    pick = report.pick
    if pick is None:
        return "칸 고르기: 그날 고른 기록이 없다(trader가 그날 돌지 않았다)"
    head = (
        f"칸 고르기: {_when(pick['at'], report.day)}"
        f"(그날 고르기 {report.picks}번 가운데 장 마감 전 마지막) · "
        f"비용 지도 as_of {pick['as_of'] or '없음'}"
    )
    lines = [head]
    chosen = pick["picks"]
    if chosen:
        lines.append(f"매매할 칸 {len(chosen)}(순위 순):")
        for n, p in enumerate(chosen, 1):
            theta = "기본값" if p.get("threshold") is None else f"{p['threshold']:g}"
            lines.append(
                f"{n}. {_cell(p['kind'], p['predictor'], p['role'])}: 하한 {_ratio(p['low'])} − p* "
                f"{_ratio(p['p_star'])} = {p['low'] - p['p_star']:+.3f} · {p['calls']}건 · θ {theta} · "
                f"비율 {p['fraction']:.2f}"
            )
        return "\n".join(lines)
    reason = pick["reason"]
    why = NO_CELL.get(reason, reason or "?")
    if reason in ("no_cost_map", "no_policy") and pick.get("detail"):
        why += f": {pick['detail']}"
    lines.append(f"매매할 칸 없음: {why}({reason})")
    for c in pick["considered"]:
        verdict = c.get("verdict")
        if verdict is None:
            lines.append(
                f"- {c['kind']} {c['role']} {c.get('predictor') or '-'}: {c.get('why') or '판정 없음'}"
            )
            continue
        figures = f"하한 {_ratio(c.get('low'))}, p* {_ratio(c.get('p_star'))}, {c.get('calls') or 0}건"
        extra = f", {c['why']}" if c.get("why") else ""
        lines.append(f"- {c['kind']} {c['role']} {c.get('predictor') or '-'}: {verdict}({figures}){extra}")
    return "\n".join(lines)


def _standings_section(report: Report) -> str:
    if not report.standings:
        return "정책별 모의 성적: 정책이 없다(첫 밤 정책 맞추기 전)"
    lines = ["정책별 모의 성적 (그날까지의 기록)"]
    for s in report.standings:
        who = f"챔피언 정책 {s.version}" if s.champion else f"도전 정책 {s.version}"
        today = (
            "그날 돌지 않았다"
            if s.today is None
            else f"그날 매매 {s.today.trades}건, 순손익 {_won(s.today.net)}"
            + (" (멈춘 날: 정지나 한도)" if s.today.stopped else "")
        )
        p = s.paper
        line = (
            f"- {who}: {today} · 기록 {p['days']}거래일, 매매 {p['trades']}건, 순손익 {_won(p['net'])}, "
            f"날마다 평균 {_won(p['mean'])}, 하한 {_won(p['low'])}"
        )
        j = s.judgment
        if j is not None:
            verdict = VERDICTS[j.why] + (f", {j.until}일을 채워 내림" if j.retires else "")
            line += (
                f" · 챔피언과 함께 돈 거래일 {j.days}일, (도전 − 챔피언) 평균 {_won(j.mean)}, "
                f"하한 {_won(j.low)}"
                f"(신뢰수준 {j.level:.2%}) → {verdict}"
            )
        lines.append(line)
    return "\n".join(lines)


def _night_section(report: Report) -> str:
    lines = ["그 밤 (판정·교체·맞추기: 다음 거래일의 08:30 고르기부터 쓴다)"]
    for c in report.crowned:
        if c.replaced is None:
            lines.append(f"- 첫 챔피언: 정책 {c.version}({_when(c.at, report.day)}). {c.reason}")
        else:
            lines.append(
                f"- 챔피언 교체: 정책 {c.replaced} → 정책 {c.version}({_when(c.at, report.day)}). {c.reason}"
            )
    if report.dropped:
        lines.append(
            f"- 내린 도전 정책(함께 돈 거래일 {promotion.RETIRE_DAYS}일 동안 이기지 못함): "
            + ", ".join(f"정책 {v}" for v in report.dropped)
        )
    if report.admitted:
        lines.append(
            "- 빈 자리에 들어간 정책(다음 거래일부터 모의로 돈다): "
            + ", ".join(f"정책 {v}" for v in report.admitted)
        )
    for f in report.fitted:
        status = POLICY_STATUSES.get(f.status, f.status)
        if f.error is not None:
            lines.append(f"- 맞춘 정책 {f.version}({status}): 읽지 못했다({f.error})")
            continue
        cells = (
            " · ".join(
                f"{n}. {_cell(c.kind, c.predictor, c.role)} θ {c.threshold:g} λ {c.kelly_fraction:g}"
                + _simulated(c.fitted)
                for n, c in enumerate(f.cells, 1)
            )
            or "칸 없음"
        )
        lines.append(f"- 맞춘 정책 {f.version}({status}): {cells}" + _simulated(f.result, "정책 전체"))
        for x in f.left_out:
            lines.append(
                f"  뺀 칸: {_cell(str(x.get('kind')), x.get('predictor'), x.get('role'))}: {x.get('why')}"
            )
    if report.waiting:
        lines.append(f"- 자리를 기다리는 정책: {report.waiting}개(자리가 나면 가장 새 것부터 들어간다)")
    if report.refit is not None:
        if report.refit:
            lines.append(f"- 밤 정책 맞추기: 실패(종료 코드 {report.refit}). 까닭은 아래 출력에 있다")
        elif not report.fitted:
            lines.append(
                "- 밤 정책 맞추기: 새 정책을 저장하지 않았다(맞춘 값이 챔피언이나 도는 도전 정책과 같다)"
            )
    if len(lines) == 1:
        lines.append("- 바뀐 것 없음")
    return "\n".join(lines)


def _simulated(fitted: Mapping[str, Any], title: str = "") -> str:
    """A fit's simulated trades and net P&L, when it has them."""
    trades, net = fitted.get("trades"), fitted.get("net")
    if not isinstance(trades, int) or not isinstance(net, int | float):
        return ""
    head = f"{title} " if title else ""
    return f" ({head}모의 {trades}건, 순손익 {_won(float(net))})"


def _gate_line(report: Report) -> str:
    g = report.gate
    if g.policy is None:
        return f"실전 관문: 챔피언이 없다(0/{g.required}거래일)"
    line = (
        f"실전 관문: 챔피언 정책 {g.policy}의 모의 기록 {g.days}/{g.required}거래일, 날마다 순손익 평균 "
        f"{_won(g.mean)}, 하한 {_won(g.low)}(신뢰수준 {g.level:.0%}) → {'넘었다' if g.passed else '아직'}"
    )
    if report.gate_passed:
        line += (
            " · 그 밤 처음 넘었다(넘어도 저절로 실전이 되지 않는다: 모드는 사용자가 서버에서 바꾼다, #408)"
        )
    return line


def _limits_section(report: Report) -> str:
    lines = ["한도와 정지"]
    ledger = report.ledger or (report.recent[-1] if report.recent else None)
    if ledger is not None:
        capital = "" if report.capital is None else f"운용 상한 {_amount(report.capital)} · "
        lines.append(
            f"- 한도: {capital}하루 손실 한도 {_amount(ledger['daily_limit'])} · 누적 손실 한도 "
            f"{_amount(ledger['cumulative_limit'])}"
        )
    if report.halts:
        for h in report.halts:
            lines.append(
                f"- 그날 닿은 한도: {LIMITS.get(h['kind'], h['kind'])} {_hms(h['at'])} · "
                f"손익 {_won(h['pnl'])}(실현 {_won(h['realized'])}, 열린 포지션 {_won(h['unrealized'])}) · "
                f"한도 {_amount(h['limit_won'])}"
                + (" · 다음 거래일에 다시 돈다" if h["kind"] == DAILY_LIMIT else "")
            )
    else:
        lines.append("- 그날 닿은 한도: 없음")
    held = report.held
    if held is not None:
        lines.append(
            f"- 누적 손실 한도로 멈춰 있다({held['day']}에 닿음): "
            "서버에서 `krx-quant trade rearm`을 할 때까지"
        )
    if any(h["kind"] == CUMULATIVE_LIMIT for h in report.halts):
        lines.append("- 누적 손실 한도는 서버에서 `krx-quant trade rearm`을 할 때까지 멈춰 둔다")
    pause = report.pause
    if pause is None:
        lines.append("- 정지: 없음")
    else:
        what = "정지하고 즉시 매도" if pause.sell_now else "새 진입 정지"
        lines.append(
            f"- 정지: {what}({_source(pause.source)}, {_when(pause.at, report.day)}부터): 서버에서 "
            "`krx-quant trade resume`을 할 때까지"
        )
    if report.controls:
        lines.append("- 그날과 그 밤의 요청: " + " · ".join(_control(c, report.day) for c in report.controls))
    return "\n".join(lines)


def _source(source: str) -> str:
    return {"api": "화면", "cli": "서버"}.get(source, source)


def _control(control: Control, day: date) -> str:
    if control.action == "pause":
        what = "정지(즉시 매도)" if control.sell_now else "정지(새 진입만)"
    else:
        what = {"resume": "재개", "rearm": "누적 한도 다시 켜기"}.get(control.action, control.action)
    read = "" if control.applied_at is not None else ", trader가 아직 읽지 않음"
    return f"{_when(control.at, day)} {what}({_source(control.source)}{read})"


def _recent_section(report: Report) -> str:
    if not report.recent:
        return "최근 거래일: 장부가 없다"
    lines = [f"최근 거래일 (챔피언의 장부, 그날 청산한 매매, 마지막 {RECENT_DAYS}일)"]
    for r in report.recent:
        line = (
            f"{r['day']} · 매매 {r['trades']}건 · 순손익 {_won(r['realized'])} · "
            f"그날 최저 {_won(r['worst'])} · "
            f"누적 {_won(r['cumulative'])} · {STATUSES.get(r['status'], r['status'])}"
        )
        if r["halt"]:
            line += f" · {LIMITS.get(r['halt'], r['halt'])}에 닿음"
        lines.append(line)
    return "\n".join(lines)
