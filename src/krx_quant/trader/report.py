"""The trader's nightly report (#402, #393 "리포트와 에이전트"): what the trader did on a trading day
and what that night changed, for the report issue, where an agent reads it
(``docs/agents/trader-report.md``).

``krx-quant trader-report [--day]`` prints it, reading ``state/trader.sqlite3`` read-only. The
scheduled job ``trader-report`` runs ``krx-quant refit-policy --report`` every trading day's night
(``run_night``): the night's judgment of the challengers and re-fit first (#399, #400,
``refit.run_refit``), then this report of that day, printed above the re-fit's own output, one
comment a night.

**Which day, which night.** A trading day's night runs from its pick (``PICK_AT``, 08:30) to the
next trading day's pick: a champion crowned, a challenger dropped, a cohort formed (#473), a policy
fitted in it takes effect from the next trading day's pick (``promotion.in_market``,
``policy.running_versions``).
The day reported is the one whose night it is (``report_day``): at the scheduled jobs' night (22:30
to 05:30) the evening's trading day, as the scheduler counts it. The day's champion and challengers
are those in force at its pick; the records are read to the day.

**What it holds** (``render``): the day's ledger and the champion's trades (times, cell, probability
and θ, p* and the hit rate's lower bound, shares, how each ended, the ticks between its fills, the
costs and the net P&L in won), what the champion's book did with the predictions it read; each
decision source's trades that day in one table (#560, ``Source``: the champion's book, the mock
practice, the champion policy's paper shadow, each challenger: their fills' P&L, costs and net in
ticks and won, how they exited, and the share of the calls they traded that the engine's answers
proved right, beside those calls' p*); when no cell traded, why, as it held (#560: the champion had no
cell, and what the fit of the night it was kept left out and why, or its cells were held: ``gone``,
``predictor_inactive``, #603), and beside the cells traded the policy's cells that bought nothing and
why (#603), then the day's pick from the cost map, every cell considered and its verdict (since #480
they decide nothing); each
policy's paper record to the day and, for a challenger, where it stands against its cohort's
champion on the days of its cycle to the day (``promotion.judge_cohort``, #473: a figure until its
review night, which judges it once); what the night changed (the champion, the cohort judged and its
verdict, the challengers dropped, the cohort formed, the policies fitted and their cells, the
policies waiting, and whether the re-fit kept none, with why and the cells it left out (#560, the
night's fit ``run_night`` hands it), or failed); the live gate (``promotion.gate_of``,
#408: the checkpoints evaluated that night and where the champion stands); what the live mode needs
(``live.readiness``: the gate and the execution checks on the mock days, and on a live day its first
period, ``live.ramp``), with a KIS day's comparison of its orders with KIS's; the limits, the halts
reached that day, a cumulative halt still holding, the pause in force and the pauses, resumes and
re-arms asked for; the last trading days' ledger. A mock day's practice (#427,
``KRX_TRADER_MOCK_PRACTICE``) is apart: its trades' sum (each trade's line folded apart since #560,
``practice_trades``: the night's job writes them to ``PRACTICE_TRADES`` and folds them under its
comment), the predictions the book read on its practice
pick and the practice cell of the day's pick, each said so (``연습``; its pairs with the practice
shadow are no more since #548, ``gap.py``); the ledger and the limits count the champion's KIS trades
alone, never the practice's
(#533): the practice's P&L and the account's (both) are said beside them. The practice cell is the
one kept for the day's review cycle (#474, ``practice.py``): its θ, when it was chosen and its
replayed figures; the night's choice of the next cycle's is in the re-fit's output below
(``practice.freeze_now``, which ``run_night`` runs after the re-fit).

**Execution cost and the risk rules** (#475, spec #468 8): under each trade its legs' adverse ticks
from the mid-point the book knew at each decision (a take-profit's from its target) and their delays
(``store.execution``), then the day's means and whose clock the times are on (a paper fill's quote, or
the trader's read of KIS's order inquiry: never KIS's own time of the fill); in the limits, the loss
budget, the day's buy orders against their limit, the cooldowns the net losses in a row began and those
losses at the day's last exit (``Risk``, of the trader's last start's rules). Each cell's calibration
is in the re-fit's output below (``refit.calibrated``). None of it changes a θ or a rule.

**What it never holds**: a price (no fill, reference, target, stop or quote), a broker's order
number, an account, a key. The scheduled job's output is masked besides, as every job's
(``redaction.py``).

**When it calls an agent** (``reasons``; the job labels the issue ``ready-for-agent`` only then,
``calls_agent``): on the week's last trading day (a Friday, or the day before a Friday the market is
closed), when a loss limit was reached that day, when the champion changed that night, when the live
gate passed that night (a checkpoint evaluated that night passed for the champion at its end), when
the live mode's first period was lifted or breached that day (#408), or when the night's re-fit
failed. Any other night is a record and changes no label.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import sqlite3
from collections import Counter
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from krx_quant.config import Settings
from krx_quant.feeds.base import KST
from krx_quant.market_calendar import MarketCalendar, calendar_store
from krx_quant.predictions import DB_NAME as ENGINE_DB
from krx_quant.trader import (
    CAPITAL,
    KIS_MODES,
    LIVE,
    PAPER,
    PICK_AT,
    cohort,
    live,
    practice,
    promotion,
    review,
)
from krx_quant.trader import policy as policies
from krx_quant.trader.cells import Cell, open_engine_db
from krx_quant.trader.db import iso
from krx_quant.trader.gap import day_gap
from krx_quant.trader.limits import Pause, pause_of
from krx_quant.trader.paper import Rules, cooldowns
from krx_quant.trader.refit import (
    BOOTSTRAP,
    FEW_DAYS,
    FEW_TRADES,
    MIN_DAYS,
    MIN_TRADES,
    NO_POLICY_KEPT,
    Refitted,
    run_refit,
)
from krx_quant.trader.replay import DOWN, FLAT, UP, day_answers
from krx_quant.trader.selection import NO_COST_MAP, NO_PASS, NO_POLICY, NOT_IN_POLICY
from krx_quant.trader.settings import TraderSettings
from krx_quant.trader.store import (
    BREACH,
    CUMULATIVE_LIMIT,
    DAILY_LIMIT,
    DB_NAME,
    LIFT,
    MID,
    SHADOW_TRADE,
    TRADER_CLOCK,
    Control,
    champion_of,
    day_buys,
    day_exits,
    day_split,
    execution,
    practice_of,
)

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
# The mock practice's trades a line each (#560, ``practice_trades``), under the state directory: the
# night's job writes them there and folds them under its comment (``scheduled.trader``'s attachment).
PRACTICE_TRADES = "trader-report/practice-trades.txt"
PRACTICE_HEADING = "연습 매매별 줄"

WEEKDAYS = "월화수목금토일"
MODES = {PAPER: "모의(paper)", "mock": "KIS 모의(mock)", "live": "실전(live)"}
STATUSES = {
    "running": "도는 중(running)",
    "entries_paused": "새 진입 정지(entries_paused)",
    "paused": "정지하고 즉시 매도(paused)",
    DAILY_LIMIT: "하루 손실 한도로 멈춤(daily_limit)",
    CUMULATIVE_LIMIT: "누적 손실 한도로 멈춤(cumulative_limit)",
    "order_failed": "주문 실패로 그날 새 진입 정지(order_failed)",  # #405
}
# A failed KIS order of mock mode (#405): what it was, what became of it.
FAILED_ORDERS = {
    "buy": "매수",
    "take_profit": "익절 매도",
    "cancel": "취소",
    "sell": "매도",
    "orderable": "주문 가능 현금 조회",
    "balance": "잔고 조회",  # #460: a live buy's, before it is sent
}
FAILED_OUTCOMES = {"refused": "KIS가 거부", "unknown": "응답 없음(결과 모름)", "error": "보내지 못함"}
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
    "not_in_policy": "챔피언 정책의 칸 가운데 사는 칸이 없다",
    "no_policy": "챔피언 정책을 읽지 못했다",
    "no_champion": "챔피언이 아직 없다(첫 정책 전에는 연습 말고는 사지 않는다, #469)",
}
# How the decision sources' table (#560) names how a trade exited, in its order; any other is "기타".
SOURCE_EXITS = {"take_profit": "목표", "stop_loss": "손절", "time": "시간"}
POLICY_STATUSES = {
    policies.CHAMPION: "챔피언",
    policies.CHALLENGER: "도전 정책: 묶음으로 주기 동안 모의로 돈다",
    policies.WAITING: "기다리는 정책: 다음 묶음(검토 날 밤)을 기다린다",
    policies.DROPPED: "내린 정책",
    policies.FORMER: "전 챔피언",
}
# Where a challenger stands before its review night (#473): a figure, not a verdict.
STANDS = {
    None: "지금은 하한 > 0",
    promotion.FEW_DAYS: f"지금은 날 수 부족({promotion.PROMOTION_DAYS}일 미만이면 판정 불가)",
    promotion.NOT_ABOVE: "지금은 하한 ≤ 0",
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
    practice: bool = False  # the mock book's practice trade (#427)
    # Its execution cost (#475, ``store.execution``): each leg's adverse ticks and delays; None from a
    # store the trader has not opened since #475.
    execution: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class Source:
    """A decision source's trades entered on the day (#560): the champion's book, the mock book's
    practice, the champion policy's paper shadow (mock, live) or a challenger's paper book. Their P&L,
    of those exited, in won and in ticks (each trade's in its own tick, times its shares, summed), how
    they exited, and how the calls they traded were answered; no price."""

    name: str
    trades: int
    open: int  # not exited by the night
    gross_ticks: float  # (sale − buy) × shares, before the costs
    cost: float  # both commissions and the tax, won
    cost_ticks: float
    net: float
    net_ticks: float
    exits: Mapping[str, int] = field(default_factory=dict)  # how each exited one ended → how many
    # The calls traded by their answer's class (``replay.UP``, ``FLAT``, ``DOWN``: the kind's rise,
    # middle and fall), those with an answer yet; None when the engine's database cannot be read.
    answers: Mapping[str, int] | None = None
    unanswered: int = 0
    p_stars: tuple[float, ...] = ()  # the p* its trades were bought on, each once, the lowest first


@dataclass(frozen=True)
class Standing:
    """A policy of the day: the champion or a challenger, its record's day of the report (None if it
    did not run that day), its paper record to the day and, a challenger's, its judgment; on its
    cohort's review day, its verdict (#473, ``cohort.Verdict``'s member's), once judged."""

    version: int
    champion: bool
    today: promotion.PolicyDay | None
    paper: Mapping[str, Any]
    judgment: promotion.Judgment | None = None
    verdict: str | None = None


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
class Risk:
    """The risk rules of the champion's book on the day (#471, #475): the rules of the trader's last
    start by the night's end (``runs``), the capital a buy sized on, the buy orders that day, the
    cooldowns its net losses in a row began (each one's start and end) and those losses in a row at
    its last exit."""

    rules: Rules
    capital: float
    buys: int
    cooldowns: tuple[tuple[datetime, datetime], ...]
    losses: int


