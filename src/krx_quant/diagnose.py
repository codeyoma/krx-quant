"""The read-only diagnosis (``krx-quant diagnose``, #82): the measurements the open decisions
need, as text to paste.

It reads the engine DB through a read-only connection and the recorded Parquet files, and
writes nothing. Run it after the close: the day's recordings are then complete.

1. Per predictor: the display delay (bar close to stored, of the ``ok`` predictions) and,
   of the scored ones, how often basis A's class differs from basis B's.
2. The scored predictions' share of special ones, and every prediction's status.
3. Per symbol: the integrated book's spread in ticks at each bar close (the last valid
   book within a minute before it, as the reference price takes it).
4. Per feed: the receive delay, receipt minus the exchange time (a second's resolution,
   so up to a second high). Rows whose exchange time is the receive second are flagged.
5. The days whose macro regime had no indicator, and those missing some.

A distribution shows a few quantiles and the maximum. A quantile is the lower of the two
values around q·(n − 1) of the sorted values: no value is made up. Nothing is judged
against a threshold. A section without its data says "없음".
"""

from __future__ import annotations

import bisect
import contextlib
import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from datetime import date, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from krx_quant.archive import partition_files
from krx_quant.feeds import BOOK, VI_STATUS
from krx_quant.macro import INDICATORS, MACRO
from krx_quant.market_calendar import MarketCalendar
from krx_quant.market_view import prediction_window
from krx_quant.pricing import tick_size
from krx_quant.reference import BAR, MAX_QUOTE_AGE

QUANTILES = (0.5, 0.9, 0.99)
NONE = "  없음"
# Feeds whose exchange time is, for some rows, the second they were received (REST answers
# without a time; docs/kis-feeds.md, "REST 대체 조회"): which rows.
RECEIVE_SECOND = {
    "FHPUP02100000": "모든 행",
    "H0IFCNT0": "REST 대체 행",
    "H0CFCNT0": "REST 대체 행",
    "HDFSCNT0": "REST 대체 행",
    "H0IOCNT0": "REST 대체 행",
}
# Feeds whose event_time is not when the message was sent: a VI's start.
NOT_SENT_AT = {VI_STATUS.tr_id}
NO_INDICATOR = "지표 없음"
_SCORED = "p.status = 'ok' AND o.scored_at IS NOT NULL AND (o.class_a IS NOT NULL OR o.class_b IS NOT NULL)"


def diagnose(data_dir: Path, db: Path, calendar: MarketCalendar, days: int | None = None) -> str:
    """The diagnosis of the latest ``days`` days with data (all of them by default)."""
    with _read_only(db) as conn:
        chosen = _days(data_dir, conn)
        if days is not None:
            chosen = chosen[-days:]
        if chosen:
            heading = f"진단 {chosen[0]} – {chosen[-1]} ({len(chosen)}일)"
        else:
            heading = "진단: 데이터 없음"
        sections = [
            ("1. 알고리즘별 표시 지연(초, 봉 마감 → 저장)과 기준 A·B", _delays(conn, chosen)),
            ("2. 채점된 예측과 예측 상태", _statuses(conn, chosen)),
            ("3. 종목별 봉 마감 스프레드(틱, 통합 호가)", _spreads(data_dir, chosen, calendar)),
            ("4. 피드별 수신 지연(초, 수신 − 거래소 시각)", _receive_delays(data_dir, chosen)),
            ("5. 매크로 국면", _macro(data_dir, chosen)),
        ]
    lines = [heading]
    for title, body in sections:
        lines += ["", title, *(body or [NONE])]
    return "\n".join(lines)


@contextlib.contextmanager
def _read_only(db: Path) -> Iterator[sqlite3.Connection | None]:
    """A read-only connection to the engine DB, or None if there is no DB."""
    if not db.exists():
        yield None
        return
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
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


