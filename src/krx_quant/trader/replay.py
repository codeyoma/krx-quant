"""Recorded days replayed by the paper trader's own rules (#470, spec #468 2 "실행 그대로의 재생"):
what the nightly re-fit fits a policy on (``refit.py``).

**What is replayed**, a trading day at a time:

- the day's recording of 005930 as the trader received it (``read_recording``): the integrated
  book's and trades' best quotes (``H0UNASP0``, ``H0UNCNT0``), one-sided ones dropped, in the order
  received (``entry_delay.day_quotes``), the collector's stream to the trader (``quotes.py``). A
  quote not newer than the one before it is passed over, as the paper trader passes it over;
- each cell's predictions of the day as the trader reads them (``cells.py``: of every status, with
  the reference, the width and a final consensus's block), each read when it was displayed
  (``displayed_at``: the trader reads every half second, the replay at the earliest). An algorithm's
  prediction by a model trained on another target than the one the cost map scores it on is read
  and never buys (``off_target``, as ``stats.on_target`` leaves it out);
- the special windows the engine kept (``special_windows``), each known from its start (the engine
  learns a VI, a circuit breaker or a sidecar as it begins), with the end it kept.

No answer is read, nor its special flag: nothing the trader could not have known then.

**By the paper trader's rules**, called, not copied (``paper.py``): the entry (``plan`` at the read,
at the cell's θ, with the special windows known then); the priority (``admit``, ``settle``: of a bar
close's rises the first ranked buys once each cell ranked above it has a prediction of that bar
close read, or ``Rules.fresh`` after its display, the clock's quarter-second ticks taken as
continuous); the buy at the best ask of the first quote received after the decision (``refused``:
none at or past its time barrier, later than ``Rules.quote_wait``, or at its target already; a
special window known by that quote that touches its window gives it up); the size (``size``, #471:
the trader's loss budget over a share's loss at the stop, within the paper account's cash, the
capital plus the replay's own net so far); the risk rules (``risk_held``, #471: the day's buy orders,
the losses in a row and their cooldown, on the replay's own trades of the day); the exits
(``exit_on``, ``clock_exit``: the take-profit at the target once a best bid reaches it, the stop at
the first best bid at or below it, the time exit at the best bid of the last quote received in the
``Rules.quote_wait`` before the time barrier, else of the first one after it); the costs (``net_of``:
the commission on both fills' notionals, the sell tax on the sale's). The quotes on which an exit
rule does nothing are passed by a search of the arrays for the first one it can act on, and
``exit_on`` judges that one. The loss limits hold a replay as they hold a challenger's book
(``limits.Simulated``, #471: from the replay's first day): a trade's exit realized at or below one
stops it (the daily one for the rest of the day), and so does the open position at a best bid it
holds through, the quote it is bought on included (``paper.unrealized``, as the paper book marks it
on each quote, found by a search of the arrays too), sold at that bid. No pause holds it, and nothing
stalls: a gap leaves its signals out instead (below).

**Two outputs.**

- *Each signal alone, a share* (``Signals``): each rise of a cell the entry rules let buy, decided
  at its read as if nothing were held and no other cell ranked: bought or not (``special``,
  ``expired``, ``target_reached``), its exit and its net in won and in ticks of its reference;
  tallied at each θ of the grid its probability reaches.
- *The policy* (``Policy``): its cells in their rank at their θ, one position at a time, with the
  waits the priority makes, sized and held as the paper book is (#471): its trades, those that
  gained, their net in won and the capital's log growth, by cell and by day (``Result``).

**Missing data**, left out of both and counted apart, never a trade that made nothing: a day without
its recording of the book, or whose files cannot be read (``no_recording``); a recorded gap of the
book's or the trades' feed of 005930 in the signal's window, from its decision to its time barrier,
those begun on the days before too (``gap``, ``day_gaps``); no quote within ``Rules.quote_wait``
after the decision (``no_quote``: the stream was down, a stall by #407's rule); no quote to sell on at
the time barrier (``no_exit_quote``). In a policy's replay a signal left out is decided as any rise
(the cells ranked below it of its bar close are outranked) and holds nothing.

**Memory** (the server has 2 GB): one day's recording is held at a time, read once a pass in batches
(``entry_delay.BATCH_ROWS`` rows) into its quotes' three arrays alone and let go before the next
day's (``footprint.give_back``); the day's predictions are read a cell at a time; across days only
the tallies are kept (each trade only for a caller that asks, ``record``). Each day's peak resident
memory is kept (``Replayer.peaks``: the highest of the day's passes).
"""

