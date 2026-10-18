"""The read-only structure check (``krx-quant structure-check``): the research claims the
literature could not settle (``research/reports/``), measured on the data the engine keeps.

The 2026-10-17 research found that the single-stock ±2x ETFs on 005930 and 000660, listed
on 2026-05-27, may have changed how the two stocks close and move the next day, but the
only evidence is one non-peer-reviewed preprint with about 41 trading days after the
listing. Several other effects have no Korean study. This measures them, per symbol and
per period (``PERIODS``: before the listing, after it, after the FSC's 2026-07-28 request
to rebalance earlier and the 2026-07-31 deposit rule, after the KRX after-market opened
on 2026-09-14):

1. The day's structure, from the one-minute bars of the regular session: the open (the
   first bar from the session's open: the opening auction's print) and the close (the last
   bar of the closing auction, up to the session's close: its print). How large the daily
   moves are, their first-order autocorrelation, how much of a day's move the next day
   takes back (the slope of the next day's on it), and whether the gap's direction held
   through the day and the next.
2. The closing auction's move: the close against the last price before the auction. How
   large, whether it went the way of the 20 minutes before the auction and of the day so
   far (leveraged ETFs buy on up days at the close), and whether the next day took it back.
3. The four-minute touch labels by the one-minute bar rule (``touch.bar_touch``, at every
   bar close the engine predicts), by period and by tick size: their classes, k, and how
   often the previous window's label (a baseline) and always-flat were right.
4. The turn of the month (the last trading day of a month and the first three of the
   next) against the other days, over the whole day-candle history, and split by the
   foreign investors' net buying the day before (KIS's daily figure only: Toss's values
   never leave the server). KIS answers 0 for the days before NXT opened (2025-03-04): no
   figure (``supply.no_figure``, #270), so the split covers the days since, which the line
   heading each symbol counts and dates. The first run (2026-10-18) counted the zeros among
   the days (2,452 a symbol, of which 395 were split).

It prints ratios, slopes, counts and medians: no price, volume or flow value, so its
output can go on the public issue. Nothing is judged against a threshold. A section without
its data says "없음".
"""

from __future__ import annotations

import bisect
import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from krx_quant.backfill import MINUTE_BARS_TR, read_bars
from krx_quant.candles import read_candles
from krx_quant.market_calendar import MarketCalendar, Session
from krx_quant.market_view import HORIZON, prediction_window
from krx_quant.pricing import Direction, flat_ticks, tick_size
from krx_quant.reference import BAR, Bar
from krx_quant.supply import KIS, read_supply
from krx_quant.touch import bar_touch

# The periods compared: (name, first day, first day of the next).
LISTING = date(2026, 5, 27)  # the 16 single-stock ±2x ETFs on 005930 and 000660 listed
RULES = date(2026, 7, 31)  # the FSC asked to rebalance earlier (07-28); the deposit rule began
AFTER_MARKET = date(2026, 9, 14)  # the KRX after-market (16:00–20:00) opened
PERIODS: tuple[tuple[str, date | None, date | None], ...] = (
    ("상장 전(~05-26)", None, LISTING),
    ("상장 뒤(05-27~07-30)", LISTING, RULES),
    ("규제 뒤(07-31~09-13)", RULES, AFTER_MARKET),
    ("애프터마켓 뒤(09-14~)", AFTER_MARKET, None),
)
# The minutes before the closing auction whose move section 2 compares the auction's with.
BEFORE_AUCTION = timedelta(minutes=20)
TOM_FIRST_DAYS = 3  # the turn of the month: the month's last trading day and the next's first three


def period_of(day: date) -> str:
    return next(
        n for n, first, end in PERIODS if (first is None or day >= first) and (end is None or day < end)
    )


@dataclass(frozen=True)
class DayPrices:
    """A day's regular-session prices from its one-minute bars (won)."""

    day: date
    open: float  # the first bar from the session's open: the opening auction's print
    drift_from: float  # the last price 20 minutes before the closing auction
    pre_close: float  # the last price before the closing auction
    close: float  # the closing auction's print