@dataclass(frozen=True)
class Report:
    """A trading day and its night, as read from the trader's store."""

    day: date
    mode: str
    last_of_week: bool  # the week's last trading day
    gate: promotion.Gate  # on the champion at the night's end, its record to the day (#408)
    found: bool = True  # the store is there
    checked: tuple[promotion.Checkpoint, ...] = ()  # the gate's checkpoints evaluated in the night
    night: tuple[datetime, datetime] | None = None  # from the day's pick to the next one's
    readiness: live.Readiness | None = None  # what the live mode needs, as of the next day (#408)
    ramp: live.Ramp | None = None  # a live day's first period
    ramp_events: tuple[tuple[str, datetime], ...] = ()  # the ramp's lifts and breaches that day
    order_check: Mapping[str, Any] | None = None  # a KIS day's orders against KIS (#408)
    capital: float | None = None
    champion: int | None = None  # in force on the day
    ledger: Mapping[str, Any] | None = None  # the day's ``days`` row
    recent: tuple[Mapping[str, Any], ...] = ()  # the last ``RECENT_DAYS`` rows, the day's last
    trades: tuple[Trade, ...] = ()
    # What the champion's book did with the predictions it read: (``bought``, None), (``unfilled``,
    # why), (``skip``, why) → how many.
    decisions: Mapping[tuple[str, str | None], int] = field(default_factory=dict)
    # The same of the predictions it read on a practice pick (#427), apart.
    practice_decisions: Mapping[tuple[str, str | None], int] = field(default_factory=dict)
    pick: Mapping[str, Any] | None = None  # the champion's last pick of the day
    picks: int = 0  # its picks that day
    standings: tuple[Standing, ...] = ()
    crowned: tuple[Crowned, ...] = ()
    dropped: tuple[int, ...] = ()
    admitted: tuple[int, ...] = ()
    # #473: the cohorts judged in the night with their verdicts, and those formed in it.
    judged: tuple[tuple[cohort.Cohort, cohort.Verdict], ...] = ()
    formed: tuple[cohort.Cohort, ...] = ()
    fitted: tuple[Fitted, ...] = ()
    waiting: int = 0
    halts: tuple[Mapping[str, Any], ...] = ()  # reached that day
    failures: tuple[Mapping[str, Any], ...] = ()  # the KIS orders that failed that day (#405, mock)
    gap: Mapping[str, Any] | None = None  # a mock day's KIS trades against the paper shadow's (#405)
    held: Mapping[str, Any] | None = None  # a cumulative halt reached before the day, not re-armed
    pause: Pause | None = None  # in force at the night's end
    controls: tuple[Control, ...] = ()  # asked from the day's start to the night's end
    refit: int | None = None  # the night's re-fit's exit code (``run_night``); None: not run with it
    cycle: review.Cycle | None = None  # the day's review cycle (#469); None before the evaluation
    # The mock practice cell kept for the day's review cycle (#474); None: none kept (yet).
    practice: practice.Frozen | None = None
    risk: Risk | None = None  # the risk rules that day (#471, #475); None before #471's trader
    sources: tuple[Source, ...] = ()  # each decision source's trades that day (#560)
    champion_policy: policies.Policy | None = None  # the day's champion as kept; None if unreadable
    tonight: Refitted | None = None  # the night's fit, kept or not (``run_night``); None: not run with it

    @property
    def gate_passed(self) -> bool:
        """The live gate passed in the night: the champion at its end passed at a checkpoint evaluated
        in it (#408)."""
        passed = self.gate.passed_at
        if passed is None or passed.at is None or self.night is None:
            return False
        return self.night[0] <= passed.at < self.night[1]


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
    mode: str | None = None,
    refit: int | None = None,
    tonight: Refitted | None = None,
) -> Report:
    """The report of the trading day ``day`` from the trader's store at ``path``, read-only (module
    doc), of ``mode``: by default the mode the trader last started in by the day's close (#405,
    ``promotion.mode_at``); the calls its trades made answered from the engine's database beside it
    (#560, read-only). ``tonight`` is the night's fit when the report is made with it (``run_night``).
    ``sqlite3.Error`` when it cannot be read."""
    week = last_of_week(calendar, day)
    if not path.exists():
        return Report(
            day, mode or PAPER, week, promotion.no_gate(), found=False, refit=refit, tonight=tonight
        )
    start, end = night_of(calendar, day)
    session = calendar.session(day)
    with _reading(path) as db:
        closed = session.close if session is not None else datetime.combine(day, time(23, 59, 59), KST)
        mode = mode or promotion.mode_at(db, closed)
        tables = _tables(db)
        has_policies = {"policies", "champions"} <= tables
        champion = _champion_at(db, start) if has_policies else None
        at_end = _champion_at(db, end) if has_policies else None
        challengers = _slots_at(db, start) if has_policies else []
        of_cohort = (
            {v: _resolved(db, cohort.of_member(db, v), calendar) for v in challengers} if has_policies else {}
        )
        against = {c.champion for c in of_cohort.values() if c is not None and c.champion is not None}
        records = promotion.records(
            db, mode, {v for v in (champion, at_end, *challengers, *against) if v is not None}
        )
        to_day = {v: _upto(r, day) for v, r in records.items()}
        verdicts = {
            int(m["version"]): str(m["verdict"])
            for c in of_cohort.values()
            if c is not None
            and c.review == day
            and (v := cohort.verdict_of(db, c.evaluation, c.cycle)) is not None
            for m in v.members
        }
        standings = _standings(day, champion, challengers, to_day, of_cohort, verdicts)
        judged = tuple(
            (cohort.resolved(db, c, calendar.session), v)
            for c in cohort.cohorts(db)
            if (v := cohort.verdict_of(db, c.evaluation, c.cycle)) is not None and start <= v.judged_at < end
        )
        formed = tuple(
            cohort.resolved(db, c, calendar.session) for c in cohort.cohorts(db) if start <= c.formed_at < end
        )
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
        points = promotion.checkpoints(db)
        later = day + timedelta(days=1)
        cycle = _cycle(db, day, calendar)
        ramp = (
            live.ramp(db, calendar.session, datetime.combine(later, time(), KST), _capital_of(db, end))
            if mode == LIVE
            else None
        )
        return Report(
            day=day,
            mode=mode,
            last_of_week=week,
            gate=promotion.gate_of(db, at_end, day, end) if has_policies else promotion.no_gate(),
            checked=tuple(c for c in points if c.at is not None and start <= c.at < end),
            night=(start, end),
            readiness=live.readiness(db, calendar.session, later),
            ramp=ramp,
            ramp_events=_ramp_events(db, start, end) if "ramp" in tables else (),
            order_check=_order_check(db, mode, day)
            if mode in KIS_MODES and "order_checks" in tables
            else None,
            capital=_capital(db, mode, end) if "runs" in tables else None,
            champion=champion,
            ledger=_ledger(db, mode, day) if "days" in tables else None,
            recent=_recent(db, mode, day) if "days" in tables else (),
            trades=_trades(db, mode, day) if "trades" in tables else (),
            decisions=_decisions(db, mode, day, champion) if "decisions" in tables else {},
            practice_decisions=_decisions(db, mode, day, champion, practice=True)
            if "decisions" in tables
            else {},
            pick=pick,
            picks=picks,
            standings=standings,
            crowned=crowned,
            dropped=dropped,
            admitted=admitted,
            judged=judged,
            formed=formed,
            fitted=fitted,
            waiting=waiting,
            halts=_halts(db, mode, day) if "halts" in tables else (),
            failures=_failures(db, mode, day) if "failures" in tables else (),
            gap=day_gap(db, day, mode) if mode in KIS_MODES and "trades" in tables else None,
            held=_held(db, mode, day, end) if "halts" in tables else None,
            pause=pause_of(everything),
            controls=controls,
            refit=refit,
            cycle=cycle,
            practice=None if cycle is None else practice.frozen(db, cycle.evaluation, cycle.number),
            risk=_risk(db, mode, day, end, ramp) if {"runs", "trades", "unfilled_buys"} <= tables else None,
            sources=_sources(db, path.parent / ENGINE_DB, mode, day, champion, challengers)
            if "trades" in tables
            else (),
            champion_policy=_policy(db, champion) if has_policies and champion is not None else None,
            tonight=tonight,
        )