from __future__ import annotations

import heapq
import logging
import math
import sqlite3
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from krx_quant.archive import partition_files
from krx_quant.entry_delay import Quotes, day_quotes
from krx_quant.feeds import BOOK, TRADE
from krx_quant.feeds.base import KST, kst_day
from krx_quant.footprint import Meter, give_back
from krx_quant.gaps_probe import Unread, read_gaps
from krx_quant.kinds import DIRECTION
from krx_quant.macro import read_partition
from krx_quant.market_calendar import Session
from krx_quant.pricing import SELL_TAX
from krx_quant.special import Window, touching
from krx_quant.stats import Interval, day_mean
from krx_quant.trader import SYMBOL
from krx_quant.trader.cells import (
    DIRECTION_COLUMNS,
    DIRECTION_FROM,
    KIND_COLUMNS,
    Cell,
    Signal,
    direction_signal,
    kind_signal,
    traded_kind,
)
from krx_quant.trader.limits import RUNNING, SALE, Simulated
from krx_quant.trader.paper import (
    HOLDING,
    NO_QUOTE,
    OUTRANKED,
    SPECIAL,
    Plan,
    Rise,
    Rules,
    Sale,
    admit,
    clock_exit,
    exit_on,
    net_of,
    plan,
    refused,
    risk_held,
    settle,
    size,
)
from krx_quant.trader.quotes import BestQuote

log = logging.getLogger(__name__)

# Why a signal is left out: its data is missing (module doc).
NO_RECORDING, GAP, NO_EXIT_QUOTE = "no_recording", "gap", "no_exit_quote"
MISSING = (NO_RECORDING, GAP, NO_QUOTE, NO_EXIT_QUOTE)
# An algorithm's prediction by a model of another target: read, never bought.
OFF_TARGET = "off_target"
# The feeds of the trader's quotes, whose recorded gaps leave a signal out.
GAP_FEEDS = frozenset({BOOK.tr_id, TRADE.tr_id})
# The days before a replayed one whose gaps are read too (``day_gaps``): a gap is kept under the day it
# started, and the collector restarts every trading day at 07:20 (``KRX_RESTART_AT``), so one from
# before the previous trading day ended by then; ten calendar days cover the longest holiday.
GAP_LOOKBACK = 10
# A day's recording, as the replay found it (``Replayer.recordings``).
RECORDED, GAPPED = "recorded", "gaps"

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICRO = timedelta(microseconds=1)


def micros(at: datetime) -> int:
    """``at`` in microseconds since the epoch, exactly (the recorded receipt times' unit)."""
    return (at - _EPOCH) // _MICRO


def moment(us: int) -> datetime:
    """A receipt time in microseconds since the epoch, in KST."""
    return (_EPOCH + us * _MICRO).astimezone(KST)


# The recordings.


@dataclass(frozen=True)
class Recording:
    """A trading day's recording of 005930 as the trader received it: the best quotes in receipt
    order (microseconds since the epoch, each newer than the one before) and the recorded gaps of
    their feeds (microseconds; an end None: still open)."""

    day: date
    quotes: Quotes
    gaps: tuple[tuple[int, int | None], ...] = ()

    def gap_in(self, start: int, end: int) -> bool:
        """Whether a gap overlaps [start, end)."""
        return any(s < end and (e is None or e > start) for s, e in self.gaps)

    def quote(self, i: int) -> BestQuote:
        q = self.quotes
        return BestQuote(moment(int(q.times[i])), float(q.bids[i]), float(q.asks[i]))


def read_recording(data_dir: Path, day: date) -> Recording | None:
    """005930's recording of ``day`` (module doc); None without its book's, or when a file of it
    cannot be read (logged: its signals are counted ``no_recording``)."""
    try:
        events = data_dir / "events" / f"date={day.isoformat()}"
        if not partition_files(events / f"feed={BOOK.tr_id}" / f"symbol={SYMBOL}"):
            return None
        quotes = day_quotes(data_dir, day, SYMBOL)
        if quotes is None or len(quotes.times) == 0:
            return None
        return Recording(day, newer(quotes), day_gaps(data_dir, day, GAP_FEEDS))
    except (OSError, pa.ArrowException) as e:
        log.warning("replay: cannot read the recording of %s (%s): its signals are left out", day, e)
        return None


