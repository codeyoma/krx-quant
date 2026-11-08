"""``krx-quant inverse-replay``: the down calls replayed through the inverse ETF's recorded quotes (#606).

A down call is tradeable through 005930's inverse ETF, 0193L0, but nothing had measured it: the
trader buys rises alone (#393, #468), and the cost map's inverse cost is a floor (``cost_map.py``).
Since 2026-11-06 the collector records the ETF's KRX book (``H0STASP0``, ``collector.INVERSE_BOOKS``),
and since #606's second step its best quotes as events (``feeds.KRX_BOOK``). This replays, on those
quotes, what one ETF share bought on each down call would have made. It places no order and changes
nothing: no scoring, no live eligibility, no promotion reads it.

**Which calls.** Every prediction of 005930 the engine made (status ok) of the kinds the trader trades
(``trader.TRADED_KINDS``: the four-minute direction and the 4-, 15- and 30-minute barrier), by every
predictor, whose most likely class (``kinds.most_likely``) is the kind's fall: the direction's
``down``, a barrier's ``down_first``. An explicit call alone: the trader's ``not_up`` is not one, as
it holds the flat calls too. Left out of the replay, each counted (``LEFT_OUT``): a final consensus
the engine blocked (``blocked``; for a special situation, ``special``); one without its reference,
tick or width (``no_reference``); a bar close before 09:05 (``before_entry``) or a time barrier after
15:15 (``past_last_exit``), the trader's entry window (``trader.ENTRY_AFTER_OPEN``,
``EXIT_BEFORE_AUCTION``); a special window the engine kept (a VI of 005930, a circuit breaker, a
sidecar, ``cells.special_windows``) touching its window from the bar close to the time barrier
(``special``: every window kept, known at the call or not).

**The trade**, one share, each call alone (nothing held across calls):

- *Entry*: at the best ask of the first ETF record received after the call was displayed
  (``displayed_at``), if it came within ``FRESH`` (5 s) and before the time barrier and is a usable
  quote (``usable``): continuous trading (``hour_cls`` 0, KIS's code), both sides quoted, the ask
  above the bid. Else ``no_entry_quote``.
- *The underlying's barriers*, as the barrier's answer has them (``barriers.py``): the reference is
  the call's (the bar close's integrated mid-point, ``anchor``), the lower barrier L the reference
  less w ticks of its tick (the direction's k), the upper U the reference plus w. On 005930's
  integrated best quotes (``entry_delay.day_quotes``: the book's and the trades', the trader's stream),
  L is reached when a best ask is at or below it, U when a best bid is at or above it. Already reached
  L at the entry (the standing quote's ask at or below it): ``target_reached``, no trade, as the
  paper trader does not buy a rise whose target the ask has reached.
- *Exit*: at the first 005930 quote received after the entry and before the time barrier (the bar
  close plus the kind's horizon) that reaches L (``down_barrier``: the call came true) or U
  (``up_barrier``; one quote reaching both counts as U, the worse), else at the time barrier
  (``expiry``). The share is sold at the best bid of the ETF's record standing then, if it is usable
  and no older than ``FRESH``, else of the first one after it within ``FRESH``; else
  ``no_exit_quote``.
- *Net*, per share, in won: the bid sold at less the ask bought at, less the commission on both sides
  (``KRX_FEE_PERCENT``, on the ETF's own prices). An ETF's sale pays no securities transaction tax
  (``products.py``). The cost map's inverse floor is not used. In the ETF's ticks too (``etf_tick``:
  1 won below 2,000 won, 5 from 2,000, checked by the probe on 2026-11-06).

**Missing data**, left out and counted: a day without the ETF's quotes or 005930's
(``no_recording``); a recorded gap of the ETF's book or of 005930's book or trades overlapping the
call's window from its display to its time barrier (``gap``, ``trader.replay.day_gaps``). The
quotes older than 5 s are left out by the entry and the exit rules above.

**Which days**: the latest ``DAYS`` (20) trading days, up to the latest whose session is over, that
have the ETF's book recorded. A day recorded before its best quotes were events (2026-11-06 to the
deploy of #606's second step) is read from its raw frames, through the same normalization
(``feeds.KRX_BOOK``; a pruned raw day is read back from the Object Store, ``raw_days.RawDays``).

**The report**: by day; then all the calls, each kind, and each kind and predictor: the calls, those
left out by reason, the trades replayed, those that netted above 0, the net in won (the sum, the mean
a trade with its day-level 95% interval, ``stats.day_mean``), the net in ETF ticks, the median spread
at the entry in ETF ticks, the exits, and of the trades whose answer the engine stored (the
direction's touch label, a barrier's answer) how many were the fall. Counts, rates, ticks and won: no
price. On the first report with ``DAYS`` recorded days or more it adds a line for an agent
(``AGENT_LINE``), once (``TOLD_FILE`` in the state directory keeps that it did, so a night missed
does not skip it): the scheduled job labels the issue then.

**Memory**: a day's quotes are held at a time (005930's arrays and the ETF's, about 40,000 records
on 2026-11-06) and let go before the next (``footprint.give_back``); the trades are kept, a few
numbers each. Its peak resident memory is printed.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import statistics
import time as clock
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pyarrow as pa

from krx_quant import kis
from krx_quant.archive import RawMissing, guarded_files, partition_batches
from krx_quant.config import Settings
from krx_quant.entry_delay import Quotes, day_quotes
from krx_quant.feeds import BOOK, KRX_BOOK, RAW_SCHEMA, TRADE
from krx_quant.feeds.base import KST
from krx_quant.footprint import Meter, give_back
from krx_quant.kinds import DIRECTION, most_likely
from krx_quant.market_calendar import MarketCalendar, Session, calendar_store
from krx_quant.object_store import open_store
from krx_quant.predictions import DB_NAME, read_only
from krx_quant.products import INVERSE_PRODUCTS, etf_tick
from krx_quant.raw_days import RawDays
from krx_quant.special import Window, touching
from krx_quant.stats import day_mean
from krx_quant.trader import ENTRY_AFTER_OPEN, EXIT_BEFORE_AUCTION, SYMBOL, TRADED_KINDS
from krx_quant.trader.cells import (
    DIRECTION_COLUMNS,
    DIRECTION_FROM,
    KIND_COLUMNS,
    Cell,
    Signal,
    direction_signal,
    kind_signal,
    special_windows,
    traded_kind,
)
from krx_quant.trader.replay import DOWN, day_answers, day_gaps, micros

log = logging.getLogger(__name__)

TITLE = "인버스 ETF 재생 (#606)"
# The traded symbol's inverse ETF, whose book the collector registers (``collector.INVERSE_BOOKS``).
ETF = INVERSE_PRODUCTS[SYMBOL].code
DAYS = 20  # the latest recorded trading days replayed
FRESH = timedelta(seconds=5)  # a quote older than this is not traded on (#606)
CONTINUOUS = "0"  # HOUR_CLS_CODE of continuous trading (KIS; ``feeds.stock.KRX_BOOK_SCHEMA``)
NO_DAYS, NO_ENGINE_DB = 3, 1  # exit codes: no recorded day of the ETF's book; no engine database

# Why a call is not replayed (module doc), in the report's order, with its words there.
BLOCKED, SPECIAL, NO_REFERENCE = "blocked", "special", "no_reference"
BEFORE_ENTRY, PAST_LAST_EXIT = "before_entry", "past_last_exit"
NO_RECORDING, GAP, NO_ENTRY_QUOTE = "no_recording", "gap", "no_entry_quote"
TARGET_REACHED, NO_EXIT_QUOTE = "target_reached", "no_exit_quote"
LEFT_OUT = {
    BLOCKED: "엔진이 막음",
    SPECIAL: "특수 상황",
    NO_REFERENCE: "기준가·폭 없음",
    BEFORE_ENTRY: "09:05 전 봉",
    PAST_LAST_EXIT: "만기가 15:15 뒤",
    NO_RECORDING: "녹화 없음",
    GAP: "결측 구간",
    NO_ENTRY_QUOTE: "5초 안에 살 호가 없음",
    TARGET_REACHED: "살 때 이미 아래 장벽",
    NO_EXIT_QUOTE: "5초 안에 팔 호가 없음",
}
# How a trade ended (module doc).
DOWN_BARRIER, UP_BARRIER, EXPIRY = "down_barrier", "up_barrier", "expiry"
EXITS = {DOWN_BARRIER: "아래 장벽", UP_BARRIER: "위 장벽", EXPIRY: "만기"}
# The feeds whose recorded gaps leave a call out: 005930's quotes (the trader's), and the ETF's book.
STOCK_GAP_FEEDS = frozenset({BOOK.tr_id, TRADE.tr_id})
ETF_GAP_FEEDS = frozenset({KRX_BOOK.tr_id})
# The line that calls an agent (``scheduled.research``'s ``inverse_replay_calls``).
AGENT_LINE = "에이전트 차례:"

_FRESH_US = FRESH // timedelta(microseconds=1)


# The ETF's quotes.


@dataclass(frozen=True)
class EtfQuotes:
    """The ETF's book records of a day in receipt order (microseconds since the epoch), every one: a
    record that is no usable quote still replaces the one before it (``usable``)."""

    times: np.ndarray
    bids: np.ndarray
    asks: np.ndarray
    usable: np.ndarray  # continuous trading, both sides quoted, the ask above the bid


_COLUMNS = ("received_at", "hour_cls", "bid_px_1", "ask_px_1")


def etf_quotes(data_dir: Path, raw: RawDays, day: date) -> EtfQuotes | None:
    """The ETF's quotes of ``day``: its events, else (a day recorded before they were made) its raw
    frames through the same normalization; None without either."""
    partition = data_dir / "events" / f"date={day.isoformat()}" / f"feed={KRX_BOOK.tr_id}" / f"symbol={ETF}"
    if guarded_files(partition):
        rows: Iterable[tuple[datetime, str, int | None, int | None]] = (
            (record["received_at"], record["hour_cls"], record["bid_px_1"], record["ask_px_1"])
            for _, batch in partition_batches(partition, _COLUMNS)
            for record in batch.to_pylist()
        )
    else:
        rows = _raw_rows(raw.feed(day, KRX_BOOK.tr_id) / f"symbol={ETF}")
    return _arrays(rows)


def _raw_rows(partition: Path) -> Iterable[tuple[datetime, str, int, int]]:
    """A raw partition's records as their events have them; a frame that is not the probed layout is
    passed over, as the collector keeps it raw only."""
    if not partition.is_dir():
        return
    width = len(KRX_BOOK.columns)
    for _, batch in partition_batches(partition, RAW_SCHEMA.names):
        rows = batch.to_pydict()
        for received_at, text in zip(rows["received_at"], rows["message"], strict=True):
            try:
                message = kis.parse_message(text)
                if not isinstance(message, kis.RealtimeFrame) or message.encrypted:
                    continue
                for fields in message.records(width):
                    row = KRX_BOOK.normalize(dict(zip(KRX_BOOK.columns, fields, strict=True)), received_at)
                    yield received_at, row["hour_cls"], row["bid_px_1"], row["ask_px_1"]
            except (kis.KisError, ValueError, KeyError, IndexError):
                continue


def _arrays(rows: Iterable[tuple[datetime, str, int | None, int | None]]) -> EtfQuotes | None:
    times: list[int] = []
    bids: list[float] = []
    asks: list[float] = []
    usable: list[bool] = []
    for received_at, hour_cls, bid, ask in rows:
        b, a = float(bid or 0), float(ask or 0)
        times.append(micros(received_at))
        bids.append(b)
        asks.append(a)
        usable.append(hour_cls == CONTINUOUS and b > 0 and a > b)
    if not times:
        return None
    order = np.argsort(np.array(times, np.int64), kind="stable")
    return EtfQuotes(
        np.array(times, np.int64)[order],
        np.array(bids, np.float64)[order],
        np.array(asks, np.float64)[order],
        np.array(usable, np.bool_)[order],
    )


@dataclass(frozen=True)
class Recording:
    """A day as the replay reads it: the ETF's quotes, 005930's, and the gaps that leave calls out
    (microseconds; an end None: still open)."""

    etf: EtfQuotes
    stock: Quotes
    gaps: tuple[tuple[int, int | None], ...] = ()

    def gap_in(self, start: int, end: int) -> bool:
        return any(s <= end and (e is None or e > start) for s, e in self.gaps)


def read_day(data_dir: Path, raw: RawDays, day: date) -> Recording | None:
    """``day``'s recording; None without the ETF's quotes or 005930's (logged when a file cannot be
    read, or a pruned raw day cannot be read back: the day's calls are counted ``no_recording``)."""
    try:
        etf = etf_quotes(data_dir, raw, day)
        stock = day_quotes(data_dir, day, SYMBOL)
    except (OSError, RawMissing, pa.ArrowException) as e:
        log.warning("inverse replay: cannot read the recording of %s (%s): its calls are left out", day, e)
        return None
    if etf is None or stock is None:
        return None
    gaps = (
        *day_gaps(data_dir, day, STOCK_GAP_FEEDS),
        *(_closed(gap, etf.times) for gap in day_gaps(data_dir, day, ETF_GAP_FEEDS, ETF)),
    )
    return Recording(etf, stock, tuple(sorted(gaps, key=lambda g: g[0])))