def _policy(db: sqlite3.Connection, version: int) -> policies.Policy | None:
    """Policy ``version`` as kept; None when it cannot be read (the report says why no cell traded
    without it)."""
    try:
        return policies.read(db, version)
    except policies.PolicyError as e:
        log.warning("trader report: %s", e)
        return None


def _sources(
    db: sqlite3.Connection,
    engine: Path,
    mode: str,
    day: date,
    champion: int | None,
    challengers: Sequence[int],
) -> tuple[Source, ...]:
    """Each decision source's trades entered on ``day`` (#560, ``Source``): the champion's book (of
    ``mode``) always; the mock book's practice when it traded; the champion policy's paper shadow on a
    KIS day; each challenger running that day. A store from before a column has none of what it
    tells apart (a practice, a shadow, a challenger)."""
    columns = _columns(db, "trades")
    champions, practised = champion_of(db), practice_of(db)
    wanted: list[tuple[str, str, str, tuple[Any, ...], bool]] = [
        (
            "챔피언 없음" if champion is None else f"챔피언 정책 {champion}",
            mode,
            f"{champions} AND NOT {practised}",
            (),
            True,
        ),
        ("연습(KIS 모의)", mode, f"{champions} AND {practised}", (), False),
    ]
    if mode in KIS_MODES and "shadow" in columns:
        whose = "" if champion is None else f"챔피언 정책 {champion}, "
        wanted.append((f"그림자({whose}모의 체결)", PAPER, f"{SHADOW_TRADE} AND NOT {practised}", (), True))
    if {"policy_id", "champion"} <= columns:
        unshadowed = f" AND NOT {SHADOW_TRADE}" if "shadow" in columns else ""
        wanted += [
            (f"도전 정책 {v}", PAPER, f"t.policy_id = ? AND t.champion = 0{unshadowed}", (v,), True)
            for v in challengers
        ]
    found: list[tuple[str, list[dict[str, Any]]]] = []
    for name, of, where, params, always in wanted:
        rows = [
            dict(r)
            for r in db.execute(
                f"SELECT t.* FROM trades t WHERE t.mode = ? AND substr(t.entry_at, 1, 10) = ? AND {where}"
                " ORDER BY t.entry_at, t.id",
                (of, day.isoformat(), *params),
            )
        ]
        if rows or always:
            found.append((name, rows))
    answers = _answers(engine, day, {Cell(r["kind"], r["predictor"]) for _, rows in found for r in rows})
    return tuple(_tally(name, rows, answers) for name, rows in found)


def _answers(engine: Path, day: date, cells: Collection[Cell]) -> dict[tuple[str, str, int], str] | None:
    """The answers' classes of ``cells``' predictions with bar closes on ``day``, by (kind, predictor,
    prediction): the engine's own (``replay.day_answers``: the direction's touch label, a kind's answer
    of its answer version), read-only; None when its database is not there or cannot be read."""
    if not cells:
        return {}
    if not engine.exists():
        return None
    try:
        db = open_engine_db(engine)
        try:
            return {
                (cell.kind, cell.predictor, prediction): answer.outcome
                for cell in cells
                for prediction, answer in day_answers(db, cell, day).items()
            }
        finally:
            db.close()
    except (sqlite3.Error, ValueError) as e:  # ValueError: a kind without a rise (``cells.traded_kind``)
        log.warning("trader report: cannot read the traded calls' answers (%s)", e)
        return None


def _tally(
    name: str, rows: Sequence[Mapping[str, Any]], answers: Mapping[tuple[str, str, int], str] | None
) -> Source:
    """A decision source's trades (``Source``), each call answered from ``answers`` (None: unknown)."""
    exited = [r for r in rows if r["exit_at"] is not None]

    def summed(column: str) -> tuple[float, float]:
        won = [(float(r[column] or 0.0), float(r["tick"] or 0.0)) for r in exited]
        return sum(w for w, _ in won), sum(w / tick for w, tick in won if tick)

    _, gross_ticks = summed("gross")
    cost, cost_ticks = summed("cost")
    net, net_ticks = summed("net")
    classes: Counter[str] | None = None if answers is None else Counter()
    unanswered = 0
    for r in rows:
        outcome = (
            None if answers is None else answers.get((r["kind"], r["predictor"], int(r["prediction_id"])))
        )
        if classes is not None and outcome is not None:
            classes[outcome] += 1
        elif classes is not None:
            unanswered += 1
    return Source(
        name=name,
        trades=len(rows),
        open=len(rows) - len(exited),
        gross_ticks=gross_ticks,
        cost=cost,
        cost_ticks=cost_ticks,
        net=net,
        net_ticks=net_ticks,
        exits=dict(Counter(str(r["exit_reason"]) for r in exited)),
        answers=None if classes is None else dict(classes),
        unanswered=unanswered,
        p_stars=tuple(sorted({float(r["p_star"]) for r in rows if r.get("p_star") is not None})),
    )


def _risk(db: sqlite3.Connection, mode: str, day: date, end: datetime, ramp: live.Ramp | None) -> Risk | None:
    """The risk rules of the champion's book on ``day`` (#475, ``Risk``): of the trader's last start of
    ``mode`` before the night's ``end``; None without one that kept them (a start before #471)."""
    row = db.execute(
        "SELECT detail FROM runs WHERE mode = ? AND at < ? ORDER BY id DESC LIMIT 1", (mode, iso(end))
    ).fetchone()
    detail = json.loads(row[0]) if row is not None else {}
    keys = ("capital", "risk_per_trade_percent", "loss_streak", "cooldown_minutes", "daily_buy_orders")
    if not all(isinstance(detail.get(k), int | float) and not isinstance(detail.get(k), bool) for k in keys):
        return None
    rules = Rules(
        capital=int(detail["capital"]),
        risk_percent=float(detail["risk_per_trade_percent"]),
        loss_streak=int(detail["loss_streak"]),
        cooldown=timedelta(minutes=float(detail["cooldown_minutes"])),
        daily_buys=int(detail["daily_buy_orders"]),
    )
    since = datetime.combine(day, time(), KST)
    until = since + timedelta(days=1)
    capital = rules.capital if ramp is None or ramp.lifted_at is not None else min(rules.capital, ramp.cap)
    began, losses = cooldowns(rules, day_exits(db, mode, since, until))
    return Risk(rules, capital, day_buys(db, mode, since, until), tuple(began), losses)


def _cycle(db: sqlite3.Connection, day: date, calendar: MarketCalendar) -> review.Cycle | None:
    """The review cycle of ``day`` (#469), None before the evaluation version was begun."""
    evaluation = review.current(db)
    return None if evaluation is None else review.cycle_of(evaluation, day, calendar.session)


def reasons(report: Report) -> list[str]:
    """Why the report calls an agent (module doc); none on a night that is a record only."""
    out = []
    if report.last_of_week:
        out.append(f"이번 주 마지막 거래일({WEEKDAYS[report.day.weekday()]}요일)")
    for halt in report.halts:
        out.append(f"{LIMITS.get(halt['kind'], halt['kind'])}에 닿음({_hms(halt['at'])})")
    if report.failures:
        out.append(f"KIS 주문 실패 {len(report.failures)}건(첫 실패 {_hms(report.failures[0]['at'])})")
    for change in report.crowned:
        if change.replaced is None:
            out.append(f"첫 챔피언(정책 {change.version})")
        else:
            out.append(f"챔피언 교체(정책 {change.replaced} → 정책 {change.version})")
    if report.gate_passed:
        out.append(f"실전 관문을 넘음(챔피언 정책 {report.gate.policy})")
    for event, _ in report.ramp_events:
        out.append(
            "실전 운용 상한을 올림(첫 단계를 마침)"
            if event == LIFT
            else "실전 첫 단계 점검을 벗어남(새 진입 정지)"
        )
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
        *([_sources_section(report)] if report.sources else []),
        *([] if report.gap is None else [_gap_section(report.gap, report.mode)]),
        _pick_section(report),
        _standings_section(report),
        _night_section(report),
        _gate_line(report),
        _live_section(report),
        _limits_section(report),
        _recent_section(report),
    ]
    return "\n\n".join(["\n".join(lines), *sections])


