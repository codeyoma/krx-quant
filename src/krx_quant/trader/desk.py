"""The trader's books (#400, #393 "정책과 자기 개선"): the champion's and each challenger's, on the
same cells' predictions and the same quotes, in one process.

- **Picks** (``select``, at the trader's start and every day at 08:30): the champion read from the
  store, applied to the cells the cost map passes (``under_champion``, ``policy.apply``), for the
  champion's book (``paper.PaperTrader``: the trades the trader makes, which its loss limits and
  pauses hold; in mock mode ``mock.MockTrader``, #405, its orders on KIS's mock server); and each
  challenger that runs that day (``policy.running_versions``: at most
  three, kept before that day's 08:30, so one fitted tonight runs from the next trading day)
  applied to the same cells, for its own paper book. A challenger book holds no limit nor pause
  (``limits.Unlimited``) and buys on the paper account's cash with nothing open, as the champion's
  book would. A challenger that no longer runs (dropped for a newer one, promoted) is retired: its
  book buys nothing more and is let go once it holds nothing. A champion that cannot be read
  trades nothing until the next pick (``NO_POLICY``); a challenger that cannot be read does not
  run until then.
- **Predictions** (``on_signal``): each to every book trading its cell; one of a cell no book
  trades (the picks changed while it was read) to the champion's, which keeps it ``not_picked``.
  The reader follows every book's cells (``cells``).
- **Quotes, special windows, the clock**: to every book. **Controls** (pauses, resumes, re-arms):
  to the champion's alone.
- **A stall of the quotes** (#407, ``stall.QuoteWatch``): the desk watches the stream once for every
  book, keeps each stall and sends it and the quotes' return to Telegram once each; every book is
  told (``on_stall``, ``on_recover``: no new buy meanwhile; the mock champion reads KIS's REST
  quotes, the paper books wait), and so is a book made during one.
- **Mock mode** (#405): the champion's book (``mock.MockTrader``) is the execution layer alone: its
  KIS orders, its trades of mode ``mock``, the limits and pauses, the state and ledger. At the start
  (``start``, #406) it reconciles the KIS account with the ledger before any book trades. Beside it
  the champion policy keeps a paper shadow (``shadow``: ``paper.PaperTrader`` in paper, held by no
  limit nor pause, as a challenger's book is), on the same picks, predictions and quotes. The
  shadow and the challengers size on the paper account (``store.paper_account``: the net of the
  champion's paper book), never on the KIS fills, so no difference of execution reaches them.
- **Live mode** (#408): with the live gate and the execution checks passed (``live.py``, judged by
  ``run.py``), the champion's book is the same KIS book of mode ``live`` on the live account, with
  its paper shadow as in mock mode, sized on the first period's cap until it is lifted (``ramp``).
  Before they pass the trader runs the desk in paper (its mode ``paper``).
- **Mock practice** (#427, ``KRX_TRADER_MOCK_PRACTICE=on``, mock mode alone): at a pick whose cost
  map was read and whose champion was read (none yet counts as read) but with no champion cell to
  trade (``NO_PASS``, ``NOT_IN_POLICY``), the champion's book trades the cost map's practice cell
  (``Selection.practice``: the greatest lower bound less p* of the cells with a verdict) one share at a
  time at ``KRX_TRADER_THRESHOLD``. Not after ``NO_COST_MAP`` nor ``NO_POLICY``. The KIS book stays
  the one owner of the account's orders: no second book orders on KIS, and one position or buy waits
  at a time whichever cell it is of. Once a pick finds a champion cell again, the practice cell is
  picked no more (a rise of it waiting is ``not_picked``); a practice position is sold by its own
  barriers, and holds the champion's cells off (``holding``) until it is. Its paper shadow is a book
  of its own (``practice_shadow``, held by nothing, as the shadow is), so that the champion policy's
  shadow and record never hold a practice trade; a practice shadow's trade left open is sold by its
  barriers whatever the mode or the setting. The practice trades are of no policy and no record
  counts them (``store.PRACTICE_TRADE``); the KIS checks, the limits and the cash do (#427).
- **Records** (``record``): while a trading day's session is open (from its open to the end of its
  closing auction), each running book's policy ran that day (``policy_days``), always in paper
  (#405): the champion's (in mock and live mode its shadow's), stopped once its status is not
  ``running`` in the session (in mock and live mode never: the shadow is held by nothing); each
  challenger's. Each policy-day once. The promotion and the live gate read them (``promotion.py``).
  Before the first champion the champion's book has no policy and keeps no record.

The champion never changes during a session: it is read at the picks only, and a promotion is
written after the market alone (``promotion.in_market``).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any

from krx_quant.feeds.base import kst_day
from krx_quant.kis_orders import Broker
from krx_quant.market_calendar import Session
from krx_quant.special import Window
from krx_quant.trader import KIS_MODES, LIVE, MOCK, PAPER, live, policy
from krx_quant.trader.cells import Cell, Signal
from krx_quant.trader.limits import RUNNING, Notify
from krx_quant.trader.mock import MockTrader, Timing
from krx_quant.trader.paper import PaperTrader, Rules
from krx_quant.trader.quotes import BestQuote
from krx_quant.trader.selection import NO_PASS, NO_POLICY, NOT_IN_POLICY, Selection
from krx_quant.trader.stall import STALL_AFTER, QuoteWatch
from krx_quant.trader.store import TraderStore

log = logging.getLogger(__name__)


def under_champion(store: TraderStore, selection: Selection) -> Selection:
    """``selection`` under the champion policy kept now (#399); as it is before the first, and with
    no cell when the champion cannot be read."""
    try:
        champion = store.champion()
    except (policy.PolicyError, sqlite3.Error) as e:
        log.error("trader: cannot read the champion policy (%s): no cell trades until the next pick", e)
        return Selection((), selection.considered, selection.as_of, NO_POLICY, str(e))
    if champion is None:
        log.info("trader: no champion policy yet: the cost map's cells at the default threshold and Kelly")
    else:
        log.info(
            "trader: under the champion, policy %s (%s): %s cell(s)",
            champion.version,
            champion.created_at.strftime("%Y-%m-%d %H:%M"),
            len(champion.cells),
        )
    return policy.apply(champion, selection)


class Desk:
    """The champion's book and the challengers' (module doc), into ``store``; the arguments are the
    champion's book's (``paper.PaperTrader``). In ``mock`` mode (#405) the champion's book orders
    through ``broker`` (``mock.MockTrader``, its fills read at ``clock``'s time, waiting ``timing``),
    and the champion policy's paper shadow (``shadow``) keeps its paper record; the challengers'
    books are in paper as ever. More than ``stall_after`` without a quote in continuous trading is a
    stall of the quotes (#407; None watches nothing). With ``practice`` (#427, mock mode alone) the
    champion's book practises a cell on a day no champion cell trades (module doc)."""

    def __init__(
        self,
        store: TraderStore,
        rules: Rules,
        sessions: Callable[[date], Session | None],
        started_at: datetime,
        mode: str = PAPER,
        notify: Notify | None = None,
        *,
        broker: Broker | None = None,
        clock: Callable[[], datetime] | None = None,
        timing: Timing | None = None,
        stall_after: timedelta | None = STALL_AFTER,
        practice: bool = False,
    ) -> None:
        if practice and mode != MOCK:  # #427: the settings refuse it before (``settings.py``)
            raise ValueError(f"the mock practice is for the mock mode alone, not {mode}")
        self.practice = practice
        self.store = store
        self.rules = rules
        self._sessions = sessions
        self._started_at = started_at
        self.mode = mode
        # The collector's quotes, watched for every book (#407): a stall the trader stopped during,
        # taken up again, holds the books made below.
        self.watch = QuoteWatch(store, mode, sessions, started_at, notify or (lambda text: None), stall_after)
        self._notify: Notify = notify or (lambda text: None)
        self.champion: PaperTrader
        if mode in KIS_MODES:
            if broker is None or clock is None:
                raise ValueError(f"the {mode} mode's champion orders through a broker, at a clock")
            self.champion = MockTrader(
                store, rules, sessions, started_at, broker, clock, notify, timing, mode=mode
            )
        else:
            self.champion = PaperTrader(store, rules, sessions, started_at, mode, notify)
        self.shadow: PaperTrader | None = None
        if mode in KIS_MODES:  # the champion policy's paper record, beside its KIS orders (#405, #408)
            self.shadow = PaperTrader(
                store,
                rules,
                sessions,
                started_at,
                PAPER,
                shadow=True,
                carry=True,
                account=self._paper_account,
            )
        elif store.open_trade(PAPER, shadow=True) is not None:
            # A shadow's trade left open by mock mode: a shadow book of its own sells it by its
            # barriers, picking nothing, apart from the champion's book, whose limits never count it.
            self.shadow = PaperTrader(
                store, rules, sessions, started_at, PAPER, shadow=True, account=self._paper_account
            )
        # The practice cell's paper shadow (#427): practising, or a trade of it left open (the practice
        # turned off, or the mode changed), which it sells by its barriers, picking nothing.
        self.practice_shadow: PaperTrader | None = None
        if practice or store.open_trade(PAPER, shadow=True, practice=True) is not None:
            self.practice_shadow = PaperTrader(
                store,
                rules,
                sessions,
                started_at,
                PAPER,
                shadow=True,
                practice=True,
                account=self._paper_account,
            )
        self.champion.practice_state = {"enabled": practice, "active": False, "cell": None}
        self.challengers: dict[int, PaperTrader] = {}
        self._running: set[int] = set()  # the challengers picked, not retired
        self._recorded: dict[tuple[int, date], bool] = {}  # each record kept today, and if stopped
        # A challenger's paper trade left open when the trader stopped: its book goes on with it.
        for version in store.open_challengers(PAPER):
            self.challengers[version] = self._book(version)
        for book in [self.champion, *self._shadows]:
            self._told(book, started_at)

    def _paper_account(self) -> float:
        """The net P&L the paper books size on: the champion's book's in paper mode (the trades its
        limits hold); in mock and live mode the champion's paper book's (``store.paper_account``),
        never the KIS fills (#405)."""
        if self.mode not in KIS_MODES:
            return self.champion.holds.realized_all
        return self.store.paper_account()

    def _book(self, version: int, at: datetime | None = None) -> PaperTrader:
        """Challenger ``version``'s book: in paper whatever the trader's mode (#405); told of a stall of
        the quotes at ``at`` (the start's if None, #407)."""
        book = PaperTrader(
            self.store,
            self.rules,
            self._sessions,
            self._started_at,
            PAPER,
            challenger=version,
            account=self._paper_account,
        )
        return self._told(book, at or self._started_at)

    def _told(self, book: PaperTrader, at: datetime) -> PaperTrader:
        """``book``, told of the stall of the quotes holding at ``at``, if one does (#407)."""
        stall = self.watch.stall
        if stall is not None:
            book.on_stall(stall, at)
        return book

    @property
    def _shadows(self) -> list[PaperTrader]:
        """The champion policy's shadow and the practice shadow (#427), those there are."""
        return [b for b in (self.shadow, self.practice_shadow) if b is not None]

    @property
    def books(self) -> list[PaperTrader]:
        return [self.champion, *self._shadows, *self.challengers.values()]

    @property
    def cells(self) -> list[Cell]:
        """Every book's cells, the champion's first: the cells the reader follows."""
        return list(dict.fromkeys(c for book in self.books for c in book.cells))

    @property
    def status(self) -> str:
        return self.champion.status

    @property
    def running(self) -> list[int]:
        """The challengers running, oldest first."""
        return sorted(self._running)

    def select(self, selection: Selection, at: datetime) -> None:
        """The cells the cost map passes at ``at`` (``selection``) under the champion for its book
        (and its shadow's), and under each challenger running that day for its own (module doc)."""
        under = under_champion(self.store, selection)
        chosen = self._practising(under)
        self.champion.select(chosen, at)
        if self.mode in KIS_MODES and self.shadow is not None:
            self.shadow.select(under, at)
        if self.practice_shadow is not None:
            if chosen.practising:
                self.practice_shadow.select(chosen, at)
            else:  # no new practice entry; a practice position is sold by its barriers (#427)
                self.practice_shadow.retire(at)
        self.champion.practice_state = {
            "enabled": self.practice,
            "active": chosen.practising,
            "cell": chosen.picks[0].as_json() if chosen.practising else None,
        }
        running: dict[int, policy.Policy] = {}
        try:
            versions = self.store.running_challengers(kst_day(at))
        except sqlite3.Error as e:
            log.error("trader: cannot read the challengers (%s): none runs until the next pick", e)
            versions = []
        for version in versions:
            try:
                running[version] = self.store.read_policy(version)
            except (policy.PolicyError, sqlite3.Error) as e:
                log.error(
                    "trader: cannot read challenger %s (%s): it does not run until the next pick", version, e
                )
        for version, challenger in running.items():
            book = self.challengers.get(version)
            if book is None:
                book = self.challengers[version] = self._book(version, at)
            book.select(policy.apply(challenger, selection), at)
        for version in sorted(self._running - set(running)):
            log.info("trader: challenger %s runs no more", version)
            self.challengers[version].retire(at)
        self._running = set(running)
        self._prune()

    def _practising(self, under: Selection) -> Selection:
        """``under`` (the champion's pick), or with the practice on and no champion cell to trade on a
        cost map and a champion read, the practice cell's (#427, module doc)."""
        if not self.practice or under.picks or under.reason not in (NO_PASS, NOT_IN_POLICY):
            return under
        if under.practice is None:
            log.info("trader: no cell can be practised on this cost map: no practice until the next pick")
            return under
        return replace(under, picks=(under.practice,))

    def on_special(self, windows: Sequence[Window]) -> None:
        for book in self.books:
            book.on_special(windows)

    def on_signal(self, signal: Signal, at: datetime) -> None:
        cell = Cell(signal.kind, signal.predictor)
        takers = [book for book in self.books if book.trades(cell)] or [self.champion]
        for book in takers:
            book.on_signal(signal, at)

    def on_quote(self, quote: BestQuote) -> None:
        ended = self.watch.on_quote(quote)  # the quotes back (#407): each book told before it takes it
        for book in self.books:
            if ended is not None:
                book.on_recover(ended, quote.received_at)
            book.on_quote(quote)
        self._prune()

    def on_controls(self, now: datetime) -> None:
        self.champion.on_controls(now)

    def on_time(self, now: datetime) -> None:
        begun = self.watch.on_time(now)  # the quotes stalled (#407)
        for book in self.books:
            if begun is not None:
                book.on_stall(begun, now)
            book.on_time(now)
        self.record(now)
        self._prune()

    def write(self, now: datetime) -> None:
        self.champion.write(now)
        self.record(now)

    def set_live(self, state: dict[str, Any], at: datetime) -> None:
        """The live mode's state as the trader judged it at ``at`` (#408, ``live.live_state``): in the
        champion's state from now (``GET /v1/trader``'s ``live``)."""
        self.champion.live = state
        self.champion.write(at)

    def wind_down(self, at: datetime) -> None:
        """The mode the trader runs in about to end (#408, ``run.LiveWatch``): the champion's book picks
        no cell from ``at`` (a rise waiting skipped), so that it buys nothing new; what it holds is sold
        by its own rules. A later pick picks again; the trader winds it down again then."""
        if self.champion.cells:
            log.info("trader: winding the %s book down: nothing new is bought", self.mode)
        self.champion.retire(at)

    def ramp(self, at: datetime) -> live.Ramp | None:
        """In live mode (#408): the first period judged at ``at`` (a lift or a breach written and told,
        ``live.step``), and the champion's book sized on its cap from now; its pause read at once."""
        if self.mode != LIVE:
            return None
        found = live.step(self.store, self._sessions, at, self.rules.capital, self._notify)
        self.champion.cap = None if found.cap >= self.rules.capital else found.cap
        self.champion.on_controls(at)
        return found

    async def start(self) -> None:
        """Before anything trades: in mock mode the champion's book reconciles the KIS account with
        the ledger (#406, ``mock.MockTrader.start``)."""
        await self.champion.start()

    async def idle(self) -> None:
        """Until the champion's book has no order in flight (#405: in mock, its KIS orders)."""
        await self.champion.idle()

    async def close(self) -> None:
        """The trader stopping: the champion's order in flight waited for (#405, ``mock.CLOSE_SECONDS``)."""
        await self.champion.close()

    def record(self, now: datetime) -> None:
        """Each running book's policy ran today, while the session is open (module doc)."""
        day = kst_day(now)
        session = self._sessions(day)
        if session is None or not session.open <= now <= session.close:
            return
        if any(d != day for _, d in self._recorded):
            self._recorded = {k: v for k, v in self._recorded.items() if k[1] == day}
        books: list[tuple[int, bool, bool]] = [(v, False, False) for v in sorted(self._running)]
        if self.champion.policy is not None:
            # In mock and live mode its record is the shadow's, which no pause nor limit stops (#405).
            stopped = self.mode not in KIS_MODES and self.champion.status != RUNNING
            books.insert(0, (self.champion.policy, True, stopped))
        for version, champion, stopped in books:
            kept = self._recorded.get((version, day))
            if kept is not None and (kept or not stopped):
                continue
            # Always the paper records (#405): one fill model for every policy, whatever the mode.
            self.store.put_policy_day(PAPER, version, day, champion, stopped, now)
            self._recorded[(version, day)] = stopped

    def _prune(self) -> None:
        """Let a retired challenger's book go once it holds nothing, outside mock mode the shadow book
        that sold the shadow's trade left open (#405), and without the practice the practice shadow's
        book once it sold its trade left open (#427)."""
        if self.mode not in KIS_MODES and self.shadow is not None and not self.shadow.busy:
            self.shadow = None
        if not self.practice and self.practice_shadow is not None and not self.practice_shadow.busy:
            self.practice_shadow = None
        for version in [
            v for v, book in self.challengers.items() if v not in self._running and not book.busy
        ]:
            del self.challengers[version]
