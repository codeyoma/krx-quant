"""The trader's books (#400, #393 "정책과 자기 개선"): the champion's and each challenger's, on the
same cells' predictions and the same quotes, in one process.

- **Picks** (``select``, at the trader's start and every day at 08:30): the champion read from the
  store, applied to the cost map's cells (``under_champion``, ``policy.apply``: #472, its cells
  whatever their verdict; none before
  the first champion, #469), for the
  champion's book (``paper.PaperTrader``: the trades the trader makes, which its loss limits and
  pauses hold; in mock mode ``mock.MockTrader``, #405, its orders on KIS's mock server); and each
  challenger that runs that day (``policy.running_versions``: the members of the cohort of that
  day's review cycle, #473, at most three, entered before that day's 08:30, so a cohort formed on a
  review night runs from the next trading day, the cycle's first) applied to the same cells, for its
  own paper book. No pause holds a challenger book; it sizes by
  the loss budget and keeps the risk rules on its own orders and trades, its loss limits simulated
  on its own trades (#471, ``limits.Simulated``: the cumulative one from ``simulated_since``, counted
  again when the trader is re-armed), and it buys on the paper account's cash with nothing open, as
  the champion's book would. A challenger that no longer runs (its cohort judged: promoted or dropped) is
  retired: its book buys nothing more and is let go once it holds nothing. A champion that cannot be read
  trades nothing until the next pick (``NO_POLICY``); a challenger that cannot be read does not
  run until then.
- **Predictions** (``on_signal``): each to every book trading its cell, or holding it off because
  its predictor is not active (#603: skipped ``predictor_inactive``); one of a cell no book reads (the
  picks changed while it was read) to the champion's, which keeps it ``not_picked``. The reader
  follows every book's cells (``cells``).
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
  pause, its loss limits simulated, as a challenger's book is), on the same picks, predictions and
  quotes. The
  shadow and the challengers size on the paper account (``store.paper_account``: the net of the
  champion's paper book), never on the KIS fills, so no difference of execution reaches them.
- **Live mode** (#408): with the live gate and the execution checks passed (``live.py``, judged by
  ``run.py``), the champion's book is the same KIS book of mode ``live`` on the live account, with
  its paper shadow as in mock mode, sized on the first period's cap until it is lifted (``ramp``).
  Before they pass the trader runs the desk in paper (its mode ``paper``).
- **Mock practice** (#427, ``KRX_TRADER_MOCK_PRACTICE=on``, mock mode alone): at a pick whose cost
  map was read and whose champion was read (none yet counts as read) but with no champion cell to
  trade (``NO_PASS``, ``NOT_IN_POLICY``, ``NO_CHAMPION``), the champion's book trades the practice
  cell kept for the review cycle in force (#474, ``practice.frozen``: chosen by the nightly job, at
  its θ) one share at a time; none while no cell is kept for the cycle (``practice.NOT_FROZEN``), nor
  for the rest of the cycle once a pick finds its predictor gone (``GONE``, ``practice.rest``: never
  another cell) or not active (``PREDICTOR_INACTIVE``, #603). Not after ``NO_COST_MAP`` nor
  ``NO_POLICY``. While it practises, the champion's cells not active are read by its shadow alone. The
  KIS book stays
  the one owner of the account's orders: no second book orders on KIS, and one position or buy waits
  at a time whichever cell it is of. Once a pick finds a champion cell again, the practice cell is
  picked no more (a rise of it waiting is ``not_picked``); a practice position is sold by its own
  barriers, and holds the champion's cells off (``holding``) until it is. It has no paper shadow
  (#548): the live mode's execution checks measure its legs against the paper rules' price at their
  decision quotes (``live.legs``), and the champion policy's shadow never holds a practice trade. A
  practice shadow's trade left open from before (``practice_shadow``: a book that picks nothing, held
  by no pause) is sold by its barriers whatever the mode or the setting. The practice trades are of
  no policy and no record counts them (``store.PRACTICE_TRADE``); the KIS checks, the risk rules and
  the cash do (#427). No loss limit counts them since #533 (``limits.py`` "The practice").

  > **바뀜** (2026-11-05, #557): the practice had a paper shadow of its own (``practice_shadow``, on
  > the practice cell's picks), which the live mode's gap paired the practice trades with → none; a
  > shadow's trade of it left open is sold. 이유: a shadow keeps its own cooldown and buy count and
  > drifts from the KIS book: on 2026-11-04, 13 of the 20 practice round trips had no shadow trade to
  > pair with.

  > **바뀜** (2026-11-03, #479): the practice cell was the cost map's at each pick
  > (``Selection.practice``: the greatest lower bound less p* of the cells with a verdict), at
  > ``KRX_TRADER_THRESHOLD`` → the cell and θ kept for the review cycle. 이유: spec #468 6 "연습"
  > (the user's choice, 2026-11-02): one practice strategy frozen for a cycle.
- **Records** (``record``): while a trading day's session is open (from its open to the end of its
  closing auction), each running book's policy ran that day (``policy_days``), always in paper
  (#405): the champion's (in mock and live mode its shadow's), stopped once a pause or a failed
  order holds it in the session (in mock and live mode never: no pause holds the shadow); each
  challenger's, never stopped (a simulated loss limit is its policy's own evidence, #471). A loss
  limit does not mark the champion's day stopped either (#473): it is its policy's rule, which the
  challengers simulate, so that the cohort's judgment counts the day for both. Each policy-day once.
  The promotion and the live gate read them (``promotion.py``). Before the first champion the
  champion's book has no policy and keeps no record.

  > **바뀜** (2026-11-03, #481): the champion's day was stopped once its status was not
  > ``running`` (a loss limit too) → by a pause or a failed order alone. 이유: spec #468 5: the days a
  > loss limit stopped count in the judgment.

The champion never changes during a session: it is read at the picks only, and a promotion is
written after the market alone (``promotion.in_market``); since #469 on a review night alone.

**The review cycle** (#469, ``review.py``): each pick is kept with the evaluation version and the
review cycle in force (``review.cycle_at``), and each policy's cells with whether they trade
(``Selection.cells``). A cell whose predictor is gone (``cells.gone``: the engine's database, at
``engine``) trades no more for the rest of that cycle, whatever later picks find
(``TraderStore.gone``): never another cell in its place. Nor does one whose predictor is not active
(#603, ``cells.inactive``: on probation, on the bench, retired, a clone; ``TraderStore.inactive``),
of the champion, its shadow, a challenger or the practice alike: its book reads its predictions and
buys none (``predictor_inactive``), and sells what it holds by its own barriers. Never the clone's
original in its place. A final consensus follows its active members alone (the engine's), and a
baseline has no status: neither is ever inactive.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Collection, Sequence
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from krx_quant.feeds.base import KST, kst_day
from krx_quant.kis_orders import Broker
from krx_quant.market_calendar import Session
from krx_quant.special import Window
from krx_quant.trader import KIS_MODES, LIVE, MOCK, PAPER, SYMBOL, live, policy, practice, review
from krx_quant.trader.cells import Cell, Signal, gone, inactive
from krx_quant.trader.limits import RUNNING, Notify
from krx_quant.trader.mock import MockTrader, Timing
from krx_quant.trader.paper import PaperTrader, Rules
from krx_quant.trader.quotes import BestQuote
from krx_quant.trader.selection import (
    GONE,
    NO_CHAMPION,
    NO_PASS,
    NO_POLICY,
    NOT_IN_POLICY,
    PREDICTOR_INACTIVE,
    Selection,
)
from krx_quant.trader.stall import STALL_AFTER, QuoteWatch
from krx_quant.trader.store import CUMULATIVE_LIMIT, DAILY_LIMIT, TraderStore

log = logging.getLogger(__name__)

# The champion's statuses that leave its day a common one (#473): running, or held by its own rules.
RUN_OR_LIMIT = (RUNNING, DAILY_LIMIT, CUMULATIVE_LIMIT)


# A policy's cells that trade no more in the review cycle (#469, ``Desk._rested``): those whose
# predictor is gone, and those whose predictor is not active (#603), which buy nothing.
Rested = tuple[Collection[Cell], Collection[Cell]]


def under_champion(
    store: TraderStore,
    selection: Selection,
    rested_of: Callable[[policy.Policy], Rested] | None = None,
) -> Selection:
    """``selection`` under the champion policy kept now (#399), its cells ``rested_of`` it trading
    none (#469: gone; #603: not active, read and never bought); with no cell before the first
    (``NO_CHAMPION``, #469) and when the champion cannot be read (``NO_POLICY``)."""
    try:
        champion = store.champion()
    except (policy.PolicyError, sqlite3.Error) as e:
        log.error("trader: cannot read the champion policy (%s): no cell trades until the next pick", e)
        return replace(selection, picks=(), reason=NO_POLICY, detail=str(e))
    if champion is None:
        log.info("trader: no champion policy yet: no cell trades before the first (#469)")
        return policy.apply(None, selection)
    log.info(
        "trader: under the champion, policy %s (%s): %s cell(s)",
        champion.version,
        champion.created_at.strftime("%Y-%m-%d %H:%M"),
        len(champion.cells),
    )
    gone, inactive = ((), ()) if rested_of is None else rested_of(champion)
    return policy.apply(champion, selection, gone, inactive)


def practice_state(enabled: bool, kept: practice.Frozen | None = None) -> dict[str, Any]:
    """``GET /v1/trader``'s ``practice`` (#427): whether it is on, whether the book practises now and
    on which cell (set by the pick), the cell kept for the review cycle (#474, ``frozen``) and why it
    does not practise (``why``, ``practice.py``; null while it does, and with the practice off)."""
    return {
        "enabled": enabled,
        "active": False,
        "cell": None,
        "frozen": None if kept is None else kept.as_json(),
        "why": None,
    }


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
        engine: Path | None = None,
    ) -> None:
        if practice and mode != MOCK:  # #427: the settings refuse it before (``settings.py``)
            raise ValueError(f"the mock practice is for the mock mode alone, not {mode}")
        self.practice = practice
        self.engine = engine  # the engine's database, where a policy's predictor is found gone (#469)
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
                since=self.simulated_since(),
            )
        elif store.open_trade(PAPER, shadow=True) is not None:
            # A shadow's trade left open by mock mode: a shadow book of its own sells it by its
            # barriers, picking nothing, apart from the champion's book, whose limits never count it.
            self.shadow = PaperTrader(
                store,
                rules,
                sessions,
                started_at,
                PAPER,
                shadow=True,
                account=self._paper_account,
                since=self.simulated_since(),
            )
        # The practice cell's paper shadow's trade left open from before #548 (#427): a book of it sells
        # it by its barriers, picking nothing. No new practice shadow is made (module doc).
        self.practice_shadow: PaperTrader | None = None
        if store.open_trade(PAPER, shadow=True, practice=True) is not None:
            self.practice_shadow = PaperTrader(
                store,
                rules,
                sessions,
                started_at,
                PAPER,
                shadow=True,
                practice=True,
                account=self._paper_account,
                since=self.simulated_since(),
            )
        self.champion.practice_state = practice_state(practice)
        self.challengers: dict[int, PaperTrader] = {}
        self._running: set[int] = set()  # the challengers picked, not retired
        self._recorded: dict[tuple[int, date], bool] = {}  # each record kept today, and if stopped
        # A challenger's paper trade left open when the trader stopped: its book goes on with it.
        for version in store.open_challengers(PAPER):
            self.challengers[version] = self._book(version)
        for book in [self.champion, *self._shadows]:
            self._told(book, started_at)

    def simulated_since(self) -> datetime | None:
        """Where a challenger's or a shadow's simulated cumulative loss counts from (#471,
        ``limits.Simulated``): the evaluation's first day (#469: its evidence alone), or the trader's
        last re-arm of its mode if later; None (every trade of its book) before either."""
        found = []
        try:
            evaluation = review.current(self.store.db)
            if evaluation is not None:
                found.append(datetime.combine(evaluation.first_day, time(), KST))
            rearmed = self.store.rearmed_at(self.mode)
        except sqlite3.Error as e:
            log.error("trader: cannot tell where the simulated loss limits count from (%s): every trade", e)
            return None
        if rearmed is not None:
            found.append(rearmed)
        return max(found, default=None)

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
            since=self.simulated_since(),
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
        """The champion policy's shadow and the practice shadow selling its trade left open (#427,
        #548), those there are."""
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
        """The cost map's cells at ``at`` (``selection``) under the champion for its book
        (and its shadow's), and under each challenger running that day for its own, in the review
        cycle in force (module doc)."""
        cycle = self._cycle(at)
        if cycle is not None:
            selection = replace(selection, evaluation=cycle.evaluation, cycle=cycle.number)

        def rested_of(kept: policy.Policy) -> Rested:
            return self._rested(kept, cycle, at)

        under = under_champion(self.store, selection, rested_of)
        chosen, self.champion.practice_state = self._practising(under, cycle, at)
        self.champion.select(chosen, at)
        if self.mode in KIS_MODES and self.shadow is not None:
            self.shadow.select(under, at)
        running: dict[int, policy.Policy] = {}
        try:
            versions = self.store.running_challengers(kst_day(at), self._sessions)
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
            book.select(policy.apply(challenger, selection, *rested_of(challenger)), at)
        for version in sorted(self._running - set(running)):
            log.info("trader: challenger %s runs no more", version)
            self.challengers[version].retire(at)
        self._running = set(running)
        self._prune()

    def _cycle(self, at: datetime) -> review.Cycle | None:
        """The review cycle in force at ``at`` (#469); None before the evaluation was begun, or when the
        store or the calendar cannot say (logged: the pick goes on without it)."""
        try:
            evaluation = review.current(self.store.db)
            return None if evaluation is None else review.cycle_at(evaluation, at, self._sessions)
        except (sqlite3.Error, ValueError) as e:
            log.error("trader: cannot tell the review cycle (%s): the pick is kept without it", e)
            return None

    def _rested(self, kept: policy.Policy, cycle: review.Cycle | None, at: datetime) -> Rested:
        """The cells of ``kept`` that trade no more in ``cycle`` (module doc): their predictor gone now
        in the engine's database, or found gone by a pick earlier in the cycle (#469); and those whose
        predictor is not active now, or was found so by a pick earlier in the cycle (#603)."""
        found: set[Cell] = set()
        statuses: dict[Cell, str] = {}
        cells = [c.cell for c in kept.cells]
        if self.engine is not None:
            try:
                found = gone(self.engine, SYMBOL, cells, at)
            except sqlite3.Error as e:
                log.warning("trader: cannot tell if policy %s's predictors predict (%s)", kept.version, e)
            try:
                statuses = inactive(self.engine, cells, at)
            except sqlite3.Error as e:
                log.warning("trader: cannot tell if policy %s's predictors are active (%s)", kept.version, e)
        held: set[Cell] = set(statuses)
        if cycle is not None:
            try:
                found |= self.store.gone(kept.version, cycle.evaluation, cycle.number)
                held |= self.store.inactive(kept.version, cycle.evaluation, cycle.number)
            except (sqlite3.Error, ValueError, KeyError, TypeError) as e:
                log.warning("trader: cannot read policy %s's cells rested this cycle (%s)", kept.version, e)
        for cell in sorted(found & set(cells), key=lambda c: (c.kind, c.predictor)):
            log.warning(
                "trader: policy %s's cell %s %s is %s: its predictor no longer predicts; it trades no"
                " more until the next review, and no other cell takes its place",
                kept.version,
                cell.kind,
                cell.predictor,
                GONE,
            )
        for cell in sorted((held - found) & set(cells), key=lambda c: (c.kind, c.predictor)):
            now = statuses.get(cell)
            log.warning(
                "trader: policy %s's cell %s %s is %s: its predictor %s; it buys nothing until the next"
                " review, its position sold by its own rules, and no other cell takes its place",
                kept.version,
                cell.kind,
                cell.predictor,
                PREDICTOR_INACTIVE,
                f"is {now}, not active" if now is not None else "was not active at a pick earlier this cycle",
            )
        return found, held

    def _practising(
        self, under: Selection, cycle: review.Cycle | None, at: datetime
    ) -> tuple[Selection, dict[str, Any]]:
        """``under`` (the champion's pick), or with the practice on and no champion cell to trade on a
        cost map and a champion read (none yet included), the practice cell kept for ``cycle`` (#474,
        module doc); with ``GET /v1/trader``'s ``practice`` (#427)."""
        if not self.practice:
            return under, practice_state(False)
        kept = self._frozen(cycle, at)
        state = practice_state(True, kept)
        why: str | None = None
        if under.picks:
            why = practice.CHAMPION
        elif under.reason not in (NO_PASS, NOT_IN_POLICY, NO_CHAMPION):
            why = under.reason
        elif kept is None:
            why = practice.NOT_FROZEN
            log.info(
                "trader: no practice cell is kept for review cycle %s yet (the nightly job chooses one):"
                " no practice until one is",
                "?" if cycle is None else cycle.number,
            )
        elif kept.rested:
            why = GONE if kept.gone_at is not None else PREDICTOR_INACTIVE
            log.info(
                "trader: the practice cell %s %s is %s: no practice until review cycle %s ends",
                kept.pick.kind,
                kept.pick.predictor,
                why,
                kept.cycle,
            )
        if why is not None or kept is None:
            return under, state | {"why": why}
        # The champion's cells not active are the shadow's to read (#603): the practice pick is its own.
        practised = replace(under, picks=(kept.pick,), inactive=())
        return practised, state | {"active": True, "cell": kept.pick.as_json()}

    def _frozen(self, cycle: review.Cycle | None, at: datetime) -> practice.Frozen | None:
        """The practice cell kept for ``cycle`` (#474), found gone at ``at`` if its predictor no longer
        predicts (``cells.gone``, as a policy's cell, #469), or not active if it is not (#603,
        ``cells.inactive``): kept, so that it rests for the rest of the cycle; None without one, or when
        the store cannot say (logged)."""
        if cycle is None:
            return None
        try:
            kept = practice.frozen(self.store.db, cycle.evaluation, cycle.number)
            if kept is None or kept.rested or self.engine is None:
                return kept
            if kept.cell in gone(self.engine, SYMBOL, [kept.cell], at):
                why, said = GONE, "no longer predicts"
            elif (status := inactive(self.engine, [kept.cell], at).get(kept.cell)) is not None:
                why, said = PREDICTOR_INACTIVE, f"is {status}, not active"
            else:
                return kept
            log.warning(
                "trader: the practice cell %s %s is %s: its predictor %s; no practice until review cycle %s"
                " ends, and no other cell takes its place",
                kept.pick.kind,
                kept.pick.predictor,
                why,
                said,
                cycle.number,
            )
            practice.rest(self.store.db, cycle.evaluation, cycle.number, at, why)
            return practice.frozen(self.store.db, cycle.evaluation, cycle.number)
        except (sqlite3.Error, ValueError, KeyError, TypeError) as e:
            log.error("trader: cannot read the practice cell of review cycle %s (%s): none", cycle.number, e)
            return None

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
        since = self.champion.guard.since
        self.champion.on_controls(now)
        if self.champion.guard.since != since:  # re-armed (#471): the simulated limits count again too
            for book in [*self._shadows, *self.challengers.values()]:
                book.rearm(now)

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
            # A loss limit is its policy's rule (#473): only what is not, a pause or a failed order.
            stopped = self.mode not in KIS_MODES and self.champion.status not in RUN_OR_LIMIT
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
        that sold the shadow's trade left open (#405), and the practice shadow's book once it sold its
        trade left open (#427, #548)."""
        if self.mode not in KIS_MODES and self.shadow is not None and not self.shadow.busy:
            self.shadow = None
        if self.practice_shadow is not None and not self.practice_shadow.busy:
            self.practice_shadow = None
        for version in [
            v for v, book in self.challengers.items() if v not in self._running and not book.busy
        ]:
            del self.challengers[version]
