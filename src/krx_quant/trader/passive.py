"""A passive entry measured on the recordings (#432, ``krx-quant measure-passive-entry``): how a buy
resting at the best bid would have done against the trader's marketable entry, on the same signals.
A measurement for a decision: the trader's ordering is not changed (it buys at the best ask,
``paper.py``, ``mock.py``).

**Which signals.** Tonight's cost map of 005930 (``GET /v1/cost-map``, as the trader reads it at
08:30 and the re-fit at night) names the cells and each kind's days (the latest 20 with
predictions). Every cell with a predictor is read, whatever its verdict: none passes at a market
order's round trip now, and the question is whether a cheaper entry would. Each cell's stored
predictions of those days are read as the re-fit reads them (``refit.rises``: made, their answer
known, no special situation in their window; an algorithm's by models trained on its target). A
signal is a rise ``paper.plan`` lets buy at the cell's θ: its most likely class the rise at θ or
above, its reference known, its bar close from 09:05 and its time barrier by 15:15, no special
window touching it, a final consensus the engine did not block. θ is the champion policy's for the
cell (#399), else ``KRX_TRADER_THRESHOLD``. Each signal stands alone: no position is held across
signals and the cells are not ranked (the trader and the re-fit hold one position at a time).

**The decision** is the prediction's display (``displayed_at``; the trader reads every half
second). Its first quote is the first best quote received after it, within ``Rules.quote_wait``:
the integrated book's and the integrated trades' best quotes, the trader's stream
(``entry_delay.day_quotes``, ``quotes.py``). Both entries start there.

- **Marketable** (the trader's now): bought at that quote's best ask. Neither entry buys when that
  ask is at or above the target (``target_reached``, as the paper trader).
- **Passive**: a buy resting at that quote's best bid from its receipt, cancelled at the time
  barrier. It fills at the bid on KRX's own trades (``H0STCNT0``: the trader orders on KRX alone,
  ``kis_orders.KRX``) received after the post and before the barrier, by one of two assumptions,
  as the queue ahead of it is not known:
  - conservative (``CONSERVATIVE``): a trade below the bid (the level was traded through);
  - optimistic (``OPTIMISTIC``): a trade at the bid or below.
  A first quote whose bid is at or above its ask (locked or crossed: the two venues' quotes meet)
  makes the buy marketable: it fills at once at the ask, as the market entry does.
- **Exits**, alike for both, by the paper fills (``paper.py``) on the quotes received after the
  fill: the take-profit at the target once a best bid reaches it, the stop at the first best bid
  at or below the stop, the time exit at the best bid of the last quote received in the
  ``Rules.quote_wait`` before the time barrier, else at the first one after it.
- **Net** per share, in ticks of the reference: the sale less the buy, less both commissions and
  the sell tax on the sale (``paper.costs``), over the tick.
- **A hit** is the stored answer's class the rise, as the cost map counts the hit rate.

**Missing data**, counted apart and left out of both entries: a day without its recorded book or
KRX trades (``NO_RECORDING``); a recorded gap (the ``gaps`` layer) of 005930's book, integrated or
KRX trades in the signal's window, from the decision to the time barrier (``GAP``); no quote within
``Rules.quote_wait`` after the decision (``NO_QUOTE``); no quote to sell on at the time barrier
(``NO_EXIT_QUOTE``).

**The report**, for all the signals, each kind and each cell: the counts (read, kept out by the
entry rules, missing, ``target_reached``, measured); the market entry's hit rate and net per trade;
and for each assumption the fill rate (and how many filled at once on a locked quote), the hit rate
of the filled and of the unfilled beside the market entry's of all (filled ones hitting less is the
adverse selection), the net per filled trade and per signal (unfilled 0), the market entry's net on
the same filled signals, the passive net less the market's per signal with its day-level 95%
interval (each day's mean one value, ``stats.day_mean``), the median wait from the post to the
fill, and each entry's exits. Ticks, ratios, seconds and counts: no price.

**Memory** (the server has 2 GB): the signals are small and read a cell at a time; the recordings
are read a day at a time and let go before the next (``footprint.give_back``). The process's peak
resident memory is printed.
"""

