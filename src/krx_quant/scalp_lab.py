"""``krx-quant scalp-lab``: the mock scalp policy's variants replayed over the recorded days and ranked
by their net per day (#711). A measurement: it places no order and changes nothing in the trader.

**Why.** On 2026-11-19 and 11-20 the scalp (#705, ``trader/scalp.py``: target +0.2 % net, the stop
raised at +0.1 % net, stop −0.3 % gross, 15 minutes, on 005930) won 40–50 % of its trades while its
break-even is about 73 %: the costs (commission 0.015 % a side, the sell tax 0.20 %) are most of the
loss. This looks for a variant whose net win rate is above its own break-even with enough trades a day,
before the trader changes.

**Which days**: the latest ``DAYS`` (20) trading days up to the latest whose session is over (``--day``:
up to that one) with 005930's integrated book recorded (``trader.replay.read_recording``).

**The signals** (the engine's, as it stored them on each day: what it knew then, each made by a model
trained on the nights before), of 005930, read at their display (``displayed_at``; nothing after it):

- *the pool* (``POOL``): the 4-minute barrier pool of each bar close's newest verdict
  (``trader.scalp.pool_signal``, today's policy's first source): its up first, and its down first;
- *the member* (``MEMBER``): ``breiman-scalp``'s scalp prediction (``trader.scalp.member_signal``, the
  policy's second source, #706), where it exists: its P(target first), and its P(stop first).

**The instruments** (``pricing.Instrument``, ``products.instrument``): 005930 bought on the rise's
probability (up first, target first), and its inverse ETF 0193L0 bought on the fall's (down first, stop
first): long the ETF when the stock's signal says down. Each on its own recorded quotes (005930's
integrated best quotes, the trader's stream; the ETF's KRX book ``H0STASP0``, #606, its usable records
alone: continuous trading, both sides quoted, the ask above the bid; ``inverse_replay.etf_quotes``), its
own tick (the stock table, the ETF's 1 or 5 won) and its own round trip (the commission
``KRX_FEE_PERCENT`` a side on both; the sell tax 0.20 % on the stock's sale only: an ETF's pays none).

**The variants** (each a row): an instrument, a source, a threshold and a geometry.

- *Threshold* on the source's probability: the pool's ``POOL_THRESHOLDS``, the member's
  ``MEMBER_THRESHOLDS``, and on the stock the member at its break-even too (``BREAK_EVEN``: the
  geometry's binary break-even on the bar close's mid-point, as ``book.scalp_threshold`` buys).
- *Geometry*: the target's net ``TARGETS`` (+0.1, +0.2, +0.3 %), the stop's gross ``STOPS`` (−0.15,
  −0.3, −0.5 %), the deadline ``DEADLINES`` (5, 15, 30 minutes after the bar close), the raised stop
  (at +``RAISE`` % net) off, or on where the target is above it. The policy's own geometry
  (``KRX_TRADER_MOCK_SCALP_*``) is added when the grid lacks it.

**A trade**, one unit, by the paper trader's rules for a scalp (``book.plan_scalp``, ``scalp_levels``,
``scalp_raise``, ``exit_on``), on the instrument's quotes: no buy before 09:05, nor with its deadline
after 15:15, nor with a special window known by its entry quote touching the bar close to the deadline
(``before_entry``, ``past_last_exit``, ``special``); bought at the ask of the first quote received after
the display, within 5 s and before the deadline (else none); none when that ask is at the target set on
the reference already (the stock's bar close mid-point, the ETF's mid-point standing at the bar close:
``target_reached``); none over a recorded gap of its quotes from the display to the deadline. On the fill
price e: the target T the first tick at which it nets the target, the stop e × (1 − stop), the raise
level R the first tick that nets +``RAISE`` %. On each quote received after the buy, in order: the
take-profit at T once a bid reaches it; the stop at the bid once a bid is at or below the stop; else,
with the raise on, the stop raised to R once a bid reaches it (selling from the next quote,
``raised_stop``); at the deadline the bid of the quote standing then if it is at most 5 s old, else of
the first after it (``time``). The trade is the policy-matched outcome: its net after the costs, with
the raised stop, the deadline and the delay from the bar close to the entry the trader has, not the
scalp kind's ``target_first``.

**One position at a time** (as the trader holds the scalp): of a variant's signals in display order, one
read while a trade is held is not taken; one that buys nothing (no quote, a gap, ``target_reached``)
holds nothing. The scalp's own daily caps are not applied to the variants (they measure the signal and
the geometry; the caps are #710's), but to the reproduction below.

**Per variant**: trades per day (over the days its instrument and source were recorded), the net win
rate (net above 0), its break-even measured (mean loss ÷ (mean win + mean loss)) and binary (the stop's
net loss ÷ (the target's net gain + that loss), each on its fill), the net per trade in percent of the
entry and in won per unit, the net per day in percent (a unit's percent summed over the day's trades,
averaged over the days), the lower bound of the net per trade's day-level 95 % interval
(``stats.day_mean``), the largest drawdown of the cumulated percent trade by trade, and the exits.

**The ranking**: the variants with at least ``MIN_TRADES`` (10) trades a day and a lower bound above 0,
by net per day, the best ``TOP``; then, for what came close, the best by net per day of those with
``MIN_TRADES`` a day whatever their bound; and the current policy's rows (the pool at its threshold and
the member at its break-even, its geometry).

**Walk-forward by day**: on each day, the variant the ranking's rule picks from the days before it alone
(at least two), and that day's result of it; a day nothing qualifies holds nothing. Its net per day over
every day is what choosing by this lab would have earned; nothing of a day chooses that day's variant.

**The reproduction** (a check): the policy's current variant (its source of that day, by the live
trades' ``scalp_signal``; its threshold, geometry and its own risk rules, ``book.scalp_held``) replayed
on the stock, with the champion's book held by the live non-scalp trades of the day (one position for
the whole book), against the live scalp trades of the trader's store (``trades.scalp``): the trades each
way, those of the same bar close and of those how many left by the same reason, and the exits each way.
A KIS mock fill differs from the paper one, so "within reason" is the bar.

**Memory**: a day's quotes are held at a time (005930's arrays and the ETF's) and let go; a call's
window of at most 30 minutes is searched by its running maximum and minimum of the bids, made once for
every geometry; what is kept across days is a few numbers per variant and day. The run time and peak
resident memory are printed. Output: counts, rates, percent, ticks and won per unit, no price.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import statistics
import time as clock
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pyarrow as pa

from krx_quant.archive import RawMissing
from krx_quant.config import Settings
from krx_quant.feeds.base import KST
from krx_quant.footprint import Meter, give_back
from krx_quant.inverse_replay import ETF_GAP_FEEDS, closed_gap, etf_quotes, probed_day
from krx_quant.market_calendar import MarketCalendar, Session, calendar_store
from krx_quant.object_store import open_store
from krx_quant.predictions import DB_NAME, read_only
from krx_quant.pricing import Instrument
from krx_quant.products import INVERSE_PRODUCTS, instrument
from krx_quant.raw_days import RawDays
from krx_quant.scalp import NAME as SCALP_KIND
from krx_quant.special import Window, touching
from krx_quant.stats import day_mean
from krx_quant.trader import ENTRY_AFTER_OPEN, EXIT_BEFORE_AUCTION, SYMBOL
from krx_quant.trader.book import EXPIRED, RAISED_STOP, STOP_LOSS, TAKE_PROFIT, TIME, Rules, scalp_held
from krx_quant.trader.cells import KIND_COLUMNS, Signal, special_windows
from krx_quant.trader.db import reading
from krx_quant.trader.replay import day_gaps, known, micros, moment, read_recording
from krx_quant.trader.scalp import MEMBER as LIVE_MEMBER
from krx_quant.trader.scalp import member_signal, pool_signal
from krx_quant.trader.store import CHAMPION_TRADE, SCALP_TRADE, SHADOW_TRADE
from krx_quant.trader.store import DB_NAME as TRADER_DB

log = logging.getLogger(__name__)

TITLE = "스캘프 실험실 (#711)"
ETF_CODE = INVERSE_PRODUCTS[SYMBOL].code
DAYS = 20
TARGETS = (0.1, 0.2, 0.3)  # the target's net, percent of the fill
STOPS = (0.15, 0.3, 0.5)  # the stop below the fill, gross percent
DEADLINES = (5, 15, 30)  # minutes after the bar close
RAISE = 0.1  # the raised stop's net, percent (the policy's ``SCALP_RAISE_PERCENT``)
POOL, MEMBER = "pool", "member"
SOURCES = (POOL, MEMBER)
POOL_THRESHOLDS = (0.2, 0.3, 0.4, 0.5)
MEMBER_THRESHOLDS = (0.4, 0.5, 0.6, 0.7)
BREAK_EVEN = -1.0  # a threshold standing for the geometry's binary break-even (the stock's member)
MEMBER_ALGORITHM = "breiman-scalp"
MIN_TRADES = 10  # trades a day a ranked variant makes at least
TOP = 12  # the rows of each ranked table
WAIT = timedelta(seconds=5)  # a buy's quote comes within this; a time exit's quote is at most this old
NO_DAYS, NO_ENGINE_DB = 3, 1  # exit codes
AGENT_LINE = "에이전트 차례:"
TOLD_FILE = "scalp-lab.json"  # in the state directory: the first night a variant qualified (``AGENT_LINE``)

# Why a call buys nothing (module doc "A trade"): the plan's, the entry's, the data's.
BEFORE_ENTRY, PAST_LAST_EXIT, SPECIAL = "before_entry", "past_last_exit", "special"
NO_QUOTE, TARGET_REACHED, GAP, NO_EXIT_QUOTE = "no_quote", "target_reached", "gap", "no_exit_quote"
EXITS = (TAKE_PROFIT, RAISED_STOP, STOP_LOSS, TIME)
EXIT_WORDS = {TAKE_PROFIT: "목표", RAISED_STOP: "올린손절", STOP_LOSS: "손절", TIME: "기한"}
SOURCE_WORDS = {POOL: "풀", MEMBER: "멤버"}
LEFT_WORDS = {
    BEFORE_ENTRY: "09:05 전",
    PAST_LAST_EXIT: "기한이 15:15 뒤",
    SPECIAL: "특수 상황",
    EXPIRED: "기한 지남",
    NO_QUOTE: "5초 안에 살 호가 없음",
    TARGET_REACHED: "살 때 이미 목표",
    GAP: "결측 구간",
    NO_EXIT_QUOTE: "팔 호가 없음",
}

_WAIT_US = WAIT // timedelta(microseconds=1)
_MINUTE_US = 60_000_000


# The variants.


@dataclass(frozen=True)
class Geometry:
    """A scalp's levels: the target's net and the stop's gross in percent of the fill, the deadline in
    minutes after the bar close, the raised stop's net in percent (None: off)."""

    target: float
    stop: float
    minutes: int
    raised: float | None = None

    @property
    def label(self) -> str:
        up = f" 올림{self.raised:g}" if self.raised is not None else ""
        return f"+{self.target:g}/-{self.stop:g}/{self.minutes}분{up}"


