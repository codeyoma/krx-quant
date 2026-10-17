"""Entry delay (#262): how a call's touch target and touch P&L hold up when it is acted on a
few seconds after its bar close, as a person reading a signal would.

The touch label (``touch.py``) and the touch P&L (``stats.touch_profit``) take a call as entered
at its bar close t: at the best quote standing then, its target counted reached by any quote
received from t to t + 4 minutes. Acted on ``delay`` seconds later, at t + delay:

- it enters at the best quote standing then: the last one received at or before t + delay,
  if received from t on (a crossed one too: each side was a price one could trade at, #187),
  else the one standing at t (the path's start quote);
- its target counts as reached only by that quote or one received after it, up to the same
  end t + 4 minutes; the targets (the bar close's mid-point ± k ticks) and the end quotes
  stay as they were.

A delay of 0 is the stored path as it is. The quotes are the integrated book's (``BOOK``) and
the integrated trades' best quotes (``TRADE``), in receipt order, a one-sided one dropped: what
the touch window follows. A day without its recording (only in the Object Store) is left out
of the delays above 0.

"Reached" is the called side's target: the upper one for an up call, the lower one for a down
call. At a delay of 0 it is the touch label's hit, except in a window that reached both
targets: the label says up, while a down call's target was reached too.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from krx_quant.baselines import baselines_for
from krx_quant.consensus import NAMES
from krx_quant.feeds import BOOK, TRADE
from krx_quant.macro import read_partition
from krx_quant.predictions import scored_rows
from krx_quant.pricing import Direction
from krx_quant.skill import day_kappa, skill
from krx_quant.stats import Prices, Scored, on_target, scored_by_algorithm, touch_profit
from krx_quant.touch import TARGET_VERSION, WINDOW

DELAYS = (0, 1, 5, 30)  # seconds after the bar close


@dataclass(frozen=True)
class Quotes:
    """A symbol's integrated best quotes on a day, in receipt order (microseconds since the
    epoch), one-sided ones dropped."""

    times: np.ndarray
    bids: np.ndarray
    asks: np.ndarray


def day_quotes(data_dir: Path, day: date, symbol: str) -> Quotes | None:
    """``symbol``'s recorded book and trade quotes on ``day``; None without either."""
    parts = []
    for feed, bid, ask in ((BOOK.tr_id, "bid_px_1", "ask_px_1"), (TRADE.tr_id, "bid1", "ask1")):
        partition = data_dir / "events" / f"date={day}" / f"feed={feed}" / f"symbol={symbol}"
        table = read_partition(partition, ("received_at", bid, ask))
        if table is None or table.num_rows == 0:
            continue
        table = table.select(["received_at", bid, ask]).rename_columns(["received_at", "bid", "ask"])
        table = table.set_column(1, "bid", pc.cast(table["bid"], pa.float64()))
        table = table.set_column(2, "ask", pc.cast(table["ask"], pa.float64()))
        two_sided = pc.and_(pc.greater(table["bid"], 0), pc.greater(table["ask"], 0))
        parts.append(table.filter(pc.fill_null(two_sided, False)))
    if not parts:
        return None
    quotes = pa.concat_tables(parts)
    times = pc.cast(pc.cast(quotes["received_at"], pa.timestamp("us", tz="UTC")), pa.int64()).to_numpy()
    order = np.argsort(times, kind="stable")
    return Quotes(times[order], quotes["bid"].to_numpy()[order], quotes["ask"].to_numpy()[order])


def _micros(at: datetime) -> int:
    return int(at.timestamp() * 1_000_000)


def delayed(prices: Prices, bar_close: datetime, quotes: Quotes | None, delay: int) -> Prices | None:
    """``prices`` as a call acted on ``delay`` seconds after ``bar_close`` sees them: its start
    quote the one standing then, its targets reached only from then on (the module's rules).
    None for a delay above 0 without the day's quotes."""
    if delay == 0:
        return prices
    if quotes is None:
        return None
    start, entry = _micros(bar_close), _micros(bar_close) + delay * 1_000_000
    end = _micros(bar_close + WINDOW)
    standing = int(np.searchsorted(quotes.times, entry, side="right")) - 1
    bid: float | None
    ask: float | None
    if standing >= 0 and quotes.times[standing] >= start:
        bid, ask, first = float(quotes.bids[standing]), float(quotes.asks[standing]), standing
    else:  # nothing received since the bar close: the quote standing at it
        bid, ask, first = prices.bid_start, prices.ask_start, standing + 1
    last = int(np.searchsorted(quotes.times, end, side="left"))
    bids, asks = quotes.bids[first:last], quotes.asks[first:last]
    return replace(
        prices,
        bid_start=bid,
        ask_start=ask,
        upper_reached=bool((bids >= prices.upper).any()),
        lower_reached=bool((asks <= prices.lower).any()),
    )