def run_report(
    settings: Settings,
    now: datetime,
    day: date | None = None,
    refit: int | None = None,
    mode: str | None = None,
    tonight: Refitted | None = None,
    fold: Path | None = None,
) -> int:
    """``krx-quant trader-report``: the report of ``day`` (else ``report_day``) printed, and the mock
    practice's trades a line each (#560, ``practice_trades``) written to ``fold`` when given (the
    night's job folds them under its comment), else printed under the report when it traded; 0, or
    ``REPORT_FAILED`` (with a line saying why) when the store cannot be read."""
    calendar = calendar_store(settings).calendar
    day = day or report_day(calendar, now)
    try:
        made = read(settings.state_dir / DB_NAME, day, calendar, mode=mode, refit=refit, tonight=tonight)
    except (sqlite3.Error, ValueError, KeyError, TypeError) as e:
        log.error("trader report: cannot read the trader's store (%s)", e)
        print(f"자동 매매 리포트 (#402) · {day}: 저장소를 읽지 못해 만들지 못했다: {type(e).__name__}: {e}")
        print(AGENT + "리포트를 만들지 못함")
        return REPORT_FAILED
    print(render(made))
    folded = practice_trades(made)
    if fold is None:
        if any(t.practice for t in made.trades):
            print(f"\n{folded}")
        return 0
    try:
        fold.parent.mkdir(parents=True, exist_ok=True)
        fold.write_text(folded + "\n", encoding="utf-8")
    except OSError as e:
        log.error("trader report: cannot write the practice's trades to %s (%s)", fold, e)
    return 0


def practice_trades(report: Report) -> str:
    """The mock practice's trades of the day (#427), a line each with its execution under it (#475), as
    the report showed them before #560: kept apart from it, the report says their sum alone."""
    practised = [t for t in report.trades if t.practice]
    if not practised:
        return f"{PRACTICE_HEADING} (#560) · {report.day}: 그날 연습 매매가 없다."
    lines = [f"{PRACTICE_HEADING} (#560) · {report.day} · {len(practised)}건(진입 순, 가격은 싣지 않는다)"]
    lines += [line for n, t in enumerate(practised, 1) for line in _trade_lines(n, t, report.day)]
    return "\n".join(lines)


async def run_night(
    settings: Settings, trader: TraderSettings, now: datetime, engine_url: str | None = None
) -> int:
    """``krx-quant refit-policy --report``, the scheduled job ``trader-report``'s: the night's re-fit
    (``refit.run_refit``) and the mock practice cell chosen for the review cycle in force if none is
    kept for it (#474, ``practice.freeze_now``), then the day's report printed above their output (with
    the fit, kept or not, #560) and the practice's trades written to ``PRACTICE_TRADES``. The re-fit's
    exit code (one that raised counts 1), else the report's: the practice's choice changes neither."""
    printed = io.StringIO()
    fitted: list[Refitted] = []
    try:
        with contextlib.redirect_stdout(printed):
            code = await run_refit(settings, trader, now, engine_url, fitted)
    except Exception:
        log.exception("trader report: the night's re-fit failed")
        code = NO_POLICY_KEPT
    try:
        with contextlib.redirect_stdout(printed):
            print()
            await practice.freeze_now(settings, trader, now, engine_url)
    except Exception:
        log.exception("trader report: the night's practice cell could not be chosen")
    reported = run_report(
        settings,
        now,
        refit=code,
        tonight=fitted[-1] if fitted else None,
        fold=settings.state_dir / PRACTICE_TRADES,
    )
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


def _champion_at(db: sqlite3.Connection, moment: datetime) -> int | None:
    """The champion before ``moment``: the latest change of it then."""
    row = db.execute(
        "SELECT policy_id FROM champions WHERE at < ? ORDER BY id DESC LIMIT 1", (iso(moment),)
    ).fetchone()
    return None if row is None else int(row[0])


def _resolved(
    db: sqlite3.Connection, found: cohort.Cohort | None, calendar: MarketCalendar
) -> cohort.Cohort | None:
    """A challenger's cohort, its days counted on ``calendar`` as it is now (#473)."""
    return None if found is None else cohort.resolved(db, found, calendar.session)


def _slots_at(db: sqlite3.Connection, moment: datetime) -> list[int]:
    """The challengers just before ``moment``, oldest first: entered a cohort before it, not dropped
    nor the champion by then (``policy.challenger_versions``)."""
    columns = _columns(db, "policies")
    if "admitted_at" not in columns:
        return []
    dropped = " AND (p.dropped_at IS NULL OR p.dropped_at >= :at)" if "dropped_at" in columns else ""
    rows = db.execute(
        "SELECT p.id FROM policies p WHERE p.admitted_at IS NOT NULL AND p.admitted_at < :at"
        f"{dropped} AND p.id NOT IN (SELECT policy_id FROM champions WHERE at < :at) ORDER BY p.id",
        {"at": iso(moment)},
    )
    return [int(r[0]) for r in rows]


def _upto(record: promotion.Record, day: date) -> promotion.Record:
    return promotion.Record(record.version, tuple(d for d in record.days if d.day <= day))


def _standings(
    day: date,
    champion: int | None,
    challengers: Sequence[int],
    records: Mapping[int, promotion.Record],
    of_cohort: Mapping[int, cohort.Cohort | None],
    verdicts: Mapping[int, str],
) -> tuple[Standing, ...]:
    found = {(c.evaluation, c.cycle): c for c in of_cohort.values() if c is not None}
    judged = {j.challenger: j for c in found.values() for j in promotion.judge_cohort(c, records)}
    out = []
    for version in [*([] if champion is None else [champion]), *challengers]:
        record = records.get(version)
        today = None if record is None else next((d for d in record.days if d.day == day), None)
        paper = promotion.paper(record)
        out.append(
            Standing(version, version == champion, today, paper, judged.get(version), verdicts.get(version))
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
        f"SELECT id FROM policies WHERE {column} >= ? AND {column} < ? ORDER BY id", (iso(start), iso(end))
    )
    return tuple(int(r[0]) for r in rows)


def _fitted(db: sqlite3.Connection, start: datetime, end: datetime) -> tuple[Fitted, ...]:
    """The policies kept in the night, each with its status at the night's end."""
    columns = _columns(db, "policies")
    champion = _champion_at(db, end)
    once = {int(r[0]) for r in db.execute("SELECT policy_id FROM champions WHERE at < ?", (iso(end),))}
    out = []
    for row in db.execute(
        "SELECT * FROM policies WHERE created_at >= ? AND created_at < ? ORDER BY id",
        (iso(start), iso(end)),
    ):
        version = int(row["id"])
        dropped = "dropped_at" in columns and row["dropped_at"] is not None and row["dropped_at"] < iso(end)
        admitted = (
            "admitted_at" in columns and row["admitted_at"] is not None and row["admitted_at"] < iso(end)
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
        {"at": iso(end)},
    ).fetchone()
    return int(row[0])


def _capital(db: sqlite3.Connection, mode: str, end: datetime) -> float | None:
    """The capital of the trader's last start before the night's end (``runs``)."""
    row = db.execute(
        "SELECT detail FROM runs WHERE mode = ? AND at < ? ORDER BY id DESC LIMIT 1", (mode, iso(end))
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
    """The champion's trades entered on ``day`` (``store.CHAMPION_TRADE``: the mock book's practice
    trades among them, each ``practice``, #427), in their order."""
    rows = db.execute(
        f"SELECT t.* FROM trades t WHERE t.mode = ? AND substr(t.entry_at, 1, 10) = ? AND {champion_of(db)}"
        " ORDER BY t.entry_at, t.id",
        (mode, day.isoformat()),
    ).fetchall()
    orders: dict[int, list[dict[str, Any]]] = {}
    for o in db.execute(
        "SELECT o.* FROM orders o JOIN trades t ON t.id = o.trade_id"
        " WHERE t.mode = ? AND substr(t.entry_at, 1, 10) = ? ORDER BY o.id",
        (mode, day.isoformat()),
    ):
        orders.setdefault(int(o["trade_id"]), []).append(dict(o))
    fills: dict[int, list[dict[str, Any]]] = {}
    for f in db.execute(
        "SELECT f.* FROM fills f JOIN orders o ON o.id = f.order_id JOIN trades t ON t.id = o.trade_id"
        " WHERE t.mode = ? AND substr(t.entry_at, 1, 10) = ? ORDER BY f.id",
        (mode, day.isoformat()),
    ):
        fills.setdefault(int(f["order_id"]), []).append(dict(f))
    measured = "decided_mid" in _columns(db, "trades")  # #475: a store the trader opened since
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
                practice=bool(r.get("practice")),
                # The champion's book's trades: a KIS day's are KIS's fills (#405).
                execution=execution(r, orders.get(int(r["id"]), []), fills, kis=mode in KIS_MODES)
                if measured
                else None,
            )
        )
    return tuple(out)


