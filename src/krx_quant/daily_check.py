"""The daily kinds' check (``krx-quant daily-check``, #264): what the server did with them on the
latest trading days, and whether the KRX day candles the answers are read from hold the KRX
auctions' prices.

1. Each daily kind's predictions of each day: how many, by predictor and status, how many
   carried the day's band, and when they were displayed.
2. Its answers of each day: how many of the symbols, by class, why not (``unscorable``), where
   from (``source``), how many special, and when they were written.
3. The KRX day candles (``candles.krx_days_path``) against the recorded KRX trades
   (``H0STCNT0``) of the day: its open against the opening auction's print (the first KRX trade
   from the session's open, within its first 10 minutes), its close against the closing
   auction's (the last KRX trade from the closing auction's start to a minute after the close,
   ``auctions.recorded_prints``), the kept closing auction price the answers end at
   (``auctions``, #264: KRX's one-minute bar of it) against that print too, and the integrated
   day candle's open against the KRX one (they differ when NXT's pre-market traded). Only same or
   different: the output holds no price, so it can go on the public issue.

   > On 2026-10-19 (the scheduled job ``daily-check-264``) the candle's open was the print and its
   > close was not, on all three symbols: since KRX's after-market (2026-09-14) the candle's close
   > is most likely its last price, so the answers end at the kept closing auction price instead.

It reads the engine DB read-only, the stored candles and the recorded events; it writes nothing
and calls nothing, so it may run beside the engine.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from pathlib import Path

from krx_quant.auctions import read_auctions, recorded_prints
from krx_quant.candles import read_candles, read_krx_days
from krx_quant.daily import DailyKind
from krx_quant.feeds.base import KST
from krx_quant.kind_registry import DAILY_KINDS
from krx_quant.market_calendar import MarketCalendar
from krx_quant.predictions import read_only

DAYS = 5


def daily_check(
    data_dir: Path,
    db: Path,
    calendar: MarketCalendar,
    symbols: Sequence[str],
    today: date,
    days: int = DAYS,
    kinds: Sequence[DailyKind] = DAILY_KINDS,
) -> str:
    """The check's text, over the latest ``days`` trading days up to ``today``."""
    checked = _trading_days(calendar, today, days)
    lines = [
        "하루 한 번 예측 점검 (#264)",
        f"기간: {checked[0]} – {checked[-1]} ({len(checked)}거래일), 종목 {', '.join(symbols)}",
    ]
    with read_only(db) as conn:
        for kind in kinds:
            lines += ["", f"1. {kind.name} 예측 ({kind.predict_at:%H:%M})"]
            lines += _predictions(conn, kind, checked)
            lines += ["", f"2. {kind.name} 정답"]
            lines += _answers(conn, kind, checked, len(symbols))
    lines += ["", "3. KRX 일봉과 녹화한 KRX 체결의 단일가"]
    lines += _auctions(data_dir, calendar, symbols, checked)
    return "\n".join(lines)


def _trading_days(calendar: MarketCalendar, today: date, days: int) -> list[date]:
    out: list[date] = []
    day = today
    while len(out) < days and day > today - timedelta(days=10 * days + 10):
        if calendar.is_trading_day(day):
            out.append(day)
        day -= timedelta(days=1)
    return sorted(out)


def _predictions(conn: sqlite3.Connection | None, kind: DailyKind, days: Sequence[date]) -> list[str]:
    if conn is None:
        return ["엔진 DB 없음"]
    out = []
    for day in days:
        rows = conn.execute(
            "SELECT algorithm, status, band IS NOT NULL, displayed_at FROM kind_predictions"
            " WHERE kind = ? AND bar_close = ?",
            (kind.name, kind.predicted_at(day).isoformat()),
        ).fetchall()
        if not rows:
            out.append(f"{day}: 예측 없음")
            continue
        by = Counter(f"{name} {status}" for name, status, _, _ in rows)
        banded = sum(1 for r in rows if r[2])
        shown = sorted(datetime.fromisoformat(r[3]).astimezone(KST).strftime("%H:%M:%S") for r in rows)
        listed = ", ".join(f"{k} {n}" for k, n in sorted(by.items()))
        out.append(
            f"{day}: 예측 {len(rows)}개 ({listed}), 보합 폭 {banded}/{len(rows)}, 표시 {shown[0]}–{shown[-1]}"
        )
    return out


def _answers(
    conn: sqlite3.Connection | None, kind: DailyKind, days: Sequence[date], symbols: int
) -> list[str]:
    if conn is None:
        return ["엔진 DB 없음"]
    out = []
    for day in days:
        rows = conn.execute(
            "SELECT class, unscorable, special, json_extract(path, '$.source'), scored_at FROM kind_answers"
            " WHERE kind = ? AND answer_version = ? AND bar_close = ?",
            (kind.name, kind.answer_version, kind.predicted_at(day).isoformat()),
        ).fetchall()
        if not rows:
            out.append(f"{day}: 정답 없음")
            continue
        classes = Counter(r[0] for r in rows if r[0] is not None)
        why = Counter(r[1] for r in rows if r[0] is None)
        sources = Counter(r[3] for r in rows)
        written = sorted(datetime.fromisoformat(r[4]).astimezone(KST).strftime("%m-%d %H:%M") for r in rows)
        text = ", ".join(f"{c} {classes[c]}" for c in kind.classes)
        line = f"{day}: 정답 {len(rows)}/{symbols} ({text})"
        if why:
            line += " · 클래스 없음 " + ", ".join(f"{k} {n}" for k, n in sorted(why.items()))
        line += " · 출처 " + ", ".join(f"{k} {n}" for k, n in sorted(sources.items(), key=str))
        line += f" · 특수 {sum(1 for r in rows if r[2])} · 채점 {written[0]}–{written[-1]}"
        out.append(line)
    return out


def _auctions(
    data_dir: Path, calendar: MarketCalendar, symbols: Sequence[str], days: Sequence[date]
) -> list[str]:
    out = []
    tally: Counter[str] = Counter()
    for symbol in symbols:
        krx = {r["date"]: r for r in read_krx_days(data_dir, symbol)}
        integrated = {r["date"]: r for r in read_candles(data_dir, "day", symbol)}
        kept = read_auctions(data_dir, symbol)
        for day in days:
            session = calendar.session(day)
            candle = krx.get(day)
            if session is None or candle is None:
                out.append(f"{day} {symbol}: KRX 일봉 없음")
                continue
            prints = recorded_prints(data_dir, day, symbol, session)
            parts = []
            if prints is None:
                parts.append("녹화 없음")
            else:
                opening, closing = prints
                auction = kept.get(day, {}).get("close")
                for name, printed, stored in (
                    ("시가", opening, candle["open"]),
                    ("종가", closing, candle["close"]),
                    ("정답 종가", closing, auction),
                ):
                    if name == "정답 종가" and stored is None:
                        parts.append("정답 종가 없음")
                        continue
                    same = printed is not None and printed == stored
                    tally[f"{name} {'같음' if same else '다름'}"] += 1
                    parts.append(
                        f"{name} " + ("체결 없음" if printed is None else "같음" if same else "다름")
                    )
            other = integrated.get(day)
            if other is not None:
                differs = other["open"] != candle["open"]
                tally[f"통합 시가 {'다름' if differs else '같음'}"] += 1
                parts.append(f"통합 일봉 시가 {'다름' if differs else '같음'}")
            out.append(f"{day} {symbol}: " + ", ".join(parts))
    if tally:
        out.append("요약: " + ", ".join(f"{k} {n}" for k, n in sorted(tally.items())))
    return out