def _closed(gap: tuple[int, int | None], times: np.ndarray) -> tuple[int, int | None]:
    """An ETF gap still open when its collector stopped, ended at the first of the day's records
    received after its start (``day_gaps`` finds that in the events alone: a day read from its raw
    frames has none); without one it covers the rest of the day."""
    start, end = gap
    if end is not None:
        return gap
    later = int(np.searchsorted(times, start, side="right"))
    return (start, int(times[later])) if later < len(times) else gap


# The calls.


@dataclass(frozen=True)
class Call:
    """A down call (module doc), with what its trade needs."""

    cell: Cell
    prediction_id: int
    displayed_at: datetime
    time_barrier: datetime
    lower: float  # L: the call's barrier
    upper: float  # U


def day_calls(
    db: sqlite3.Connection, day: date, session: Session, windows: Sequence[Window]
) -> tuple[list[Call], dict[Cell, Counter[str]]]:
    """``day``'s down calls of 005930 by every predictor of the traded kinds: those to replay and, by
    cell, those left out by reason (module doc)."""
    start = datetime.combine(day, time(), KST)
    span = (SYMBOL, start.isoformat(), (start + timedelta(days=1)).isoformat())
    calls: list[Call] = []
    out: dict[Cell, Counter[str]] = {}
    for name in TRADED_KINDS:
        kind = traded_kind(name)
        if kind.name == DIRECTION:
            rows = db.execute(
                f"SELECT p.algorithm, {DIRECTION_COLUMNS} FROM {DIRECTION_FROM}"
                " WHERE p.symbol = ? AND p.bar_close >= ? AND p.bar_close < ? ORDER BY p.bar_close, p.id",
                span,
            ).fetchall()
            signals = [direction_signal(r, Cell(name, r["algorithm"]), SYMBOL) for r in rows]
        else:
            rows = db.execute(
                f"SELECT p.algorithm, {KIND_COLUMNS} FROM kind_predictions p WHERE p.kind = ?"
                " AND p.symbol = ? AND p.bar_close >= ? AND p.bar_close < ? ORDER BY p.bar_close, p.id",
                (name, *span),
            ).fetchall()
            signals = [kind_signal(r, Cell(name, r["algorithm"]), SYMBOL) for r in rows]
        fall = kind.index(kind.sides[1]) if kind.sides else 2
        for signal in signals:
            if signal.status != "ok" or signal.probabilities is None:
                continue
            if most_likely(signal.probabilities) != fall:
                continue
            cell = Cell(signal.kind, signal.predictor)
            reason = _left_out(signal, session, windows)
            if reason is not None:
                out.setdefault(cell, Counter())[reason] += 1
                continue
            assert signal.anchor is not None and signal.tick and signal.width  # not no_reference
            half = signal.width * signal.tick
            calls.append(
                Call(
                    cell,
                    signal.prediction_id,
                    signal.displayed_at,
                    signal.bar_close + signal.horizon,
                    signal.anchor - half,
                    signal.anchor + half,
                )
            )
    return calls, out