def grid(current: Geometry | None = None) -> list[Geometry]:
    """The geometries measured (module doc "The variants"), and ``current`` if the grid lacks it."""
    found = [
        Geometry(t, s, m, r)
        for t in TARGETS
        for s in STOPS
        for m in DEADLINES
        for r in (None, RAISE)
        if r is None or t > r
    ]
    if current is not None and current not in found:
        found.append(current)
    return found


@dataclass(frozen=True)
class Variant:
    leg: str  # the instrument's code
    source: str
    threshold: float  # ``BREAK_EVEN``: the geometry's
    geometry: Geometry

    @property
    def label(self) -> str:
        side = "↑" if self.leg == SYMBOL else "↓"
        at = "BE" if self.threshold == BREAK_EVEN else f"{self.threshold:.2f}"
        return f"{self.leg} {SOURCE_WORDS[self.source]}{side}≥{at} {self.geometry.label}"


def thresholds(leg: str, source: str) -> tuple[float, ...]:
    if source == POOL:
        return POOL_THRESHOLDS
    return (*MEMBER_THRESHOLDS, BREAK_EVEN) if leg == SYMBOL else MEMBER_THRESHOLDS


def current_geometry(rules: Rules) -> Geometry:
    scalp = rules.scalp
    minutes = round(scalp.deadline.total_seconds() / 60)
    raised = scalp.raise_percent if scalp.raise_percent < scalp.target_percent else None
    return Geometry(scalp.target_percent, scalp.stop_percent, minutes, raised)


