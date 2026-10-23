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

``krx-quant check-krx-net-assets`` (``check_krx_net_assets``) reads KRX's rows of the same file
(#305, ``krx_net_assets``) and says what only they show: whether every trading day from the
listing is in, when each of the last days first came (KRX's FAQ says 08:00 on the next business
day), which day's listed units ``INVSTASST_NETASST_TOTAMT`` equals ``LIST_SHRS × NAV`` with (the
probe found the row's own on 169 of 400 rows only), and how KRX's net assets compare with KIS's on
the days both have. It reads the file only and prints counts, dates, times and product codes, no
value (KRX's terms forbid passing its data on): the scheduled job ``check-krx-net-assets-305``.
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
from krx_quant.kis import KisError, RestClient, rest_session
from krx_quant.krx_net_assets import FLAG, against_kis, next_trading_day
from krx_quant.market_calendar import MarketCalendar, calendar_store
from krx_quant.net_assets import KIS, KRX, read_net_assets
from krx_quant.products import Product, products_of
from krx_quant.structure_check import day_prices
from krx_quant.supply_probe import REQUEST_TIMEOUT, kis_error

TITLE = "KRX 마감 흐름 확인 (#305)"
SAME_WITHIN = 0.01  # two auction values within 1% of the recorded one are the same


def _counts(counter: Counter[str], unit: str = "") -> str:
    return " · ".join(f"{k} {n}{unit}" for k, n in sorted(counter.items())) if counter else "없음"


def net_asset_lines(rows: Sequence[Row], codes: Sequence[str]) -> list[str]:
    """KIS's rows by day: how many products, and the first and last time a value of the day came
    (KRX's: ``check_krx_net_assets``)."""
    lines = [f"1. 순자산 (state/closing_flow/net_assets.parquet): 행 {len(rows)}"]
    if not rows:
        return [*lines, "  없음"]
    lines.append(f"  출처: {_counts(Counter(str(r['source']) for r in rows))}")
    rows = [r for r in rows if r["source"] == KIS]
    if not rows:
        return [*lines, "  KIS 행 없음"]
    by_day: dict[date, list[Row]] = defaultdict(list)
    for row in rows:
        by_day[row["date"]].append(row)
    lines.append(
        f"  KIS 날: {min(by_day)} – {max(by_day)} ({len(by_day)}일)."
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
    async with rest_session(timeout=timeout) as session:
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


# The KRX Open API's rows (#305)

KRX_TITLE = "KRX 순자산 확인 (#305)"
EXACT, NEAR = 0.0001, 0.005  # a total within 0.01% of the product is the same; within 0.5%, near
LAST_DAYS = 10  # the last days whose first fetch is shown
# The hypotheses for the total of day t (its own NAV, or the day before's) against the units of t,
# t + 1 (the day's creations and redemptions listed the next trading day), t − 1 (the day
# before's), and t with the NAV of t − 1 (the net assets of the day before).
HYPOTHESES = (
    ("LIST_SHRS(t) × NAV(t)", 0, 0),
    ("LIST_SHRS(t+1) × NAV(t)", 1, 0),
    ("LIST_SHRS(t−1) × NAV(t)", -1, 0),
    ("LIST_SHRS(t) × NAV(t−1)", 0, -1),
)


def krx_day_lines(rows: Sequence[Row], products: Sequence[Product], calendar: MarketCalendar) -> list[str]:
    """1: the days KRX's rows cover, the trading days without one, the products missing."""
    lines = [f"1. KRX 행 (state/closing_flow/net_assets.parquet, 출처 {KRX}): 행 {len(rows)}"]
    if not rows:
        return [*lines, "  없음"]
    by_day: dict[date, set[str]] = defaultdict(set)
    for row in rows:
        by_day[row["date"]].add(row["code"])
    first, last = min(by_day), max(by_day)
    absent = [d for d in calendar.trading_days(first, last) if d not in by_day]
    lacking = {
        d: sorted({p.code for p in products if p.listed <= d} - codes)
        for d, codes in by_day.items()
        if {p.code for p in products if p.listed <= d} - codes
    }
    lines.append(
        f"  날: {first} – {last} ({len(by_day)}일) · 그 사이 거래일 중 행 없는 날 {len(absent)}"
        + (
            f"({', '.join(d.isoformat() for d in absent[:5])}{' 외' if len(absent) > 5 else ''})"
            if absent
            else ""
        )
    )
    lines.append(
        f"  상품이 다 있는 날 {len(by_day) - len(lacking)} · 빠진 상품이 있는 날 {len(lacking)}"
        + "".join(f" · {d} 없음 {', '.join(c)}" for d, c in sorted(lacking.items())[:5])
    )
    shares = sum(r.get("shares") is not None for r in rows)
    navs = sum(r.get("nav") is not None for r in rows)
    lines.append(f"  상장좌수가 있는 행 {shares} · NAV가 있는 행 {navs}")
    return lines


def krx_timing_lines(rows: Sequence[Row]) -> list[str]:
    """2: when each of the last days first came (the earliest and latest product)."""
    lines = [
        f"2. 날마다 처음 받은 때 (마지막 {LAST_DAYS}일. KRX FAQ: 다음 영업일 08:00부터."
        " 수집: 수집기가 시작할 때, 거래일 08:05부터 전 거래일 값이 들어올 때까지 5분마다)"
    ]
    by_day: dict[date, list[datetime]] = defaultdict(list)
    for row in rows:
        by_day[row["date"]].append(row["fetched_at"].astimezone(KST))
    for day in sorted(by_day)[-LAST_DAYS:]:
        times = sorted(by_day[day])
        lines.append(f"  {day}: {len(times)}종 · {times[0]:%m-%d %H:%M:%S} – {times[-1]:%m-%d %H:%M:%S}")
    return lines if by_day else [*lines, "  없음"]


def _kind(total: float, product: float) -> str:
    gap = abs(total - product) / abs(total)
    return f"{EXACT:.2%} 안" if gap <= EXACT else f"{NEAR:.1%} 안" if gap <= NEAR else "다름"


def krx_unit_lines(rows: Sequence[Row], calendar: MarketCalendar) -> list[str]:
    """3: which day's units and NAV the total equals the product of, on the rows of a day t whose
    trading days before and after have rows of the product (all four hypotheses comparable)."""
    lines = [
        "3. INVSTASST_NETASST_TOTAMT(t) = LIST_SHRS × NAV, 어느 날의 주수와 NAV인가 (원. 상품마다 t의 전·다음"
        f" 거래일에도 행이 있는 t. 같음 = {EXACT:.2%} 안, 가까움 = {NEAR:.1%} 안)"
    ]
    by_key = {(r["code"], r["date"]): r for r in rows}
    base: list[tuple[Row, Row, Row]] = []
    for row in rows:
        before = by_key.get((row["code"], calendar.previous_trading_day(row["date"])))
        after = by_key.get((row["code"], next_trading_day(calendar, row["date"])))
        if before is None or after is None:
            continue
        if any(r.get("shares") is None or r.get("nav") is None for r in (before, row, after)):
            continue
        base.append((before, row, after))
    changed = [t for t in base if len({r["shares"] for r in t}) > 1]
    lines.append(f"  견준 행 {len(base)} · 그 가운데 t−1, t, t+1의 주수가 다 같지는 않은 행 {len(changed)}")
    kinds = (f"{EXACT:.2%} 안", f"{NEAR:.1%} 안", "다름")
    for name, shift, nav_shift in HYPOTHESES:
        for title, chosen in (("모든 행", base), ("주수가 바뀐 행", changed)):
            counts = Counter[str](dict.fromkeys(kinds, 0))
            for triple in chosen:
                units = triple[1 + shift]["shares"]
                nav = triple[1 + nav_shift]["nav"]
                counts[_kind(triple[1]["net_assets"], units * nav)] += 1
            lines.append(f"  {name} · {title}: " + " · ".join(f"{k} {counts[k]}" for k in kinds))
    return lines


def krx_kis_lines(rows: Sequence[Row]) -> list[str]:
    """4: KRX's net assets against KIS's on each day both have."""
    lines = [f"4. KRX와 KIS의 같은 날 순자산 (다름 = {FLAG:.1%}와 0.5억 넘게 다름)"]
    days = sorted(
        {r["date"] for r in rows if r["source"] == KIS} & {r["date"] for r in rows if r["source"] == KRX}
    )
    if not days:
        return [*lines, "  둘 다 있는 날 없음"]
    for day in days:
        compared, flagged = against_kis(rows, day)
        lines.append(
            f"  {day}: 견준 상품 {compared} · 다름 {len(flagged)}"
            + (f"({', '.join(flagged)})" if flagged else "")
        )
    return lines


def check_krx_net_assets(settings: Settings, now: datetime) -> str:
    """``krx-quant check-krx-net-assets``: the module doc's second part."""
    calendar = calendar_store(settings).calendar
    products = [p for s in settings.symbols for p in products_of(s)]
    rows = read_net_assets(settings.state_dir)
    krx = [r for r in rows if r["source"] == KRX]
    head = [
        f"{KRX_TITLE} {now.astimezone(KST):%Y-%m-%d %H:%M} KST: 상품 {len(products)}종",
        "값은 싣지 않는다(개수, 날짜, 시각, 상품 코드만. KRX 약관: 비상업 이용, 제3자 제공 금지).",
    ]
    sections = [
        krx_day_lines(krx, products, calendar),
        krx_timing_lines(krx),
        krx_unit_lines(krx, calendar),
        krx_kis_lines(rows),
    ]
    lines = head
    for section in sections:
        lines += ["", *section]
    return "\n".join(lines)