def _in(days: Sequence[date]) -> tuple[str, list[str]]:
    """A SQL condition on the bar close's day being one of ``days``, and its parameters."""
    return f"substr(p.bar_close, 1, 10) IN ({', '.join('?' * len(days))})", [d.isoformat() for d in days]


def quantiles(values: pa.Array | pa.ChunkedArray) -> list[float]:
    """The QUANTILES of ``values`` (the lower value around q·(n − 1)), then the maximum."""
    found = pc.quantile(values, q=list(QUANTILES), interpolation="lower").to_pylist()
    return [*found, pc.max(values).as_py()]


def _spread(values: list[float], show: str) -> str:
    *qs, top = values
    shown = " · ".join(f"p{round(q * 100)} {format(v, show)}" for q, v in zip(QUANTILES, qs, strict=True))
    return f"{shown} · 최대 {format(top, show)}"


def _delays(conn: sqlite3.Connection | None, days: Sequence[date]) -> list[str]:
    if conn is None or not days:
        return []
    where, params = _in(days)
    delays: dict[str, list[float]] = defaultdict(list)
    for name, bar_close, displayed_at in conn.execute(
        "SELECT p.algorithm, p.bar_close, p.displayed_at FROM predictions p"
        f" WHERE p.status = 'ok' AND {where}",
        params,
    ):
        seconds = (datetime.fromisoformat(displayed_at) - datetime.fromisoformat(bar_close)).total_seconds()
        delays[name].append(seconds)
    differ: dict[str, tuple[int, int]] = {}
    for name, n, other in conn.execute(
        "SELECT p.algorithm, COUNT(*),"
        " SUM(o.class_a IS NOT NULL AND o.class_b IS NOT NULL AND o.class_a != o.class_b)"
        f" FROM predictions p JOIN outcomes o ON o.prediction_id = p.id WHERE {_SCORED} AND {where}"
        " GROUP BY p.algorithm",
        params,
    ):
        differ[name] = (n, other or 0)
    lines = []
    for name in sorted(delays):
        n, other = differ.get(name, (0, 0))
        share = f"{other / n:.1%}" if n else "—"
        values = quantiles(pa.array(delays[name], pa.float64()))
        lines.append(
            f"  {name}: 예측 {len(delays[name])}, 지연 {_spread(values, '.2f')} · 채점 {n}, A≠B {share}"
        )
    return lines


def _statuses(conn: sqlite3.Connection | None, days: Sequence[date]) -> list[str]:
    if conn is None or not days:
        return []
    where, params = _in(days)
    n, special = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(o.special), 0)"
        f" FROM predictions p JOIN outcomes o ON o.prediction_id = p.id WHERE {_SCORED} AND {where}",
        params,
    ).fetchone()
    statuses = conn.execute(
        f"SELECT p.status, COUNT(*) FROM predictions p WHERE {where} GROUP BY p.status ORDER BY p.status",
        params,
    ).fetchall()
    if not statuses:
        return []
    share = f" ({special / n:.1%})" if n else ""
    counts = ", ".join(f"{status} {count}" for status, count in statuses)
    return [f"  채점 {n}, 특수 상황 {special}{share}", f"  상태: {counts}"]


def _read(partition: Path, columns: Sequence[str]) -> pa.Table | None:
    """The partition's rows, each once, with ``columns``; None if a column is not recorded."""
    files = partition_files(partition)
    if not files or not set(columns) <= set(pq.read_schema(files[0]).names):
        return None
    return pa.concat_tables(pq.read_table(f, columns=list(columns)) for f in files)


def _partitions(data_dir: Path, days: Iterable[date], feed: str | None = None) -> Iterable[tuple[str, Path]]:
    """The recorded events' partitions on ``days`` (of ``feed`` only if given): (feed, path)."""
    for day in days:
        for feed_dir in sorted((data_dir / "events" / f"date={day}").glob("feed=*")):
            name = feed_dir.name.removeprefix("feed=")
            if feed is None or name == feed:
                for partition in sorted(feed_dir.glob("symbol=*")):
                    yield name, partition