@dataclass
class Tally:
    """A predictor's up and down calls acted on after each delay: whether each reached its
    target, and its touch P&L in ticks (research only)."""

    calls: int = 0
    reached: dict[int, list[bool]] = field(default_factory=lambda: {d: [] for d in DELAYS})
    profits: dict[int, list[float]] = field(default_factory=lambda: {d: [] for d in DELAYS})

    def add(
        self,
        r: Scored,
        quotes: Quotes | None,
        fee_rate: float,
        seen: dict[tuple[str, datetime, int], Prices | None] | None = None,
    ) -> None:
        """``r`` acted on after each delay, if it called up or down; ``seen`` keeps each bar
        close's delayed prices for the other predictors of it (they share its path)."""
        if r.predicted is Direction.FLAT or r.prices is None:
            return
        self.calls += 1
        for delay in DELAYS:
            key = (r.symbol, r.bar_close, delay)
            if seen is not None and key in seen:
                prices = seen[key]
            else:
                prices = delayed(r.prices, r.bar_close, quotes, delay)
                if seen is not None:
                    seen[key] = prices
            if prices is None:
                continue
            up = r.predicted is Direction.UP
            self.reached[delay].append(prices.upper_reached if up else prices.lower_reached)
            if (profit := touch_profit(r.predicted, prices, fee_rate)) is not None:
                self.profits[delay].append(profit)


def entry_delays(
    conn: sqlite3.Connection, data_dir: Path, days: Sequence[date], fee_rate: float
) -> list[str]:
    """Each predictor's line (``diagnose`` section 10): its scored predictions on ``days`` as the
    leaderboard reads them (special ones left out; an algorithm's by models of the touch target
    only), their skill against the class rates (``skill.py``: kappa, by trading day too), and its
    up and down calls' target reached and touch P&L after each delay. A day at a time, its
    quotes let go after it, to stay within the server's memory."""
    baselines = {b.name for b in baselines_for(TARGET_VERSION)} | set(NAMES)
    tallies: dict[str, Tally] = {}
    calls: dict[str, dict[date, list[tuple[Direction, Direction]]]] = {}
    for day in days:
        factory, conn.row_factory = conn.row_factory, sqlite3.Row  # as the store reads its rows
        try:
            rows = scored_rows(conn, [day], None, False)
        finally:
            conn.row_factory = factory  # the diagnosis's other sections read tuples
        by_name = scored_by_algorithm(on_target(rows, baselines, TARGET_VERSION))
        symbols = {r.symbol for records in by_name.values() for r in records}
        quotes = {symbol: day_quotes(data_dir, day, symbol) for symbol in sorted(symbols)}
        seen: dict[tuple[str, datetime, int], Prices | None] = {}
        for name, records in by_name.items():
            t = tallies.setdefault(name, Tally())
            for r in records:
                t.add(r, quotes[r.symbol], fee_rate, seen)
            calls.setdefault(name, {})[day] = [(r.predicted, r.outcome) for r in records]
    return [line for name in sorted(tallies) for line in _lines(name, tallies[name], calls[name])]


def _lines(name: str, t: Tally, calls: Mapping[date, Sequence[tuple[Direction, Direction]]]) -> list[str]:
    s = skill([c for day in calls.values() for c in day])
    if s is None:
        return []

    def score(value: float | None) -> str:
        return "—" if value is None else f"{value:+.3f}"

    by_day = day_kappa(calls)
    day = (
        "—"
        if by_day is None
        else f"{score(by_day.value)} [{score(by_day.low)}, {score(by_day.high)}] {by_day.n}일"
    )
    lines = [
        f"{name}: 채점 {s.n:,} · 정답률 {s.accuracy:.1%} · 기대 정답률 {s.expected_accuracy:.1%} · "
        f"kappa {score(s.kappa)} (거래일 {day}) · Gerrity {score(s.gerrity)} · 상승·하락 예측 {t.calls:,}"
    ]
    if not t.calls:
        return lines

    def counted(delay: int, values: Sequence[object]) -> str:
        return f"{_delay(delay)} " + ("" if len(values) == t.calls else f"(n {len(values):,}) ")

    reached = [
        counted(d, t.reached[d])
        + ("—" if not t.reached[d] else f"{sum(t.reached[d]) / len(t.reached[d]):.1%}")
        for d in DELAYS
    ]
    profits = [
        counted(d, t.profits[d])
        + ("—" if not t.profits[d] else f"{sum(t.profits[d]) / len(t.profits[d]):+.2f}")
        for d in DELAYS
    ]
    return lines + [
        f"  목표 도달: {' · '.join(reached)}",
        f"  닿음 손익(틱/번, 연구용): {' · '.join(profits)}",
    ]


def _delay(seconds: int) -> str:
    return "봉 마감" if seconds == 0 else f"+{seconds}초"
