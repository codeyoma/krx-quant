"""``krx-quant check-closing-flow``: what the closing flow (#305) has kept on the server, and the
two things only the server shows.

- **When KIS moves to the new day.** ``probe-nav-305`` saw KIS's ETF current price still on the
  old day at 23:50, so the collector asks again every 10 minutes on trading-day mornings until the
  previous trading day's net assets are in (``net_assets``). Each row keeps when its value was
  first fetched: a day's earliest ``fetched_at`` is the first ask after KIS moved (or the
  collector's start, if it moved before), within the 10 minutes.
- **Which one-minute bar gives the closing auction value.** The denominator takes a day's value
  from the recorded KRX closing print only (``closing_flow``); before the recording began
  (2026-10-12) no day has one. The issue's fallback is the auction's one-minute bar, volume ×
  close, and neither bar is measured for it: the backfill's integrated (``UN``) 15:30 bar may hold
  NXT's after-market trades of that minute, and the KRX (``J``) bar's close was measured equal to
  the recorded print (#317, #342) but not its volume. On each day with a recorded print, both are
  set against it: the integrated bar from the backfill's files, the KRX bar asked of KIS
  (``FHKST03010230``, market ``J``, as ``auctions`` asks it: one call a symbol and day, half a
  second apart, with the cached token).

Also the 15:20 rows stored and the days computed. It prints counts, dates, times and the ratios of
the bars' values to the recorded one, no price, volume or net asset value, so its output can go
on the public issue (the scheduled job ``check-closing-flow-305``). Beside the running collector:
it writes nothing, and its few KIS calls are spaced.
"""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path

import aiohttp

from krx_quant.auctions import PRINT_WITHIN, bars_page, print_bar, recorded_prints
from krx_quant.auth import tokens_and_client
from krx_quant.backfill import MINUTE_BARS_TR, read_bars
from krx_quant.closing_flow import read_days, read_demand, recorded_auction_value
from krx_quant.config import Settings
from krx_quant.feeds import KRX_TRADE
from krx_quant.feeds.base import KST, Row
from krx_quant.kis import KisError, RestClient
from krx_quant.market_calendar import MarketCalendar, calendar_store
from krx_quant.net_assets import read_net_assets
from krx_quant.products import products_of
from krx_quant.structure_check import day_prices
from krx_quant.supply_probe import REQUEST_TIMEOUT, kis_error

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


PACE = 0.5  # seconds between KIS calls


def recorded_days(data_dir: Path, symbol: str, calendar: MarketCalendar, now: datetime) -> list[date]:
    """The trading days up to ``now``'s with ``symbol``'s KRX trades recorded on this server."""
    found = []
    for partition in (data_dir / "events").glob(f"date=*/feed={KRX_TRADE.tr_id}/symbol={symbol}"):
        day = date.fromisoformat(partition.parent.parent.name.removeprefix("date="))
        if calendar.is_trading_day(day) and day <= now.astimezone(KST).date():
            found.append(day)
    return sorted(found)


@dataclass
class Compared:
    """One bar's closing auction values against the recorded ones."""

    ratios: list[float] = field(default_factory=list)  # the bar's ÷ the recorded
    missing: int = 0  # days without the bar
    failed: list[str] = field(default_factory=list)  # KIS's errors
    closes: int = 0  # days the bar's close is the recorded closing print

    def line(self) -> str:
        same = sum(abs(r - 1) <= SAME_WITHIN for r in self.ratios)
        text = f"같음 {same} · 다름 {len(self.ratios) - same}"
        if self.ratios:
            text += f" · 비율 {min(self.ratios):.3f} – {max(self.ratios):.3f}"
            text += f" · 종가가 녹화 단일가와 같음 {self.closes}"
        text += f" · 봉 없음 {self.missing}"
        if self.failed:
            text += f" · 조회 실패 {len(self.failed)}({', '.join(sorted(set(self.failed)))})"
        return text


def _compare(compared: Compared, bar: Row | None, recorded: float, close: int | None) -> None:
    if bar is None or not bar.get("volume") or not bar.get("close"):
        compared.missing += 1
        return
    compared.ratios.append(bar["volume"] * bar["close"] / recorded)
    compared.closes += close is not None and bar["close"] == close


