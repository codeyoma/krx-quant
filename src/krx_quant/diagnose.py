"""The read-only diagnosis (``krx-quant diagnose``, #82): the measurements the open decisions
need, as text to paste.

It reads the engine DB without writing to it (no journal files are made when the engine
is stopped) and reads the recorded Parquet files. Run it after the close: the day's
recordings are then complete. A recording merged by the archive while it is read is
listed again.

1. Per predictor: the display delay (bar close to stored, of the ``ok`` predictions) and,
   of those scored on both bases, how often basis A's class differs from basis B's.
2. The scored predictions' share of special ones, and every prediction's status.
3. Per symbol: the integrated book's (``H0UNASP0``) spread in ticks at each bar close: the
   last valid book received within the minute before it, from the NXT open on. The
   reference price also takes the quotes the trades carry, so it may see a later book.
4. Per feed: the receive delay, receipt minus the exchange time (a second's resolution,
   so up to a second high). Feeds whose exchange time is, for some rows, the receive
   second are flagged with which rows.
5. Per trading day: the macro regime decided before the open, when it had no indicator
   or missed some; and the days without one.

A distribution shows a few quantiles and the maximum. A quantile is the lower of the two
values around q·(n − 1) of the sorted values: no value is made up. Nothing is judged
against a threshold. A section without its data says "없음". Each feed is read and let go
in turn, as float32 seconds, to stay within the server's memory.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from datetime import date, datetime, time
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from krx_quant.feeds import (
    BOOK,
    INDEX,
    INDEX_REST,
    KOSPI200_FUTURES,
    OPTION,
    US_TRADE,
    USD_FUTURES,
    VI_STATUS,
)
from krx_quant.feeds.base import KST
from krx_quant.macro import INDICATORS, MACRO, decided_before, read_partition
from krx_quant.market_calendar import MarketCalendar
from krx_quant.market_view import prediction_window
from krx_quant.predictions import SCORED
from krx_quant.pricing import tick_size
from krx_quant.reference import BAR, KRX_ONLY, MAX_QUOTE_AGE
from krx_quant.regime import NO_INDICATOR

DEFAULT_DAYS = 20
QUANTILES = (0.5, 0.9, 0.99)
_REST = "REST 대체 행"
_CLOSING = "장 마감 기록"
# Feeds whose exchange time is, for some rows, the second they were received (REST answers
# without a time, and the closing records; docs/kis-feeds.md): which rows.
RECEIVE_SECOND = {
    INDEX_REST.tr_id: "모든 행",
    INDEX.tr_id: _CLOSING,
    KOSPI200_FUTURES.tr_id: f"{_REST}, {_CLOSING}",
    USD_FUTURES.tr_id: f"{_REST}, {_CLOSING}",
    OPTION.tr_id: f"{_REST}, {_CLOSING}",
    US_TRADE.tr_id: _REST,
}
# A VI status's event_time is when the VI started, not when the answer was sent.
NOT_SENT_AT = {VI_STATUS.tr_id}


def diagnose(data_dir: Path, db: Path, calendar: MarketCalendar, days: int | None = DEFAULT_DAYS) -> str:
    """The diagnosis of the latest ``days`` trading days with data (every one if None)."""
    with _read_only(db) as conn:
        chosen = [d for d in _days(data_dir, conn) if calendar.is_trading_day(d)]
        if days is not None:
            chosen = chosen[-days:]
        sections = [
            ("1. 알고리즘별 표시 지연(초, 봉 마감 → 저장)과 기준 A·B", _delays(conn, chosen)),
            ("2. 채점된 예측과 예측 상태", _statuses(conn, chosen)),
            ("3. 종목별 봉 마감 스프레드(틱, 통합 호가)", _spreads(data_dir, chosen, calendar)),
            ("4. 피드별 수신 지연(초, 수신 − 거래소 시각)", _receive_delays(data_dir, chosen)),
            ("5. 매크로 국면(개장 전 결정)", _macro(data_dir, chosen, calendar)),
        ]
    lines = [f"진단 {chosen[0]} – {chosen[-1]} ({len(chosen)}거래일)" if chosen else "진단: 데이터 없음"]
    for title, body in sections:
        lines += ["", title, *(f"  {line}" for line in body or ["없음"])]
    return "\n".join(lines)


@contextlib.contextmanager
def _read_only(db: Path) -> Iterator[sqlite3.Connection | None]:
    """The engine DB, read without writing: while the engine runs (its write-ahead log is
    there) as a reader beside it, else as a file nothing changes; None if there is no DB."""
    if not db.exists():
        yield None
        return
    live = db.with_name(db.name + "-wal").exists()
    conn = sqlite3.connect(db.resolve().as_uri() + ("?mode=ro" if live else "?mode=ro&immutable=1"), uri=True)
    try:
        yield conn
    finally:
        conn.close()


def _days(data_dir: Path, conn: sqlite3.Connection | None) -> list[date]:
    """Every day with predictions, recorded events or a macro reading, oldest first."""
    found: set[date] = set()
    if conn is not None:
        rows = conn.execute("SELECT DISTINCT substr(bar_close, 1, 10) FROM predictions").fetchall()
        found |= {date.fromisoformat(r[0]) for r in rows}
    for layer in ("events", MACRO):
        for partition in (data_dir / layer).glob("date=*"):
            found.add(date.fromisoformat(partition.name.removeprefix("date=")))
    return sorted(found)


def _on_days(days: Sequence[date]) -> tuple[str, list[str]]:
    """A SQL condition on the bar close's day being one of ``days``, and its parameters."""
    return f"substr(p.bar_close, 1, 10) IN ({', '.join('?' * len(days))})", [d.isoformat() for d in days]


