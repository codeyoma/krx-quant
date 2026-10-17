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
8. The barrier answers (#183): their classes by symbol and 30-minute slot; the width w's
   spread and how often it is k; how often the one-minute bar rule (``barriers.bar_barrier``,
   the width from the backfilled bars of the day and the previous trading day) gives the
   quote answer's class on the recorded days, and how often its barriers are both reached in
   the same minute (left out of training); and the barrier P&L had every call been right
   (research only). Special windows are left out, as for the range.
9. The crossed quotes (#187): the integrated book's (``H0UNASP0``) updates whose best ask is
   below the best bid, in continuous trading, by symbol, day and 30-minute slot (their share
   of the updates and of the time), how long a spell lasts and how deep it is; the integrated
   trades' (``H0UNCNT0``) best quotes crossed; the bar closes whose last quote was crossed and
   the age of the valid quote the reference price used then (``reference.ReferencePrice``:
   the book's and the trades' quotes, under a minute old); which venue's bid was above which
   venue's ask, from the per-venue trades' (``H0STCNT0``, ``H0NXCNT0``) best quotes, and
   whether trades printed at those prices; the VIs (``special.SpecialSituations``) and the
   feeds' raw time codes during the spells. It only measures. #187 decided from it: the
   touch label and the barrier answer count crossed quotes (target version 3, barrier answer
   version 2); the reference price, the quote features and the range answer drop them.
10. Per predictor (#262): its skill against the class rates (``skill.py``: the expected
   accuracy, kappa and kappa by trading day, Gerrity) and its up and down calls acted on at
   the bar close and 1, 5 and 30 seconds after it, as a person reading a signal would: how
   often each reached its target, and the touch P&L (``entry_delay.py``). Its scored
   predictions as the leaderboard reads them.

Sections 6, 7 and 8 read the answers of the version the code makes now (``touch.TARGET_VERSION``,
the kinds' answer versions) and say which in their titles.

A distribution shows a few quantiles and the maximum. A quantile is the lower of the two
values around q·(n − 1) of the sorted values: no value is made up. Nothing is judged
against a threshold. A section without its data says "없음". Each feed is read and let go
in turn, as float32 seconds, to stay within the server's memory.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from functools import partial
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from krx_quant.backfill import PreviousDay
from krx_quant.barriers import BARRIER, SAME_MINUTE, TIMEOUT, barrier_profit
from krx_quant.consensus import slot
from krx_quant.entry_delay import entry_delays
from krx_quant.feeds import (
    BOOK,
    BOOK_SNAPSHOT,
    INDEX,
    INDEX_REST,
    KOSPI200_FUTURES,
    KRX_TRADE,
    NXT_TRADE,
    OPTION,
    TRADE,
    US_TRADE,
    USD_FUTURES,
    VI_STATUS,
)
from krx_quant.feeds.base import KST
from krx_quant.macro import INDICATORS, MACRO, decided_before, read_partition
from krx_quant.market_calendar import MarketCalendar, Session
from krx_quant.market_view import prediction_window
from krx_quant.predictions import SCORED, TOUCH_JOIN, read_only
from krx_quant.pricing import most_likely, tick_size
from krx_quant.ranges import ANSWER_VERSION, BAR_C, RANGE, bar_range
from krx_quant.reference import BAR, KRX_ONLY, MAX_QUOTE_AGE
from krx_quant.regime import NO_INDICATOR
from krx_quant.special import Kind, SpecialSituations
from krx_quant.stats import mean_interval
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
    with read_only(db) as conn:
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
                f"6. 도달 정답(#160, 정답 버전 {TARGET_VERSION}): 분포·폭·체결가 대비·표시 전·머문 시간",
                _touches(conn, chosen),
            ),
            (
                f"7. 변동폭 정답(#180, 정답 버전 {ANSWER_VERSION}): 종목 × 시간대 분포·1분봉 규칙 일치율"
                "·위아래로 간 폭",
                _ranges(conn, chosen, data_dir, calendar, fee_rate),
            ),
            (
                f"8. 장벽 정답(#183, 정답 버전 {BARRIER.answer_version}): 종목 × 시간대 분포·폭 w"
                "·1분봉 규칙 일치율·같은 분·미래를 안 손익",
                _barriers(conn, chosen, data_dir, calendar, fee_rate),
            ),
            (
                "9. 뒤집힌 호가(#187): 종목 × 날 × 30분 칸 비율·지속·깊이·봉 마감·거래소 귀속·체결·VI",
                _crossed(data_dir, chosen, calendar),
            ),
            (
                f"10. 예측자별 실력과 진입 지연(#262, 정답 버전 {TARGET_VERSION}): 봉 마감·+1초·+5초·+30초에"
                " 들어갔을 때의 목표 도달과 닿음 손익",
                [] if conn is None else entry_delays(conn, data_dir, chosen, fee_rate),
            ),
        ]
    lines = [f"진단 {chosen[0]} – {chosen[-1]} ({len(chosen)}거래일)" if chosen else "진단: 데이터 없음"]
    for title, body in sections:
        lines += ["", title, *(f"  {line}" for line in body or ["없음"])]
    return "\n".join(lines)


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


_BARRIER_CLASSES = (("up_first", "위 먼저"), ("timeout", "시간 만료"), ("down_first", "아래 먼저"))


def _barriers(
    conn: sqlite3.Connection | None,
    days: Sequence[date],
    data_dir: Path,
    calendar: MarketCalendar,
    fee_rate: float,
) -> list[str]:
    """The barrier answers stored (#183), special windows left out: their classes by symbol and
    30-minute slot, the width w's spread and share at k by symbol, the bar rule against them on
    the recorded days' backfilled bars (with the share of same-minute touches), and the barrier
    P&L of a call always right."""
    if conn is None or not days:
        return []
    cond, params = _on_days(days)
    try:
        rows = conn.execute(
            "SELECT symbol, bar_close, class, unscorable, special, path FROM kind_answers p"
            f" WHERE kind = ? AND answer_version = ? AND {cond} ORDER BY bar_close, symbol",
            [BARRIER.name, BARRIER.answer_version, *params],
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
    cells: dict[tuple[str, str], list[str]] = defaultdict(list)
    for symbol, bar_close, cls, _ in scored:
        cells[(symbol, slot_of(bar_close))].append(cls)
    for (symbol, cell), classes in sorted(cells.items()):
        split = " · ".join(
            f"{label} {share(classes.count(c), len(classes))}" for c, label in _BARRIER_CLASSES
        )
        lines.append(f"{symbol} {cell}: {split} ({len(classes)}창)")
    if special or unscorable:
        lines.append(
            f"특수 상황 {special}창(뺌)"
            + "".join(f" · {why} {n}" for why, n in sorted(unscorable.items()))
            + (" (채점 불가)" if unscorable else "")
        )
    for symbol in sorted({s for s, *_ in scored}):
        paths = [p for s, _, _, p in scored if s == symbol and p.get("w") is not None]
        if paths:
            widths = _distribution(pa.array([p["w"] for p in paths], pa.float64()), "g")
            at_k = sum(p["w"] == p["k"] for p in paths)
            lines.append(f"{symbol} 폭 w(틱): {widths} · w = k {share(at_k, len(paths))} ({len(paths)}창)")
    lines += _barrier_bar_rule(scored, data_dir, calendar, fee_rate)
    for symbol in sorted({s for s, *_ in scored}):
        trades = [
            profit
            for s, _, cls, p in scored
            if s == symbol and cls != TIMEOUT and (profit := barrier_profit(cls, cls, p)) is not None
        ]
        mean = mean_interval(trades)
        if mean is None:
            continue
        interval = (
            f" (95% {mean.low:.2f}–{mean.high:.2f})" if mean.low is not None and mean.high is not None else ""
        )
        lines.append(
            f"{symbol} 미래를 알았을 때의 장벽 손익(틱, 연구용): 거래 {len(trades)} · 합계 {sum(trades):.1f}"
            f" · 거래당 {mean.value:.2f}{interval}"
        )
    return lines


def _barrier_bar_rule(
    scored: Sequence[tuple[str, str, str, dict[str, float]]],
    data_dir: Path,
    calendar: MarketCalendar,
    fee_rate: float,
) -> list[str]:
    """How often the one-minute bar rule gives the quote answer's class, on the days with both
    (the recorded days' backfilled bars, the width from them and the previous trading day's);
    and how often it found both barriers reached in the same minute (no class: left out of
    training), of the windows it answered."""
    symbols = sorted({s for s, *_ in scored})
    days = sorted({date.fromisoformat(b[:10]) for _, b, _, _ in scored})
    bar_days = bar_days_on(data_dir, symbols, days)
    by_bars = bar_answers(bar_days, calendar, fee_rate, [BARRIER], previous=PreviousDay(data_dir, calendar))
    same: dict[str, list[bool]] = defaultdict(list)
    both: dict[str, list[bool]] = defaultdict(list)
    for symbol, bar_close, cls, _ in scored:
        answer = by_bars.get((BARRIER.name, symbol, datetime.fromisoformat(bar_close)))
        if answer is None:
            continue
        both[symbol].append(answer.unscorable == SAME_MINUTE)
        if answer.label is not None:
            same[symbol].append(answer.label == cls)
    if not both:
        return ["1분봉 규칙: 정답이 있는 날의 1분봉 없음"]

    def shown(symbol: str | None) -> str:
        hits = [h for s, values in same.items() if symbol in (None, s) for h in values]
        twice = [h for s, values in both.items() if symbol in (None, s) for h in values]
        agreement = f"{100 * sum(hits) / len(hits):.1f}% ({len(hits)}창)" if hits else "—"
        return f"{agreement} · 같은 분 양쪽 닿음 {100 * sum(twice) / len(twice):.1f}% ({len(twice)}창 중)"

    lines = [f"1분봉 규칙과 호가 정답의 클래스 일치율: {shown(None)}"]
    lines += [f"  {symbol}: {shown(symbol)}" for symbol in sorted(both)]
    return lines


# 9. The crossed quotes (#187). Times are kept as integer microseconds since the epoch.
_MICROS = 1_000_000
_QUOTE_AGE = int(MAX_QUOTE_AGE.total_seconds()) * _MICROS  # the reference price's limit
_KST_OFFSET = 9 * 3600  # seconds: Korea keeps no daylight saving
_SLOT = 30 * 60  # seconds
_LISTED = 10  # the spells over a minute listed, at most, by symbol
_VENUE_FEEDS = (("KRX", KRX_TRADE.tr_id), ("NXT", NXT_TRADE.tr_id))
# Which venue's bid was above which venue's ask: (the bid's venue, the ask's venue).
_ACROSS = {("KRX", "NXT"): "KRX 매수 > NXT 매도", ("NXT", "KRX"): "NXT 매수 > KRX 매도"}
_OWN = {"KRX": "KRX 자체 뒤집힘", "NXT": "NXT 자체 뒤집힘"}  # a venue's own best quotes crossed
_MIXED, _UNMATCHED, _UNSEEN = "섞임", "통합과 맞지 않음", "거래소별 체결 없음"
_ATTRIBUTIONS = (*_ACROSS.values(), *_OWN.values(), _MIXED, _UNMATCHED, _UNSEEN)
_PER_VENUE = (
    "거래소별 1호가는 거래소별 체결(H0STCNT0 KRX, H0NXCNT0 NXT)에 실린 것뿐이라"
    " 그 거래소에 체결이 난 순간에만 있다. 귀속은 뒤집힌 동안 난 거래소별 체결마다"
    " 그 거래소의 1호가를 그때의 통합 1호가와 견준 것이다."
)


@dataclass
class _CrossedSymbol:
    """A symbol's crossed quotes over the days read: the day lines, and what adds up."""

    days: list[str] = field(default_factory=list)
    durations: list[np.ndarray] = field(default_factory=list)  # seconds, a spell each
    depths: list[np.ndarray] = field(default_factory=list)  # ticks, a crossed update each
    long: list[str] = field(default_factory=list)  # the spells over a minute
    closes: int = 0
    crossed_closes: list[int] = field(default_factory=list)  # by day
    ages: list[float] = field(default_factory=list)  # seconds, of the valid quote used then
    no_mid: int = 0
    spells: Counter[str] = field(default_factory=Counter)  # by attribution
    seconds: dict[str, float] = field(default_factory=lambda: defaultdict(float))  # by attribution
    # By cross-venue attribution: its spells, the high bid traded, the low ask traded, both.
    traded: dict[str, list[int]] = field(default_factory=lambda: {a: [0, 0, 0, 0] for a in _ACROSS.values()})
    vi_spells: int = 0
    vi_seconds: float = 0.0
    vis: int = 0
    # The raw time codes, during a spell (True) or not: the book's, and the per-venue trades' by venue.
    book_codes: dict[bool, Counter[str]] = field(default_factory=lambda: {True: Counter(), False: Counter()})
    venue_codes: dict[bool, dict[str, Counter[str]]] = field(
        default_factory=lambda: {True: defaultdict(Counter), False: defaultdict(Counter)}
    )

    def lines(self, symbol: str) -> list[str]:
        out = list(self.days)
        durations = np.concatenate(self.durations)
        n = len(durations)
        if n:
            depths = np.concatenate(self.depths)
            out.append(
                f"{symbol} 한 번 뒤집혀 있는 시간(초, {n}번): {_distribution(pa.array(durations), '.1f')}"
            )
            out.append(
                f"{symbol} 깊이(틱, 뒤집힌 호가 {len(depths)}건): {_distribution(pa.array(depths), 'g')}"
            )
            more = len(self.long) - _LISTED
            listed = " · ".join(self.long[:_LISTED]) + (f" 외 {more}번" if more > 0 else "")
            out.append(f"{symbol} 60초 넘게 뒤집힘: {listed or '없음'}")
        else:
            out.append(f"{symbol} 뒤집힌 통합 호가 없음")
        crossed = sum(self.crossed_closes)
        closing = (
            f"{symbol} 봉 마감 직전 호가가 뒤집힘: {self.closes}개 중 {crossed}개"
            f" (날마다 {min(self.crossed_closes)}–{max(self.crossed_closes)})"
        )
        if self.ages:
            closing += (
                f" · 그때 쓴 유효 호가의 나이(초): {_distribution(pa.array(self.ages, pa.float64()), '.1f')}"
            )
        if crossed:
            closing += f" · 유효 호가 없음(중간가 모름) {self.no_mid}개"
        out.append(closing)
        if not n:
            return out
        total = sum(self.seconds.values())
        shares = " · ".join(
            f"{a} {_pct(self.spells[a], n)} (시간 {_pct(self.seconds[a], total)})" for a in _ATTRIBUTIONS
        )
        out.append(f"{symbol} 거래소 귀속(뒤집힘 {n}번): {shares}")
        traded = " | ".join(
            f"{label} {k}번 중 높은 매수호가({high})에 체결 {_pct(bid, k)} · 낮은 매도호가({low})에 체결"
            f" {_pct(ask, k)} · 둘 다 {_pct(both, k)}"
            for (high, low), label in _ACROSS.items()
            for k, bid, ask, both in [self.traded[label]]
        )
        out.append(f"{symbol} 뒤집힌 가격에 체결: {traded}")
        out.append(
            f"{symbol} VI와 겹친 뒤집힘: {self.vi_spells}번 (뒤집힌 시간의 {_pct(self.vi_seconds, total)})"
            f" · 그 날들의 VI {self.vis}번"
        )
        out.append(
            f"{symbol} 통합 호가의 시간 구분 코드(원문): 뒤집힌 호가 {_codes(self.book_codes[True])}"
            f" | 그 밖 {_codes(self.book_codes[False])}"
        )

        def by_venue(inside: bool) -> str:
            codes = self.venue_codes[inside]
            return ", ".join(f"{v} {_codes(codes[v])}" for v, _ in _VENUE_FEEDS if codes[v]) or "없음"

        out.append(
            f"{symbol} 거래소별 체결의 장운영/시간 구분 코드(원문): 뒤집힌 동안 {by_venue(True)}"
            f" | 그 밖 {by_venue(False)}"
        )
        return out


def _crossed(data_dir: Path, days: Sequence[date], calendar: MarketCalendar) -> list[str]:
    """The crossed quotes (#187), read a symbol-day at a time and let go: only the spells'
    lengths and depths, the bar closes' ages and the counts are kept."""
    found: dict[str, _CrossedSymbol] = {}
    for day, partition in _partitions(data_dir, days, BOOK.tr_id):
        session = calendar.session(day)
        if session is not None:
            _crossed_day(data_dir, day, partition.name.removeprefix("symbol="), session, calendar, found)
    lines = [line for symbol, seen in sorted(found.items()) for line in seen.lines(symbol)]
    return [*lines, _PER_VENUE] if lines else []


def _between(
    data_dir: Path, day: date, feed: str, symbol: str, columns: Sequence[str], start: int, end: int
) -> tuple[np.ndarray, pa.Table] | None:
    """A symbol-day's rows of ``feed`` received in [start, end) (microseconds), in the order
    received: their receipt times and ``columns``; None without any."""
    partition = data_dir / "events" / f"date={day}" / f"feed={feed}" / f"symbol={symbol}"
    table = read_partition(partition, ("received_at", *columns))
    if table is None:
        return None
    at = pc.cast(pc.cast(table["received_at"], pa.timestamp("us", tz="UTC")), pa.int64()).to_numpy()
    keep = np.flatnonzero((at >= start) & (at < end))
    if len(keep) == 0:
        return None
    order = keep[np.argsort(at[keep], kind="stable")]
    return at[order], table.take(pa.array(order)).combine_chunks()


def _prices(table: pa.Table, column: str) -> np.ndarray:
    """A price column as int64, a missing price 0 (an empty side)."""
    prices: np.ndarray = pc.fill_null(table[column], 0).to_numpy().astype(np.int64)
    return prices


def _pct(k: float, n: float) -> str:
    return f"{100 * k / n:.1f}%" if n else "—"


def _count(counter: Counter[str], values: pa.ChunkedArray) -> None:
    for item in pc.value_counts(values).to_pylist():
        counter[item["values"] if item["values"] is not None else "-"] += item["counts"]


def _codes(counter: Counter[str]) -> str:
    total = sum(counter.values())
    if not total:
        return "없음"
    return " · ".join(f"{code} {_pct(c, total)}" for code, c in counter.most_common()) + f" ({total}건)"


def _crossed_day(
    data_dir: Path,
    day: date,
    symbol: str,
    session: Session,
    calendar: MarketCalendar,
    found: dict[str, _CrossedSymbol],
) -> None:
    """One symbol-day's crossed quotes in continuous trading (the open to the closing auction),
    added to ``found``.

    An integrated book update holds until the next one, a minute at most (the reference price's
    limit: past it the book is taken as gone). A spell is a run of crossed updates, ended by an
    update that is not crossed or by a minute's silence; a one-sided update is not crossed.
    """
    start, end = _micros(session.open), _micros(session.closing_auction)
    book = _between(data_dir, day, BOOK.tr_id, symbol, ("bid_px_1", "ask_px_1", "hour_cls"), start, end)
    if book is None:
        return
    seen = found.setdefault(symbol, _CrossedSymbol())
    at, rows = book
    bid, ask = _prices(rows, "bid_px_1"), _prices(rows, "ask_px_1")
    gap = np.append(at[1:], end) - at
    hold = np.minimum(gap, _QUOTE_AGE)
    crossed = (bid > 0) & (ask > 0) & (ask < bid)
    goes_on = np.concatenate(([False], crossed[:-1] & (gap[:-1] < _QUOTE_AGE)))
    first = crossed & ~goes_on
    starts = at[first]
    spell_of = np.cumsum(first) - 1
    lengths = np.bincount(spell_of[crossed], weights=hold[crossed], minlength=len(starts)).astype(np.int64)
    ends = starts + lengths

    # The day's shares, and by the 30-minute slot each update was received in.
    slots = (at // _MICROS + _KST_OFFSET) % 86_400 // _SLOT
    updates, crossed_updates = np.bincount(slots, minlength=48), np.bincount(slots[crossed], minlength=48)
    held = np.bincount(slots, weights=hold, minlength=48)
    crossed_held = np.bincount(slots[crossed], weights=hold[crossed], minlength=48)
    cells = " · ".join(
        f"{s * _SLOT // 3600:02d}:{s * _SLOT % 3600 // 60:02d}"
        f" {_pct(crossed_updates[s], updates[s])}/{_pct(crossed_held[s], held[s])}"
        for s in np.flatnonzero(updates)
    )
    trades = _between(data_dir, day, TRADE.tr_id, symbol, ("bid1", "ask1"), start, end)
    quoted = "통합 체결 없음"
    if trades is not None:
        t_bid, t_ask = _prices(trades[1], "bid1"), _prices(trades[1], "ask1")
        t_crossed = int(((t_bid > 0) & (t_ask > 0) & (t_ask < t_bid)).sum())
        quoted = f"통합 체결 1호가 {len(t_bid)}건 중 뒤집힘 {_pct(t_crossed, len(t_bid))}"
    seen.days += [
        f"{symbol} {day}: 통합 호가 {len(at)}건 중 뒤집힘 {_pct(int(crossed.sum()), len(at))}"
        f" · 시간 {_pct(float(hold[crossed].sum()), float(hold.sum()))} · 뒤집힘 {len(starts)}번 · {quoted}",
        f"  30분 칸(호가/시간): {cells}",
    ]
    seen.durations.append(lengths / _MICROS)
    mids = (bid[crossed] + ask[crossed]) / 2
    ticks = np.array([tick_size(float(m)) for m in mids], dtype=np.float64)
    seen.depths.append((bid[crossed] - ask[crossed]) / ticks)
    for s in np.flatnonzero(lengths > _QUOTE_AGE):
        began = datetime.fromtimestamp(starts[s] / _MICROS, KST).strftime("%H:%M:%S")
        seen.long.append(f"{day} {began}부터 {lengths[s] / _MICROS:.1f}초")
    _count(seen.book_codes[True], rows["hour_cls"].filter(pa.array(crossed)))
    _count(seen.book_codes[False], rows["hour_cls"].filter(pa.array(~crossed)))

    _crossed_closes(data_dir, day, symbol, session, (at, bid, ask), trades, seen)
    _attribute(data_dir, day, symbol, (start, end), (at, bid, ask), (starts, ends, lengths), seen)
    # The VIs, as the engine reads them (``special.SpecialSituations.vi``): did a spell meet one?
    vi_rows = read_partition(
        data_dir / "events" / f"date={day}" / f"feed={VI_STATUS.tr_id}" / f"symbol={symbol}",
        ("symbol", "event_time", "received_at", "end_time", "active"),
    )
    situations = SpecialSituations(calendar)
    for row in vi_rows.to_pylist() if vi_rows is not None else []:
        situations.vi(row)
    windows: list[tuple[int, int]] = []
    for window in situations.known(datetime.combine(day + timedelta(days=1), time(), KST)):
        if window.kind is Kind.VI:
            seen.vis += 1
            windows.append((_micros(window.start), _micros(window.end)))
    met = np.zeros(len(starts), dtype=bool)
    overlap = np.zeros(len(starts), dtype=np.int64)
    for low, high in _union(windows):  # disjoint, so a spell's overlaps add up without double counting
        within = np.clip(np.minimum(ends, high) - np.maximum(starts, low), 0, None)
        met |= (starts < high) & (ends > low)
        overlap += within
    seen.vi_spells += int(met.sum())
    seen.vi_seconds += float(overlap.sum()) / _MICROS


def _union(windows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """``windows`` merged where they overlap or touch, in order."""
    merged: list[tuple[int, int]] = []
    for low, high in sorted(windows):
        if merged and low <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], high))
        else:
            merged.append((low, high))
    return merged


def _crossed_closes(
    data_dir: Path,
    day: date,
    symbol: str,
    session: Session,
    book: tuple[np.ndarray, np.ndarray, np.ndarray],
    trades: tuple[np.ndarray, pa.Table] | None,
    seen: _CrossedSymbol,
) -> None:
    """The bar closes whose last quote was crossed, and the valid quote the reference price used
    then: of the integrated quotes it takes (the book's, the trades', a REST book snapshot's)
    received from the NXT open, the last valid one received strictly before the close, if under
    a minute old (else the mid-point is unknown)."""
    start, end = _micros(session.open), _micros(session.closing_auction)
    parts = [book]
    if trades is not None:
        parts.append((trades[0], _prices(trades[1], "bid1"), _prices(trades[1], "ask1")))
    snapshots = _between(data_dir, day, BOOK_SNAPSHOT.tr_id, symbol, ("bid_px_1", "ask_px_1"), start, end)
    if snapshots is not None:
        parts.append((snapshots[0], _prices(snapshots[1], "bid_px_1"), _prices(snapshots[1], "ask_px_1")))
    since = _micros(session.open + KRX_ONLY)
    order = np.argsort(np.concatenate([p[0] for p in parts]), kind="stable")
    at, bid, ask = (np.concatenate([p[i] for p in parts])[order] for i in range(3))
    # The last quote is the last one received, one-sided too (not crossed); a trade carrying no
    # quotes is none. The reference price falls back on the last valid (two-sided) one.
    quoted = ((bid > 0) | (ask > 0)) & (at >= since)
    two_sided = (bid > 0) & (ask > 0)
    times, crossed = at[quoted], (two_sided & (ask < bid))[quoted]
    valid = at[quoted & two_sided & (ask >= bid)]
    first, last = prediction_window(session)
    closes = np.array(
        [_micros(first + i * BAR) for i in range(int((last - first) / BAR) + 1)], dtype=np.int64
    )
    before = np.searchsorted(times, closes) - 1  # the last quote received strictly before
    hit = before >= 0
    hit[hit] = crossed[before[hit]]
    at_close = closes[hit]
    used = np.searchsorted(valid, at_close) - 1
    fresh = used >= 0
    fresh[fresh] = valid[used[fresh]] >= np.maximum(since, at_close[fresh] - _QUOTE_AGE)
    seen.closes += len(closes)
    seen.crossed_closes.append(int(hit.sum()))
    seen.ages += ((at_close[fresh] - valid[used[fresh]]) / _MICROS).tolist()
    seen.no_mid += int((~fresh).sum())


def _attribute(
    data_dir: Path,
    day: date,
    symbol: str,
    hours: tuple[int, int],
    book: tuple[np.ndarray, np.ndarray, np.ndarray],
    spells: tuple[np.ndarray, np.ndarray, np.ndarray],
    seen: _CrossedSymbol,
) -> None:
    """Which venue's bid was above which venue's ask in each spell, and whether those prices
    traded, from the per-venue trades received during it.

    Each such trade carries its venue's best quotes; the integrated ones in force (crossed: bid
    B above ask A) are the higher bid and the lower ask of the two venues. The venue's bid at B
    with its ask above A (or none) leaves A to the other venue: this venue's bid above the
    other's ask; its ask at A with its bid below B, the other way round; its own bid above its
    own ask, its own book crossed. Neither matching, the trade says nothing (the feeds arrive
    apart). A spell takes its trades' one attribution, ``섞임`` if they differ. A trade at B on
    the bid's venue, or at A on the ask's, printed at that price.
    """
    at, bid, ask = book
    starts, ends, lengths = spells
    labels: dict[int, set[str]] = defaultdict(set)
    prints: dict[int, set[tuple[str, str]]] = defaultdict(set)
    observed: set[int] = set()
    columns = ("price", "bid1", "ask1", "session_code", "hour_cls")
    for venue, feed in _VENUE_FEEDS:
        found = _between(data_dir, day, feed, symbol, columns, *hours)
        if found is None:
            continue
        v_at, rows = found
        spell = np.searchsorted(starts, v_at, side="right") - 1
        inside = spell >= 0
        inside[inside] = v_at[inside] < ends[spell[inside]]
        codes = pc.binary_join_element_wise(
            pc.fill_null(rows["session_code"], "-"), pc.fill_null(rows["hour_cls"], "-"), "/"
        )
        _count(seen.venue_codes[True][venue], codes.filter(pa.array(inside)))
        _count(seen.venue_codes[False][venue], codes.filter(pa.array(~inside)))
        in_force = np.searchsorted(at, v_at, side="right") - 1  # the book update last received
        price, v_bid, v_ask = _prices(rows, "price"), _prices(rows, "bid1"), _prices(rows, "ask1")
        other = "NXT" if venue == "KRX" else "KRX"
        for i in np.flatnonzero(inside):
            s, j = int(spell[i]), int(in_force[i])
            high, low = int(bid[j]), int(ask[j])
            mine_bid, mine_ask = int(v_bid[i]), int(v_ask[i])
            observed.add(s)
            if mine_bid > 0 and mine_ask > 0 and mine_ask < mine_bid:
                labels[s].add(_OWN[venue])
            elif mine_bid == high and (mine_ask <= 0 or mine_ask > low):
                labels[s].add(_ACROSS[(venue, other)])
            elif mine_ask == low and mine_bid < high:
                labels[s].add(_ACROSS[(other, venue)])
            if price[i] == high:
                prints[s].add((venue, "bid"))
            if price[i] == low:
                prints[s].add((venue, "ask"))
    for s, length in enumerate(lengths / _MICROS):
        found_labels = labels.get(s, set())
        if found_labels:
            label = next(iter(found_labels)) if len(found_labels) == 1 else _MIXED
        else:
            label = _UNMATCHED if s in observed else _UNSEEN
        seen.spells[label] += 1
        seen.seconds[label] += float(length)
        for (high_venue, low_venue), across in _ACROSS.items():
            if label == across:
                printed = prints.get(s, set())
                bid_traded, ask_traded = (high_venue, "bid") in printed, (low_venue, "ask") in printed
                counts = seen.traded[across]
                counts[0] += 1
                counts[1] += bid_traded
                counts[2] += ask_traded
                counts[3] += bid_traded and ask_traded