# The quotes.


@dataclass(frozen=True)
class Leg:
    """An instrument's quotes of a day in receipt order (microseconds since the epoch, each newer than
    the one before) and its recorded gaps (an end None: still open)."""

    code: str
    instrument: Instrument
    times: np.ndarray
    bids: np.ndarray
    asks: np.ndarray
    gaps: tuple[tuple[int, int | None], ...] = ()

    def gap_in(self, start: int, end: int) -> bool:
        return any(s < end and (e is None or e > start) for s, e in self.gaps)


def stock_leg(data_dir: Path, day: date) -> Leg | None:
    recording = read_recording(data_dir, day)
    if recording is None:
        return None
    q = recording.quotes
    return Leg(SYMBOL, instrument(SYMBOL), q.times, q.bids, q.asks, recording.gaps)


def etf_leg(data_dir: Path, raw: RawDays, day: date) -> Leg | None:
    """The ETF's usable records of ``day`` (module doc "The instruments"); None without them, or when
    they cannot be read (logged)."""
    try:
        etf = etf_quotes(data_dir, raw, day)
        if etf is None or not etf.usable.any():
            return None
        gaps = tuple(closed_gap(g, etf.times) for g in day_gaps(data_dir, day, ETF_GAP_FEEDS, ETF_CODE))
    except (OSError, RawMissing, pa.ArrowException) as e:
        log.warning("scalp lab: cannot read the ETF's recording of %s (%s): no ETF variant that day", day, e)
        return None
    keep = etf.usable
    times, bids, asks = etf.times[keep], etf.bids[keep], etf.asks[keep]
    later = np.ones(len(times), dtype=bool)
    later[1:] = times[1:] > times[:-1]
    return Leg(ETF_CODE, instrument(ETF_CODE), times[later], bids[later], asks[later], gaps)


# The signals.


@dataclass(frozen=True, slots=True)
class Call:
    """A signal of a source at a bar close, read at its display: the rise's probability and the
    fall's, and the stock's bar close mid-point."""

    source: str
    bar_close: datetime
    at: datetime
    up: float
    down: float
    anchor: float


def _call(source: str, signal: Signal) -> Call | None:
    p = signal.probabilities
    if signal.status != "ok" or p is None or not signal.anchor:
        return None
    return Call(source, signal.bar_close, signal.displayed_at, p[0], p[2], signal.anchor)


def day_calls(db: sqlite3.Connection, day: date) -> dict[str, list[Call]]:
    """``day``'s calls by source (module doc "The signals"), in display order."""
    start = datetime.combine(day, time(), KST)
    span = (start.isoformat(), (start + timedelta(days=1)).isoformat())
    found: dict[str, list[Call]] = {POOL: [], MEMBER: []}
    if _has(db, "verdicts"):
        rows = db.execute(
            "SELECT rowid AS id, bar_close, version, made_at, body FROM verdicts"
            " WHERE symbol = ? AND bar_close >= ? AND bar_close < ? ORDER BY bar_close, version",
            (SYMBOL, *span),
        ).fetchall()
        for row in {r["bar_close"]: r for r in rows}.values():  # each bar close's newest version
            try:
                call = _call(POOL, pool_signal(row, SYMBOL))
            except (ValueError, KeyError, TypeError):
                continue
            if call is not None:
                found[POOL].append(call)
    if _has(db, "kind_predictions"):
        rows = db.execute(
            f"SELECT {KIND_COLUMNS} FROM kind_predictions p WHERE p.kind = ? AND p.symbol = ?"
            " AND p.algorithm = ? AND p.bar_close >= ? AND p.bar_close < ? ORDER BY p.bar_close, p.id",
            (SCALP_KIND, SYMBOL, MEMBER_ALGORITHM, *span),
        ).fetchall()
        for row in rows:
            call = _call(MEMBER, member_signal(row, MEMBER_ALGORITHM, SYMBOL))
            if call is not None:
                found[MEMBER].append(call)
    for calls in found.values():
        calls.sort(key=lambda c: c.at)
    return found