def day_prices(bars: Sequence[Bar], session: Session) -> DayPrices | None:
    """The day's prices; None without a bar in continuous trading or in the closing auction
    (a day the auction printed nothing is left out, not given the last trade)."""
    ordered = sorted(bars, key=lambda b: b.start)
    trading = [b for b in ordered if session.open <= b.start < session.closing_auction]
    auction = [b for b in ordered if session.closing_auction <= b.start <= session.close]
    if not trading or not auction:
        return None
    before_drift = [b for b in trading if b.start < session.closing_auction - BEFORE_AUCTION]
    return DayPrices(
        day=session.day,
        open=trading[0].open,
        drift_from=(before_drift[-1] if before_drift else trading[0]).close,
        pre_close=trading[-1].close,
        close=auction[-1].close,
    )


@dataclass(frozen=True)
class Label:
    """A bar close's touch label by the bar rule, and the one four minutes before it."""

    period: str
    tick: int
    k: int
    label: Direction
    previous: Direction | None  # None at the day's first windows


def day_labels(bars: Sequence[Bar], session: Session, fee_rate: float) -> list[Label]:
    """The bar rule's label at every bar close the engine predicts that has its closing bar
    and a trade in the four minutes after it (``training`` labels the same way)."""
    ordered = sorted(bars, key=lambda b: b.start)
    starts = [b.start for b in ordered]
    by_start = {b.start: b for b in ordered}
    first, last = prediction_window(session)
    found: dict[datetime, Direction] = {}
    out: list[Label] = []
    t = first
    period = period_of(session.day)
    while t <= last:
        closing = by_start.get(t - BAR)
        if closing is not None:
            after = ordered[bisect.bisect_left(starts, t) : bisect.bisect_left(starts, t + HORIZON)]
            label = bar_touch(closing, after, fee_rate)
            if label is not None:
                found[t] = label
                price = closing.close
                out.append(
                    Label(
                        period, tick_size(price), flat_ticks(price, fee_rate), label, found.get(t - HORIZON)
                    )
                )
        t += BAR
    return out


def structure_check(
    data_dir: Path,
    state_dir: Path,
    calendar: MarketCalendar,
    symbols: Sequence[str],
    *,
    fee_rate: float,
    days: int | None = None,
) -> str:
    """The check over the latest ``days`` trading days with one-minute bars (every one if
    None); the turn of the month over the whole day-candle history."""
    chosen = _bar_days(data_dir, symbols, calendar)
    if days is not None:
        chosen = chosen[-days:]
    prices: dict[str, list[DayPrices]] = {}
    labels: dict[str, list[Label]] = {}
    for symbol in symbols:
        prices[symbol], labels[symbol] = [], []
        for day in chosen:
            session = calendar.session(day)
            bars = read_bars(data_dir, day, MINUTE_BARS_TR, symbol)
            if session is None or not bars:
                continue
            found = day_prices(bars, session)
            if found is not None:
                prices[symbol].append(found)
            labels[symbol] += day_labels(bars, session, fee_rate)
    sections = [
        ("1. 일 단위 구조(정규장 시가·종가, 1분봉)", _days_section(prices, calendar)),
        ("2. 마감 단일가(종가 ÷ 15:20 직전가)", _auction_section(prices, calendar)),
        ("3. 4분 도달 정답(1분봉 규칙): 기간·틱 크기별", _labels_section(labels)),
        (
            "4. 월말·월초 × 전날 외국인 순매수(일봉 전체, KIS 수급)",
            _tom_section(data_dir, state_dir, symbols),
        ),
    ]
    head = (
        f"구조 실측 {chosen[0]} – {chosen[-1]} (1분봉 {len(chosen)}거래일, {', '.join(symbols)})"
        if chosen
        else "구조 실측: 1분봉 없음"
    )
    periods = " / ".join(name for name, _, _ in PERIODS)
    lines = [head, f"  기간: {periods}. 가격·거래량·수급 값은 싣지 않는다."]
    for title, body in sections:
        lines += ["", title, *(f"  {line}" for line in body or ["없음"])]
    return "\n".join(lines)