def _left_out(signal: Signal, session: Session, windows: Sequence[Window]) -> str | None:
    if signal.blocked is not None:
        return SPECIAL if signal.blocked == SPECIAL else BLOCKED
    if signal.anchor is None or not signal.tick or not signal.width:
        return NO_REFERENCE
    if signal.bar_close < session.open + ENTRY_AFTER_OPEN:
        return BEFORE_ENTRY
    barrier = signal.bar_close + signal.horizon
    if barrier > session.closing_auction - EXIT_BEFORE_AUCTION:
        return PAST_LAST_EXIT
    if touching(windows, SYMBOL, signal.bar_close, barrier):
        return SPECIAL
    return None


# A trade.


@dataclass(frozen=True)
class Trade:
    """A call's share: ``net`` in won and in the ETF's ticks, the spread it was bought at in ticks,
    how it ended."""

    net: float
    net_ticks: float
    spread_ticks: float
    exit: str


def replay_call(call: Call, day: Recording, fee_rate: float) -> Trade | str:
    """``call``'s trade on ``day``'s recording (module doc), or why it has none."""
    shown, barrier = micros(call.displayed_at), micros(call.time_barrier)
    if day.gap_in(shown, barrier):
        return GAP
    etf = day.etf
    first = int(np.searchsorted(etf.times, shown, side="right"))
    if (
        first >= len(etf.times)
        or etf.times[first] > shown + _FRESH_US
        or etf.times[first] >= barrier
        or not etf.usable[first]
    ):
        return NO_ENTRY_QUOTE
    entered, ask = int(etf.times[first]), float(etf.asks[first])
    stock = day.stock
    standing = int(np.searchsorted(stock.times, entered, side="right")) - 1
    if standing >= 0 and stock.asks[standing] <= call.lower:
        return TARGET_REACHED
    end = int(np.searchsorted(stock.times, barrier, side="left"))
    after = standing + 1
    exit_at, reason = barrier, EXPIRY
    if end > after:
        up = stock.bids[after:end] >= call.upper
        down = stock.asks[after:end] <= call.lower
        reached = np.flatnonzero(up | down)
        if len(reached):
            k = int(reached[0])
            exit_at, reason = int(stock.times[after + k]), UP_BARRIER if up[k] else DOWN_BARRIER
    bid = _sale(etf, exit_at, first)
    if bid is None:
        return NO_EXIT_QUOTE
    net = bid - ask - fee_rate * (ask + bid)
    tick = etf_tick(int(ask))
    spread = (ask - float(etf.bids[first])) / tick
    return Trade(net, net / tick, spread, reason)