from __future__ import annotations

import logging
import sqlite3
import statistics
import time as clock
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from krx_quant.archive import partition_files
from krx_quant.config import Settings
from krx_quant.entry_delay import Quotes, day_quotes
from krx_quant.feeds import BOOK, KRX_TRADE, TRADE
from krx_quant.feeds.base import KST, kst_day
from krx_quant.footprint import Meter, give_back
from krx_quant.gaps_probe import Unread, read_gaps
from krx_quant.macro import read_partition
from krx_quant.market_calendar import Session, calendar_store
from krx_quant.predictions import DB_NAME as ENGINE_DB
from krx_quant.special import Window
from krx_quant.stats import day_mean
from krx_quant.trader import SYMBOL, TRADED_KINDS
from krx_quant.trader import policy as policies
from krx_quant.trader.cells import Cell, open_engine_db, special_windows, traded_kind
from krx_quant.trader.paper import BELOW_THRESHOLD, STOP_LOSS, TAKE_PROFIT, TIME, Rules, costs
from krx_quant.trader.refit import cost_map_days, rises
from krx_quant.trader.selection import CostMapError, choose, read_cost_map
from krx_quant.trader.settings import TraderSettings
from krx_quant.trader.store import DB_NAME as TRADER_DB

log = logging.getLogger(__name__)

# The fill assumptions (module doc).
CONSERVATIVE, OPTIMISTIC = "conservative", "optimistic"
ASSUMPTIONS = (CONSERVATIVE, OPTIMISTIC)
# Why a signal is not measured: its data is missing (module doc).
NO_RECORDING, GAP, NO_QUOTE, NO_EXIT_QUOTE = "no_recording", "gap", "no_quote", "no_exit_quote"
MISSING = (NO_RECORDING, GAP, NO_QUOTE, NO_EXIT_QUOTE)
# The feeds whose recorded gaps leave a signal out.
GAP_FEEDS = frozenset({BOOK.tr_id, TRADE.tr_id, KRX_TRADE.tr_id})
# What the command's exit code says: 0 measured, 1 not (the cost map or the engine's database).
NOT_MEASURED = 1

# The paper trader's rules (``paper.Rules``): the quote wait the first quote and the time exit use.
RULES = Rules()

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICRO = timedelta(microseconds=1)


def micros(at: datetime) -> int:
    """``at`` in microseconds since the epoch, exactly (the recorded receipt times' unit)."""
    return (at - _EPOCH) // _MICRO


@dataclass(frozen=True, slots=True)
class Eligible:
    """A signal: a rise the trader would buy (module doc), with what its trade needs."""

    cell: Cell
    decided_at: datetime  # the prediction's display
    target: float
    stop: float
    time_barrier: datetime
    tick: float  # the reference's
    hit: bool  # its stored answer's class is the rise

    @property
    def day(self) -> date:
        return kst_day(self.decided_at)


@dataclass(frozen=True)
class Recording:
    """A trading day's recording of 005930 as the measurement reads it: the best quotes (the
    trader's stream), KRX's trades (receipt times in microseconds, prices; in receipt order) and the
    recorded gaps of the feeds that matter (microseconds; an end None: still open)."""

    quotes: Quotes
    trade_times: np.ndarray
    trade_prices: np.ndarray
    gaps: tuple[tuple[int, int | None], ...] = ()

    def gap_in(self, start: int, end: int) -> bool:
        """Whether a gap overlaps [start, end)."""
        return any(s < end and (e is None or e > start) for s, e in self.gaps)