def _bar_days(data_dir: Path, symbols: Sequence[str], calendar: MarketCalendar) -> list[date]:
    """The trading days with a symbol's one-minute bars, oldest first."""
    found: set[date] = set()
    for partition in (data_dir / "bars").glob("date=*"):
        day = date.fromisoformat(partition.name.removeprefix("date="))
        if calendar.is_trading_day(day) and any(
            (partition / f"feed={MINUTE_BARS_TR}" / f"symbol={s}" / "bars.parquet").exists() for s in symbols
        ):
            found.add(day)
    return sorted(found)


def _follows(calendar: MarketCalendar, before: DayPrices, after: DayPrices) -> bool:
    """``after`` is the trading day right after ``before``'s (no day missing between)."""
    return calendar.previous_trading_day(after.day) == before.day


def _change(new: float, old: float) -> float:
    return new / old - 1


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def _days_section(prices: Mapping[str, Sequence[DayPrices]], calendar: MarketCalendar) -> list[str]:
    lines = [
        "일수 · |종가 대비 종가| 중앙·평균 · ρ1 · β(다음날 ← 오늘: −1이면 다 되돌림) · β(장중 ← 갭)"
        " · 갭 방향이 장중에 이어진 비율 · 다음날 종가까지 이어진 비율"
    ]
    for symbol, days in prices.items():
        rows: dict[str, list[tuple[float, float, float, float | None]]] = defaultdict(list)
        for before, day, after in zip(days, days[1:], [*days[2:], None], strict=False):
            if not _follows(calendar, before, day):
                continue
            today = _change(day.close, before.close)
            following = (
                _change(after.close, day.close)
                if after is not None and _follows(calendar, day, after)
                else None
            )
            rows[period_of(day.day)].append(
                (today, _change(day.open, before.close), _change(day.close, day.open), following)
            )
        for name, _, _ in PERIODS:
            found = rows.get(name)
            if not found:
                continue
            moves = [r[0] for r in found]
            pairs = [(r[0], r[3]) for r in found if r[3] is not None]
            gap_pairs = [(r[1], r[3]) for r in found if r[3] is not None]
            lines.append(
                f"{symbol} {name}: {len(found)}일 · {_pct(statistics.median(abs(m) for m in moves))}"
                f"·{_pct(statistics.fmean(abs(m) for m in moves))} · ρ1 {_corr(pairs)}"
                f" · β {_slope(pairs)} · β(장중←갭) {_slope([(r[1], r[2]) for r in found])}"
                f" · 갭→장중 {_same([(r[1], r[2]) for r in found])} · 갭→다음날 {_same(gap_pairs)}"
            )
    return lines[1:] and lines


def _auction_section(prices: Mapping[str, Sequence[DayPrices]], calendar: MarketCalendar) -> list[str]:
    lines = [
        "일수 · |단일가 움직임| 중앙(bp, 틱) · 15:00–15:20과 같은 방향 · 그날 15:20까지(전일 종가 대비)와"
        " 같은 방향 · 다음날 시가 갭이 반대 · β(다음날 갭 ← 단일가) · 다음날 종가까지 반대"
    ]
    for symbol, days in prices.items():
        rows: dict[str, list[tuple[float, float, float, float, float | None, float | None]]] = defaultdict(
            list
        )
        for before, day, after in zip(days, days[1:], [*days[2:], None], strict=False):
            if not _follows(calendar, before, day):
                continue
            nxt = after if after is not None and _follows(calendar, day, after) else None
            rows[period_of(day.day)].append(
                (
                    _change(day.close, day.pre_close),
                    (day.close - day.pre_close) / tick_size(day.pre_close),
                    _change(day.pre_close, day.drift_from),
                    _change(day.pre_close, before.close),
                    _change(nxt.open, day.close) if nxt is not None else None,
                    _change(nxt.close, day.close) if nxt is not None else None,
                )
            )
        for name, _, _ in PERIODS:
            found = rows.get(name)
            if not found:
                continue
            gaps = [(r[0], r[4]) for r in found if r[4] is not None]
            nexts = [(r[0], r[5]) for r in found if r[5] is not None]
            lines.append(
                f"{symbol} {name}: {len(found)}일 · {statistics.median(abs(r[0]) * 1e4 for r in found):.1f}bp"
                f" {statistics.median(abs(r[1]) for r in found):.1f}틱"
                f" · 15:00–15:20과 {_same([(r[2], r[0]) for r in found])}"
                f" · 그날 흐름과 {_same([(r[3], r[0]) for r in found])}"
                f" · 다음날 갭 반대 {_opposite(gaps)} · β {_slope(gaps)}"
                f" · 다음날 종가 반대 {_opposite(nexts)}"
            )
    return lines[1:] and lines


