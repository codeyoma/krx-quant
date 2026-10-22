"""``krx-quant check-closing-flow``: what the closing flow (#305) has kept on the server, for the
two things only its files show.

- **When KIS moves to the new day.** ``probe-nav-305`` saw KIS's ETF current price still on the
  old day at 23:50, so the collector asks again every 10 minutes on trading-day mornings until the
  previous trading day's net assets are in (``net_assets``). Each row keeps when its value was
  first fetched: a day's earliest ``fetched_at`` is the first ask after KIS moved (or the
  collector's start, if it moved before), within the 10 minutes.
- **The closing auction value from the bars.** A day without the closing print in its recorded KRX
  trades takes the one-minute bar of the auction (volume × close, ``closing_flow``). Those bars are
  the backfill's integrated ones: NXT's after-market trades of that minute may be in them. On the
  days with both, the two are compared.

Also the 15:20 rows stored and the days computed. It prints counts, dates, times and ratios of the
two auction values, no price, volume or net asset value, so its output can go on the public issue
(the scheduled job ``check-closing-flow-305``). It reads files only, beside the running collector.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import date, datetime

from krx_quant.backfill import MINUTE_BARS_TR, read_bars
from krx_quant.closing_flow import read_days, read_demand, recorded_auction_value
from krx_quant.config import Settings
from krx_quant.feeds.base import KST, Row
from krx_quant.market_calendar import MarketCalendar, calendar_store
from krx_quant.net_assets import read_net_assets
from krx_quant.products import products_of
from krx_quant.structure_check import bar_days, day_prices

TITLE = "KRX 마감 흐름 확인 (#305)"
SAME_WITHIN = 0.01  # two auction values within 1% of the recorded one are the same


def _counts(counter: Counter[str], unit: str = "") -> str:
    return " · ".join(f"{k} {n}{unit}" for k, n in sorted(counter.items())) if counter else "없음"


def net_asset_lines(rows: Sequence[Row], codes: Sequence[str]) -> list[str]:
    """The rows by day: how many products, and the first and last time a value of the day came."""
    lines = [f"1. 순자산 (state/closing_flow/net_assets.parquet): 행 {len(rows)}"]
    if not rows:
        return [*lines, "  없음"]
    lines.append(f"  출처: {_counts(Counter(str(r['source']) for r in rows))}")
    by_day: dict[date, list[Row]] = defaultdict(list)
    for row in rows:
        by_day[row["date"]].append(row)
    lines.append(
        f"  날: {min(by_day)} – {max(by_day)} ({len(by_day)}일)."
        " 날마다 상품 수, 처음 받은 때(가장 이른 – 가장 늦은):"
    )
    for day in sorted(by_day):
        found = by_day[day]
        times = sorted(r["fetched_at"].astimezone(KST) for r in found)
        lacking = sorted(set(codes) - {r["code"] for r in found})
        lines.append(
            f"  {day}: {len({r['code'] for r in found})}/{len(codes)}종 · {times[0]:%m-%d %H:%M:%S}"
            f" – {times[-1]:%m-%d %H:%M:%S}" + (f" · 없음 {', '.join(lacking)}" if lacking else "")
        )
    return lines


def demand_lines(rows: Sequence[Row]) -> list[str]:
    lines = [f"2. 15:20 수요 (state/closing_flow/demand.parquet): 행 {len(rows)}"]
    by_symbol: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        by_symbol[row["symbol"]].append(row)
    for symbol, found in sorted(by_symbol.items()):
        days = sorted(r["date"] for r in found)
        counted = Counter(f"{r['products']}종" for r in found)
        at = Counter(f"{r['at'].astimezone(KST):%H:%M:%S}" for r in found)
        empty = sum(r["demand"] is None for r in found)
        lines.append(
            f"  {symbol}: {len(found)}일 {days[0]} – {days[-1]} · 센 상품 {_counts(counted, '일')}"
            f" · 시각 {_counts(at, '일')} · 수요 없음 {empty}일"
        )
    return lines if by_symbol else [*lines, "  없음"]


def days_lines(kept: dict[str, object] | None) -> list[str]:
    if kept is None:
        return ["3. 지난날 표 (state/closing_flow/days.json): 없음(아직 계산하지 않았다)"]
    lines = [f"3. 지난날 표 (state/closing_flow/days.json): 계산한 때 {kept['computed_at']}"]
    symbols = kept["symbols"]
    assert isinstance(symbols, dict)
    for symbol, body in symbols.items():
        days = body["days"]
        sources = Counter(str(d["demand_source"] or "없음") for d in days)
        values = Counter(str(v["source"]) for v in body["auction_values"].values())
        span = f"{days[0]['date']} – {days[-1]['date']}" if days else "없음"
        lines.append(
            f"  {symbol}: {len(days)}일 {span} · 수요 출처 {_counts(sources, '일')}"
            f" · 체결대금 출처 {_counts(values, '일')}"
        )
    return lines


def auction_value_lines(settings: Settings, calendar: MarketCalendar) -> list[str]:
    """On each day with both: the recorded closing auction value against the bar's."""
    lines = [
        "4. 종가 단일가 체결대금: 녹화한 KRX 체결(H0STCNT0)과 1분봉(통합)의 단일가 봉(거래량 × 종가)이"
        f" 둘 다 있는 날. 같음 = 녹화의 {SAME_WITHIN:.0%} 안. 비율 = 1분봉 ÷ 녹화"
    ]
    for symbol in settings.symbols:
        same, ratios, only_bar = 0, [], 0
        days: list[date] = []
        for day in bar_days(settings.data_dir, [symbol], calendar):
            session = calendar.session(day)
            bars = read_bars(settings.data_dir, day, MINUTE_BARS_TR, symbol)
            prices = None if session is None or not bars else day_prices(bars, session)
            if session is None or prices is None:
                continue
            bar = prices.auction_volume * prices.close
            recorded = recorded_auction_value(
                settings.data_dir, day, symbol, session.closing_auction, session.close
            )
            if recorded is None:
                only_bar += 1
                continue
            days.append(day)
            ratios.append(bar / recorded)
            same += abs(bar - recorded) <= SAME_WITHIN * recorded
        if not ratios:
            lines.append(f"  {symbol}: 둘 다 있는 날 없음 (1분봉만 {only_bar}일)")
            continue
        lines.append(
            f"  {symbol}: 둘 다 {len(ratios)}일 {days[0]} – {days[-1]} · 같음 {same}"
            f" · 다름 {len(ratios) - same} · 비율 {min(ratios):.3f} – {max(ratios):.3f}"
            f" · 1분봉만 {only_bar}일"
        )
    return lines


def check_closing_flow(settings: Settings, now: datetime) -> str:
    calendar = calendar_store(settings).calendar
    codes = [p.code for s in settings.symbols for p in products_of(s)]
    head = [
        f"{TITLE} {now.astimezone(KST):%Y-%m-%d %H:%M} KST: 종목 {', '.join(settings.symbols)}"
        f" · 상품 {len(codes)}종",
        "시세·거래량·순자산 값은 싣지 않는다(개수, 날짜, 시각, 두 체결대금의 비율만).",
    ]
    sections = [
        net_asset_lines(read_net_assets(settings.state_dir), codes),
        demand_lines(read_demand(settings.state_dir)),
        days_lines(read_days(settings.state_dir)),
        auction_value_lines(settings, calendar),
    ]
    lines = head
    for section in sections:
        lines += ["", *section]
    return "\n".join(lines)
