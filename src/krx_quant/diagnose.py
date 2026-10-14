"""The read-only diagnosis (``krx-quant diagnose``, #82): the measurements the open decisions
need, as text to paste.

It reads the engine DB without writing to it (no journal files are made when the engine
is stopped) and reads the recorded Parquet files. Run it after the close: the day's
recordings are then complete. A recording merged by the archive while it is read is
listed again.

1. Per predictor: the display delay (bar close to stored, of the ``ok`` predictions) and
   how many were scored (on the touch label, #170).
2. The scored predictions' share of special ones, and every prediction's status.
3. Per symbol: the integrated book's (``H0UNASP0``) spread in ticks at each bar close: the
   last valid book received within the minute before it, from the NXT open on. The
   reference price also takes the quotes the trades carry, so it may see a later book.
4. Per feed: the receive delay, receipt minus the exchange time (a second's resolution,
   so up to a second high). Feeds whose exchange time is, for some rows, the receive
   second are flagged with which rows.
5. Per trading day: the macro regime decided before the open, when it had no indicator
   or missed some; and the days without one.
6. The touch labels (#167): their classes, at other widths, against the trade prints, before
   a hit was displayed, and how long the quote stayed at the target.
7. The range answers (#180): their classes and r by symbol and 30-minute slot; how often the
   one-minute bar rule (c = 0 and c = 1, ``ranges.bar_range``) gives the quote answer's class
   on the recorded days, from those days' backfilled bars (what chooses ``ranges.BAR_C``);
   how far the mid-point went up and down. Special windows are left out, as training leaves
   them out.

A distribution shows a few quantiles and the maximum. A quantile is the lower of the two
values around q·(n − 1) of the sorted values: no value is made up. Nothing is judged
against a threshold. A section without its data says "없음". Each feed is read and let go
in turn, as float32 seconds, to stay within the server's memory.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import replace
from datetime import date, datetime, time
from functools import partial
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from krx_quant.consensus import slot
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
from krx_quant.predictions import SCORED, TOUCH_JOIN
from krx_quant.pricing import most_likely, tick_size
from krx_quant.ranges import ANSWER_VERSION, BAR_C, RANGE, bar_range
from krx_quant.reference import BAR, KRX_ONLY, MAX_QUOTE_AGE
from krx_quant.regime import NO_INDICATOR
from krx_quant.touch import TARGET_VERSION
from krx_quant.training import bar_answers, bar_days_on

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


def diagnose(
    data_dir: Path, db: Path, calendar: MarketCalendar, days: int | None = DEFAULT_DAYS, *, fee_rate: float
) -> str:
    """The diagnosis of the latest ``days`` trading days with data (every one if None); the
    bar rule's flat band at ``fee_rate``."""
    with _read_only(db) as conn:
        chosen = [d for d in _days(data_dir, conn) if calendar.is_trading_day(d)]
        if days is not None:
            chosen = chosen[-days:]
        sections = [
            ("1. 알고리즘별 표시 지연(초, 봉 마감 → 저장)과 채점 수", _delays(conn, chosen)),
            ("2. 채점된 예측과 예측 상태", _statuses(conn, chosen)),
            ("3. 종목별 봉 마감 스프레드(틱, 통합 호가)", _spreads(data_dir, chosen, calendar)),
            ("4. 피드별 수신 지연(초, 수신 − 거래소 시각)", _receive_delays(data_dir, chosen)),
            ("5. 매크로 국면(개장 전 결정)", _macro(data_dir, chosen, calendar)),
            (
                "6. 도달 정답(#160): 분포·폭·체결가 대비·표시 전·머문 시간",
                _touches(conn, chosen),
            ),
            (
                "7. 변동폭 정답(#180): 종목 × 시간대 분포·1분봉 규칙 일치율·위아래로 간 폭",
                _ranges(conn, chosen, data_dir, calendar, fee_rate),
            ),
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
    scored: dict[str, int] = dict(
        conn.execute(
            f"SELECT p.algorithm, COUNT(*) FROM predictions p JOIN outcomes o ON o.prediction_id = p.id"
            f" {TOUCH_JOIN} WHERE {SCORED} AND {where} GROUP BY p.algorithm",
            params,
        ).fetchall()
    )
    lines = []
    for name in sorted(delays):
        values = pa.array(delays[name], pa.float64())
        lines.append(
            f"{name}: 예측 {len(values)}, 지연 {_distribution(values, '.2f')} · 채점 {scored.get(name, 0)}"
        )
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
        "SELECT COUNT(*), COALESCE(SUM(o.special), 0) FROM predictions p"
        f" JOIN outcomes o ON o.prediction_id = p.id {TOUCH_JOIN} WHERE {SCORED} AND {where}",
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


def _touches(conn: sqlite3.Connection | None, days: Sequence[date]) -> list[str]:
    """The touch labels recorded (#167): their classes by symbol and at the open, at other
    widths, against the trade prints, touched before a hit was displayed, and how long the
    quote stayed at the target."""
    if conn is None or not days:
        return []
    cond, params = _on_days(days)
    try:
        rows = conn.execute(
            "SELECT symbol, bar_close, class, unscorable, path FROM touches p"
            f" WHERE target_version = {TARGET_VERSION} AND {cond} ORDER BY bar_close",
            params,
        ).fetchall()
    except sqlite3.OperationalError:
        return []  # a DB from before the touch labels
    if not rows:
        return []

    def share(k: int, n: int) -> str:
        return f"{100 * k / n:.0f}%" if n else "—"

    def split(classes: list[str]) -> str:
        n = len(classes)
        return " · ".join(f"{label} {share(classes.count(c), n)}" for c, label in _CLASSES) + f" ({n}창)"

    scored = [(r[0], r[1], r[2], json.loads(r[4])) for r in rows if r[2] is not None]
    unscorable: dict[str, int] = {}
    for r in rows:
        if r[2] is None:
            unscorable[r[3]] = unscorable.get(r[3], 0) + 1
    lines = []
    for symbol in sorted({s for s, *_ in scored}):
        lines.append(f"{symbol}: {split([c for s, _, c, _ in scored if s == symbol])}")
    opening = [c for _, close, c, _ in scored if close[11:16] <= "09:30"]
    later = [c for _, close, c, _ in scored if close[11:16] > "09:30"]
    lines.append(f"09:01–09:30: {split(opening)} · 그 뒤: {split(later)}")
    if unscorable:
        lines.append("채점 불가: " + ", ".join(f"{why} {n}" for why, n in sorted(unscorable.items())))
    # Other widths, from the highest bid and lowest ask (the same order rule: up first).
    widths = []
    for name, scale, extra in (("k", 1, 0), ("k+1", 1, 1), ("2k", 2, 0)):
        classes = []
        for *_, path in scored:
            w = (scale * path["k"] + extra) * path["tick"]
            up = path["max_bid"] is not None and path["max_bid"] >= path["anchor"] + w
            down = path["min_ask"] is not None and path["min_ask"] <= path["anchor"] - w
            classes.append("up" if up else "down" if down else "flat")
        widths.append(f"{name} {split(classes)}")
    lines.append("폭별: " + " | ".join(widths))
    # The trade prints against the quote touch: a print at the target while the bid never got there.
    print_only = sum(
        1
        for *_, p in scored
        if (p["trade_high"] is not None and p["trade_high"] >= p["upper"] and p["upper_at"] is None)
        or (p["trade_low"] is not None and p["trade_low"] <= p["lower"] and p["lower_at"] is None)
    )
    both = sum(1 for *_, p in scored if p["upper_at"] and p["lower_at"])
    n = len(scored)
    lines.append(
        f"체결가만 목표에 닿고 호가는 닿지 않음 {share(print_only, n)} · 위아래 둘 다 닿음 {share(both, n)}"
    )
    lines += _touched_before_display(conn, cond, params)
    dwells = [
        p[f"{side}_dwell"]
        for *_, p in scored
        for side in ("upper", "lower")
        if p[f"{side}_dwell"] is not None
    ]
    if dwells:
        values = pa.array(sorted(dwells))
        short = sum(1 for d in dwells if d < 1)
        lines.append(
            f"목표에 머문 시간(초): {_distribution(values, '.1f')} · 1초 미만 {share(short, len(dwells))}"
        )
    return lines


_CLASSES = (("up", "상승"), ("flat", "보합"), ("down", "하락"))


def _touched_before_display(conn: sqlite3.Connection, cond: str, params: list[str]) -> list[str]:
    """Of the forecasts right on the touch label (up or down), those whose target was reached
    before they were displayed: done before anyone could act on them."""
    rows = conn.execute(
        "SELECT p.p_up, p.p_flat, p.p_down, p.displayed_at, t.class, t.path FROM predictions p"
        " JOIN touches t ON t.symbol = p.symbol AND t.bar_close = p.bar_close"
        f" AND t.target_version = {TARGET_VERSION}"
        f" WHERE p.status = 'ok' AND t.class IN ('up', 'down') AND {cond}",
        params,
    ).fetchall()
    hits = before = 0
    for p_up, p_flat, p_down, displayed_at, cls, path in rows:
        if most_likely(p_up, p_flat, p_down).value != cls:
            continue
        hits += 1
        reached = json.loads(path)["upper_at" if cls == "up" else "lower_at"]
        if reached is not None and datetime.fromisoformat(reached) < datetime.fromisoformat(displayed_at):
            before += 1
    if not hits:
        return []
    return [f"맞은 상승·하락 예측 {hits}개 중 표시 전에 이미 닿음 {before}개 ({100 * before / hits:.1f}%)"]


_RANGE_CLASSES = (("small", "작음"), ("medium", "보통"), ("large", "큼"))
# The bar rule's corrections measured against the quote answers (#180).
BAR_CS = (0, 1)


def _ranges(
    conn: sqlite3.Connection | None,
    days: Sequence[date],
    data_dir: Path,
    calendar: MarketCalendar,
    fee_rate: float,
) -> list[str]:
    """The range answers stored (#180), special windows left out: their classes and r by
    symbol and 30-minute slot, how often the bar rule with each c gives their class on the
    recorded days' backfilled bars, and how far the mid-point went up and down."""
    if conn is None or not days:
        return []
    cond, params = _on_days(days)
    try:
        rows = conn.execute(
            "SELECT symbol, bar_close, class, unscorable, special, path FROM kind_answers p"
            f" WHERE kind = ? AND answer_version = ? AND {cond} ORDER BY bar_close, symbol",
            [RANGE.name, ANSWER_VERSION, *params],
        ).fetchall()
    except sqlite3.OperationalError:
        return []  # a DB from before the kinds
    if not rows:
        return []
    scored = [(r[0], r[1], r[2], json.loads(r[5])) for r in rows if r[2] is not None and not r[4]]
    unscorable: dict[str, int] = {}
    for r in rows:
        if r[2] is None:
            unscorable[r[3]] = unscorable.get(r[3], 0) + 1
    special = sum(1 for r in rows if r[2] is not None and r[4])

    def share(k: int, n: int) -> str:
        return f"{100 * k / n:.0f}%" if n else "—"

    lines = []
    cells: dict[tuple[str, str], list[tuple[str, float]]] = defaultdict(list)
    for symbol, bar_close, cls, path in scored:
        cells[(symbol, slot_of(bar_close))].append((cls, path["r"]))
    for (symbol, cell), seen in sorted(cells.items()):
        classes = [c for c, _ in seen]
        split = " · ".join(f"{label} {share(classes.count(c), len(classes))}" for c, label in _RANGE_CLASSES)
        r = _distribution(pa.array([v for _, v in seen], pa.float64()), "g")
        lines.append(f"{symbol} {cell}: {split} ({len(classes)}창) · r {r}")
    if special or unscorable:
        lines.append(
            f"특수 상황 {special}창(뺌)"
            + "".join(f" · {why} {n}" for why, n in sorted(unscorable.items()))
            + (" (채점 불가)" if unscorable else "")
        )
    lines += _bar_rule(scored, data_dir, calendar, fee_rate)
    for symbol in sorted({s for s, *_ in scored}):
        ups = pa.array([p["up"] for s, _, _, p in scored if s == symbol], pa.float64())
        downs = pa.array([p["down"] for s, _, _, p in scored if s == symbol], pa.float64())
        up, down = _distribution(ups, "g"), _distribution(downs, "g")
        lines.append(f"{symbol} 위로 간 폭(틱): {up} | 아래로 간 폭(틱): {down}")
    return lines


def slot_of(bar_close: str) -> str:
    """The 30-minute slot of a bar close (``consensus.slot``): 09:01–09:30 is ``09:00``."""
    return slot(datetime.fromisoformat(bar_close))


def _bar_rule(
    scored: Sequence[tuple[str, str, str, dict[str, float]]],
    data_dir: Path,
    calendar: MarketCalendar,
    fee_rate: float,
) -> list[str]:
    """How often the one-minute bar rule with each c in ``BAR_CS`` gives the quote answer's
    class, on the days with both (the recorded days' backfilled bars); and the bars' range
    less the quotes' (c = 0)."""
    symbols = sorted({s for s, *_ in scored})
    days = sorted({date.fromisoformat(b[:10]) for _, b, _, _ in scored})
    bar_days = bar_days_on(data_dir, symbols, days)
    ruled = [replace(RANGE, name=f"c{c}", bar_answer=partial(bar_range, c=c)) for c in BAR_CS]
    by_bars = bar_answers(bar_days, calendar, fee_rate, ruled)
    same: dict[int, dict[str, list[bool]]] = {c: defaultdict(list) for c in BAR_CS}
    wider: list[float] = []
    for symbol, bar_close, cls, path in scored:
        at = datetime.fromisoformat(bar_close)
        for c in BAR_CS:
            answer = by_bars.get((f"c{c}", symbol, at))
            if answer is None or answer.label is None:
                continue
            same[c][symbol].append(answer.label == cls)
            if c == 0:
                wider.append(float(answer.path["r"]) - float(path["r"]))
    if not wider:
        return ["1분봉 규칙: 정답이 있는 날의 1분봉 없음"]

    def agreement(c: int, symbol: str | None = None) -> tuple[int, int]:
        hits = [h for s, values in same[c].items() if symbol in (None, s) for h in values]
        return sum(hits), len(hits)

    def shown(symbol: str | None) -> str:
        parts = []
        for c in BAR_CS:
            hits, n = agreement(c, symbol)
            parts.append(f"c = {c} {100 * hits / n:.1f}%" if n else f"c = {c} —")
        return " · ".join(parts) + f" ({agreement(BAR_CS[0], symbol)[1]}창)"

    best = max(BAR_CS, key=lambda c: (agreement(c)[0], -c))
    lines = [
        f"1분봉 규칙과 호가 정답의 구간 일치율: {shown(None)} · 더 많이 같은 c {best} (지금 BAR_C {BAR_C})"
    ]
    lines += [f"  {symbol}: {shown(symbol)}" for symbol in sorted({s for s, *_ in scored})]
    lines.append(f"1분봉 범위 − 호가 범위(틱, c = 0): {_distribution(pa.array(wider, pa.float64()), 'g')}")
    return lines