def _labels_section(labels: Mapping[str, Sequence[Label]]) -> list[str]:
    lines = ["창 수 · 상승/보합/하락 · k 중앙값 · 직전 창 정답이 맞은 비율 · 늘 보합이 맞은 비율(보합 비율)"]
    for symbol, found in labels.items():
        for name, _, _ in PERIODS:
            rows = [x for x in found if x.period == name]
            if rows:
                lines.append(f"{symbol} {name}: {_label_line(rows)}")
        for tick in sorted({x.tick for x in found}):
            rows = [x for x in found if x.tick == tick]
            lines.append(f"{symbol} 틱 {tick:,}원: {_label_line(rows)}")
    return lines[1:] and lines


def _label_line(rows: Sequence[Label]) -> str:
    counts = Counter(x.label for x in rows)
    n = len(rows)
    shares = "/".join(f"{100 * counts[d] / n:.1f}" for d in (Direction.UP, Direction.FLAT, Direction.DOWN))
    paired = [x for x in rows if x.previous is not None]
    previous = (
        f"{100 * sum(x.previous == x.label for x in paired) / len(paired):.1f}% (n={len(paired):,})"
        if paired
        else "없음"
    )
    k = statistics.median_low(x.k for x in rows)
    flat = 100 * counts[Direction.FLAT] / n
    return f"{n:,}창 · {shares}% · k {k} · 직전 창 {previous} · 늘 보합 {flat:.1f}%"


def _tom_section(data_dir: Path, state_dir: Path, symbols: Sequence[str]) -> list[str]:
    lines = [
        "월말·월초 = 달의 마지막 거래일과 다음 달 첫 3거래일. 일수 · 평균 일 수익률(종가 대비 종가)"
        " · 상승일 비율 · 전날 외국인 순매수 +/− 로 나눈 것"
    ]
    for symbol in symbols:
        candles = [
            (r["date"], float(r["close"])) for r in read_candles(data_dir, "day", symbol) if r["close"]
        ]
        if len(candles) < 2:
            continue
        # The days with a figure: KIS's 0 net buys (every day before NXT opened) are none (#270).
        foreign = {
            r["date"]: r["foreign_net"]
            for r in read_supply(state_dir, symbol, KIS)
            if r.get("foreign_net") is not None
        }
        turn = _turn_of_month([d for d, _ in candles])
        groups: dict[tuple[bool, int | None], list[float]] = defaultdict(list)
        for (before_day, before), (day, close) in zip(candles, candles[1:], strict=False):
            change = _change(close, before)
            net = foreign.get(before_day)
            groups[(day in turn, None)].append(change)
            if net is not None:
                groups[(day in turn, _sign(net))].append(change)
        figures = f"{len(foreign):,}일({min(foreign)} – {max(foreign)})" if foreign else "없음"
        lines.append(f"{symbol} 일봉 {candles[0][0]} – {candles[-1][0]}, 외국인 순매수 {figures}")
        for is_turn in (True, False):
            parts = [f"{'월말·월초' if is_turn else '그 밖'}: {_returns(groups.get((is_turn, None), []))}"]
            for sign, name in ((1, "외국인 +"), (-1, "외국인 −")):
                parts.append(f"{name} {_returns(groups.get((is_turn, sign), []))}")
            lines.append("  " + " · ".join(parts))
    return lines[1:] and lines