@dataclass(frozen=True)
class Trade:
    """One entry's trade of a signal, a share: bought at ``entry``, sold at ``exit`` for ``reason``;
    ``net`` in ticks after its costs; ``wait`` the seconds from the post to the fill."""

    entry: float
    exit: float
    reason: str
    net: float
    wait: float = 0.0


@dataclass(frozen=True)
class Outcome:
    """A signal measured: missing data (``missing``), the target reached already, or the market
    entry's trade and each assumption's passive one (None: not filled); ``marketable`` when the
    first quote was locked or crossed (the passive buy filled at once at the ask)."""

    missing: str | None = None
    target_reached: bool = False
    market: Trade | None = None
    passive: Mapping[str, Trade | None] = field(default_factory=dict)
    marketable: bool = False


def measure_one(signal: Eligible, day: Recording, fee_rate: float, rules: Rules = RULES) -> Outcome:
    """Both entries of ``signal`` on its day's recording (module doc)."""
    quotes = day.quotes
    decided, barrier = micros(signal.decided_at), micros(signal.time_barrier)
    if day.gap_in(decided, barrier):
        return Outcome(missing=GAP)
    first = int(np.searchsorted(quotes.times, decided, side="right"))
    if (
        first >= len(quotes.times)
        or quotes.times[first] > decided + rules.quote_wait // _MICRO
        or quotes.times[first] >= barrier
    ):
        return Outcome(missing=NO_QUOTE)
    posted = int(quotes.times[first])
    bid, ask = float(quotes.bids[first]), float(quotes.asks[first])
    if ask >= signal.target:
        return Outcome(target_reached=True)
    market = _hold(signal, quotes, posted, ask, posted, fee_rate, rules)
    if market is None:
        return Outcome(missing=NO_EXIT_QUOTE)
    marketable = bid >= ask
    passive: dict[str, Trade | None] = {}
    for assumption in ASSUMPTIONS:
        if marketable:
            passive[assumption] = market
            continue
        filled = fill_at(day, posted, barrier, bid, assumption)
        if filled is None:
            passive[assumption] = None
            continue
        trade = _hold(signal, quotes, filled, bid, posted, fee_rate, rules)
        if trade is None:
            return Outcome(missing=NO_EXIT_QUOTE)
        passive[assumption] = trade
    return Outcome(market=market, passive=passive, marketable=marketable)


def fill_at(day: Recording, posted: int, barrier: int, price: float, assumption: str) -> int | None:
    """When a buy resting at ``price`` from ``posted`` fills (microseconds) by ``assumption`` on KRX's
    trades received after the post and before the ``barrier``; None if it does not."""
    times, prices = day.trade_times, day.trade_prices
    start = int(np.searchsorted(times, posted, side="right"))
    end = int(np.searchsorted(times, barrier, side="left"))
    window = prices[start:end]
    through = window < price if assumption == CONSERVATIVE else window <= price
    found = np.flatnonzero(through)
    return None if len(found) == 0 else int(times[start + int(found[0])])


def _hold(
    signal: Eligible,
    quotes: Quotes,
    entered: int,
    price: float,
    posted: int,
    fee_rate: float,
    rules: Rules,
) -> Trade | None:
    """The trade bought at ``price`` at ``entered`` (microseconds), sold by the paper exits; None
    without a quote to sell on at the time barrier."""
    sale = sale_of(quotes, entered, signal.target, signal.stop, micros(signal.time_barrier), rules)
    if sale is None:
        return None
    sold, reason = sale
    paid = costs(price, sold, 1, fee_rate).total
    return Trade(price, sold, reason, (sold - price - paid) / signal.tick, (entered - posted) / 1e6)