def _distribution(values: pa.Array | pa.ChunkedArray, fmt: str) -> str:
    """The QUANTILES of ``values`` (the lower value around q·(n − 1)), then the maximum."""
    found = pc.quantile(values, q=list(QUANTILES), interpolation="lower").to_pylist()
    shown = " · ".join(f"p{round(q * 100)} {format(v, fmt)}" for q, v in zip(QUANTILES, found, strict=True))
    return f"{shown} · 최대 {format(pc.max(values).as_py(), fmt)}"


def _delays(conn: sqlite3.Connection | None, days: Sequence[date]) -> list[str]:
    if conn is None or not days:
        return []
    where, params = _on_days(days)
    delays: dict[str, list[float]] = defaultdict(list)
    for name, bar_close, displayed_at in conn.execute(
        "SELECT p.algorithm, p.bar_close, p.displayed_at FROM predictions p"
        f" WHERE p.status = 'ok' AND {where}",
        params,
    ):
        seconds = (datetime.fromisoformat(displayed_at) - datetime.fromisoformat(bar_close)).total_seconds()
        delays[name].append(seconds)
    scored: dict[str, tuple[int, int, int]] = {}
    for name, n, both, other in conn.execute(
        "SELECT p.algorithm, COUNT(*), SUM(o.class_a IS NOT NULL AND o.class_b IS NOT NULL),"
        " SUM(o.class_a IS NOT NULL AND o.class_b IS NOT NULL AND o.class_a != o.class_b)"
        f" FROM predictions p JOIN outcomes o ON o.prediction_id = p.id WHERE {SCORED} AND {where}"
        " GROUP BY p.algorithm",
        params,
    ):
        scored[name] = (n, both or 0, other or 0)
    lines = []
    for name in sorted(delays):
        n, both, other = scored.get(name, (0, 0, 0))
        differ = f"A≠B {other / both:.1%} (둘 다 채점 {both})" if both else "A≠B —"
        values = pa.array(delays[name], pa.float64())
        lines.append(f"{name}: 예측 {len(values)}, 지연 {_distribution(values, '.2f')} · 채점 {n}, {differ}")
    return lines


def _statuses(conn: sqlite3.Connection | None, days: Sequence[date]) -> list[str]:
    if conn is None or not days:
        return []
    where, params = _on_days(days)
    statuses = conn.execute(
        f"SELECT p.status, COUNT(*) FROM predictions p WHERE {where} GROUP BY p.status ORDER BY p.status",
        params,
    ).fetchall()
    if not statuses:
        return []
    n, special = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(o.special), 0)"
        f" FROM predictions p JOIN outcomes o ON o.prediction_id = p.id WHERE {SCORED} AND {where}",
        params,
    ).fetchone()
    share = f" ({special / n:.1%})" if n else ""
    return [f"채점 {n}, 특수 상황 {special}{share}", "상태: " + ", ".join(f"{s} {c}" for s, c in statuses)]