def _has(db: sqlite3.Connection, table: str) -> bool:
    row = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
    return row is not None


# A trade.


@dataclass(frozen=True, slots=True)
class Outcome:
    """A call's trade, a unit (module doc "A trade"): decided and freed (microseconds), its net in won
    and in percent of the entry, how it ended, and what the target nets and the stop loses on its fill
    (percent, the loss positive)."""

    bar_close: int
    at: int
    free: int
    net: float
    pct: float
    reason: str
    gain: float
    loss: float


@dataclass(frozen=True)
class Entry:
    """A call's entry on a leg and the bids after it up to the longest deadline, with their running
    maximum and minimum: what every geometry's exit is found in."""

    first: int  # the entry quote's index
    ask: float
    reference: float  # what the plan's target is set on (module doc "A trade")
    bids: np.ndarray
    high: np.ndarray
    low: np.ndarray


def entry(leg: Leg, call: Call, longest: int) -> Entry | str:
    """``call``'s entry on ``leg`` and its window, or why it buys nothing (no quote within 5 s)."""
    shown = micros(call.at)
    first = int(np.searchsorted(leg.times, shown, side="right"))
    if first >= len(leg.times) or leg.times[first] > shown + _WAIT_US:
        return NO_QUOTE
    if leg.code == SYMBOL:
        reference = call.anchor
    else:
        standing = int(np.searchsorted(leg.times, micros(call.bar_close), side="right")) - 1
        if standing < 0:
            return NO_QUOTE
        reference = (float(leg.bids[standing]) + float(leg.asks[standing])) / 2
    end = int(np.searchsorted(leg.times, micros(call.bar_close) + longest * _MINUTE_US, side="left"))
    bids = leg.bids[first + 1 : max(first + 1, end)]
    high = np.maximum.accumulate(bids) if len(bids) else bids
    low = np.minimum.accumulate(bids) if len(bids) else bids
    return Entry(first, float(leg.asks[first]), reference, bids, high, low)


def plan_refused(call: Call, minutes: int, session: Session, windows: Sequence[Window]) -> str | None:
    """Why the plan buys nothing whatever the quotes (``book.plan_scalp``'s session rules)."""
    deadline = call.bar_close + timedelta(minutes=minutes)
    if call.bar_close < session.open + ENTRY_AFTER_OPEN:
        return BEFORE_ENTRY
    if deadline > session.closing_auction - EXIT_BEFORE_AUCTION:
        return PAST_LAST_EXIT
    if call.at >= deadline:
        return EXPIRED
    if touching(known(windows, call.at), SYMBOL, call.bar_close, deadline):
        return SPECIAL
    return None


def trade(
    leg: Leg, call: Call, window: Entry, geometry: Geometry, fee_rate: float, windows: Sequence[Window]
) -> Outcome | str:
    """``call``'s trade on ``leg`` with ``geometry`` (module doc "A trade"), or why it has none."""
    inst = leg.instrument
    close = micros(call.bar_close)
    deadline = close + geometry.minutes * _MINUTE_US
    shown, entered = micros(call.at), int(leg.times[window.first])
    if entered >= deadline:
        return NO_QUOTE
    if leg.gap_in(shown, deadline):
        return GAP
    if touching(known(windows, moment(entered)), SYMBOL, call.bar_close, moment(deadline)):
        return SPECIAL
    if window.ask >= inst.netting(window.reference, geometry.target, fee_rate):
        return TARGET_REACHED
    e = window.ask
    target = inst.netting(e, geometry.target, fee_rate)
    stop = e * (1 - geometry.stop / 100)
    end = int(np.searchsorted(leg.times, deadline, side="left"))  # the first quote at or after it
    n = min(len(window.bids), max(0, end - window.first - 1))  # the window's quotes before it
    hit = int(np.searchsorted(window.high[:n], target, side="left"))
    fell = int(np.searchsorted(-window.low[:n], -stop, side="left"))
    k, reason, price = min(hit, fell), "", 0.0
    if k < n:
        reason, price = (TAKE_PROFIT, float(target)) if hit <= fell else (STOP_LOSS, float(window.bids[k]))
    if geometry.raised is not None:
        level = inst.netting(e, geometry.raised, fee_rate)
        r = int(np.searchsorted(window.high[:n], level, side="left"))
        if level < target and r < k:  # raised on quote r, selling from the next
            # Up to the exit found (a stop then is the raised one's), the first bid at or below it.
            below = np.flatnonzero(window.bids[r + 1 : min(k + 1, n)] <= level)
            if len(below):
                k = r + 1 + int(below[0])
                reason, price = RAISED_STOP, float(window.bids[k])
    if reason:
        exit_at = int(leg.times[window.first + 1 + k])
    else:  # the deadline: the quote standing then if at most 5 s old, else the first after it
        standing = end - 1
        if 0 < deadline - int(leg.times[standing]) <= _WAIT_US:
            exit_at, price = deadline, float(leg.bids[standing])
        elif end < len(leg.times):
            exit_at, price = int(leg.times[end]), float(leg.bids[end])
        else:
            return NO_EXIT_QUOTE
        reason = TIME
    net = inst.net(e, price, fee_rate)
    gain = inst.net(e, target, fee_rate) / e * 100
    loss = -inst.net(e, stop, fee_rate) / e * 100
    return Outcome(close, shown, exit_at, net, net / e * 100, reason, gain, loss)