def newer(quotes: Quotes) -> Quotes:
    """``quotes`` (in receipt order) without each one not newer than the one before it: the paper
    trader passes such a quote over (``PaperTrader.on_quote``)."""
    times = quotes.times
    if len(times) < 2:
        return quotes
    keep = np.ones(len(times), dtype=bool)
    keep[1:] = times[1:] > times[:-1]
    if keep.all():
        return quotes
    return Quotes(times[keep], quotes.bids[keep], quotes.asks[keep])


def day_gaps(data_dir: Path, day: date, feeds: frozenset[str]) -> tuple[tuple[int, int | None], ...]:
    """The recorded gaps of 005930's ``feeds`` overlapping ``day`` (microseconds). A gap is kept
    under the day it started on (``health.FeedHealth``), so the ``GAP_LOOKBACK`` days before are read
    too: an outage that began the evening before may run into the session. A gap without an end was
    still open when its collector stopped; it ended, at the latest, at that feed's first row received
    on ``day`` after its start (a feed delivers again only once it is back), and without one it
    covers the rest of the day."""
    start, end = (micros(datetime.combine(d, time(), KST)) for d in (day, day + timedelta(days=1)))
    unread = Unread()
    found: list[tuple[int, int | None]] = []
    for back in range(GAP_LOOKBACK + 1):
        for gap in read_gaps(data_dir, day - timedelta(days=back), unread):
            if gap.feed not in feeds or gap.key != SYMBOL:
                continue
            began = micros(gap.start)
            ended = _back_at(data_dir, day, gap.feed, began) if gap.end is None else micros(gap.end)
            if began < end and (ended is None or ended > start):
                found.append((began, ended))
    return tuple(sorted(found, key=lambda g: g[0]))


def _back_at(data_dir: Path, day: date, feed: str, after: int) -> int | None:
    """The first receipt time on ``day`` of ``feed``'s rows of 005930 after ``after``; None without
    one."""
    partition = data_dir / "events" / f"date={day.isoformat()}" / f"feed={feed}" / f"symbol={SYMBOL}"
    table = read_partition(partition, ("received_at",))
    if table is None or table.num_rows == 0:
        return None
    times = pc.cast(pc.cast(table["received_at"], pa.timestamp("us", tz="UTC")), pa.int64()).to_numpy()
    later = times[times > after]
    return int(later.min()) if len(later) else None


# The predictions.


@dataclass(frozen=True, slots=True)
class Read:
    """A prediction of a cell as the replay reads it, at its display; ``on_target`` False: by a
    model of another target (``off_target``), never bought."""

    signal: Signal
    on_target: bool

    @property
    def at(self) -> datetime:
        return self.signal.displayed_at


def target_of(cell: Cell, role: str, target_version: int) -> int | None:
    """The model target a cell's predictions count from, as the cost map's statistics keep them
    (``stats.on_target``): an algorithm's (``top``) the kind's answer version, the direction's the
    touch target from 2 on; None: every one (a baseline's, a consensus's)."""
    kind = traded_kind(cell.kind)
    if role != "top" or (kind.name == DIRECTION and target_version < 2):
        return None
    return kind.answer_version if kind.name != DIRECTION else target_version


_MODELS = "LEFT JOIN model_targets m ON m.algorithm = p.algorithm AND m.version = p.model_version"


def day_reads(db: sqlite3.Connection, cell: Cell, role: str, day: date, target_version: int) -> list[Read]:
    """``cell``'s predictions of 005930 with bar closes on ``day``, as the trader reads them
    (``cells.py``), on the engine database's indexes."""
    kind = traded_kind(cell.kind)
    start = datetime.combine(day, time(), KST)
    span = (start.isoformat(), (start + timedelta(days=1)).isoformat())
    target = target_of(cell, role, target_version)
    if kind.name == DIRECTION:
        rows = db.execute(
            f"SELECT {DIRECTION_COLUMNS}, m.target_version AS model_target FROM {DIRECTION_FROM} {_MODELS}"
            " WHERE p.algorithm = ? AND p.symbol = ? AND p.bar_close >= ? AND p.bar_close < ?"
            " ORDER BY p.bar_close, p.id",
            (cell.predictor, SYMBOL, *span),
        )
        made = direction_signal
    else:
        rows = db.execute(
            f"SELECT {KIND_COLUMNS}, m.target_version AS model_target FROM kind_predictions p {_MODELS}"
            " WHERE p.kind = ? AND p.symbol = ? AND p.algorithm = ? AND p.bar_close >= ? AND p.bar_close < ?"
            " ORDER BY p.bar_close, p.id",
            (kind.name, SYMBOL, cell.predictor, *span),
        )
        made = kind_signal
    return [Read(made(r, cell, SYMBOL), target is None or r["model_target"] == target) for r in rows]


