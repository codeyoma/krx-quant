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
- **Records** (``record``): while a trading day's session is open (from its open to the end of its
  closing auction), each running book's policy ran that day (``policy_days``): the champion's,
  stopped once its status is not ``running`` in the session; each challenger's. The promotion and
  the live gate read them (``promotion.py``). Before the first champion the champion's book has no
  policy and keeps no record.

The champion never changes during a session: it is read at the picks only, and a promotion is
written after the market alone (``promotion.in_market``).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Sequence
from datetime import date, datetime

from krx_quant.feeds.base import kst_day
from krx_quant.kis_orders import Broker
from krx_quant.market_calendar import Session
from krx_quant.special import Window
from krx_quant.trader import MOCK, PAPER, policy
from krx_quant.trader.cells import Cell, Signal
from krx_quant.trader.limits import RUNNING, Notify
from krx_quant.trader.mock import MockTrader, Timing
from krx_quant.trader.paper import PaperTrader, Rules
from krx_quant.trader.quotes import BestQuote
from krx_quant.trader.selection import NO_POLICY, Selection
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
    through ``broker`` (``mock.MockTrader``, its fills read at ``clock``'s time, waiting ``timing``);
    the challengers' are in paper as ever."""

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
    ) -> None:
        self.store = store
        self.rules = rules
        self._sessions = sessions
        self._started_at = started_at
        self.mode = mode
        self.champion: PaperTrader
        if mode == MOCK:
            if broker is None or clock is None:
                raise ValueError("the mock mode's champion orders through a broker, at a clock")
            self.champion = MockTrader(store, rules, sessions, started_at, broker, clock, notify, timing)
        else:
            self.champion = PaperTrader(store, rules, sessions, started_at, mode, notify)
        self.challengers: dict[int, PaperTrader] = {}
        self._running: set[int] = set()  # the challengers picked, not retired
        self._recorded: dict[tuple[int, date], bool] = {}  # each record kept today, and if stopped
        # A challenger's paper trade left open when the trader stopped: its book goes on with it.
        for version in store.open_challengers(mode):
            self.challengers[version] = self._book(version)

    def _book(self, version: int) -> PaperTrader:
        return PaperTrader(
            self.store,
            self.rules,
            self._sessions,
            self._started_at,
            self.mode,
            challenger=version,
            account=lambda: self.champion.holds.realized_all,
        )

    @property
    def books(self) -> list[PaperTrader]:
        return [self.champion, *self.challengers.values()]

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
        """The cells the cost map passes at ``at`` (``selection``) under the champion for its book,
        and under each challenger running that day for its own (module doc)."""
        self.champion.select(under_champion(self.store, selection), at)
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
                book = self.challengers[version] = self._book(version)
            book.select(policy.apply(challenger, selection), at)
        for version in sorted(self._running - set(running)):
            log.info("trader: challenger %s runs no more", version)
            self.challengers[version].retire(at)
        self._running = set(running)
        self._prune()

    def on_special(self, windows: Sequence[Window]) -> None:
        for book in self.books:
            book.on_special(windows)

    def on_signal(self, signal: Signal, at: datetime) -> None:
        cell = Cell(signal.kind, signal.predictor)
        takers = [book for book in self.books if book.trades(cell)] or [self.champion]
        for book in takers:
            book.on_signal(signal, at)

    def on_quote(self, quote: BestQuote) -> None:
        for book in self.books:
            book.on_quote(quote)
        self._prune()

    def on_controls(self, now: datetime) -> None:
        self.champion.on_controls(now)

    def on_time(self, now: datetime) -> None:
        for book in self.books:
            book.on_time(now)
        self.record(now)
        self._prune()

    def write(self, now: datetime) -> None:
        self.champion.write(now)
        self.record(now)

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
            books.insert(0, (self.champion.policy, True, self.champion.status != RUNNING))
        for version, champion, stopped in books:
            kept = self._recorded.get((version, day))
            if kept is not None and (kept or not stopped):
                continue
            self.store.put_policy_day(self.mode, version, day, champion, stopped, now)
            self._recorded[(version, day)] = stopped

    def _prune(self) -> None:
        """Let a retired challenger's book go once it holds nothing."""
        for version in [
            v for v, book in self.challengers.items() if v not in self._running and not book.busy
        ]:
            del self.challengers[version]