def _partitions(data_dir: Path, days: Iterable[date], feed: str) -> Iterator[tuple[date, Path]]:
    """The recorded partitions of ``feed`` on ``days``: (day, path), a symbol each."""
    for day in days:
        for partition in sorted((data_dir / "events" / f"date={day}" / f"feed={feed}").glob("symbol=*")):
            yield day, partition


def _spreads(data_dir: Path, days: Sequence[date], calendar: MarketCalendar) -> list[str]:
    ticks: dict[str, list[float]] = defaultdict(list)
    for day, partition in _partitions(data_dir, days, BOOK.tr_id):
        session = calendar.session(day)
        table = read_partition(partition, ("received_at", "ask_px_1", "bid_px_1"))
        if session is None or table is None or table.num_rows == 0:
            continue
        ask, bid = table["ask_px_1"], table["bid_px_1"]
        valid = pc.and_(pc.and_(pc.greater(ask, 0), pc.greater(bid, 0)), pc.greater_equal(ask, bid))
        books = table.filter(pc.fill_null(valid, False)).sort_by("received_at")
        times = pc.cast(pc.cast(books["received_at"], pa.timestamp("us", tz="UTC")), pa.int64()).to_numpy()
        asks, bids = books["ask_px_1"].to_numpy(), books["bid_px_1"].to_numpy()
        since = session.open + KRX_ONLY  # the integrated book counts from the NXT open
        first, last = prediction_window(session)
        closes = [first + i * BAR for i in range(int((last - first) / BAR) + 1)]
        # The last valid book received strictly before each bar close, if under a minute old.
        found = np.searchsorted(times, np.array([_micros(t) for t in closes], dtype=np.int64)) - 1
        for close, i in zip(closes, found, strict=True):
            if i >= 0 and times[i] >= _micros(max(since, close - MAX_QUOTE_AGE)):
                spread = float(asks[i] - bids[i]) / tick_size(float(bids[i]))
                ticks[partition.name.removeprefix("symbol=")].append(spread)
    return [
        f"{symbol}: 봉 마감 {len(values)}, {_distribution(pa.array(values, pa.float64()), 'g')}"
        for symbol, values in sorted(ticks.items())
    ]


def _micros(at: datetime) -> int:
    return int(at.timestamp() * 1_000_000)


def _receive_delays(data_dir: Path, days: Sequence[date]) -> list[str]:
    feeds = {
        p.name.removeprefix("feed=") for d in days for p in (data_dir / "events" / f"date={d}").glob("feed=*")
    }
    lines = []
    for feed in sorted(feeds - NOT_SENT_AT):  # one feed at a time: its delays, as float32, then let go
        chunks: list[pa.Array] = []
        for _, partition in _partitions(data_dir, days, feed):
            table = read_partition(partition, ("received_at", "event_time"))
            if table is None or table.num_rows == 0:
                continue
            received = pc.cast(table["received_at"], pa.timestamp("us", tz="UTC"))
            sent = pc.cast(table["event_time"], pa.timestamp("us", tz="UTC"))
            micros = pc.cast(pc.subtract(received, sent), pa.int64())
            chunks += pc.cast(pc.divide(pc.cast(micros, pa.float64()), 1e6), pa.float32()).chunks
        values = pa.chunked_array(chunks, pa.float32()).drop_null()
        if len(values) == 0:
            continue
        line = f"{feed}: {len(values)}건, {_distribution(values, '.2f')}"
        if feed in RECEIVE_SECOND:
            line += f" · 거래소 시각이 수신 초인 행: {RECEIVE_SECOND[feed]}"
        lines.append(line)
    return lines


def _macro(data_dir: Path, days: Sequence[date], calendar: MarketCalendar) -> list[str]:
    nothing: list[str] = []
    partial: list[str] = []
    undecided: list[str] = []
    for day in days:
        session = calendar.session(day)
        before = session.open if session is not None else datetime.combine(day, time.max, KST)
        decided = decided_before(data_dir, day, before)
        if decided is None:
            undecided.append(str(day))
        elif decided.reasons == [NO_INDICATOR]:
            nothing.append(str(day))
        elif missing := [i.name for i in INDICATORS if i.name not in decided.valued]:
            partial.append(f"{day} ({', '.join(missing)})")
    if len(undecided) == len(days):
        return []
    return [
        f"{NO_INDICATOR}: {', '.join(nothing) or '없음'}",
        f"일부 빠짐: {'; '.join(partial) or '없음'}",
        f"기록 없음: {', '.join(undecided) or '없음'}",
    ]