def break_even(inst: Instrument, reference: float, geometry: Geometry, fee_rate: float) -> float:
    """The geometry's binary break-even on ``reference`` (``book.ScalpRules.break_even``'s): the stop's
    net loss over the target's net gain plus that loss."""
    won = inst.net(reference, inst.netting(reference, geometry.target, fee_rate), fee_rate)
    lost = -inst.net(reference, reference * (1 - geometry.stop / 100), fee_rate)
    return lost / (won + lost)


@dataclass
class DayLeg:
    """A leg's and a source's day: each call's probability and, by geometry, its trade (None: none),
    and for the stock's member each call's break-even by geometry."""

    probability: list[float]
    trades: dict[Geometry, list[Outcome | None]]
    break_even: dict[Geometry, list[float]]
    left_out: Counter[str] = field(default_factory=Counter)


def day_leg(
    leg: Leg,
    source: str,
    calls: Sequence[Call],
    geometries: Sequence[Geometry],
    session: Session,
    windows: Sequence[Window],
    fee_rate: float,
) -> DayLeg:
    """Every call's trade on ``leg`` for each geometry (one window per call, module doc "Memory")."""
    longest = max(g.minutes for g in geometries)
    rise = leg.code == SYMBOL
    out = DayLeg([c.up if rise else c.down for c in calls], {g: [] for g in geometries}, {})
    if rise and source == MEMBER:
        out.break_even = {
            g: [break_even(leg.instrument, c.anchor, g, fee_rate) for c in calls] for g in geometries
        }
    minutes = sorted({g.minutes for g in geometries})
    for call in calls:
        window = entry(leg, call, longest)
        refused = {m: plan_refused(call, m, session, windows) for m in minutes}
        for g in geometries:
            why = refused[g.minutes]
            made = why if why is not None else window
            if not isinstance(made, Entry):
                out.trades[g].append(None)
                out.left_out[made] += 1
                continue
            done = trade(leg, call, made, g, fee_rate, windows)
            if isinstance(done, str):
                out.left_out[done] += 1
                out.trades[g].append(None)
            else:
                out.trades[g].append(done)
    return out


# One position at a time.


Held = Callable[[Outcome, Sequence[Outcome]], bool]


def one_at_a_time(
    trades: Sequence[Outcome | None],
    probability: Sequence[float],
    threshold: float | Sequence[float],
    held: Held | None = None,
) -> list[Outcome]:
    """The trades a variant makes (module doc "One position at a time"): in display order, each call at
    its threshold or more that buys, unless a trade is held at its display (or ``held`` says so)."""
    taken: list[Outcome] = []
    free = -1
    for i, (done, p) in enumerate(zip(trades, probability, strict=True)):
        at = threshold if isinstance(threshold, float) else threshold[i]
        if done is None or p < at or done.at < free:
            continue
        if held is not None and held(done, taken):
            continue
        taken.append(done)
        free = done.free
    return taken


# The tallies.


@dataclass
class Tally:
    """A variant's trades over the days (module doc "Per variant"): by day the trades and their net
    percent and won, the wins and losses, the levels' percents, the exits, the drawdown."""

    days: dict[date, tuple[int, float, float]] = field(default_factory=dict)
    wins: int = 0
    won: float = 0.0
    losses: int = 0
    lost: float = 0.0
    gain: float = 0.0
    loss: float = 0.0
    exits: Counter[str] = field(default_factory=Counter)
    cum: float = 0.0
    peak: float = 0.0
    drawdown: float = 0.0

    def add(self, day: date, trades: Sequence[Outcome]) -> None:
        self.days[day] = (len(trades), sum(t.pct for t in trades), sum(t.net for t in trades))
        for t in trades:
            if t.pct > 0:
                self.wins += 1
                self.won += t.pct
            else:
                self.losses += 1
                self.lost -= t.pct
            self.gain += t.gain
            self.loss += t.loss
            self.exits[t.reason] += 1
            self.cum += t.pct
            self.peak = max(self.peak, self.cum)
            self.drawdown = max(self.drawdown, self.peak - self.cum)

    @property
    def trades(self) -> int:
        return self.wins + self.losses


@dataclass(frozen=True)
class Summary:
    days: int
    trades: int
    per_day: float
    net_day: float  # percent a day
    low: float | None  # the net per trade's day-level lower bound, percent