async def auction_value_lines(settings: Settings, calendar: MarketCalendar, now: datetime) -> list[str]:
    """On each day with a recorded closing print: the integrated and the KRX bar against it."""
    lines = [
        "4. 종가 단일가 체결대금: 녹화한 KRX 종가 단일가(H0STCNT0, 가격 × 수량)가 있는 날마다, 그 분의 1분봉"
        f" 단일가 봉(거래량 × 종가)과 견준다. 같음 = 녹화의 {SAME_WITHIN:.0%} 안. 비율 = 봉 ÷ 녹화."
        " 통합(UN)은 백필 파일, KRX(J)는 KIS에 묻는다"
    ]
    asks = {
        s: [
            (d, v)
            for d in recorded_days(settings.data_dir, s, calendar, now)
            if (v := _recorded(settings, calendar, s, d))
        ]
        for s in settings.symbols
    }
    client: RestClient | None = None
    calls = 0
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        if any(asks.values()):
            # The cached token while it is valid: no early refresh beside the running collector.
            tokens, client = tokens_and_client(
                session,
                replace(settings, token_refresh_margin_seconds=0.0),
                lambda: datetime.now().astimezone(),
            )
            try:
                await tokens.get()
            except (KisError, aiohttp.ClientError, OSError, ValueError) as e:
                reason = kis_error(e) if isinstance(e, KisError) else f"error {type(e).__name__}"
                lines.append(f"  KIS 접근 토큰이 없다: {reason}. KRX(J) 봉은 묻지 않는다")
                client = None
        for symbol, days in asks.items():
            if not days:
                lines.append(f"  {symbol}: 녹화한 종가 단일가가 있는 날 없음")
                continue
            integrated, krx = Compared(), Compared()
            for day, (recorded, close) in days:
                session_of = calendar.session(day)
                assert session_of is not None
                bars = read_bars(settings.data_dir, day, MINUTE_BARS_TR, symbol)
                prices = None if not bars else day_prices(bars, session_of)
                _compare(
                    integrated,
                    None if prices is None else {"volume": prices.auction_volume, "close": prices.close},
                    recorded,
                    close,
                )
                if client is None:
                    krx.failed.append("토큰 없음")
                    continue
                if calls:
                    await asyncio.sleep(PACE)
                calls += 1
                try:
                    page = await bars_page(
                        client, symbol, day, session_of.close + PRINT_WITHIN - timedelta(seconds=1)
                    )
                except KisError as e:
                    krx.failed.append(kis_error(e))
                    continue
                except (aiohttp.ClientError, OSError, ValueError, KeyError) as e:
                    krx.failed.append(f"error {type(e).__name__}")
                    continue
                _compare(krx, print_bar(page, session_of.close), recorded, close)
            lines.append(f"  {symbol}: 녹화 {len(days)}일 {days[0][0]} – {days[-1][0]}")
            lines.append(f"    통합(UN) 봉: {integrated.line()}")
            lines.append(f"    KRX(J) 봉: {krx.line()}")
    lines.append(f"  KIS: {calls}번 조회")
    return lines


def _recorded(
    settings: Settings, calendar: MarketCalendar, symbol: str, day: date
) -> tuple[float, int | None] | None:
    """The day's recorded closing auction value and its closing print's price; None without."""
    session = calendar.session(day)
    if session is None:
        return None
    value = recorded_auction_value(settings.data_dir, day, symbol, session.closing_auction, session.close)
    if value is None:
        return None
    prints = recorded_prints(settings.data_dir, day, symbol, session)
    return value, None if prints is None else prints[1]


async def check_closing_flow(settings: Settings, now: datetime) -> str:
    calendar = calendar_store(settings).calendar
    codes = [p.code for s in settings.symbols for p in products_of(s)]
    head = [
        f"{TITLE} {now.astimezone(KST):%Y-%m-%d %H:%M} KST: 종목 {', '.join(settings.symbols)}"
        f" · 상품 {len(codes)}종",
        "시세·거래량·순자산 값은 싣지 않는다(개수, 날짜, 시각, 봉과 녹화의 체결대금 비율만).",
    ]
    sections = [
        net_asset_lines(read_net_assets(settings.state_dir), codes),
        demand_lines(read_demand(settings.state_dir)),
        days_lines(read_days(settings.state_dir)),
        await auction_value_lines(settings, calendar, now),
    ]
    lines = head
    for section in sections:
        lines += ["", *section]
    return "\n".join(lines)