def _sale(etf: EtfQuotes, at: int, entry: int) -> float | None:
    """The best bid sold at ``at``: the record standing then (the entry's or a later one), if it is
    usable and no older than ``FRESH``, else the first after it within ``FRESH`` if usable; None
    without one."""
    standing = int(np.searchsorted(etf.times, at, side="right")) - 1
    if standing >= entry and at - etf.times[standing] <= _FRESH_US and etf.usable[standing]:
        return float(etf.bids[standing])
    after = standing + 1
    if after < len(etf.times) and etf.times[after] - at <= _FRESH_US and etf.usable[after]:
        return float(etf.bids[after])
    return None


# The tallies.


@dataclass
class Tally:
    """A group's calls (all, a kind's, a cell's) and how they were replayed."""

    calls: int = 0
    left_out: Counter[str] = field(default_factory=Counter)
    trades: list[Trade] = field(default_factory=list)
    by_day: dict[date, list[float]] = field(default_factory=dict)
    answered: int = 0  # trades whose answer the engine stored
    fell: int = 0  # of those, the fall

    def leave_out(self, reason: str, n: int = 1) -> None:
        self.calls += n
        self.left_out[reason] += n

    def add(self, day: date, trade: Trade, outcome: str | None) -> None:
        self.calls += 1
        self.trades.append(trade)
        self.by_day.setdefault(day, []).append(trade.net)
        if outcome is not None:
            self.answered += 1
            self.fell += outcome == DOWN