def summary(tally: Tally, days: Iterable[date] | None = None) -> Summary:
    """``tally`` over ``days`` (all its days by default)."""
    kept = [(d, v) for d, v in tally.days.items() if days is None or d in days]
    if not kept:
        return Summary(0, 0, 0.0, 0.0, None)
    n = sum(v[0] for _, v in kept)
    interval = day_mean({d: [v[1] / v[0]] for d, v in kept if v[0]})
    low = None if interval is None else interval.low
    return Summary(len(kept), n, n / len(kept), sum(v[1] for _, v in kept) / len(kept), low)


def qualifies(s: Summary) -> bool:
    return s.per_day >= MIN_TRADES and s.low is not None and s.low > 0


def best(tallies: Mapping[Variant, Tally], days: Iterable[date] | None = None) -> Variant | None:
    """The variant the ranking's rule picks on ``days`` (module doc "The ranking"); None if none."""
    chosen = set(days) if days is not None else None
    found = [(summary(t, chosen), v) for v, t in tallies.items()]
    ranked = sorted(((s.net_day, v) for s, v in found if qualifies(s)), key=lambda x: (-x[0], x[1].label))
    return ranked[0][1] if ranked else None


# The reproduction.


@dataclass(frozen=True)
class Live:
    """The live trades of a day (the trader's store): the scalp's (bar close, exit reason, source) and
    the champion's book's other trades' spans (microseconds; an end None: still open)."""

    scalp: list[tuple[int, str | None, str | None]]
    busy: list[tuple[int, int | None]]


def live_day(path: Path, day: date) -> Live | None:
    """``day``'s live trades in the trader's store at ``path``; None without the store or its scalp."""
    start = datetime.combine(day, time(), KST)
    span = (start.isoformat(), (start + timedelta(days=1)).isoformat())
    try:
        with reading(path) as db:
            if db is None:
                return None
            rows = db.execute(
                "SELECT t.bar_close, t.entry_at, t.exit_at, t.exit_reason, t.scalp_signal,"
                f" {SCALP_TRADE} AS is_scalp FROM trades t"
                f" WHERE t.entry_at >= ? AND t.entry_at < ? AND {CHAMPION_TRADE} AND NOT {SHADOW_TRADE}"
                " ORDER BY t.entry_at",
                span,
            ).fetchall()
    except sqlite3.Error as e:
        log.warning("scalp lab: cannot read the trader's store (%s): no reproduction", e)
        return None
    scalp: list[tuple[int, str | None, str | None]] = []
    busy: list[tuple[int, int | None]] = []
    for r in rows:
        if r["is_scalp"]:
            try:
                source = (json.loads(r["scalp_signal"] or "{}") or {}).get("source")
            except ValueError:
                source = None
            scalp.append((micros(datetime.fromisoformat(r["bar_close"])), r["exit_reason"], source))
        else:
            end = None if r["exit_at"] is None else micros(datetime.fromisoformat(r["exit_at"]))
            busy.append((micros(datetime.fromisoformat(r["entry_at"])), end))
    return Live(scalp, busy)


def policy_held(rules: Rules, busy: Sequence[tuple[int, int | None]]) -> Held:
    """The policy's own risk rules (``book.scalp_held``) and the live book's other trades holding it."""

    def held(done: Outcome, taken: Sequence[Outcome]) -> bool:
        if any(s <= done.at and (e is None or done.at < e) for s, e in busy):
            return True
        exits = [(moment(t.free), t.net) for t in taken]
        return scalp_held(rules, moment(done.at), len(taken), exits) is not None

    return held


@dataclass
class Reproduced:
    day: date
    source: str
    live: int
    replayed: int
    same_close: int
    same_exit: int
    live_exits: Counter[str]
    replayed_exits: Counter[str]


def reproduce(
    day: date, live: Live, made: Mapping[str, DayLeg], geometry: Geometry, rules: Rules
) -> Reproduced:
    """The current variant replayed against the day's live scalp trades (module doc "The reproduction")."""
    sources = Counter(MEMBER if s == LIVE_MEMBER else POOL for _, _, s in live.scalp)
    source = sources.most_common(1)[0][0] if sources else POOL
    leg = made.get(source)
    replayed: list[Outcome] = []
    if leg is not None and geometry in leg.trades:
        threshold: float | Sequence[float] = (
            leg.break_even[geometry] if source == MEMBER else float(rules.scalp.threshold)
        )
        replayed = one_at_a_time(
            leg.trades[geometry], leg.probability, threshold, policy_held(rules, live.busy)
        )
    by_close = {t.bar_close: t.reason for t in replayed}
    same = [(r, by_close[c]) for c, r, _ in live.scalp if c in by_close]
    return Reproduced(
        day,
        source,
        len(live.scalp),
        len(replayed),
        len(same),
        sum(a == b for a, b in same),
        Counter(r or "open" for _, r, _ in live.scalp),
        Counter(t.reason for t in replayed),
    )


# The run.


@dataclass
class Lab:
    days: list[date]
    variants: int = 0
    tallies: dict[Variant, Tally] = field(default_factory=dict)
    calls: Counter[str] = field(default_factory=Counter)
    left_out: dict[tuple[str, str], Counter[str]] = field(default_factory=dict)
    reproduced: list[Reproduced] = field(default_factory=list)


