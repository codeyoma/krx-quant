"""The trader's limits and pauses (#393 "한도와 정지", #396): what stops it buying, what sells at
once, and what lets it go on. The policy never touches them (#399): they come from the code and
the server's ``.env`` alone, which may narrow them, never widen them (``settings.py``).

**Whose.** The limits and the pauses hold the trades the trader makes: the champion policy's
(#399; before the first champion, the defaults'). The loss counts those alone
(``store.CHAMPION_TRADE``), never a challenger's paper trades (#400) nor the champion's paper
shadow's in mock mode (#405). No pause holds a challenger's book nor a shadow's: each is a simulation
of its policy's rules, which a promotion compares with the champion's (``promotion.py``), and since
#471 the loss limits are simulated on its own trades (``Simulated``), so that it is held as the
champion would have been.

**The practice** (#427, mock mode): the mock book's practice trades are the trader's own, but the loss
counts none of them (#533): neither their realized P&L nor a practice position's open one. A limit
the champion's trades reach holds them all the same (the status buys nothing, a practice position is
sold). The account's P&L is both (``realized_all``: the cash buys on it), the practice's kept beside
the limits' count (``practice_day``, ``practice_cumulative``, ``practice_unrealized``) for the state,
the ledger and the report to show apart. The risk rules count the practice as ever (#471,
``paper.risk_held``). The practice shadow's simulated limits count none of its trades either: they
are all practice.

> **바뀜** (2026-11-05, #534): the loss counted the mock book's practice trades with the champion's
> (the same KIS account) → it counts none of them. 이유: the user's decision (2026-11-04): a practice
> may trade many times; at 2026-11-04's pace (−9,140 won in 20 trades) its losses alone would have
> reached the cumulative limit around 2026-12-03 and stopped the champion until a re-arm.

> **바뀜** (2026-11-03, #478): no limit held a challenger's book nor a shadow's (``Unlimited``) → the
> daily and the cumulative loss limits are simulated on its own trades (``Simulated``). 이유: spec #468
> 5 "모든 그림자 장부 … 기존 손실 한도를 흉내 낸다": unheld, they could not be compared with the champion.

**Loss.** A P&L is the net realized one (the trades exited, after their costs) plus the open
position's at the best bid (neither a practice trade's, #533): what selling it at that bid would
net, after the buy's commission and the sale's commission and tax (``paper.unrealized``). The
day's counts from 00:00 KST of that day; the cumulative one from the mode's first start, or from its
last re-arm. A limit is reached when the P&L is at or below its negative: the open position's on
every quote, the realized one at every exit.

- **The daily limit** (``KRX_TRADER_DAILY_LOSS_PERCENT``, 10% of the capital: 100,000 won): the
  trader buys nothing more that day and sells the open position at once. It holds for that day
  only: the next trading day it trades again.
- **The cumulative limit** (``KRX_TRADER_CUMULATIVE_LOSS_PERCENT``, 20%: 200,000 won): the same,
  and it holds until ``krx-quant trade rearm`` on the server, which starts the count again from
  then. A limit reached is kept (``halts``): it holds across restarts, and a later recovery of
  the P&L does not lift it.

**Pauses.** The screen's two buttons (``POST /v1/trader/pause``, the Engine API writing it into
the trader's store, read within ``CONTROL_SECONDS``): ``sell_now`` false pauses new entries (the
open position is sold by its own barriers), true pauses them and sells the open position at once.
Both hold until ``krx-quant trade resume`` on the server; a pause with ``sell_now`` since the last
resume sells, whatever came after it. The API can neither resume nor re-arm. A mock start that finds
the KIS account unlike the ledger (#406, ``mock.py``) pauses new entries the same way (source
``reconcile``, its own notice saying what it found); the resume that lifts it also accepts what is
outside the ledger as the account's (``store.accept_reconciles``). The live mode's first period
(#408, ``live.py``) pauses them the same way on a breach of its checks (source ``ramp``, its own
notice saying which); the resume that lifts it begins a new period (``store.resume_ramp``).

**A failed order** (#405, mock mode): a KIS order refused, one whose outcome is not known (no
answer) or one that could not be sent (``mock.py``) is kept (``failures``) and stops new entries for
the rest of that day (``order_failed``); the open position is sold by its own barriers. The next
day the trader buys again. The first of a day is sent to Telegram, the later ones are logged.

**Status**, the first that holds: ``cumulative_limit``, ``daily_limit``, ``paused`` (sold at
once), ``entries_paused``, ``order_failed``, ``running``. Every status but ``running`` buys
nothing: a prediction that would have bought is skipped with it as its reason, a buy waiting for
its quote is given up.
``paused`` and the two limits sell the open position at the best bid of the first quote received
after the status began (a limit reached on a quote: at that quote's), the exit's reason ``pause``,
``daily_limit`` or ``cumulative_limit``.

Each halt, pause, resume and re-arm is sent to Telegram (``notify``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol

from krx_quant.feeds.base import KST, kst_day
from krx_quant.trader.store import (
    API,
    CLI,
    CUMULATIVE_LIMIT,
    DAILY_LIMIT,
    PAUSE,
    RAMP,
    REARM,
    RECONCILE,
    RESUME,
    Control,
    Failure,
    Halt,
    TraderStore,
)

log = logging.getLogger(__name__)

RUNNING, ENTRIES_PAUSED, PAUSED = "running", "entries_paused", "paused"
ORDER_FAILED = "order_failed"  # a KIS order failed today (#405): no new entry until the next day
STATUSES = (RUNNING, ENTRIES_PAUSED, PAUSED, ORDER_FAILED, DAILY_LIMIT, CUMULATIVE_LIMIT)
# The statuses that sell the open position at once, and the exit's reason each gives.
SALE = {PAUSED: "pause", DAILY_LIMIT: DAILY_LIMIT, CUMULATIVE_LIMIT: CUMULATIVE_LIMIT}
# The trader reads the controls asked for this often (#396: within a second).
CONTROL_SECONDS = 0.5
# It rewrites its state (``snapshot``) at most this often while only the open position's P&L
# moves, and at least this often all the same (a heartbeat: GET /v1/trader's ``alive``).
SNAPSHOT_SECONDS = timedelta(seconds=1)
HEARTBEAT = timedelta(seconds=5)

Notify = Callable[[str], None]


@dataclass(frozen=True)
class Limits:
    """The capital and the loss limits, in percent of it."""

    capital: int
    daily_percent: float
    cumulative_percent: float

    @property
    def daily(self) -> float:
        """The daily limit, won."""
        return self.capital * self.daily_percent / 100

    @property
    def cumulative(self) -> float:
        return self.capital * self.cumulative_percent / 100


@dataclass(frozen=True)
class Pause:
    """The pause in force: since when, whether it sells at once, who asked for it first."""

    at: datetime
    sell_now: bool
    source: str


def pause_of(controls: Iterable[Control]) -> Pause | None:
    """The pause in force after ``controls``, in the order asked: the pauses since the last resume,
    from the first of them, selling at once if any of them does."""
    pause: Pause | None = None
    for c in controls:
        if c.action == PAUSE:
            sell_now = bool(c.sell_now) or (pause is not None and pause.sell_now)
            pause = (
                Pause(c.at, sell_now, c.source) if pause is None else Pause(pause.at, sell_now, pause.source)
            )
        elif c.action == RESUME:
            pause = None
    return pause


# How a notice names the order that failed and what became of it (#405).
_SAID = {"refused": "KIS가 거부했습니다", "unknown": "응답이 없어 결과를 모릅니다"}
_WHAT = {
    "buy": "매수",
    "take_profit": "익절 매도",
    "cancel": "취소",
    "sell": "매도",
    "orderable": "주문 가능 현금 조회",
    "balance": "잔고 조회",  # #460: a live buy's, before it is sent
    "reconcile": "계좌 맞추기",
}


def won(value: float) -> str:
    return f"{value:,.0f}원"


class Guard:
    """``mode``'s limits and pauses on ``store``, from ``now``: its status, and its P&L of the day
    and since the count started, as the trades exit (``exited``) and the open position's bid moves
    (``mark``)."""

    def __init__(self, store: TraderStore, mode: str, limits: Limits, now: datetime, notify: Notify) -> None:
        self.store = store
        self.mode = mode
        self.limits = limits
        self._notify = notify
        self.pause = pause_of(store.controls(applied=True))
        self._first_run = store.first_run(mode)
        self._rearmed_at = store.rearmed_at(mode)
        self.unrealized = 0.0  # the open position's P&L at the last bid, after its costs
        self.practising = False  # the open position is a practice one (#533): no limit counts it
        self.day = kst_day(now)
        self._load()
        self._check(now)  # a limit narrowed below what was realized holds from the start

    @property
    def since(self) -> datetime | None:
        """Where the cumulative count starts: the last re-arm, else the mode's first start."""
        return self._rearmed_at or self._first_run

    def _load(self) -> None:
        """The day's figures, and the cumulative ones, as the store has them: the limits' count and
        the practice's apart (#533)."""
        midnight = datetime.combine(self.day, time(), KST)
        self.realized_day, self.trades_day = self.store.realized(self.mode, midnight)
        self.practice_day, self.practice_trades_day = self.store.realized(self.mode, midnight, practice=True)
        self.realized_cumulative, _ = self.store.realized(self.mode, self._rearmed_at)
        self.practice_cumulative, _ = self.store.realized(self.mode, self._rearmed_at, practice=True)
        # The account's, every trade of the trader's (#427: the practice too): the cash buys on it.
        self.realized_all = self.store.realized(self.mode, None)[0]
        self.realized_all += self.store.realized(self.mode, None, practice=True)[0]
        self.daily_halt, self.cumulative_halt = self.store.halts(self.mode, self.day)
        self.failure: Failure | None = self.store.failure(self.mode, self.day)  # #405
        kept = self.store.day(self.mode, self.day) or {}
        self.worst = min(0.0, self.realized_day, kept.get("worst", 0.0))
        self.cumulative_worst = min(0.0, self.realized_cumulative, kept.get("cumulative_worst", 0.0))

    @property
    def status(self) -> str:
        if self.cumulative_halt is not None:
            return CUMULATIVE_LIMIT
        if self.daily_halt is not None:
            return DAILY_LIMIT
        if self.pause is not None:
            return PAUSED if self.pause.sell_now else ENTRIES_PAUSED
        if self.failure is not None:
            return ORDER_FAILED
        return RUNNING

    @property
    def halt(self) -> str | None:
        """The limit reached today, if any (the cumulative one first)."""
        if self.cumulative_halt is not None and kst_day(self.cumulative_halt.at) == self.day:
            return CUMULATIVE_LIMIT
        return None if self.daily_halt is None else DAILY_LIMIT

    def roll(self, now: datetime) -> bool:
        """A new day at ``now``: its figures from the store; the last day's daily limit no longer
        holds. Whether it was one."""
        day = kst_day(now)
        if day <= self.day:  # forward only: an exit is never stamped before the day it is counted in
            return False
        if self.store.day(self.mode, self.day) is not None:  # a trading day's ledger: as it ended
            self.store.put_day(self.mode, self.day, self.ledger(), now)
        held = self.daily_halt is not None
        failed = self.failure is not None
        self.day = day
        self._load()
        if held and self.daily_halt is None:
            log.info("trader: a new day (%s): the daily loss limit reached before holds no more", day)
        if failed and self.failure is None:
            log.info("trader: a new day (%s): the failed order of the day before stops entries no more", day)
        self._check(now)
        return True

    def mark(self, unrealized: float, at: datetime, practice: bool = False) -> str | None:
        """The open position's P&L at the bid at ``at``, after its costs (0 with none), a practice
        position's if ``practice`` (#533: no limit counts it): the limit it makes reached, if one is."""
        self.roll(at)
        self.unrealized = unrealized
        self.practising = practice
        return self._check(at)

    def exited(self, net: float, at: datetime, practice: bool = False) -> str | None:
        """A trade exited at ``at`` with ``net``, kept in the store already, a practice one if
        ``practice`` (#533: no limit counts it): the limit its realized P&L makes reached, if one is.
        The caller rolls the day before it keeps the exit (``roll``); if the exit rolls it here
        instead, the day's figures read again hold the exit already."""
        self.unrealized = 0.0
        self.practising = False
        if not self.roll(at):
            if practice:
                self.practice_day += net
                self.practice_trades_day += 1
                self.practice_cumulative += net
            else:
                self.realized_day += net
                self.trades_day += 1
                self.realized_cumulative += net
            self.realized_all += net
        return self._check(at)

    def ledger(self) -> dict[str, Any]:
        """The day's ledger as it stands (``days``' columns): the limits' count, and the practice's
        beside it (#533)."""
        return {
            "realized": self.realized_day,
            "trades": self.trades_day,
            "worst": self.worst,
            "cumulative": self.cumulative_pnl,
            "cumulative_worst": self.cumulative_worst,
            "daily_limit": self.limits.daily,
            "cumulative_limit": self.limits.cumulative,
            "status": self.status,
            "halt": self.halt,
            "practice": self.practice_day,
            "practice_trades": self.practice_trades_day,
            "practice_cumulative": self.practice_cumulative + self.practice_unrealized,
        }

    @property
    def counted_unrealized(self) -> float:
        """The open position's P&L the limits count: none of a practice position's (#533)."""
        return 0.0 if self.practising else self.unrealized

    @property
    def practice_unrealized(self) -> float:
        """A practice position's open P&L (#533), which no limit counts; 0 with none."""
        return self.unrealized if self.practising else 0.0

    @property
    def day_pnl(self) -> float:
        """The day's P&L the daily limit counts (#533: no practice trade's)."""
        return self.realized_day + self.counted_unrealized

    @property
    def cumulative_pnl(self) -> float:
        """The cumulative P&L the cumulative limit counts (#533: no practice trade's)."""
        return self.realized_cumulative + self.counted_unrealized

    def _check(self, at: datetime) -> str | None:
        day, cumulative = self.day_pnl, self.cumulative_pnl
        self.worst = min(self.worst, day)
        self.cumulative_worst = min(self.cumulative_worst, cumulative)
        reached = None
        if self.daily_halt is None and day <= -self.limits.daily:
            self.daily_halt = self._halt(DAILY_LIMIT, at, day, self.realized_day, self.limits.daily)
            reached = DAILY_LIMIT
        if self.cumulative_halt is None and cumulative <= -self.limits.cumulative:
            self.cumulative_halt = self._halt(
                CUMULATIVE_LIMIT, at, cumulative, self.realized_cumulative, self.limits.cumulative
            )
            reached = CUMULATIVE_LIMIT
        return reached

    def _halt(self, kind: str, at: datetime, pnl: float, realized: float, limit: float) -> Halt:
        open_ = self.counted_unrealized
        halt = self.store.add_halt(at, self.mode, kind, pnl, realized, open_, limit)
        what = "하루" if kind == DAILY_LIMIT else "누적"
        percent = self.limits.daily_percent if kind == DAILY_LIMIT else self.limits.cumulative_percent
        # The practice's P&L beside it (#533), which the limit does not count.
        practised = (self.practice_day if kind == DAILY_LIMIT else self.practice_cumulative) + (
            self.practice_unrealized
        )
        aside = f" 연습 매매의 손익 {won(practised)}은 한도에 세지 않습니다." if practised else ""
        until = (
            "다음 거래일에 다시 돕니다."
            if kind == DAILY_LIMIT
            else "서버에서 krx-quant trade rearm으로 다시 켤 때까지 멈춥니다."
        )
        log.warning(
            "trader: the %s loss limit is reached: %.0f won (realized %.0f, open %.0f), the limit -%.0f",
            "daily" if kind == DAILY_LIMIT else "cumulative",
            pnl,
            realized,
            open_,
            limit,
        )
        self._notify(
            f"자동 매매({self.mode}): {what} 손실 한도에 닿았습니다. {what} 손익 {won(pnl)}"
            f"(실현 {won(realized)}, 열린 포지션 {won(open_)}), 한도 -{won(limit)}({percent:g}%).{aside}"
            f" 새로 사지 않고 열린 포지션을 바로 팝니다. {until}"
        )
        return halt

    def order_failed(
        self,
        at: datetime,
        what: str,
        outcome: str,
        detail: str,
        code: str | None = None,
        trade: int | None = None,
        told: bool = False,
    ) -> None:
        """A KIS order of ``what`` (buy, take_profit, cancel, sell, orderable, balance) failed at
        ``at`` (#405): kept, and new entries stop for the rest of the day; the day's first is sent to
        Telegram, unless the caller tells it itself (``told``: a sell whose answer was lost, #460).
        ``detail`` is KIS's code and message or the error, never an account or order number."""
        self.roll(at)
        kept = self.store.add_failure(at, self.mode, what, outcome, detail, code, trade)
        first = self.failure is None
        if first:
            self.failure = kept
        log.error("trader: the %s order %s (%s): %s", what, outcome, code or "-", detail)
        if not first or told:
            return
        said = _SAID.get(outcome, "보내지 못했습니다")
        self._notify(
            f"자동 매매({self.mode}): {_WHAT.get(what, what)} 주문을 {said}({detail})."
            " 오늘은 새로 사지 않고, 들고 있는 것은 원래 규칙(목표가, 손절가, 시간)대로 팝니다."
            " 다음 거래일에 다시 돕니다."
            + (" 결과를 모르는 주문은 KIS 계좌에서 확인하세요." if outcome == "unknown" else "")
        )

    def apply(self, control: Control, at: datetime) -> None:
        """A control read at ``at``: marked applied, and the pause, resume or re-arm made."""
        self.store.applied(control.id, at)
        if control.action == PAUSE:
            self._paused(control)
        elif control.action == RESUME:
            self._resumed(control, at)
        elif control.action == REARM:
            self._rearmed(control, at)
        else:
            log.warning("trader: control %s asks for %r, which it does not know", control.id, control.action)

    def _paused(self, control: Control) -> None:
        sell_now = bool(control.sell_now)
        if self.pause is None:
            self.pause = Pause(control.at, sell_now, control.source)
        else:  # paused already: from the first pause, selling at once if either does
            self.pause = Pause(self.pause.at, self.pause.sell_now or sell_now, self.pause.source)
        who = "화면" if control.source == API else control.source
        log.warning("trader: paused (%s, control %s, sell now: %s)", control.source, control.id, sell_now)
        if control.source in (RECONCILE, RAMP):
            return  # the reconcile (#406) and the live ramp (#408) tell what they found themselves
        if sell_now:
            what = "정지하고 즉시 매도 요청을 받았습니다"
            then = "새로 사지 않고 열린 포지션을 바로 팝니다."
        else:
            what = "새 진입 정지 요청을 받았습니다"
            then = "새로 사지 않고, 열린 포지션은 원래 규칙(목표가, 손절가, 시간)대로 팝니다."
        self._notify(
            f"자동 매매({self.mode}): {what}({who}). {then}"
            " 다시 켜기는 서버의 krx-quant trade resume뿐입니다."
        )

    def _resumed(self, control: Control, at: datetime) -> None:
        if self.pause is None:
            log.info("trader: control %s resumes a trader not paused: nothing to do", control.id)
            return
        self.pause = None
        # What the account holds outside the ledger, as a reconcile found it, is the account's (#406).
        if self.store.accept_reconciles(self.mode, at):
            log.warning(
                "trader: the KIS account as the reconcile found it is accepted (control %s)", control.id
            )
        # A breach of the live mode's first period, reviewed: its period begins again from now (#408).
        if self.store.resume_ramp(at):
            log.warning("trader: the live ramp's breach is reviewed: its first period begins again now")
        log.warning("trader: resumed (control %s): %s", control.id, self.status)
        self._notify(f"자동 매매({self.mode}): 정지를 풀었습니다(krx-quant trade resume). {self._still()}")

    def _rearmed(self, control: Control, at: datetime) -> None:
        if control.mode != self.mode:
            log.info(
                "trader: control %s re-arms %s, not %s: nothing to do", control.id, control.mode, self.mode
            )
            return
        if self.cumulative_halt is None:
            log.info("trader: control %s re-arms a cumulative limit not reached: nothing to do", control.id)
            return
        self.store.lift(self.cumulative_halt.id, at, control.id)
        self.cumulative_halt = None
        self._rearmed_at = at
        self.realized_cumulative = 0.0  # counted again from now
        self.practice_cumulative = 0.0  # and the practice's beside it (#533)
        self.cumulative_worst = min(0.0, self.counted_unrealized)
        log.warning("trader: the cumulative loss limit re-armed (control %s): %s", control.id, self.status)
        self._notify(
            f"자동 매매({self.mode}): 누적 손실 한도를 다시 켰습니다(krx-quant trade rearm)."
            f" 누적 손익을 지금부터 다시 셉니다. {self._still()}"
        )

    def _still(self) -> str:
        """What holds now, in a notice."""
        status = self.status
        if status == RUNNING:
            return "다시 삽니다."
        if status == DAILY_LIMIT:
            return "오늘은 하루 손실 한도로 멈춰 있고, 다음 거래일에 다시 돕니다."
        if status == CUMULATIVE_LIMIT:
            return "누적 손실 한도로 멈춰 있습니다: krx-quant trade rearm."
        if status == ORDER_FAILED:
            return "오늘은 주문 실패로 새로 사지 않고, 다음 거래일에 다시 돕니다."
        return "정지 요청으로 멈춰 있습니다: krx-quant trade resume."