@dataclass
class Replay:
    """The replay of the days: all the calls, by kind, by cell, by day."""

    days: list[date] = field(default_factory=list)
    all: Tally = field(default_factory=Tally)
    kinds: dict[str, Tally] = field(default_factory=dict)
    cells: dict[Cell, Tally] = field(default_factory=dict)
    daily: dict[date, Tally] = field(default_factory=dict)

    def groups(self, day: date, cell: Cell) -> list[Tally]:
        return [
            self.all,
            self.kinds.setdefault(cell.kind, Tally()),
            self.cells.setdefault(cell, Tally()),
            self.daily.setdefault(day, Tally()),
        ]


def replay_days(
    db: sqlite3.Connection,
    data_dir: Path,
    raw: RawDays,
    calendar: MarketCalendar,
    days: Sequence[date],
    fee_rate: float,
) -> Replay:
    """The down calls of ``days`` replayed, a day's recording at a time."""
    db.row_factory = sqlite3.Row
    out = Replay(days=list(days))
    for day in days:
        session = calendar.session(day)
        if session is None:
            continue
        windows = special_windows(db, SYMBOL, datetime.combine(day, time(), KST))
        calls, left = day_calls(db, day, session, windows)
        for cell, reasons in left.items():
            for reason, n in reasons.items():
                for tally in out.groups(day, cell):
                    tally.leave_out(reason, n)
        out.daily.setdefault(day, Tally())
        if not calls:
            continue
        recording = read_day(data_dir, raw, day)
        answers = {c: day_answers(db, c, day) for c in {call.cell for call in calls}}
        for call in calls:
            result = NO_RECORDING if recording is None else replay_call(call, recording, fee_rate)
            groups = out.groups(day, call.cell)
            if isinstance(result, str):
                for tally in groups:
                    tally.leave_out(result)
                continue
            answer = answers[call.cell].get(call.prediction_id)
            for tally in groups:
                tally.add(day, result, None if answer is None else answer.outcome)
        del recording
        give_back()
    return out