def known(windows: Sequence[Window], at: datetime) -> list[Window]:
    """The special windows known at ``at``: those begun by then (module doc)."""
    return [w for w in windows if w.start <= at]


# The fills.


@dataclass(frozen=True, slots=True)
class Fill:
    """A rise's buy decided at a time, on its day's recording, a share: its data missing
    (``missing``), given up (``refused``: ``special``, ``expired``, ``target_reached``), or bought at
    ``entry`` at ``entry_at`` and sold by ``sale``. ``free_at``: when the book holds nothing again. A
    buy's quotes held through (#471: what a loss limit marks it on) are the recording's from
    ``bought`` (the buy's, in: the paper book marks a position on the quote it is bought on) to
    ``until`` (the one its exit is made on, out; the day's end for the clock's time exit)."""

    free_at: datetime
    missing: str | None = None
    refused: str | None = None
    entry_at: datetime | None = None
    entry: float | None = None
    sale: Sale | None = None
    bought: int = 0
    until: int = 0


class Fills:
    """A day's fills on its recording (None: not recorded), each rise's at each decision time made
    once: the policies of a pass and the signals measured alone share them."""

    def __init__(self, day: Recording | None, windows: Sequence[Window], rules: Rules) -> None:
        self.day = day
        self._windows = windows
        self._rules = rules
        self._made: dict[tuple[str, str, int, datetime], Fill] = {}

    def at(self, signal: Signal, planned: Plan, decided_at: datetime) -> Fill:
        key = (signal.kind, signal.predictor, signal.prediction_id, decided_at)
        made = self._made.get(key)
        if made is None:
            made = self._made[key] = self._fill(signal, planned, decided_at)
        return made

    def _fill(self, signal: Signal, planned: Plan, decided_at: datetime) -> Fill:
        day, rules = self.day, self._rules
        if day is None:
            return Fill(decided_at, missing=NO_RECORDING)
        times = day.quotes.times
        if day.gap_in(micros(decided_at), micros(planned.time_barrier)):
            return Fill(decided_at, missing=GAP)
        first = int(np.searchsorted(times, micros(decided_at), side="right"))  # received after it
        given_up = decided_at + rules.quote_wait + rules.grace  # by the clock (``PaperTrader.on_time``)
        if first >= len(times) or times[first] >= micros(given_up):
            return Fill(decided_at, missing=NO_QUOTE)
        quote = day.quote(first)
        # A window learned before the quote gives the buy up (``PaperTrader.on_special``).
        if touching(
            known(self._windows, quote.received_at), signal.symbol, signal.bar_close, planned.time_barrier
        ):
            return Fill(quote.received_at, refused=SPECIAL)
        why = refused(decided_at, planned, quote, rules)
        if why == NO_QUOTE:
            return Fill(decided_at, missing=NO_QUOTE)
        if why is not None:
            return Fill(quote.received_at, refused=why)
        sold = self._sale(day, first, planned)
        if sold is None:
            return Fill(decided_at, missing=NO_EXIT_QUOTE)
        sale, taken, until = sold
        return Fill(taken, entry_at=quote.received_at, entry=quote.ask, sale=sale, bought=first, until=until)

    def _sale(self, day: Recording, bought: int, planned: Plan) -> tuple[Sale, datetime, int] | None:
        """The exit of a position bought on quote ``bought``, by the paper exits on the quotes
        received after it, when the book takes it and the quote it is made on (the day's end for the
        clock's); None without a quote to sell on at the time barrier."""
        rules = self._rules
        times, bids = day.quotes.times, day.quotes.bids
        n = len(times)
        end = int(np.searchsorted(times, micros(planned.time_barrier), side="left"))  # at or after it
        k = bought + 1
        while k < n:
            if k < end:  # the first quote before the barrier whose bid takes profit or stops
                window = bids[k:end]
                found = np.flatnonzero((window >= planned.target) | (window <= planned.stop))
                k = k + int(found[0]) if len(found) else end
                if k >= n:
                    break
            quote, standing = day.quote(k), day.quote(k - 1)
            sale = exit_on(planned, quote, standing, rules)
            if sale is None:  # a quote no exit takes: on to the next
                k += 1
                continue
            taken = quote.received_at
            if sale.quote is standing:  # sold at the barrier on the quote before it: the clock may first
                taken = min(taken, planned.time_barrier + rules.grace)
            return sale, taken, k
        # No quote at or after the barrier: the clock's time exit on the last quote, if recent.
        clocked = planned.time_barrier + rules.grace
        sale = clock_exit(planned, day.quote(n - 1), clocked, rules)
        return None if sale is None else (sale, clocked, n)