def sale_of(
    quotes: Quotes, entered: int, target: float, stop: float, barrier: int, rules: Rules = RULES
) -> tuple[float, str] | None:
    """Where and why a position bought at ``entered`` is sold, by the paper exits on the quotes
    received after it (module doc); None without a quote to sell on at the time barrier."""
    times, bids = quotes.times, quotes.bids
    after = int(np.searchsorted(times, entered, side="right"))
    end = int(np.searchsorted(times, barrier, side="left"))  # the first quote at or after the barrier
    if end > after:
        window = bids[after:end]
        reached = np.flatnonzero((window >= target) | (window <= stop))
        if len(reached):
            k = after + int(reached[0])
            return (target, TAKE_PROFIT) if bids[k] >= target else (float(bids[k]), STOP_LOSS)
    standing = end - 1  # the last quote received before the barrier
    if standing >= 0 and 0 < barrier - int(times[standing]) <= rules.quote_wait // _MICRO:
        return float(bids[standing]), TIME
    if end < len(times):
        return float(bids[end]), TIME
    return None


# Reading the recordings, a day at a time.


def read_day(data_dir: Path, day: date) -> Recording | None:
    """005930's recording of ``day`` (module doc); None without its book or KRX trades."""
    events = data_dir / "events" / f"date={day.isoformat()}"
    if not partition_files(events / f"feed={BOOK.tr_id}" / f"symbol={SYMBOL}"):
        return None
    quotes = day_quotes(data_dir, day, SYMBOL)
    table = read_partition(events / f"feed={KRX_TRADE.tr_id}" / f"symbol={SYMBOL}", ("received_at", "price"))
    if quotes is None or table is None or table.num_rows == 0:
        return None
    table = table.filter(pc.fill_null(pc.greater(table["price"], 0), False))
    times = pc.cast(pc.cast(table["received_at"], pa.timestamp("us", tz="UTC")), pa.int64()).to_numpy()
    order = np.argsort(times, kind="stable")
    prices = pc.cast(table["price"], pa.float64()).to_numpy()
    gaps = tuple(
        (micros(g.start), None if g.end is None else micros(g.end))
        for g in read_gaps(data_dir, day, Unread())
        if g.feed in GAP_FEEDS and g.key == SYMBOL
    )
    return Recording(quotes, times[order], prices[order], gaps)


# Reading the signals.


@dataclass
class CellSignals:
    """A cell as measured: its role on the cost map, its θ and the counts of its rows read
    (``refit.rises``'s, ``below_threshold``, and ``signals``)."""

    cell: Cell
    role: str
    threshold: float
    counts: Counter[str] = field(default_factory=Counter)


def signals_of(
    db: sqlite3.Connection,
    read: CellSignals,
    days: Sequence[date],
    sessions: Callable[[date], Session | None],
    windows: Sequence[Window],
    target_version: int,
) -> list[Eligible]:
    """``read``'s cell's signals of ``days`` (module doc), its rows counted in ``read.counts``."""
    rise = traded_kind(read.cell.kind).sides
    assert rise is not None  # a traded kind has a rise
    out = []
    for signal, planned, _, label in rises(
        db, read.cell, read.role, days, sessions, windows, target_version, read.counts
    ):
        p = signal.rise_probability
        assert p is not None and signal.tick is not None  # planned: made, its reference known
        if p < read.threshold:
            read.counts[BELOW_THRESHOLD] += 1
            continue
        read.counts["signals"] += 1
        out.append(
            Eligible(
                read.cell,
                signal.displayed_at,
                planned.target,
                planned.stop,
                planned.time_barrier,
                float(signal.tick),
                label == rise[0],
            )
        )
    return out


@dataclass(frozen=True)
class Thresholds:
    """The θ of the champion policy's cells (#399), read-only: its version, or why there is none."""

    version: int | None = None
    by_cell: Mapping[Cell, float] = field(default_factory=dict)
    why: str | None = None  # None: read (no champion yet, or this one)