def recorded_days(data_dir: Path, through: date) -> list[date]:
    """The days up to ``through`` with the ETF's book recorded: its events or its raw frames (their
    folder stays when the archive prunes them)."""
    found = set()
    for layer in ("events", "raw"):
        for partition in (data_dir / layer).glob(f"date=*/feed={KRX_BOOK.tr_id}/symbol={ETF}"):
            try:
                day = date.fromisoformat(partition.parent.parent.name.removeprefix("date="))
            except ValueError:
                continue
            if day <= through:
                found.add(day)
    return sorted(found)


# The report.


def _pct(part: float, whole: float) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "-"


def _won(value: float) -> str:
    return f"{value:+,.1f}원"


def tally_lines(tally: Tally) -> list[str]:
    """A group's lines (module doc "The report"): counts, rates, ticks and won."""
    left = " · ".join(f"{LEFT_OUT[r]} {tally.left_out[r]}" for r in LEFT_OUT if tally.left_out[r])
    lines = [f"아래 호출 {tally.calls} · 재생 {len(tally.trades)} · 뺀 것: {left or '없음'}"]
    trades = tally.trades
    if not trades:
        return lines
    nets = [t.net for t in trades]
    gained = sum(n > 0 for n in nets)
    interval = day_mean(tally.by_day)
    spread = statistics.median(t.spread_ticks for t in trades)
    mean = f"매매당 평균 {_won(statistics.fmean(nets))}"
    if interval is not None and interval.low is not None and interval.high is not None:
        mean += (
            f"(거래일 단위 {_won(interval.value)}, 95% [{_won(interval.low)}, {_won(interval.high)}],"
            f" {interval.n}일)"
        )
    ticks = statistics.fmean(t.net_ticks for t in trades)
    lines.append(f"1주 순손익: 합 {_won(sum(nets))} · {mean} · 이익 {gained}({_pct(gained, len(nets))})")
    lines.append(f"순 틱 평균 {ticks:+.2f} · 살 때 스프레드 중앙 {spread:.0f}틱")
    exits = Counter(t.exit for t in trades)
    lines.append("청산: " + " · ".join(f"{EXITS[e]} {exits[e]}" for e in EXITS))
    if tally.answered:
        lines.append(
            f"엔진의 답이 있는 {tally.answered}개 중 아래 {tally.fell}({_pct(tally.fell, tally.answered)})"
        )
    return lines