# Each signal alone.


@dataclass
class Signals:
    """A cell's signals at a θ, each measured alone a share (module doc): how many, those whose data
    is missing and those given up by why, those bought, their exits, those that gained and their net
    in won and in ticks of their reference; by day, the signals measured and their net in ticks (one
    given up nets 0)."""

    signals: int = 0
    missing: Counter[str] = field(default_factory=Counter)
    refused: Counter[str] = field(default_factory=Counter)
    bought: int = 0
    wins: int = 0
    exits: Counter[str] = field(default_factory=Counter)
    net: float = 0.0
    ticks: float = 0.0
    days: dict[date, list[float]] = field(default_factory=dict)  # [signals measured, Σ ticks]

    @property
    def measured(self) -> int:
        return self.signals - sum(self.missing.values())

    def add(self, signal: Signal, filled: Fill, fee_rate: float) -> None:
        self.signals += 1
        if filled.missing is not None:
            self.missing[filled.missing] += 1
            return
        day = self.days.setdefault(kst_day(signal.bar_close), [0, 0.0])
        day[0] += 1
        if filled.refused is not None:
            self.refused[filled.refused] += 1
            return
        assert filled.entry is not None and filled.sale is not None and signal.tick
        net = net_of(filled.entry, filled.sale.price, 1, fee_rate)
        ticks = net / signal.tick
        self.bought += 1
        self.wins += net > 0
        self.exits[filled.sale.reason] += 1
        self.net += net
        self.ticks += ticks
        day[1] += ticks

    def per_signal(self) -> Interval | None:
        """The net in ticks per signal measured (one given up 0), each day's mean one value, with its
        day-level 95% interval (``stats.day_mean``); None from fewer than two days."""
        return day_mean({d: [total / n] for d, (n, total) in self.days.items() if n})

    def as_json(self) -> dict[str, Any]:
        interval = self.per_signal()
        return {
            "signals": self.signals,
            "measured": self.measured,
            "missing": dict(sorted(self.missing.items())),
            "refused": dict(sorted(self.refused.items())),
            "bought": self.bought,
            "wins": self.wins,
            "exits": dict(sorted(self.exits.items())),
            "net": round(self.net, 3),
            "ticks": round(self.ticks, 4),
            "per_signal": None
            if interval is None
            else {"value": interval.value, "low": interval.low, "high": interval.high, "days": interval.n},
        }


# A policy.


@dataclass(frozen=True)
class Ranked:
    """A policy's cell as a replay trades it: its θ (#471: sized by the loss budget, no λ)."""

    cell: Cell
    threshold: float


@dataclass
class Tally:
    """Trades: how many, those that gained, the net in won and the capital's log growth."""

    trades: int = 0
    wins: int = 0
    net: float = 0.0
    growth: float = 0.0

    def add(self, net: float, capital: float) -> None:
        self.trades += 1
        self.wins += net > 0
        self.net += net
        self.growth += math.log1p(net / capital) if net > -capital else -math.inf

    def as_json(self) -> dict[str, Any]:
        growth = self.growth if math.isfinite(self.growth) else None  # a trade that lost the capital
        return {"trades": self.trades, "wins": self.wins, "net": round(self.net, 3), "growth": growth}