def champion_thresholds(trader_db: Path) -> Thresholds:
    """The champion's θ by cell from the trader's store, opened read-only; none (and why) before the
    first champion, without the store, or when it cannot be read."""
    if not trader_db.exists():
        return Thresholds(why="no store")
    try:
        db = sqlite3.connect(trader_db.resolve().as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            champion = policies.champion(db)
        finally:
            db.close()
    except (sqlite3.Error, policies.PolicyError) as e:
        log.warning("passive entry: cannot read the champion policy (%s): every cell at the default θ", e)
        return Thresholds(why=str(e))
    if champion is None:
        return Thresholds()
    return Thresholds(champion.version, {c.cell: c.threshold for c in champion.cells})


def cells_of(considered: Iterable[Mapping[str, Any]]) -> list[tuple[Cell, str]]:
    """The cost map's cells with a predictor, in its order (kinds, then roles), each once."""
    seen: dict[Cell, str] = {}
    for c in considered:
        if isinstance(c.get("predictor"), str):
            seen.setdefault(Cell(c["kind"], c["predictor"]), str(c["role"]))
    return list(seen.items())


# The tallies.


@dataclass
class Leg:
    """One entry's trades of a group's signals: how many, their hits, their net in ticks, their exits."""

    trades: int = 0
    hits: int = 0
    net: float = 0.0
    exits: Counter[str] = field(default_factory=Counter)

    def add(self, trade: Trade, hit: bool) -> None:
        self.trades += 1
        self.hits += hit
        self.net += trade.net
        self.exits[trade.reason] += 1


@dataclass
class Passive:
    """A fill assumption's passive entry on a group's signals."""

    filled: Leg = field(default_factory=Leg)
    unfilled: int = 0
    unfilled_hits: int = 0
    marketable: int = 0  # filled at once: the first quote locked or crossed
    market_net: float = 0.0  # the market entry's net on the filled signals
    waits: list[float] = field(default_factory=list)  # seconds from the post to the fill
    # Per signal, the passive net (0 unfilled) less the market's, by day.
    differences: dict[date, list[float]] = field(default_factory=lambda: defaultdict(list))


@dataclass
class Tally:
    """A group's signals (all, a kind's, a cell's) and how they were measured."""

    signals: int = 0
    missing: Counter[str] = field(default_factory=Counter)
    target_reached: int = 0
    market: Leg = field(default_factory=Leg)
    passive: dict[str, Passive] = field(default_factory=lambda: {a: Passive() for a in ASSUMPTIONS})

    @property
    def measured(self) -> int:
        return self.market.trades

    def add(self, signal: Eligible, outcome: Outcome) -> None:
        self.signals += 1
        if outcome.missing is not None:
            self.missing[outcome.missing] += 1
            return
        if outcome.target_reached:
            self.target_reached += 1
            return
        market = outcome.market
        assert market is not None
        self.market.add(market, signal.hit)
        for assumption, trade in outcome.passive.items():
            leg = self.passive[assumption]
            if trade is None:
                leg.unfilled += 1
                leg.unfilled_hits += signal.hit
                leg.differences[signal.day].append(-market.net)
                continue
            leg.filled.add(trade, signal.hit)
            leg.marketable += outcome.marketable
            leg.market_net += market.net
            leg.waits.append(trade.wait)
            leg.differences[signal.day].append(trade.net - market.net)


@dataclass(frozen=True)
class Measurement:
    """The night's measurement, as reported."""

    as_of: str | None  # the cost map's
    period: Any
    cells: tuple[CellSignals, ...]
    thresholds: Thresholds
    default_threshold: float
    days: tuple[date, ...]  # the days with signals
    recorded: tuple[date, ...]  # those with their recording
    total: Tally
    kinds: Mapping[str, Tally]
    tallies: Mapping[Cell, Tally]
    run: Mapping[str, Any]


def measure(
    body: Mapping[str, Any],
    engine_db: Path,
    trader_db: Path,
    data_dir: Path,
    sessions: Callable[[date], Session | None],
    *,
    fee_rate: float,
    target_version: int,
    threshold: float,
    meter: Meter | None = None,
) -> Measurement:
    """Both entries of every signal of the cost map ``body``'s cells (module doc), from the engine's
    database at ``engine_db`` (read-only) and the recordings under ``data_dir``, a day at a time.
    ``CostMapError`` when ``body`` is not the cost map asked for."""
    meter = meter or Meter()
    started = clock.monotonic()
    meter.reset()
    start_mb = meter.rss_mb()
    selection = choose(body)
    days_of = cost_map_days(body)
    thresholds = champion_thresholds(trader_db)
    reads = [
        CellSignals(c, role, thresholds.by_cell.get(c, threshold))
        for c, role in cells_of(selection.considered)
    ]
    by_day: dict[date, list[Eligible]] = defaultdict(list)
    if reads:
        db = open_engine_db(engine_db)
        try:
            first = min((min(d) for d in days_of.values() if d), default=None)
            windows = (
                special_windows(db, SYMBOL, datetime.combine(first, time(), KST)) if first is not None else []
            )
            for read in reads:
                days = days_of.get(read.cell.kind, ())
                for signal in signals_of(db, read, days, sessions, windows, target_version):
                    by_day[signal.day].append(signal)
        finally:
            db.close()
    total = Tally()
    kinds: dict[str, Tally] = {}
    tallies: dict[Cell, Tally] = {}
    recorded: list[date] = []
    for day in sorted(by_day):
        recording = read_day(data_dir, day)
        if recording is not None:
            recorded.append(day)
        for signal in by_day[day]:
            outcome = (
                Outcome(missing=NO_RECORDING)
                if recording is None
                else measure_one(signal, recording, fee_rate)
            )
            for tally in (
                total,
                kinds.setdefault(signal.cell.kind, Tally()),
                tallies.setdefault(signal.cell, Tally()),
            ):
                tally.add(signal, outcome)
        del recording
        give_back()  # the day's arrays and Arrow's buffers, before the next day's
    run = {
        "seconds": round(clock.monotonic() - started, 2),
        "rss_start_mb": start_mb,
        "peak_rss_mb": meter.peak_mb(),
        "rss_mb": meter.rss_mb(),
        "peak_since": meter.peak_since,
    }
    return Measurement(
        as_of=selection.as_of,
        period=body.get("period"),
        cells=tuple(reads),
        thresholds=thresholds,
        default_threshold=threshold,
        days=tuple(sorted(by_day)),
        recorded=tuple(recorded),
        total=total,
        kinds=kinds,
        tallies=tallies,
        run=run,
    )


# The report: ticks, ratios, seconds and counts, no price.

NAMES = {CONSERVATIVE: "보수적(그 가격 아래 체결)", OPTIMISTIC: "낙관적(그 가격 이하 체결)"}
MISSING_NAMES = {
    NO_RECORDING: "녹화 없음",
    GAP: "결측 구간",
    NO_QUOTE: "결정 뒤 호가 없음",
    NO_EXIT_QUOTE: "청산 호가 없음",
}
EXIT_NAMES = {TAKE_PROFIT: "익절", STOP_LOSS: "손절", TIME: "시간"}
# The reasons a cell's rows were kept out, as the report names them (``paper.plan``'s and the read's).
READ_NAMES = {
    "off_target": "다른 정답의 모델",
    "not_ok": "만들지 못함",
    "blocked": "엔진이 막음",
    "special": "특수 상황",
    "not_up": "오름 아님",
    BELOW_THRESHOLD: "θ 미만",
    "no_reference": "기준가 없음",
    "closed": "휴장",
    "before_entry": "09:05 전",
    "past_last_exit": "시간 장벽이 15:15 뒤",
    "stale": "늦게 읽음",
    "expired": "시간 장벽 뒤",
}


def report(m: Measurement) -> str:
    """The command's output, for the issue."""
    t = m.thresholds
    if t.version is not None:
        theta = (
            f"챔피언 정책 {t.version}의 칸 값, 정책에 없는 칸은 KRX_TRADER_THRESHOLD {m.default_threshold:g}"
        )
    elif t.why not in (None, "no store"):
        theta = f"KRX_TRADER_THRESHOLD {m.default_threshold:g}(챔피언 정책을 읽지 못함: {t.why})"
    else:
        theta = f"KRX_TRADER_THRESHOLD {m.default_threshold:g}(챔피언 정책 없음)"
    lines = [
        f"대기 매수 측정 (#432): {SYMBOL}, 비용 지도 {m.period}(as_of {m.as_of}) · 칸 {len(m.cells)}개 · "
        f"신호가 있는 날 {len(m.days)}일(녹화 있는 날 {len(m.recorded)}일)",
        "진입은 결정(예측이 표시된 때) 뒤 첫 호가에서 한다. 시장가는 그 매도 1호가에 산다(지금의 trader). "
        "대기는 그 매수 1호가에 걸고 시간 장벽에 취소한다. 체결은 KRX 체결(H0STCNT0)로 보고, 첫 호가가 "
        "맞물리거나 뒤집혔으면(매수 1호가 ≥ 매도 1호가) 대기도 그 매도 1호가에 바로 산다.",
        "청산은 둘 다 모의 체결 규칙이다(익절은 목표가, 손절과 시간은 매수 1호가). 손익은 주당 순손익"
        "(수수료 양쪽, 매도세)을 기준가의 틱으로 센다. 맞힘률은 저장된 정답이 오름인 비율이다. "
        "신호는 하나씩 따로 잰다(포지션 하나와 칸 순위를 두지 않는다).",
        f"θ: {theta}",
        "",
        *_group("전체", m.total),
        "",
    ]
    for kind in TRADED_KINDS:
        if kind in m.kinds:
            lines += _group(f"종류 {kind}", m.kinds[kind])
    for read in m.cells:
        lines += ["", *_cell(read, m.tallies.get(read.cell, Tally()))]
    run = m.run
    lines += [
        "",
        f"메모리: 최대 RSS {_mb(run['peak_rss_mb'])} MB("
        + ("측정 동안" if run["peak_since"] == "step" else "프로세스 시작부터")
        + f"), 시작 {_mb(run['rss_start_mb'])} MB, 끝 {_mb(run['rss_mb'])} MB · {run['seconds']:.1f}초",
    ]
    return "\n".join(lines)


def _cell(read: CellSignals, tally: Tally) -> list[str]:
    counts = read.counts
    left = " · ".join(
        f"{READ_NAMES.get(r, r)} {n}" for r, n in sorted(counts.items()) if r not in ("read", "signals")
    )
    head = (
        f"칸 {read.cell.kind} {read.cell.predictor} ({read.role}, θ {read.threshold:g}): 읽은 예측 "
        f"{counts.get('read', 0)}" + (f" · 뺀 것: {left}" if left else "")
    )
    return [head, *_group("측정", tally, "  ")]


def _group(title: str, t: Tally, pad: str = "") -> list[str]:
    """A group's lines: its counts, then the market entry's and each assumption's."""
    head = f"{pad}{title}: 신호 {t.signals}"
    if t.missing:
        missing = ", ".join(f"{MISSING_NAMES[r]} {t.missing[r]}" for r in MISSING if t.missing[r])
        head += f" · 데이터 빠짐 {sum(t.missing.values())}({missing})"
    if t.target_reached:
        head += f" · 목표가 이미 닿음 {t.target_reached}"
    head += f" · 잰 신호 {t.measured}"
    if not t.measured:
        return [head]
    m = t.market
    pad += "  "
    lines = [
        head,
        f"{pad}시장가: 맞힘률 {_pct(m.hits, m.trades)} · 순손익 {_ticks(m.net / m.trades)}틱/매매 · "
        f"{_exits(m.exits)}",
    ]
    for assumption in ASSUMPTIONS:
        p = t.passive[assumption]
        f = p.filled
        line = (
            f"{pad}대기 {NAMES[assumption]}: 체결 {f.trades}/{t.measured} = {_pct(f.trades, t.measured)}"
            f"(맞물려 바로 {p.marketable}) · 맞힘률: 체결 {_pct(f.hits, f.trades)}, 미체결 "
            f"{_pct(p.unfilled_hits, p.unfilled)}(시장가 {_pct(m.hits, m.trades)})"
        )
        if p.waits:
            line += f" · 체결까지 중앙 {statistics.median(p.waits):.1f}초"
        lines.append(line)
        net = f"{pad}  순손익: 신호당 {_ticks(f.net / t.measured)}틱(미체결 0)"
        if f.trades:
            net += (
                f", 체결당 {_ticks(f.net / f.trades)}틱 · 같은 신호의 시장가 "
                f"{_ticks(p.market_net / f.trades)}틱/매매 · {_exits(f.exits)}"
            )
        interval = day_mean(p.differences)
        if interval is not None and interval.low is not None and interval.high is not None:
            net += (
                f" · 대기 − 시장가 신호당 {_ticks(interval.value)}틱(날 단위 95% {_ticks(interval.low)} ~ "
                f"{_ticks(interval.high)}, {interval.n}일)"
            )
        lines.append(net)
    return lines


def _pct(k: int, n: int) -> str:
    return f"{100 * k / n:.1f}%" if n else "—"


def _ticks(value: float) -> str:
    return f"{value:+.2f}"


def _exits(exits: Counter[str]) -> str:
    return ", ".join(f"{EXIT_NAMES[r]} {exits[r]}" for r in (TAKE_PROFIT, STOP_LOSS, TIME))


def _mb(value: float | None) -> str:
    return "?" if value is None else f"{value:.1f}"


# The command.


async def run_passive_entry(settings: Settings, trader: TraderSettings, engine_url: str | None = None) -> int:
    """``krx-quant measure-passive-entry``: tonight's cost map from the Engine API (``engine_url``,
    else ``KRX_TRADER_ENGINE_URL``), the measurement, its report printed. 0 when measured,
    ``NOT_MEASURED`` when the cost map or the engine's database cannot be read."""
    url = engine_url or trader.engine_url
    try:
        async with aiohttp.ClientSession() as session:
            body = await read_cost_map(session, url)
    except CostMapError as e:
        print(f"대기 매수 측정 (#432): 비용 지도를 읽지 못해 재지 않았다: {e}")
        log.error("passive entry: could not read the cost map (%s)", e)
        return NOT_MEASURED
    engine_db = settings.state_dir / ENGINE_DB
    if not engine_db.exists():
        print(f"대기 매수 측정 (#432): 엔진 DB가 없어({engine_db}) 재지 않았다")
        return NOT_MEASURED
    calendar = calendar_store(settings).calendar
    try:
        done = measure(
            body,
            engine_db,
            settings.state_dir / TRADER_DB,
            settings.data_dir,
            calendar.session,
            fee_rate=settings.fee_rate,
            target_version=settings.target_version,
            threshold=trader.threshold,
        )
    except CostMapError as e:
        print(f"대기 매수 측정 (#432): 비용 지도가 아니어서 재지 않았다: {e}")
        return NOT_MEASURED
    except sqlite3.Error as e:
        print(f"대기 매수 측정 (#432): 엔진 DB를 읽지 못해 재지 않았다: {e}")
        log.error("passive entry: %s", e)
        return NOT_MEASURED
    log.info(
        "passive entry: %s signal(s) of %s cell(s) measured on %s recorded day(s); peak RSS %s MB, %.1fs",
        done.total.measured,
        len(done.cells),
        len(done.recorded),
        _mb(done.run["peak_rss_mb"]),
        done.run["seconds"],
    )
    print(report(done))
    return 0