def _decisions(
    db: sqlite3.Connection, mode: str, day: date, champion: int | None, practice: bool = False
) -> dict[tuple[str, str | None], int]:
    """What the champion's book did with the predictions it read on ``day``: its decisions are
    under the champion's policy, or none (before the first, or a champion that could not be read);
    those it read on a practice pick (#427, ``selections.practice``) apart, with ``practice``."""
    whose = ""
    params: list[Any] = [mode, day.isoformat()]
    if "policy_id" in _columns(db, "decisions"):
        whose = " AND (d.policy_id IS NULL OR d.policy_id = ?)"
        params.append(-1 if champion is None else champion)
    joined = ""
    if "selection_id" in _columns(db, "decisions") and "practice" in _columns(db, "selections"):
        joined = " LEFT JOIN selections s ON s.id = d.selection_id"
        whose += f" AND coalesce(s.practice, 0) = {int(practice)}"
    elif practice:
        return {}  # a store the trader has not opened since #427: no practice pick
    out: dict[tuple[str, str | None], int] = {}
    for action, reason, filled, count in db.execute(
        "SELECT d.action, d.reason, d.trade_id IS NOT NULL, count(*) FROM decisions d"
        f"{joined} WHERE d.mode = ? AND substr(d.decided_at, 1, 10) = ?{whose} GROUP BY 1, 2, 3",
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
    last["cells"] = json.loads(last["cells"]) if last.get("cells") else []  # #469: the champion's
    return last, len(rows)


def _halts(db: sqlite3.Connection, mode: str, day: date) -> tuple[dict[str, Any], ...]:
    rows = db.execute("SELECT * FROM halts WHERE mode = ? AND day = ? ORDER BY id", (mode, day.isoformat()))
    return tuple(dict(r) for r in rows)


def _capital_of(db: sqlite3.Connection, end: datetime) -> int:
    """The capital of the trader's last start before ``end``, whatever its mode (``runs``); the code's
    without one."""
    row = (
        db.execute("SELECT detail FROM runs WHERE at < ? ORDER BY id DESC LIMIT 1", (iso(end),)).fetchone()
        if "runs" in _tables(db)
        else None
    )
    capital = json.loads(row[0]).get("capital") if row is not None else None
    return capital if isinstance(capital, int) and not isinstance(capital, bool) else CAPITAL


def _ramp_events(db: sqlite3.Connection, start: datetime, end: datetime) -> tuple[tuple[str, datetime], ...]:
    """The live first period's lifts and breaches from ``start`` to ``end`` (#408)."""
    rows = db.execute(
        "SELECT event, at FROM ramp WHERE mode = ? AND event IN (?, ?) AND at >= ? AND at < ? ORDER BY id",
        (LIVE, LIFT, BREACH, iso(start), iso(end)),
    )
    return tuple((str(r[0]), datetime.fromisoformat(r[1])) for r in rows)


def _order_check(db: sqlite3.Connection, mode: str, day: date) -> dict[str, Any] | None:
    """A KIS day's orders against KIS (#408, ``order_checks``)."""
    row = db.execute(
        "SELECT verdict, detail FROM order_checks WHERE mode = ? AND day = ?", (mode, day.isoformat())
    ).fetchone()
    return None if row is None else {"verdict": row[0], "detail": json.loads(row[1])}


def _failures(db: sqlite3.Connection, mode: str, day: date) -> tuple[dict[str, Any], ...]:
    rows = db.execute(
        "SELECT at, what, outcome, code, detail FROM failures WHERE mode = ? AND day = ? ORDER BY id",
        (mode, day.isoformat()),
    )
    return tuple(dict(r) for r in rows)


def _held(db: sqlite3.Connection, mode: str, day: date, end: datetime) -> dict[str, Any] | None:
    """A cumulative halt reached before ``day`` and still holding at the night's ``end``: not re-armed
    by then (a re-arm writes ``lifted_at`` on its row, so a later one does not change the day's)."""
    row = db.execute(
        "SELECT * FROM halts WHERE mode = ? AND kind = ? AND day < ?"
        " AND (lifted_at IS NULL OR lifted_at >= ?) ORDER BY id LIMIT 1",
        (mode, CUMULATIVE_LIMIT, day.isoformat(), iso(end)),
    ).fetchone()
    return None if row is None else dict(row)


def _controls(db: sqlite3.Connection, start: datetime | None, end: datetime) -> tuple[Control, ...]:
    """The pauses, resumes and re-arms asked for from ``start`` (all if None) to ``end``, in order."""
    where, params = "at < ?", [iso(end)]
    if start is not None:
        where, params = "at >= ? AND " + where, [iso(start), *params]
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


def _gap_section(gap: Mapping[str, Any], mode: str = "mock") -> str:
    """A mock or live day's KIS trades against the champion's paper shadow (#405, #408,
    ``gap.day_gap``): ticks and won, no price."""
    mock, shadow = gap["mock"], gap["shadow"]
    name = "KIS 실전" if mode == LIVE else "KIS 모의"
    lines = [
        f"{name}와 그림자 장부 ({'#408' if mode == LIVE else '#405'})",
        f"- {name} 매매 {mock['trades']}건 {_won(mock['net'])} · 그림자(모의 체결) {shadow['trades']}건"
        f" {_won(shadow['net'])} · 차이(KIS − 그림자) {_won(gap['difference'])}",
    ]
    for p in gap["pairs"]:
        cell = f"{_hms(p['bar_close'])[:5]} {p['kind']} {p['predictor']}"
        if p["mock"] is None:
            lines.append(f"- {cell}: 그림자만 샀다({_end(p['shadow'])}) · KIS는 사지 않았다")
        elif p["shadow"] is None:
            lines.append(f"- {cell}: KIS만 샀다({_end(p['mock'])}) · 그림자는 사지 않았다")
        else:
            lines.append(
                f"- {cell}: 진입 {_ticks(p['entry_ticks'])} · 청산 {_ticks(p['exit_ticks'])}"
                f" · {p['mock']['quantity']}주 ↔ {p['shadow']['quantity']}주"
                f" · 순손익 차이 {_won(p['net_difference'])}"
                f" (KIS {_end(p['mock'])}, 그림자 {_end(p['shadow'])})"
            )
    lines.append(
        "- 틱은 KIS 체결가 − 그림자 체결가: 진입은 0보다 크면 KIS가 비싸게 샀고,"
        " 청산은 0보다 크면 KIS가 비싸게 팔았다"
    )
    return "\n".join(lines)


def _end(side: Mapping[str, Any]) -> str:
    reason = side["exit_reason"]
    return "열림" if reason is None else EXITS.get(reason, reason)


def _ticks(value: float | None) -> str:
    return "없음" if value is None else f"{value:+.2f}틱"


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
        else "그날 · 챔피언 없음(첫 정책 전: 연습 말고는 사지 않는다, #469)"
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
        lines += _split_line(ledger)
    champions = [t for t in report.trades if not t.practice]
    practised = [t for t in report.trades if t.practice]
    if champions:
        net = sum(t.net or 0.0 for t in champions)
        lines.append(f"매매 {len(champions)}건(진입 순, 가격은 싣지 않는다), 순손익 합 {_won(net)}:")
        lines += [line for n, t in enumerate(champions, 1) for line in _trade_lines(n, t, report.day)]
    else:
        lines.append("매매 없음.")
    if practised:  # #427: the mock book's practice, apart from the champion's trades
        net = sum(t.net or 0.0 for t in practised)
        lines.append(
            f"연습 매매 {len(practised)}건(KIS 모의, 챔피언 칸이 없던 때 1주씩: 승격·관문·모의 기록·손실"
            f" 한도에는 세지 않고, 계좌 손익·위험 규칙·실행 점검에는 센다), 순손익 합 {_won(net)} ·"
            f" 매매별 줄은 맨 아래 `{PRACTICE_HEADING}`에 따로 둔다(#560)"
        )
    lines += _execution_lines(report)
    lines.append(_decisions_line(report.decisions))
    if report.practice_decisions:
        lines.append(_decisions_line(report.practice_decisions, "연습 칸 고르기의 챔피언 장부"))
    return "\n".join(lines)


def _sources_section(report: Report) -> str:
    """Each decision source's trades that day (#560, ``Source``), a row each: why the day made or lost
    what it did (the fills' ticks against the costs) and whether the calls it traded were right
    against the hit rate they had to beat (p*). Ticks, won and shares; no price."""
    lines = [
        "출처별 손익 (#560): 그날 진입한 매매, 손익은 그 가운데 청산한 것 · 틱은 매매마다 손익을 그 매매의 "
        "틱으로 나눠 더한 것(주 수가 곱해져 있다) · 맞음은 매매한 호출 가운데 답이 오름(상승)인 몫, p*는 그 "
        "호출이 본전인 맞힘률",
        "출처 | 매매 | 체결 손익 | 비용 | 순손익 | 청산 | 맞음 | p*",
    ]
    lines += [_source_row(s) for s in report.sources]
    return "\n".join(lines)


def _source_row(s: Source) -> str:
    if not s.trades:
        return f"{s.name} | 0건 | - | - | - | - | - | -"
    count = f"{s.trades}건" + (f"(열림 {s.open})" if s.open else "")
    if s.open == s.trades:  # none exited: no P&L yet
        money = ["-", "-", "-", "-"]
    else:
        money = [
            _tick_sum(s.gross_ticks),
            f"{s.cost_ticks:.1f}틱 {_amount(s.cost)}",
            f"{_tick_sum(s.net_ticks)} {_won(s.net)}",
            _exits(s.exits),
        ]
    return " | ".join([s.name, count, *money, _hits(s), _p_stars(s.p_stars)])


def _tick_sum(value: float) -> str:
    return f"{value:+.1f}틱"


def _exits(exits: Mapping[str, int]) -> str:
    """How the exited trades ended, as shares of them: the target, the stop, the time, and the rest
    (a pause, a limit) together."""
    total = sum(exits.values())
    if not total:
        return "-"
    shares = [f"{said} {exits.get(reason, 0) / total:.0%}" for reason, said in SOURCE_EXITS.items()]
    other = total - sum(exits.get(reason, 0) for reason in SOURCE_EXITS)
    return ", ".join(shares + ([f"기타 {other / total:.0%}"] if other else []))


def _hits(s: Source) -> str:
    """The calls traded that were right: the share whose answer was the rise, and each class's count."""
    if s.answers is None:
        return "?(엔진 DB를 읽지 못함)"
    answered = sum(s.answers.values())
    unanswered = f", 답 없음 {s.unanswered}" if s.unanswered else ""
    if not answered:
        return f"답 없음 {s.unanswered}"
    up, flat, down = (s.answers.get(c, 0) for c in (UP, FLAT, DOWN))
    return f"{up / answered:.0%}(상승 {up}·보합 {flat}·하락 {down}{unanswered})"


def _p_stars(values: Sequence[float]) -> str:
    if not values:
        return "?"
    return f"{values[0]:.3f}" if len(values) == 1 else f"{values[0]:.3f}–{values[-1]:.3f}"


def _split_line(ledger: Mapping[str, Any]) -> list[str]:
    """A day's P&L three ways (#533), when the mock practice traded or holds a cumulative P&L: the
    account's (KIS) is what the loss limits count plus the practice's, which they do not."""
    split = day_split(ledger)
    if split is None or not any(split["practice"].values()):
        return []
    counted, practice, account = split["counted"], split["practice"], split["account"]
    return [
        f"손익 셋 (#533): 계좌(KIS) {_won(account['net'])}(매매 {account['trades']}건) = 한도가 세는 것 "
        f"{_won(counted['net'])}({counted['trades']}건) + 연습 {_won(practice['net'])}"
        f"({practice['trades']}건) · 누적 계좌 {_won(account['cumulative'])} = 한도가 세는 것 "
        f"{_won(counted['cumulative'])} + 연습 {_won(practice['cumulative'])}. 연습 매매의 손익은 손실"
        " 한도에 세지 않는다(위 장부의 순손익·최저·누적은 한도가 세는 것이다)"
    ]


def _trade_lines(n: int, t: Trade, day: date) -> list[str]:
    """A trade's line, and under it its execution cost (#475), when the store has it."""
    executed = _executed(t.execution)
    return [_trade_line(n, t, day), *([] if executed is None else [f"   실행: {executed}"])]


def _executed(found: Mapping[str, Any] | None) -> str | None:
    """A trade's legs' adverse ticks and delays (#475, ``store.execution``): ticks and seconds, no
    price."""
    if found is None:
        return None
    entry, out = found["entry"], found["exit"]
    parts = [
        f"매수 {_versus(entry['ticks'], '결정 때 중간가')} · 결정 뒤 주문 {_secs(entry['placed'])}, "
        f"체결 {_secs(entry['filled'])}"
    ]
    if out is None:
        parts.append("매도 아직")
    elif out["reference"] == MID:
        parts.append(
            f"매도({out['reason']}) {_versus(out['ticks'], '결정 때 중간가')} · 결정 뒤 주문 "
            f"{_secs(out['placed'])}, 체결 {_secs(out['filled'])}"
        )
    else:  # a take-profit: the limit at its target, resting from the buy
        parts.append(
            f"매도({out['reason']}) {_ticks(out['ticks'])}(목표가 대비: 매수 뒤 걸어 둔 지정가) · "
            f"매수 체결 뒤 주문 {_secs(out['placed'])}"
        )
    return " · ".join(parts)


def _versus(ticks: float | None, reference: str) -> str:
    """A leg's adverse ticks from ``reference``; without it (no quote known at the decision), so."""
    return f"?({reference} 없음)" if ticks is None else f"{_ticks(ticks)}({reference} 대비)"


def _execution_lines(report: Report) -> list[str]:
    """The day's execution cost (#475, spec #468 8 "실행 비용"): each leg's mean adverse ticks and delay
    to its fill, and whose clock the times are on."""
    found = [t.execution for t in report.trades if t.execution is not None]
    if not found:
        return []
    kis = any(e["clock"] == TRADER_CLOCK for e in found)
    clock = (
        "시각은 trader가 KIS 주문 체결 조회에서 체결을 읽은 때다(KIS가 알린 체결 시각이 아니다)"
        if kis
        else "시각은 체결한 호가를 받은 때다(모의 체결: collector가 받은 호가 시각)"
    )
    bought = [e["entry"] for e in found]
    sold = [e["exit"] for e in found if e["exit"] is not None]
    at_mid = [x for x in sold if x["reference"] == MID]
    at_target = [x for x in sold if x["reference"] != MID]
    parts = [
        f"매수 {_mean(bought, 'ticks', _ticks)}, 결정 뒤 체결 {_mean(bought, 'filled', _secs)}"
        f"({len(bought)}건)"
    ]
    if at_mid:
        parts.append(
            f"손절·시간·정지·한도 매도 {_mean(at_mid, 'ticks', _ticks)}, 결정 뒤 체결 "
            f"{_mean(at_mid, 'filled', _secs)}({len(at_mid)}건)"
        )
    if at_target:
        parts.append(f"익절 목표가 대비 {_mean(at_target, 'ticks', _ticks)}({len(at_target)}건)")
    return [
        "실행 비용 (#475): 틱은 불리한 쪽이 +(매수는 결정 때 중간가보다 비싸게, 매도는 싸게 체결; 익절은 "
        f"목표가 대비), 지연은 결정부터 · {clock}",
        "- 평균: " + " · ".join(parts),
    ]


def _mean(legs: Sequence[Mapping[str, Any]], key: str, said: Any) -> str:
    """The mean of ``key`` over the ``legs`` that have it, as ``said`` says it; "없음" without any."""
    values = [x[key] for x in legs if x.get(key) is not None]
    return said(sum(values) / len(values)) if values else "없음"


def _secs(value: float | None) -> str:
    return "?" if value is None else f"{value:.1f}초"


def _trade_line(n: int, t: Trade, day: date) -> str:
    exit_ = (
        "열림(아직 청산하지 않았다)"
        if t.exit_at is None
        else f"{_when(t.exit_at, day)} {EXITS.get(t.reason or '', t.reason)}"
    )
    moved = "" if t.ticks is None else f" · {t.ticks:+g}틱"
    return (
        f"{n}. {_when(t.entry_at, day)} → {exit_} · {_cell(t.kind, t.predictor, t.role)} · "
        f"오름 {t.probability:.2f} ≥ θ {t.threshold:g} · "
        f"p* {_ratio(t.p_star)}, 하한 {_ratio(t.low)} · "
        f"{t.quantity}주{moved} · 비용 {_amount(t.cost)} · 순손익 {_won(t.net)}"
    )


def _decisions_line(decisions: Mapping[tuple[str, str | None], int], whose: str = "챔피언의 장부") -> str:
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
    return f"읽은 예측({whose}): " + " · ".join(parts)


def _pick_section(report: Report) -> str:
    pick = report.pick
    if pick is None:
        return "\n".join(
            ["칸 고르기: 그날 고른 기록이 없다(trader가 그날 돌지 않았다)", *_practice_line(report)]
        )
    head = (
        f"칸 고르기: {_when(pick['at'], report.day)}"
        f"(그날 고르기 {report.picks}번 가운데 장 마감 전 마지막) · "
        f"비용 지도 as_of {pick['as_of'] or '없음'}"
    )
    lines = [head, *_practice_line(report)]
    chosen = pick["picks"]
    if pick.get("practice") and chosen:  # #427: no champion cell to trade, the mock book practised one
        practised = chosen[0]
        cell = _cell(practised["kind"], practised["predictor"], practised["role"])
        shares = f"{practised.get('quantity') or 1}주씩"
        if practised.get("threshold") is None:  # a pick before #474: the cost map's at each pick
            low, p_star = practised["low"], practised["p_star"]
            lines.append(
                f"연습 칸(KIS 모의, KRX_TRADER_MOCK_PRACTICE): {cell}: 판정이 있는 칸 가운데 하한 − p*가 "
                f"가장 큰 칸 · 하한 {_ratio(low)} − p* {_ratio(p_star)} = {low - p_star:+.3f} ·"
                f" {practised['calls']}건 · θ 기본값 · {shares}"
            )
        else:
            lines.append(
                f"연습 칸(KIS 모의, KRX_TRADER_MOCK_PRACTICE): {cell}: 검토 주기에 고정한 칸(#474) · "
                f"θ {practised['threshold']:g} · {shares}(승격·관문·모의 기록에는 세지 않는다)"
            )
        chosen = []
    if chosen:
        lines.append(f"매매할 칸 {len(chosen)}(순위 순):")
        for n, p in enumerate(chosen, 1):
            theta = "기본값" if p.get("threshold") is None else f"{p['threshold']:g}"
            # #471: a loss budget sizes the buy; a pick kept before it bought a fraction of the capital.
            sized = f"비율 {p['fraction']:.2f}" if p.get("fraction") is not None else "손실 예산"
            lines.append(
                f"{n}. {_cell(p['kind'], p['predictor'], p['role'])}: {_pick_figures(p)} · θ {theta} · "
                + sized
            )
        rested = _rested(pick)
        if rested:  # #603: the policy's cells that bought nothing beside them, and why
            lines.append(f"사지 않은 정책의 칸: {rested}")
        return "\n".join(lines)
    lines.append(f"매매할 칸 없음: {_no_cell(report, pick)}")
    lines += _champion_fit(report, pick["reason"])
    if pick["considered"]:
        lines.append("비용 지도가 본 칸(그 판정은 #480 뒤로 매매를 정하지 않는다):")
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


def _no_cell(report: Report, pick: Mapping[str, Any]) -> str:
    """Why no cell traded (#560): what held it, not only the pick's name for it. Since #480 a champion's
    cells trade whatever the cost map's verdicts (``policy.apply``), so the pick's ``no_pass`` or
    ``not_in_policy`` means the champion had no cell to trade: it has none, or each was held (a
    predictor gone, #469)."""
    reason = pick["reason"]
    if reason in (NO_COST_MAP, NO_POLICY):
        detail = f": {pick['detail']}" if pick.get("detail") else ""
        return f"{NO_CELL[reason]}{detail}({reason})"
    policy = report.champion_policy
    if reason not in (NO_PASS, NOT_IN_POLICY) or policy is None:
        return f"{NO_CELL.get(reason, reason or '?')}({reason})"
    if not policy.cells:
        return (
            f"챔피언 정책 {policy.version}에 칸이 없다(빈 정책 · 고르기의 기록은 {reason}이나, #480 뒤로 "
            "비용 지도의 판정은 매매를 정하지 않는다)"
        )
    held = _rested(pick)
    return f"챔피언 정책 {policy.version}의 칸 {len(policy.cells)}개가 그날 매매하지 않았다" + (
        f": {held}({reason})" if held else f"({reason})"
    )


def _rested(pick: Mapping[str, Any]) -> str:
    """The pick's policy cells that did not trade, each with why (#469: ``gone``; #603:
    ``predictor_inactive``, read and never bought)."""
    return ", ".join(
        f"{_cell(c['kind'], c.get('predictor'), c.get('role'))} {c.get('why') or '?'}"
        for c in pick.get("cells") or []
        if not c.get("traded")
    )


def _champion_fit(report: Report, reason: str | None) -> list[str]:
    """When the champion had no cell (#560): the night it was kept, and what that night's fit left out
    and why (the re-fit's own ``few_days``, ``few_trades``, ``no_edge``)."""
    policy = report.champion_policy
    if policy is None or policy.cells or reason not in (NO_PASS, NOT_IN_POLICY):
        return []
    head = f"- 챔피언 정책 {policy.version}: {policy.created_at.astimezone(KST):%Y-%m-%d %H:%M}에"
    if policy.reason == BOOTSTRAP:
        return [f"{head} 빈 챔피언으로 두었다({BOOTSTRAP})"]
    if not policy.left_out:
        return [f"{head} 맞춘 정책: 비용 지도에 이름이 있는 칸이 없었다"]
    return [
        f"{head} 맞춘 정책: 그 밤 맞추기가 칸을 남기지 않았다 · "
        + _left_out_said(policy.left_out, policy.fit.get("grid"))
    ]


def _left_out_said(left_out: Sequence[Mapping[str, Any]], grid: Mapping[str, Any] | None) -> str:
    """The cells a fit left out, by why (#560, ``refit.judge``): how many, and what each why fell
    short of: the recorded days (``few_days``) or the trades bought alone a share (``few_trades``)."""
    grid = grid or {}
    by: dict[str, list[Mapping[str, Any]]] = {}
    for x in left_out:
        by.setdefault(str(x.get("why")), []).append(x)
    parts = []
    for why, found in by.items():
        part = f"{why} {len(found)}"
        if why == FEW_DAYS:
            days = sorted(int(x["recorded_days"]) for x in found if isinstance(x.get("recorded_days"), int))
            if days:
                span = f"{days[0]}" if days[0] == days[-1] else f"{days[0]}–{days[-1]}"
                part += f"(녹화한 날 {span}일, {grid.get('min_days') or MIN_DAYS}일 필요)"
        elif why == FEW_TRADES:
            most = [int(x["most_trades"]) for x in found if isinstance(x.get("most_trades"), int)]
            if most:
                needed = grid.get("min_trades") or MIN_TRADES
                part += f"(1주씩 잰 체결이 가장 많을 때 {max(most)}건, {needed}건 필요)"
        parts.append(part)
    return f"뺀 칸 {len(left_out)}: " + ", ".join(parts)


def _pick_figures(p: Mapping[str, Any]) -> str:
    """A pick's cost map figures (#472: a champion's cell trades whatever they are, off the map too)."""
    low, p_star = p.get("low"), p.get("p_star")
    if low is None and p_star is None:
        return "비용 지도에 없음"
    margin = "" if low is None or p_star is None else f" = {low - p_star:+.3f}"
    return f"하한 {_ratio(low)} − p* {_ratio(p_star)}{margin} · {p.get('calls') or 0}건"


def _practice_line(report: Report) -> list[str]:
    """The mock practice cell kept for the day's review cycle (#474), if one is."""
    kept = report.practice
    if kept is None:
        return []
    cell = _cell(kept.pick.kind, kept.pick.predictor, kept.pick.role)
    return [
        f"연습 칸 (#474): 주기 {kept.cycle}에 고정 · {cell} · θ {kept.threshold:g}"
        f"({kept.chosen_at.astimezone(KST):%Y-%m-%d %H:%M}에 고름) · {practice.figures(kept.chosen)}"
        f"{practice.resting(kept)}"
    ]


def _cycle_line(report: Report) -> list[str]:
    """The day's review cycle (#469): the champion changes on its review night alone."""
    c = report.cycle
    if c is None:
        return []
    place = "첫 주기 전" if c.day is None else f"그날 {c.day}일째"
    tonight = " · 그날 밤이 검토다" if c.day == c.days else ""
    return [
        f"검토 주기 (#469): 평가 버전 {c.evaluation} · 주기 {c.number}({c.first}부터, 검토 날 {c.review}) · "
        f"{place}{tonight} · 챔피언은 검토 날 장이 끝난 뒤에만 바뀐다(판정은 그날 밤)"
    ]


def _standings_section(report: Report) -> str:
    if not report.standings:
        return "\n".join([*_cycle_line(report), "정책별 모의 성적: 정책이 없다(첫 밤 정책 맞추기 전)"])
    lines = [*_cycle_line(report), "정책별 모의 성적 (그날까지의 기록)"]
    for s in report.standings:
        who = f"챔피언 정책 {s.version}" if s.champion else f"도전 정책 {s.version}"
        today = (
            "그날 돌지 않았다"
            if s.today is None
            else f"그날 매매 {s.today.trades}건, 순손익 {_won(s.today.net)}"
            + (" (멈춘 날: 정지나 주문 실패)" if s.today.stopped else "")
        )
        p = s.paper
        line = (
            f"- {who}: {today} · 기록 {p['days']}거래일, 매매 {p['trades']}건, 순손익 {_won(p['net'])}, "
            f"날마다 평균 {_won(p['mean'])}, 하한 {_won(p['low'])}"
        )
        j = s.judgment
        if j is not None:
            line += (
                f" · 챔피언 정책 {j.champion}과 함께 돈 거래일 {j.days}일, "
                f"(도전 − 챔피언) 평균 {_won(j.mean)}, 하한 {_won(j.low)}"
                f"(신뢰수준 {j.level:.2%}, 묶음 안 Bonferroni) · "
                + (
                    f"{STANDS[j.why]} · 검토 날 {j.review} 밤에 한 번 판정한다"
                    if s.verdict is None
                    else f"그 밤 판정: {promotion.VERDICTS.get(s.verdict, s.verdict)}"
                )
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
    for judged, v in report.judged:
        figures = ", ".join(
            f"정책 {m['version']} {promotion.VERDICTS.get(str(m['verdict']), m['verdict'])}"
            + ("" if m.get("low") is None else f"(하한 {_won(m['low'])}, {m['days']}일)")
            for m in v.members
        )
        lines.append(
            f"- 묶음 판정 (#473): 주기 {judged.cycle}({judged.first}–{judged.review}) · "
            f"챔피언 정책 {judged.champion}과 견줌 · " + (figures or "도전 정책 없음")
        )
    if report.dropped:
        lines.append(
            "- 내린 정책(묶음에서 뽑히지 않았거나 묶음에 들지 못함, 다시 돌지 않는다): "
            + ", ".join(f"정책 {v}" for v in report.dropped)
        )
    for formed in report.formed:
        lines.append(
            f"- 새 묶음 (#473): 주기 {formed.cycle}({formed.first}–{formed.review}) · "
            + (", ".join(f"정책 {v}" for v in formed.members) or "도전 정책 없음")
            + f" · 비교할 챔피언 정책 {formed.champion} · 검토 날 {formed.review} 밤에 한 번 판정한다"
        )
    for f in report.fitted:
        status = POLICY_STATUSES.get(f.status, f.status)
        if f.error is not None:
            lines.append(f"- 맞춘 정책 {f.version}({status}): 읽지 못했다({f.error})")
            continue
        cells = (
            " · ".join(
                f"{n}. {_cell(c.kind, c.predictor, c.role)} θ {c.threshold:g}" + _simulated(c.fitted)
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
        lines.append(
            f"- 다음 묶음을 기다리는 정책: {report.waiting}개(검토 날 밤에 새것부터 셋까지 들어간다)"
        )
    if report.refit is not None:
        if report.refit:
            lines.append(f"- 밤 정책 맞추기: 실패(종료 코드 {report.refit}). 까닭은 아래 출력에 있다")
        elif not report.fitted:
            lines.append(_not_kept(report.tonight))
    if len(lines) == 1:
        lines.append("- 바뀐 것 없음")
    return "\n".join(lines)


def _not_kept(tonight: Refitted | None) -> str:
    """The night's fit that kept no new policy (#560): the policy it equals, or the whole replay that
    did not pass (#472), with the cells it left out and why; without the fit (a report not made with
    it), the two causes it may be."""
    head = "- 밤 정책 맞추기: 새 정책을 저장하지 않았다"
    if tonight is None:
        return (
            f"{head}(맞춘 값이 챔피언, 도는 도전 정책이나 기다리는 정책과 같거나, #472: 정책 전체의 재생이 "
            "하한을 넘지 못했다. 까닭은 아래 출력에 있다)"
        )
    cells = f"넣은 칸 {len(tonight.cells)}개"
    left = "" if not tonight.left_out else " · " + _left_out_said(tonight.left_out, tonight.fit.get("grid"))
    if tonight.rejected is not None:
        why = (
            f"{cells}로 짠 정책 전체의 재생에서 날마다 순손익의 95% 하한이 0을 넘지 않았다"
            f"(#472, {tonight.rejected})"
        )
    elif tonight.same_as is not None:
        whose = (
            "챔피언"
            if tonight.same_as == tonight.champion_version
            else "도는 도전 정책"
            if tonight.same_as in tonight.challengers
            else "기다리는 정책"
        )
        why = f"맞춘 값({cells})이 {whose} 정책 {tonight.same_as}과 같다"
    else:  # kept, but not in the night's policies (a clock off the night's): said as it is
        return f"- 밤 정책 맞추기: 정책 {tonight.version}을 저장했다({cells}){left}"
    return f"{head}: {why}{left}"


def _simulated(fitted: Mapping[str, Any], title: str = "") -> str:
    """A fit's simulated trades and net P&L, when it has them."""
    trades, net = fitted.get("trades"), fitted.get("net")
    if not isinstance(trades, int) or not isinstance(net, int | float):
        return ""
    head = f"{title} " if title else ""
    return f" ({head}모의 {trades}건, 순손익 {_won(float(net))})"


def _gate_line(report: Report) -> str:
    g = report.gate
    lines = [promotion.checkpoint_line(c) for c in report.checked]
    if g.policy is None:
        lines.append(f"실전 관문: 챔피언이 없다(모의 이력 {g.history}거래일, 다음 판정 {g.next_day}거래일째)")
        return "\n".join(lines)
    line = promotion.gate_line(g)
    if report.gate_passed:
        line += (
            " · 그 밤 처음 넘었다(넘어도 저절로 실전이 되지 않는다: 모드는 사용자가 서버에서 바꾸고,"
            " live 모드는 실행 점검도 넘어야 주문한다, #408)"
        )
    lines.append(line)
    return "\n".join(lines)


def _live_section(report: Report) -> str:
    """What the live mode needs (#408): the execution checks on the mock days, a KIS day's comparison
    of its orders with KIS's, and on a live day the first period. Counts, ticks and won, no price."""
    lines = ["실전 준비 (#408)"]
    found = report.readiness
    if found is not None:
        period = found.period
        span = (
            f"실행 점검은 KIS 모의 기간 {period[0]}–{period[-1]}({len(period)}거래일)로 본다"
            if period
            else "KIS 모의 거래일이 아직 없다"
        )
        verdict = "실전 주문 가능" if found.unlocked else "아직 실전 주문 안 함"
        lines.append(f"- 다음 거래일 08:30 고르기의 판단: {verdict} · {span}")
        lines.append(f"- {'✓' if found.gate.passed else '✗'} {found.gate_said()}")
        lines += [f"- {'✓' if c.passed else '✗'} {c.said}" for c in found.checks]
    check = report.order_check
    if check is not None:
        verdict = {"same": "같다", "different": "다르다", "no_orders": "견줄 주문이 없다"}.get(
            check["verdict"], check["verdict"]
        )
        detail = check["detail"]
        lines.append(
            f"- 그날 주문의 KIS 대조: {verdict}(장부 {detail.get('orders', 0)}건,"
            f" 같은 것 {detail.get('same', 0)}건, KIS에 없는 것 {detail.get('missing', 0)}건,"
            f" 장부에 없는 것 {detail.get('extra', 0)}건)"
        )
    elif report.mode in KIS_MODES:
        lines.append(
            "- 그날 주문의 KIS 대조: 아직 없다(장 마감 뒤 trader가 견준다. 견주지 못한 날은 통과가 아니다)"
        )
    ramp = report.ramp
    if ramp is not None:
        if ramp.lifted_at is not None:
            lines.append(
                f"- 실전 운용 상한 {ramp.cap:,}원(첫 단계를 {_hms(ramp.lifted_at.isoformat())}에 마침)"
            )
        else:
            lines.append(
                f"- 실전 첫 단계: 운용 상한 {ramp.cap:,}원, 실전 {len(ramp.days)}/{live.RAMP_DAYS}거래일,"
                f" 왕복 매매 {ramp.trips}/{live.RAMP_TRIPS}건"
                + (" · 점검을 벗어나 새 진입이 멈춰 있다(krx-quant trade resume)" if ramp.breached_at else "")
            )
            lines += [f"  - {'✓' if c.passed else '✗'} {c.said}" for c in ramp.checks]
    for event, at in report.ramp_events:
        what = "운용 상한을 올렸다" if event == LIFT else "점검을 벗어나 새 진입을 멈췄다"
        lines.append(f"- 그날 {_hms(at.isoformat())}: 실전 첫 단계의 {what}")
    return "\n".join(lines)


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
    lines += _risk_lines(report.risk)
    for f in report.failures:  # #405: KIS's code and message, never an order or account number
        lines.append(
            f"- KIS 주문 실패: {_hms(f['at'])} {FAILED_ORDERS.get(f['what'], f['what'])} · "
            f"{FAILED_OUTCOMES.get(f['outcome'], f['outcome'])}({f['code'] or '-'}: {f['detail']})"
            " · 그날 새 진입 정지"
        )
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


def _risk_lines(risk: Risk | None) -> list[str]:
    """The risk rules that day (#471, #475): the loss budget, the buy orders and their limit, the
    cooldowns begun and the net losses in a row at the day's last exit; won, counts and times."""
    if risk is None:
        return []
    rules = risk.rules
    minutes = f"{rules.cooldown.total_seconds() / 60:g}"
    rested = (
        "그날 쉼 없음"
        if not risk.cooldowns
        else f"그날 쉼 {len(risk.cooldowns)}번("
        + ", ".join(f"{_hms(start)}–{_hms(end)}" for start, end in risk.cooldowns)
        + ")"
    )
    return [
        f"- 위험 규칙 (#471): 매매마다 손실 예산 {_amount(rules.budget(risk.capital))}(운용 상한 "
        f"{_amount(risk.capital)}의 {rules.risk_percent:g}%) · 그날 매수 주문 {risk.buys}/{rules.daily_buys}"
        + (" (한도에 닿아 그 뒤 사지 않았다)" if risk.buys >= rules.daily_buys else "")
        + f" · 순손실 {rules.loss_streak}번 연달으면 {minutes}분 쉰다: {rested} · 그날 마지막 청산까지 "
        f"연달은 순손실 {risk.losses}번"
    ]


def _source(source: str) -> str:
    return {"api": "화면", "cli": "서버", "reconcile": "계좌 맞추기"}.get(source, source)


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
        split = day_split(r)
        if split is not None and any(split["practice"].values()):  # #533: apart, never in the limits
            practice = split["practice"]
            line += f" · 연습 {practice['trades']}건 {_won(practice['net'])}(한도에 세지 않음)"
        if r["halt"]:
            line += f" · {LIMITS.get(r['halt'], r['halt'])}에 닿음"
        lines.append(line)
    return "\n".join(lines)