def run(
    db: sqlite3.Connection,
    data_dir: Path,
    raw: RawDays,
    calendar: MarketCalendar,
    days: Sequence[date],
    rules: Rules,
    trader_db: Path | None = None,
) -> Lab:
    """The lab over ``days`` (module doc), a day's recording at a time."""
    db.row_factory = sqlite3.Row
    current = current_geometry(rules)
    geometries = grid(current)
    lab = Lab(list(days))
    for day in days:
        session = calendar.session(day)
        if session is None:
            continue
        windows = special_windows(db, SYMBOL, datetime.combine(day, time(), KST))
        calls = day_calls(db, day)
        for source, found in calls.items():
            lab.calls[source] += len(found)
        stock = stock_leg(data_dir, day)
        legs = [leg for leg in (stock, etf_leg(data_dir, raw, day) if stock is not None else None) if leg]
        made_stock: dict[str, DayLeg] = {}
        for leg in legs:
            for source in SOURCES:
                if not calls[source]:
                    continue
                made = day_leg(leg, source, calls[source], geometries, session, windows, rules.fee_rate)
                lab.left_out.setdefault((leg.code, source), Counter()).update(made.left_out)
                if leg.code == SYMBOL:
                    made_stock[source] = made
                for g in geometries:
                    for threshold in thresholds(leg.code, source):
                        at = made.break_even[g] if threshold == BREAK_EVEN else threshold
                        taken = one_at_a_time(made.trades[g], made.probability, at)
                        lab.tallies.setdefault(Variant(leg.code, source, threshold, g), Tally()).add(
                            day, taken
                        )
        if trader_db is not None and made_stock:
            live = live_day(trader_db, day)
            if live is not None and live.scalp:
                lab.reproduced.append(reproduce(day, live, made_stock, current, rules))
        del legs, stock, made_stock
        give_back()
    lab.variants = len(lab.tallies)
    return lab


def recorded_days(data_dir: Path, through: date) -> list[date]:
    """The days up to ``through`` with 005930's integrated book recorded."""
    from krx_quant.feeds import BOOK

    found = set()
    for partition in (data_dir / "events").glob(f"date=*/feed={BOOK.tr_id}/symbol={SYMBOL}"):
        try:
            day = date.fromisoformat(partition.parent.parent.name.removeprefix("date="))
        except ValueError:
            continue
        if day <= through:
            found.add(day)
    return sorted(found)


# The report.


def _p(value: float | None, spec: str = "+.3f") -> str:
    return "-" if value is None or math.isnan(value) else f"{value:{spec}}"


HEADER = (
    "변형 | 하루매매 | 이김 | 손익분기 잰/이진 | 매매당 %·원 | 하루 % | 하한 % | 최대낙폭 %"
    " | 목표/올린손절/손절/기한"
)


def row(variant: Variant, tally: Tally) -> str:
    s = summary(tally)
    n = tally.trades
    win = f"{100 * tally.wins / n:.0f}%" if n else "-"
    mean_win = tally.won / tally.wins if tally.wins else None
    mean_loss = tally.lost / tally.losses if tally.losses else None
    measured = mean_loss / (mean_win + mean_loss) if mean_win is not None and mean_loss is not None else None
    binary = tally.loss / (tally.gain + tally.loss) if n and tally.gain + tally.loss else None
    pct = sum(v[1] for v in tally.days.values()) / n if n else None
    won = sum(v[2] for v in tally.days.values()) / n if n else None
    exits = "/".join(str(tally.exits[e]) for e in EXITS)
    return (
        f"{variant.label} | {s.per_day:.1f} | {win} | {_p(measured, '.2f')}/{_p(binary, '.2f')} |"
        f" {_p(pct)}·{_p(won, '+.1f')} | {_p(s.net_day)} | {_p(s.low)} | {tally.drawdown:.2f} | {exits}"
    )