def report(settings: Settings, calendar: MarketCalendar, through: date, raw: RawDays) -> tuple[str, int]:
    """The report of the latest ``DAYS`` recorded days up to ``through``, and the exit code."""
    started, meter = clock.monotonic(), Meter()
    every = recorded_days(settings.data_dir, through)
    days = [d for d in every if calendar.session(d) is not None][-DAYS:]
    head = [
        f"{TITLE}: {SYMBOL}의 아래 호출을 {ETF} 1주로, 매매하지 않는다",
        '개수, 비율, 틱, 원만 보인다(가격은 없다). 규칙은 `docs/operations.md` "인버스 ETF 재생 (#606)".',
        "",
    ]
    if not days:
        return "\n".join([*head, f"{through}까지 {ETF}의 호가가 기록된 거래일이 없다."]), NO_DAYS
    with read_only(settings.state_dir / DB_NAME) as db:
        if db is None:
            return "\n".join([*head, "엔진 DB가 없다."]), NO_ENGINE_DB
        result = replay_days(db, settings.data_dir, raw, calendar, days, settings.fee_rate)
    recorded = [d for d in every if calendar.session(d) is not None]
    out = [
        *head[:2],
        f"재생한 날: {days[0]} – {days[-1]}, {len(days)}거래일(기록된 날 모두 {len(recorded)}일)",
        "",
        "1. 날마다",
    ]
    for day in days:
        tally = result.daily.get(day, Tally())
        net = sum(t.net for t in tally.trades)
        weekday = "월화수목금토일"[day.weekday()]
        out.append(
            f"  {day}({weekday}): 아래 호출 {tally.calls} · 재생 {len(tally.trades)} · 순손익 합 {_won(net)}"
        )
    out += ["", "2. 모든 아래 호출", *(f"  {line}" for line in tally_lines(result.all)), "", "3. 종류별"]
    for name in TRADED_KINDS:
        if name in result.kinds:
            out += [f"  {name}", *(f"    {line}" for line in tally_lines(result.kinds[name]))]
    out += ["", "4. 칸별(종류 · 예측기)"]
    for cell in sorted(result.cells, key=lambda c: (TRADED_KINDS.index(c.kind), c.predictor)):
        out += [
            f"  {cell.kind} · {cell.predictor}",
            *(f"    {line}" for line in tally_lines(result.cells[cell])),
        ]
    peak = meter.peak_mb()
    out += [
        "",
        "5. 실행",
        f"  {clock.monotonic() - started:.1f}초 · 최대 RSS {'-' if peak is None else f'{peak:.0f} MB'}"
        f" · raw에서 다시 읽은 파티션 {raw.restored}",
    ]
    if len(recorded) >= DAYS and recorded[-1] == days[-1] and not _told(settings.state_dir):
        _tell(settings.state_dir, days[-1])
        out += [
            "",
            f"{AGENT_LINE} 기록된 날이 {DAYS}거래일이 됐다. 2절과 3절로 아래 호출을 인버스 ETF로 사도 될지"
            " 안을 이 이슈에 남기고 사용자에게 돌린다(매매, 채점, 실전 자격, 승격은 바꾸지 않는다).",
        ]
    return "\n".join(out), 0


# Whether the review's line (``AGENT_LINE``) was printed already, and for which day: once, on the
# first report with ``DAYS`` recorded days or more, so that a night missed does not skip it.
TOLD_FILE = "inverse-replay.json"  # in the state directory


def _told(state_dir: Path) -> bool:
    return (state_dir / TOLD_FILE).is_file()


def _tell(state_dir: Path, day: date) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / TOLD_FILE
    path.with_suffix(".tmp").write_text(json.dumps({"agent_line": day.isoformat()}) + "\n", encoding="utf-8")
    path.with_suffix(".tmp").replace(path)


def probed_day(calendar: MarketCalendar, now: datetime) -> date:
    """The trading day a run at ``now`` reads up to: today's once its session is over, else the
    trading day before; a run after midnight (the scheduled jobs run until 05:30) reads the evening's.
    ``probe-inverse-book``'s default day too."""
    local = now.astimezone(KST)
    day = (local - timedelta(hours=6)).date()
    session = calendar.session(day)
    if session is not None and local >= session.close:
        return day
    return calendar.previous_trading_day(day)


def inverse_replay(settings: Settings, now: datetime, day: date | None = None) -> tuple[str, int]:
    """The command: the report up to ``day``, by default the latest trading day whose session is over."""
    calendar = calendar_store(settings).calendar
    through = probed_day(calendar, now) if day is None else day
    with RawDays(settings.data_dir, open_store(settings)) as raw:
        return report(settings, calendar, through, raw)