def _turn_of_month(days: Sequence[date]) -> set[date]:
    """The month's last trading day and the next month's first ``TOM_FIRST_DAYS`` among
    ``days`` (the candles' days, oldest first)."""
    by_month: dict[tuple[int, int], list[date]] = defaultdict(list)
    for day in days:
        by_month[(day.year, day.month)].append(day)
    months = sorted(by_month)
    turn: set[date] = set()
    for i, month in enumerate(months):
        in_month = by_month[month]
        if i > 0:  # the first month's start may be cut off by the history's start
            turn |= set(in_month[:TOM_FIRST_DAYS])
        if i < len(months) - 1:  # the last month may not be over
            turn.add(in_month[-1])
    return turn


def _returns(changes: Sequence[float]) -> str:
    if not changes:
        return "없음"
    up = sum(c > 0 for c in changes)
    mean = _pct(statistics.fmean(changes), signed=True)
    return f"{len(changes):,}일 평균 {mean} 상승 {100 * up / len(changes):.1f}%"


def _pct(x: float, *, signed: bool = False) -> str:
    return f"{100 * x:+.2f}%" if signed else f"{100 * x:.2f}%"


def _same(pairs: Iterable[tuple[float, float | None]]) -> str:
    """How often the second went the first's way, of the pairs where both moved."""
    moved = [(a, b) for a, b in pairs if b is not None and a != 0 and b != 0]
    if not moved:
        return "없음"
    same = sum(_sign(a) == _sign(b) for a, b in moved)
    return f"{100 * same / len(moved):.1f}% (n={len(moved)})"


def _opposite(pairs: Iterable[tuple[float, float | None]]) -> str:
    """How often the second went against the first, of the pairs where both moved."""
    moved = [(a, b) for a, b in pairs if b is not None and a != 0 and b != 0]
    if not moved:
        return "없음"
    against = sum(_sign(a) != _sign(b) for a, b in moved)
    return f"{100 * against / len(moved):.1f}% (n={len(moved)})"


def ols(pairs: Sequence[tuple[float, float]]) -> tuple[float, float] | None:
    """The slope of the second on the first and its t-value (classical standard error);
    None from fewer than three pairs, without spread, or when both the slope and its error
    are 0 (the second never moves: no t-value). An exact fit's t-value is infinite, with the
    slope's sign."""
    n = len(pairs)
    if n < 3:
        return None
    mx = statistics.fmean(x for x, _ in pairs)
    my = statistics.fmean(y for _, y in pairs)
    sxx = sum((x - mx) ** 2 for x, _ in pairs)
    if sxx == 0:
        return None
    beta = sum((x - mx) * (y - my) for x, y in pairs) / sxx
    residual = sum((y - my - beta * (x - mx)) ** 2 for x, y in pairs)
    se = math.sqrt(residual / (n - 2) / sxx)
    if se == 0:
        return None if beta == 0 else (beta, math.copysign(math.inf, beta))
    return beta, beta / se


def _slope(pairs: Sequence[tuple[float, float | None]]) -> str:
    found = ols([(x, y) for x, y in pairs if y is not None])
    if found is None:
        return "없음"
    beta, t = found
    return f"{beta:+.2f} (t {t:+.1f}, n={sum(y is not None for _, y in pairs)})"


def correlation(pairs: Sequence[tuple[float, float]]) -> float | None:
    if len(pairs) < 3:
        return None
    try:
        return statistics.correlation([x for x, _ in pairs], [y for _, y in pairs])
    except statistics.StatisticsError:  # no spread
        return None


def _corr(pairs: Sequence[tuple[float, float | None]]) -> str:
    found = correlation([(x, y) for x, y in pairs if y is not None])
    return "없음" if found is None else f"{found:+.2f}"