@dataclass(frozen=True)
class Result:
    """A policy's replayed days: all its trades, each cell's, the net of each day traded; the
    signals it decided whose data was missing, and why the rises read bought nothing, by why."""

    total: Tally
    cells: Mapping[Cell, Tally]
    days: Mapping[date, float]
    missing: Mapping[str, int] = field(default_factory=dict)
    skipped: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Trade:
    """A trade of a policy's replay (``record``), as the paper trader keeps one."""

    cell: Cell
    prediction_id: int
    bar_close: datetime
    decided_at: datetime
    entry_at: datetime
    entry: float
    quantity: int
    exit_at: datetime
    exit: float
    reason: str
    net: float


class Policy:
    """A policy's replay, a day at a time (module doc): its cells in their rank (``ranked``, the first
    first), held by ``rules`` (the capital, the loss budget, the risk rules and the loss limits, #471);
    the paper account's net so far (its cash), its tallies."""

    def __init__(
        self,
        ranked: Sequence[Ranked],
        rules: Rules,
        sessions: Callable[[date], Session | None],
        record: Callable[[Trade], None] | None = None,
    ) -> None:
        self.ranked = tuple(ranked)
        self.ranks = {r.cell: i for i, r in enumerate(self.ranked)}
        self._capital = rules.capital
        self._rules = rules
        self._sessions = sessions
        self._record = record
        self._realized = 0.0
        # The loss limits, simulated from the first day (#471), and the risk rules' day: its buys and
        # its exits (time, net), in order.
        self.holds = Simulated(rules.limits, date.min, account=lambda: self._realized)
        self._buys = 0
        self._exits: list[tuple[datetime, float]] = []
        self.total = Tally()
        self.cells = {r.cell: Tally() for r in self.ranked}
        self.days: dict[date, float] = {}
        self.missing: Counter[str] = Counter()
        self.skipped: Counter[str] = Counter()
        self._fills: Fills | None = None
        self._held: tuple[datetime, Signal] | None = None  # held or waiting to buy until, and on what

    def result(self) -> Result:
        return Result(self.total, self.cells, self.days, dict(self.missing), dict(self.skipped))

    def day(self, reads: Mapping[Cell, Sequence[Read]], fills: Fills, windows: Sequence[Window]) -> None:
        """The day of ``reads`` (each cell's), on ``fills``, with the special ``windows`` of the day: the
        predictions in the order read, the clock at each rise's deadline (``settle``)."""
        rules = self._rules
        self._fills, self._held = fills, None
        self._buys, self._exits = 0, []
        pending: dict[datetime, list[Rise]] = {}
        seen: dict[datetime, set[Cell]] = {}
        deadlines: list[datetime] = []
        events = sorted(
            ((r.at, r.signal.bar_close, self.ranks[c], r) for c in self.ranks for r in reads.get(c, ())),
            key=lambda e: (e[0], e[1], e[2]),
        )

        def settled(now: datetime) -> None:
            settle(
                pending,
                seen,
                self.ranks,
                known(windows, now),
                now,
                rules,
                skip=self._skip,
                held=self._busy,
                decide=self._decide,
            )

        for at, close, rank, read in events:
            while deadlines and deadlines[0] <= at:  # the clock first, as the trader's ticks
                settled(heapq.heappop(deadlines))
            ranked = self.ranked[rank]
            seen.setdefault(close, set()).add(ranked.cell)
            signal = read.signal
            if not read.on_target:
                self._skip(signal, at, OFF_TARGET)
            else:
                session = self._sessions(kst_day(close))
                planned = plan(signal, rules, session, at, known(windows, at), ranked.threshold)
                if admit(pending, signal, planned, at, self._skip, self._busy):
                    heapq.heappush(deadlines, signal.displayed_at + rules.fresh)
            settled(at)
        while deadlines:
            settled(heapq.heappop(deadlines))
        self._fills = None

    def _skip(self, signal: Signal, at: datetime, reason: str) -> None:
        self.skipped[reason] += 1

    def _busy(self, signal: Signal, at: datetime) -> str | None:
        """Why a rise does not buy at ``at``, as ``PaperTrader._not_now``: a loss limit reached (its
        status), something held or waiting to be bought (``outranked`` when of the same bar close and
        ranked above it, as ``PaperTrader._held_off``), the risk rules (#471, ``risk_held``)."""
        self.holds.roll(at)
        if self.holds.status != RUNNING:
            return self.holds.status
        held = self._held
        if held is not None and at < held[0]:
            of = held[1]
            mine = self.ranks[Cell(signal.kind, signal.predictor)]
            if of.bar_close == signal.bar_close and self.ranks[Cell(of.kind, of.predictor)] < mine:
                return OUTRANKED
            return HOLDING
        return risk_held(self._rules, at, self._buys, self._exits)

    def _decide(self, rise: Rise, at: datetime) -> None:
        assert self._fills is not None
        signal = rise.signal
        filled = self._fills.at(signal, rise.plan, at)
        if filled.missing is not None:  # nothing known of it: it holds nothing
            self.missing[filled.missing] += 1
            return
        self._held = (filled.free_at, signal)
        if filled.refused is not None:
            self.skipped[filled.refused] += 1
            return
        assert filled.entry is not None and filled.entry_at is not None and filled.sale is not None
        rules = self._rules
        cash = self._capital + self._realized  # nothing open: one at a time
        fee_rate = rules.fee_rate
        quantity = size(
            filled.entry, rise.plan.stop, rules.budget(self._capital), self._capital, cash, fee_rate
        )
        if isinstance(quantity, str):  # ``no_shares``, ``bad_stop``
            self.skipped[quantity] += 1
            self._held = (filled.entry_at, signal)
            return
        self._buys += 1
        sale = filled.sale
        limited = self._limited(filled, quantity)
        if limited is not None:  # sold at once by a loss limit, before its own exit
            sale = limited
            self._held = (sale.at, signal)
        net = net_of(filled.entry, sale.price, quantity, fee_rate)
        self.holds.exited(net, sale.at)
        self._exits.append((sale.at, net))
        self._realized += net
        self.total.add(net, self._capital)
        self.cells[rise.cell].add(net, self._capital)
        day = kst_day(signal.bar_close)
        self.days[day] = self.days.get(day, 0.0) + net
        if self._record is not None:
            self._record(
                Trade(
                    rise.cell,
                    signal.prediction_id,
                    signal.bar_close,
                    at,
                    filled.entry_at,
                    filled.entry,
                    quantity,
                    sale.at,
                    sale.price,
                    sale.reason,
                    net,
                )
            )

    def _limited(self, filled: Fill, quantity: int) -> Sale | None:
        """The sale a loss limit makes of ``quantity`` shares bought as ``filled`` (#471, module doc):
        at the best bid of the first quote it holds through at which the day's or the cumulative P&L,
        with the position's open one there (``paper.unrealized``, the same sums), is at or below its
        limit (``limits.Simulated``, which keeps it reached); None if none is. The buy's own quote
        first: ``PaperTrader._take`` marks a position on the quote that fills it, its spread and costs
        open at once, before any exit rule acts."""
        day = self._fills.day if self._fills is not None else None
        start, end = filled.bought, filled.until
        if day is None or start >= end or filled.entry is None:
            return None
        holds, limits, fee_rate = self.holds, self._rules.limits, self._rules.fee_rate
        bids = day.quotes.bids[start:end]
        sold = bids * quantity
        buy_fee = filled.entry * quantity * fee_rate
        held = (bids - filled.entry) * quantity - buy_fee - sold * fee_rate - sold * SELL_TAX
        hit = np.flatnonzero(
            (holds.realized_day + held <= -limits.daily)
            | (holds.realized_cumulative + held <= -limits.cumulative)
        )
        if not len(hit):
            return None
        quote = day.quote(start + int(hit[0]))
        holds.mark(float(held[hit[0]]), quote.received_at)
        return Sale(quote.received_at, quote.bid, SALE[holds.status], quote)