def _spreads(data_dir: Path, days: Sequence[date], calendar: MarketCalendar) -> list[str]:
    ticks: dict[str, list[float]] = defaultdict(list)
    for _, partition in _partitions(data_dir, days, BOOK.tr_id):
        day = date.fromisoformat(partition.parent.parent.name.removeprefix("date="))
        session = calendar.session(day)
        table = _read(partition, ("received_at", "ask_px_1", "bid_px_1"))
        if session is None or table is None:
            continue
        books = sorted(
            (r["received_at"], r["ask_px_1"], r["bid_px_1"])
            for r in table.to_pylist()
            if r["ask_px_1"] and r["bid_px_1"] and r["ask_px_1"] >= r["bid_px_1"]
        )
        times = [b[0] for b in books]
        first, last = prediction_window(session)
        close = first
        while close <= last:
            # The last valid book received before the bar close, if it is under a minute old.
            i = bisect.bisect_left(times, close) - 1
            if i >= 0 and times[i] >= close - MAX_QUOTE_AGE:
                _, ask, bid = books[i]
                ticks[partition.name.removeprefix("symbol=")].append((ask - bid) / tick_size(bid))
            close += BAR
    return [
        f"  {symbol}: 봉 마감 {len(values)}, {_spread(quantiles(pa.array(values, pa.float64())), 'g')}"
        for symbol, values in sorted(ticks.items())
    ]


def _receive_delays(data_dir: Path, days: Sequence[date]) -> list[str]:
    chunks: dict[str, list[pa.Array]] = defaultdict(list)
    for feed, partition in _partitions(data_dir, days):
        if feed in NOT_SENT_AT:
            continue
        table = _read(partition, ("received_at", "event_time"))
        if table is None or table.num_rows == 0:
            continue
        received = pc.cast(table["received_at"], pa.timestamp("us", tz="UTC"))
        sent = pc.cast(table["event_time"], pa.timestamp("us", tz="UTC"))
        micros = pc.cast(pc.subtract(received, sent), pa.int64())
        chunks[feed].extend(pc.divide(pc.cast(micros, pa.float64()), 1e6).chunks)
    lines = []
    for feed in sorted(chunks):
        values = pa.chunked_array(chunks[feed], pa.float64()).drop_null()
        if len(values) == 0:
            continue
        line = f"  {feed}: {len(values)}건, {_spread(quantiles(values), '.2f')}"
        if feed in RECEIVE_SECOND:
            line += f" · 거래소 시각이 수신 초인 행: {RECEIVE_SECOND[feed]}"
        lines.append(line)
    return lines


def _macro(data_dir: Path, days: Sequence[date]) -> list[str]:
    nothing: list[date] = []
    partial: list[str] = []
    seen = False
    for day in days:
        base = data_dir / MACRO / f"date={day}"
        regimes = _read(base / "feed=regime" / "symbol=market", ("recorded_at", "reasons"))
        if regimes is None or regimes.num_rows == 0:
            continue
        seen = True
        latest = max(regimes.to_pylist(), key=lambda r: r["recorded_at"])
        if json.loads(latest["reasons"]) == [NO_INDICATOR]:
            nothing.append(day)
            continue
        valued = set()
        for partition in sorted((base / "feed=readings").glob("symbol=*")):
            readings = _read(partition, ("recorded_at", "indicator", "value"))
            for r in [] if readings is None else readings.to_pylist():
                if r["recorded_at"] == latest["recorded_at"] and r["value"] is not None:
                    valued.add(r["indicator"])
        missing = [i.name for i in INDICATORS if i.name not in valued]
        if missing:
            partial.append(f"{day} ({', '.join(missing)})")
    if not seen:
        return []
    return [
        f"  {NO_INDICATOR}: {', '.join(str(d) for d in nothing) or '없음'}",
        f"  일부 빠짐: {'; '.join(partial) or '없음'}",
    ]