def report_lines(lab: Lab, rules: Rules) -> list[str]:
    """The report's sections (module doc), no price."""
    out = ["1. 신호와 뺀 것"]
    for source in SOURCES:
        out.append(f"  {SOURCE_WORDS[source]}: 호출 {lab.calls[source]}")
    for (leg, source), reasons in sorted(lab.left_out.items()):
        words = " · ".join(f"{LEFT_WORDS.get(r, r)} {n}" for r, n in sorted(reasons.items()))
        out.append(f"  {leg} {SOURCE_WORDS[source]} (기하마다 센 합): {words or '없음'}")
    found = [(summary(t), v, t) for v, t in lab.tallies.items()]
    ranked = sorted((x for x in found if qualifies(x[0])), key=lambda x: (-x[0].net_day, x[1].label))
    near = sorted((x for x in found if x[0].per_day >= MIN_TRADES), key=lambda x: (-x[0].net_day, x[1].label))
    out += [
        "",
        f"2. 순위: 하루 {MIN_TRADES}회 이상, 매매당 순손익 하한 > 0, 하루 순손익(%) 순",
        f"  {HEADER}",
    ]
    out += [f"  {row(v, t)}" for _, v, t in ranked[:TOP]] or ["  없음"]
    out += ["", f"3. 참고: 하루 {MIN_TRADES}회 이상 중 하루 순손익 상위(하한 무관)", f"  {HEADER}"]
    out += [f"  {row(v, t)}" for _, v, t in near[:TOP]] or ["  없음"]
    current = current_geometry(rules)
    out += ["", "4. 지금 정책(같은 기하)", f"  {HEADER}"]
    for variant in (
        Variant(SYMBOL, POOL, float(rules.scalp.threshold), current),
        Variant(SYMBOL, MEMBER, BREAK_EVEN, current),
    ):
        tally = lab.tallies.get(variant)
        out.append(f"  {row(variant, tally)}" if tally is not None else f"  {variant.label} | 신호 없음")
    out += ["", "5. 날 단위 전진(그날 전의 날들만으로 고른 변형의 그날 결과)"]
    every = sorted({d for t in lab.tallies.values() for d in t.days})
    walked: dict[date, list[float]] = {}
    for k, day in enumerate(every):
        chosen = best(lab.tallies, every[:k]) if k >= 2 else None
        if chosen is None:
            walked[day] = [0.0]
            out.append(f"  {day}: 고른 변형 없음")
            continue
        n, pct, _ = lab.tallies[chosen].days.get(day, (0, 0.0, 0.0))
        walked[day] = [pct]
        out.append(f"  {day}: {chosen.label} · 매매 {n} · 순손익 {pct:+.3f}%")
    interval = day_mean(walked)
    if walked:
        mean = statistics.fmean(v[0] for v in walked.values())
        bound = "" if interval is None else f", 95% [{interval.low:+.3f}, {interval.high:+.3f}]"
        out.append(f"  하루 평균 {mean:+.3f}%{bound} ({len(walked)}일, 고르지 못한 날 0)")
    out += ["", "6. 재현: 지금 변형의 재생 대 실제 스캘프 매매(정책의 위험 규칙과 장부의 다른 매매 포함)"]
    for r in lab.reproduced:
        live = " ".join(f"{EXIT_WORDS.get(e, e)} {n}" for e, n in sorted(r.live_exits.items()))
        again = " ".join(f"{EXIT_WORDS.get(e, e)} {n}" for e, n in sorted(r.replayed_exits.items()))
        out.append(
            f"  {r.day} ({SOURCE_WORDS[r.source]}): 실제 {r.live} · 재생 {r.replayed}"
            f" · 같은 봉 마감 {r.same_close} · 그중 같은 청산 {r.same_exit}"
            f" · 청산 실제 [{live}] 재생 [{again or '없음'}]"
        )
    if not lab.reproduced:
        out.append("  실제 스캘프 매매가 있는 날이 없다")
    return out


def report(
    settings: Settings, calendar: MarketCalendar, through: date, raw: RawDays, rules: Rules, days: int = DAYS
) -> tuple[str, int]:
    """The report of the latest ``days`` recorded days up to ``through``, and the exit code."""
    started, meter = clock.monotonic(), Meter()
    chosen = [d for d in recorded_days(settings.data_dir, through) if calendar.session(d) is not None][-days:]
    head = [
        f"{TITLE}: {SYMBOL}과 인버스 ETF {ETF_CODE}의 스캘프 변형을 기록된 날로 재생한다(매매하지 않는다)",
        '개수, 비율, %, 원/1주만 보인다(가격은 없다). 규칙은 `docs/operations.md` "스캘프 실험실 (#711)".',
    ]
    if not chosen:
        return "\n".join([*head, f"{through}까지 {SYMBOL}의 호가가 기록된 거래일이 없다."]), NO_DAYS
    with read_only(settings.state_dir / DB_NAME) as db:
        if db is None:
            return "\n".join([*head, "엔진 DB가 없다."]), NO_ENGINE_DB
        lab = run(db, settings.data_dir, raw, calendar, chosen, rules, settings.state_dir / TRADER_DB)
    out = [
        *head,
        f"재생한 날: {chosen[0]} – {chosen[-1]}, {len(chosen)}거래일 · 변형 {lab.variants}개"
        f" (기하 {len(grid(current_geometry(rules)))} × 종목·신호·문턱)",
        "",
        *report_lines(lab, rules),
    ]
    peak = meter.peak_mb()
    out += [
        "",
        "7. 실행",
        f"  {clock.monotonic() - started:.1f}초 · 최대 RSS {'-' if peak is None else f'{peak:.0f} MB'}"
        f" · raw에서 다시 읽은 파티션 {raw.restored}",
    ]
    if best(lab.tallies) is not None and not (settings.state_dir / TOLD_FILE).is_file():
        _tell(settings.state_dir, chosen[-1])
        out += [
            "",
            f"{AGENT_LINE} 처음으로 순위(2절)에 든 변형이 있다. 전진 결과(5절)와 재현(6절)을 보고 정책을 바꿀"
            " 안을 이 이슈에 남기고 사용자에게 돌린다(trader는 바꾸지 않는다).",
        ]
    return "\n".join(out), 0


def _tell(state_dir: Path, day: date) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / TOLD_FILE
    path.with_suffix(".tmp").write_text(json.dumps({"agent_line": day.isoformat()}) + "\n", encoding="utf-8")
    path.with_suffix(".tmp").replace(path)


def scalp_lab(
    settings: Settings, rules: Rules, now: datetime, day: date | None = None, days: int = DAYS
) -> tuple[str, int]:
    """The command: the report up to ``day``, by default the latest trading day whose session is over."""
    calendar = calendar_store(settings).calendar
    through = probed_day(calendar, now) if day is None else day
    with RawDays(settings.data_dir, open_store(settings)) as raw:
        return report(settings, calendar, through, raw, rules, days)