# A pass over the days.


@dataclass(frozen=True)
class Source:
    """A cell the replay reads: its role on the cost map (whose predictions count, ``target_of``) and
    its days (the cost map's for its kind)."""

    role: str
    days: tuple[date, ...]


@dataclass(frozen=True)
class Pass:
    """A pass's outputs: each policy's ``Result``, in the order asked; the cell measured alone, its
    ``Signals`` at each θ and the counts of its predictions read (``read``, ``off_target``, the entry
    rules' reasons, ``rises``)."""

    results: tuple[Result, ...]
    signals: Mapping[float, Signals]
    counts: Mapping[str, int]


class Replayer:
    """The recorded days of ``cells`` (module doc), from the engine's database ``db`` (read-only) and
    the recordings under ``data_dir``, with the special ``windows`` the engine kept; a day at a time,
    a pass at each ``run``, its policies held by ``rules``. ``thresholds``: the θ each signal measured
    alone is tallied at."""

    def __init__(
        self,
        db: sqlite3.Connection,
        data_dir: Path,
        sessions: Callable[[date], Session | None],
        windows: Sequence[Window],
        cells: Mapping[Cell, Source],
        *,
        rules: Rules,
        target_version: int,
        thresholds: Sequence[float],
        meter: Meter | None = None,
    ) -> None:
        self._db = db
        self._data_dir = data_dir
        self._sessions = sessions
        self._windows = sorted(windows, key=lambda w: w.start)
        self._cells = dict(cells)
        self._target_version = target_version
        self._thresholds = tuple(thresholds)
        self.rules = rules  # the trader's (#471: its capital, loss budget, risk rules and loss limits)
        self._meter = meter or Meter()
        self.passes = 0
        self.days_read = 0
        self.peaks: dict[date, float | None] = {}  # each day's peak resident memory (MB), of its passes
        self.recordings: dict[date, str] = {}  # each day's: recorded, gaps (in its session) or no_recording

    def run(
        self,
        policies: Sequence[Sequence[Ranked]],
        alone: Cell | None = None,
        record: Callable[[Trade], None] | None = None,
    ) -> Pass:
        """One pass over the days: each of ``policies`` replayed (its trades to ``record``, if given)
        and ``alone``'s signals measured alone."""
        self.passes += 1
        books = [Policy(p, self.rules, self._sessions, record) for p in policies]
        cells = {r.cell for p in policies for r in p} | ({alone} if alone is not None else set())
        signals = {t: Signals() for t in self._thresholds}
        counts: Counter[str] = Counter()
        for day in sorted({d for c in cells for d in self._cells[c].days}):
            self._meter.reset()
            recording = read_recording(self._data_dir, day)
            self.days_read += 1
            self._note(day, recording)
            windows = self._of_day(day)
            fills = Fills(recording, windows, self.rules)
            reads = {
                c: day_reads(self._db, c, self._cells[c].role, day, self._target_version)
                for c in sorted(cells, key=lambda c: (c.kind, c.predictor))
                if day in self._cells[c].days
            }
            for book in books:
                book.day(reads, fills, windows)
            if alone is not None:
                self._alone(reads.get(alone, ()), fills, windows, signals, counts)
            del recording, fills, reads
            give_back()  # the day's arrays and Arrow's buffers, before the next day's
            peak = self._meter.peak_mb()
            before = self.peaks.get(day)
            self.peaks[day] = peak if before is None or (peak is not None and peak > before) else before
        return Pass(tuple(b.result() for b in books), signals, dict(sorted(counts.items())))

    @property
    def peak_since(self) -> str:
        return self._meter.peak_since

    def _of_day(self, day: date) -> list[Window]:
        """The special windows that touch ``day``."""
        start = datetime.combine(day, time(), KST)
        end = start + timedelta(days=1)
        return [w for w in self._windows if w.start < end and w.end > start]

    def _note(self, day: date, recording: Recording | None) -> None:
        if recording is None:
            self.recordings[day] = NO_RECORDING
            return
        session = self._sessions(day)
        if session is not None and recording.gap_in(micros(session.open), micros(session.close)):
            self.recordings[day] = GAPPED
        else:
            self.recordings[day] = RECORDED

    def _alone(
        self,
        reads: Sequence[Read],
        fills: Fills,
        windows: Sequence[Window],
        signals: Mapping[float, Signals],
        counts: Counter[str],
    ) -> None:
        """Each rise of ``reads`` decided at its read alone, a share (module doc), tallied at each θ its
        probability reaches."""
        rules = self.rules
        for read in reads:
            counts["read"] += 1
            signal = read.signal
            if not read.on_target:
                counts[OFF_TARGET] += 1
                continue
            session = self._sessions(kst_day(signal.bar_close))
            planned = plan(signal, rules, session, read.at, known(windows, read.at), 0.0)  # every rise
            if not isinstance(planned, Plan):
                counts[planned] += 1
                continue
            counts["rises"] += 1
            filled = fills.at(signal, planned, read.at)
            p = signal.rise_probability
            assert p is not None  # planned: made, with its probabilities
            for threshold, tally in signals.items():
                if p >= threshold:
                    tally.add(signal, filled, rules.fee_rate)