class Holds(Protocol):
    """What holds a book's trades (``paper.PaperTrader``): the champion's ``Guard``, a challenger's
    or a shadow's ``Simulated``."""

    unrealized: float

    @property
    def status(self) -> str: ...

    @property
    def realized_all(self) -> float: ...

    def roll(self, now: datetime) -> bool: ...

    def mark(self, unrealized: float, at: datetime, practice: bool = False) -> str | None: ...

    def exited(self, net: float, at: datetime, practice: bool = False) -> str | None: ...


class Simulated:
    """The loss limits of a challenger's paper book (#400), the champion's paper shadow (#405) or the
    practice shadow (#427), simulated (#471, module doc "Whose"): the champion's limits (``Guard``'s
    rules: the P&L realized and open at the best bid, after its costs, the day's from 00:00 KST and the
    cumulative one's from where it was given, ``realized_day`` and ``realized_cumulative``) on the
    book's own trades, never a practice one's (#533: the practice shadow's count nothing). A limit
    reached holds its book as the champion's: its status (``daily_limit``, ``cumulative_limit``) buys
    nothing and sells the open position at once; the daily one ends with its day, the cumulative one
    with the trader's re-arm (``rearm``). Nothing of it is kept nor told: it is read again from the
    book's trades at a start, and a new day's P&L from them too (``day_realized``, as ``Guard.roll``
    reads the store; none: from 0). No pause holds it. It buys on the paper account's cash as it
    stands with nothing open (``account``: the capital plus the champion's realized P&L, the cash the
    trader buys on), so that its size is the one it would have as the champion."""

    def __init__(
        self,
        limits: Limits,
        day: date,
        realized_day: float = 0.0,
        realized_cumulative: float = 0.0,
        account: Callable[[], float] = lambda: 0.0,
        day_realized: Callable[[date], float] | None = None,
    ) -> None:
        self.limits = limits
        self.day = day
        self.realized_day = realized_day
        self.realized_cumulative = realized_cumulative
        self._account = account
        self._day_realized = day_realized
        self.unrealized = 0.0
        self.daily_halt = False
        self.cumulative_halt = False
        self._check()  # a loss realized beyond a limit already holds from the start

    @property
    def status(self) -> str:
        if self.cumulative_halt:
            return CUMULATIVE_LIMIT
        return DAILY_LIMIT if self.daily_halt else RUNNING

    @property
    def realized_all(self) -> float:
        return self._account()

    def roll(self, now: datetime) -> bool:
        """A new day at ``now``: its P&L as the book's trades of it have it (``day_realized``, else 0),
        the daily limit lifted unless that P&L reaches it. Whether it was one. The caller rolls the day
        before it keeps an exit (``exited`` adds it)."""
        day = kst_day(now)
        if day <= self.day:
            return False
        self.day = day
        self.realized_day = 0.0 if self._day_realized is None else self._day_realized(day)
        self.daily_halt = False
        self._check()
        return True

    def mark(self, unrealized: float, at: datetime, practice: bool = False) -> str | None:
        """The open position's P&L at the bid at ``at`` (none counted of a practice one, #533): the limit
        it makes reached, if one is."""
        self.roll(at)
        self.unrealized = 0.0 if practice else unrealized
        return self._check()

    def exited(self, net: float, at: datetime, practice: bool = False) -> str | None:
        """A trade exited at ``at`` with ``net`` (not counted if a practice one, #533): the limit its
        realized P&L makes reached, if one is."""
        self.roll(at)
        self.unrealized = 0.0
        if not practice:
            self.realized_day += net
            self.realized_cumulative += net
        return self._check()

    def rearm(self) -> None:
        """The trader's cumulative limit re-armed: the cumulative P&L counted again from now."""
        self.cumulative_halt = False
        self.realized_cumulative = 0.0

    def _check(self) -> str | None:
        """As ``Guard._check``: a limit is reached when the P&L is at or below its negative."""
        reached = None
        if not self.daily_halt and self.realized_day + self.unrealized <= -self.limits.daily:
            self.daily_halt = True
            reached = DAILY_LIMIT
        if not self.cumulative_halt and self.realized_cumulative + self.unrealized <= -self.limits.cumulative:
            self.cumulative_halt = True
            reached = CUMULATIVE_LIMIT
        return reached


def ask(store: TraderStore, action: str, mode: str, at: datetime) -> Control | str:
    """``krx-quant trade resume|rearm`` (#396): the control written for the trader to read, or why
    none is (nothing to resume or re-arm). A resume needs a pause in force (asked for, read yet or
    not); a re-arm, ``mode``'s cumulative limit reached and no re-arm of it waiting to be read."""
    controls = store.controls()
    if action == RESUME:
        if pause_of(controls) is None:
            return "the trader is not paused: nothing to resume"
        return store.add_control(at, RESUME, CLI)
    if action == REARM:
        _, cumulative = store.halts(mode, kst_day(at))
        if cumulative is None:
            return f"the {mode} trader has not reached its cumulative loss limit: nothing to re-arm"
        if any(c.action == REARM and c.mode == mode and c.applied_at is None for c in controls):
            return f"a re-arm of {mode} is waiting for the trader to read it already"
        return store.add_control(at, REARM, CLI, mode=mode)
    raise ValueError(f"not a control the server asks for: {action!r}")
