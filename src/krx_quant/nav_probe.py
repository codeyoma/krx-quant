"""``krx-quant probe-nav``: where KIS gives the net assets of the single-stock leveraged and
inverse ETFs (#305), before the closing flow uses any.

The closing flow's demand of a product is A·(L² − L)·r, with A its net assets on the previous
trading day (#305). The engine receives none, and no KIS query for them was measured. The
issue's three candidates (docs/kis-feeds.md, "ETF 순자산 (#305)"):

- (a) KIS's ETF/ETN current price (``FHPST02400000``): its net asset total ``etf_ntas_ttam``,
  beside ``etf_crcl_ntas_ttam``, ``nav``, ``prdy_last_nav``, ``lstn_stcn`` and
  ``etf_crcl_stcn``. It takes no date: today's values only;
- (b) KIS's NAV trend: by day (``FHPST02440200`` NAV 비교추이(일), a date range, up to 100 days a
  call), the product's (``FHPST02440000`` NAV 비교추이(종목), with ``prdy_clpr_nav``) and by
  minute (``FHPST02440100`` NAV 비교추이(분), the latest 30 rows);
- (c) the listed units × NAV: (a)'s ``lstn_stcn`` × its ``nav`` or ``prdy_last_nav``.

For every product of ``products.PRODUCTS`` it asks each query (the daily one paged back from
today to before the listing) and says:

- whether each answers, the look of its fields (``probe_fields.flags``) and how many products
  have a value (not empty, not 0: KIS's 0 may mean no figure, #295) in each wanted field;
- whether (a)'s ``etf_trc_ert_mltp`` is the product's L and its ``stck_lstn_date`` the listing
  day: the product list checked against KIS (counts only);
- how far back the daily NAV goes: rows, first and last day, whether it reaches the listing day
  (2026-05-27), and the trading days missing between;
- when the latest value is in, as far as one night shows: the time it asked, the daily rows'
  latest day against the latest trading day, which day's daily NAV (a)'s ``nav`` and
  ``prdy_last_nav`` and (b)'s ``nav`` and ``prdy_clpr_nav`` equal, the minute NAV's last time;
- whether (a)'s net asset totals are (c), within rounding and in which unit (counts only).

It prints counts, dates, times, field names and flags, no value, so its report can go on the
public issue (the scheduled job ``probe-nav-305``). Calls are spaced ``PACE`` apart, under the
REST limit the collector shares, with the cached access token while it is valid (no early
refresh). KIS's examples list these queries for real accounts only (the server's key is one).
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any

import aiohttp

from krx_quant.auth import tokens_and_client
from krx_quant.config import Settings
from krx_quant.feeds.base import KST
from krx_quant.kis import KisError, RestClient, rest_session
from krx_quant.market_calendar import MarketCalendar, calendar_store
from krx_quant.probe_fields import MISSING, day_of, describe, flags, order_of, present, same
from krx_quant.products import PRODUCTS, Product, Structure
from krx_quant.supply_probe import REQUEST_TIMEOUT, kis_error

Out = Callable[[str], None]

PACE = 0.5  # seconds between calls: about 100 calls for the 16 products, a minute
MAX_FAILED_IN_A_ROW = 10  # then KIS is down or refusing: the rest is not asked
HISTORY_FROM = date(2026, 5, 1)  # the daily NAV is asked from here: before the listing
MAX_PAGES = 5  # daily NAV calls a product, at most (100 days a call)
MINUTE_MARKETS = ("E", "J")  # KIS's example asks the minute NAV with E; J if E does not answer
MINUTE_PERIOD = "60"  # FID_HOUR_CLS_CODE: one-minute rows
# The units KIS might give a net asset total in, and the share of it two figures may differ
# by and still be the same (or half the unit, whichever is more: a total rounded to it).
UNITS = (("원", 1.0), ("천원", 1e3), ("백만원", 1e6), ("억원", 1e8))
TOLERANCE = 0.005


@dataclass(frozen=True)
class Query:
    tr: str
    path: str
    title: str  # KIS's name for it


ETFETN = "/uapi/etfetn/v1/quotations"
PRICE = Query("FHPST02400000", f"{ETFETN}/inquire-price", "ETF/ETN 현재가")
DAILY = Query("FHPST02440200", f"{ETFETN}/nav-comparison-daily-trend", "NAV 비교추이(일)")
TREND = Query("FHPST02440000", f"{ETFETN}/nav-comparison-trend", "NAV 비교추이(종목)")
MINUTES = Query("FHPST02440100", f"{ETFETN}/nav-comparison-time-trend", "NAV 비교추이(분)")

PRICE_FIELDS = ("etf_ntas_ttam", "etf_crcl_ntas_ttam", "nav", "prdy_last_nav", "lstn_stcn", "etf_crcl_stcn")
TREND_FIELDS = ("nav", "prdy_clpr_nav")
DAILY_DATE = "stck_bsop_date"
MINUTE_TIME = "bsop_hour"
# (a)'s total against (c): (the total, the units, the NAV).
PAIRS = (
    ("etf_ntas_ttam", "lstn_stcn", "nav"),
    ("etf_ntas_ttam", "lstn_stcn", "prdy_last_nav"),
    ("etf_crcl_ntas_ttam", "etf_crcl_stcn", "nav"),
)
STRUCTURES = {Structure.SPOT_FUTURES: "현물+선물형", Structure.FUTURES: "선물형"}


def _ymd(day: date) -> str:
    return day.strftime("%Y%m%d")


def _number(value: Any) -> float | None:
    """A value (not empty, not 0) as a number; None otherwise."""
    if not present(value):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if number != 0 else None


def has_value(value: Any) -> bool:
    return _number(value) is not None


def _object(body: Mapping[str, Any], key: str) -> dict[str, Any] | None:
    value = body.get(key)
    if isinstance(value, list) and len(value) == 1:  # an object wrapped in a list
        value = value[0]
    return value if isinstance(value, dict) else None


def one_object(body: Mapping[str, Any]) -> dict[str, Any] | None:
    """A snapshot's object (``output``, else ``output1``); None if there is none."""
    return next((o for key in ("output", "output1") if (o := _object(body, key)) is not None), None)


def two_objects(body: Mapping[str, Any]) -> dict[str, dict[str, Any]] | None:
    """The objects of an answer of two (``output1``, ``output2``), by key; None if neither."""
    parts = {key: o for key in ("output1", "output2", "output") if (o := _object(body, key)) is not None}
    return parts or None


def row_list(body: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    """The answer's rows with any value (``output``, ``output2`` or ``output1``); None if no list."""
    for key in ("output", "output2", "output1"):
        value = body.get(key)
        if isinstance(value, list):
            return [r for r in value if isinstance(r, dict) and any(present(v) for v in r.values())]
    return None


@dataclass
class Asked:
    """One call: what the parser made of the answer, KIS's continuation header, or the error."""

    value: Any = None
    more: str = ""
    error: str | None = None


class _Asker:
    def __init__(self, client: RestClient) -> None:
        self._client = client
        self.calls = 0
        self.failed = 0
        self._failed_in_a_row = 0

    async def ask(
        self, query: Query, params: dict[str, str], parse: Callable[[Mapping[str, Any]], Any]
    ) -> Asked:
        """``parse``'s reading of the answer; an answer it reads as None is no usable answer, a
        failed call (#297)."""
        if self._failed_in_a_row >= MAX_FAILED_IN_A_ROW:
            return Asked(error=f"묻지 않음: {MAX_FAILED_IN_A_ROW}번 잇달아 실패")
        if self.calls:
            await asyncio.sleep(PACE)
        self.calls += 1
        try:
            body, more = await self._client.get_page(query.tr, query.path, params)
        except KisError as e:
            error = kis_error(e)
        except (aiohttp.ClientError, OSError, ValueError) as e:  # OSError: timeouts too
            error = f"error {type(e).__name__}"
        else:
            value = parse(body)
            if value is not None:
                self._failed_in_a_row = 0
                return Asked(value=value, more=more)
            error = f"OK, 읽을 것이 없다(본문 키: {', '.join(sorted(body))})"
        self.failed += 1
        self._failed_in_a_row += 1
        return Asked(error=error)


@dataclass
class Found:
    """What the queries said of one product."""

    product: Product
    price: dict[str, Any] | None = None  # (a)'s object
    price_error: str | None = None
    trend: dict[str, dict[str, Any]] | None = None  # NAV 비교추이(종목)'s objects by key
    trend_error: str | None = None
    daily: dict[date, Mapping[str, Any]] = field(default_factory=dict)  # by day
    daily_order: list[date] = field(default_factory=list)  # the first page's days, as sent
    daily_calls: int = 0
    daily_error: str | None = None  # the first call's error; or a later page's, after rows
    daily_more: str = ""  # the first page's tr_cont
    minutes: list[dict[str, Any]] = field(default_factory=list)
    minutes_market: str | None = None  # the market that gave the rows
    minutes_error: str | None = None

    @property
    def trend_values(self) -> dict[str, Any]:
        """The objects of NAV 비교추이(종목) as one (the first having a field wins)."""
        merged: dict[str, Any] = {}
        for part in (self.trend or {}).values():
            for name, value in part.items():
                merged.setdefault(name, value)
        return merged


def _codes(params: Mapping[str, str], market: str, code: str) -> dict[str, str]:
    return {"FID_COND_MRKT_DIV_CODE": market, "FID_INPUT_ISCD": code, **params}


async def _ask_product(asker: _Asker, product: Product, today: date) -> Found:
    found = Found(product)
    code = product.code
    price = await asker.ask(PRICE, _codes({}, "J", code), one_object)
    found.price, found.price_error = price.value, price.error
    trend = await asker.ask(TREND, _codes({}, "J", code), two_objects)
    found.trend, found.trend_error = trend.value, trend.error
    end = today
    while found.daily_calls < MAX_PAGES:
        params = {"FID_INPUT_DATE_1": _ymd(HISTORY_FROM), "FID_INPUT_DATE_2": _ymd(end)}
        asked = await asker.ask(DAILY, _codes(params, "J", code), row_list)
        found.daily_calls += 1
        if asked.error is not None:
            found.daily_error = asked.error if not found.daily else f"{asked.error}, {len(found.daily)}행 뒤"
            break
        page = [(d, row) for row in asked.value if (d := day_of(row, DAILY_DATE)) is not None]
        if found.daily_calls == 1:
            found.daily_more = asked.more
            found.daily_order = [d for d, _ in page]
        new = {d: row for d, row in page if d <= end and d not in found.daily}
        if not new:
            break
        found.daily.update(new)
        end = min(new) - timedelta(days=1)
        if end < HISTORY_FROM:
            break
    tried: list[str] = []
    for market in MINUTE_MARKETS:
        asked = await asker.ask(MINUTES, _codes({"FID_HOUR_CLS_CODE": MINUTE_PERIOD}, market, code), row_list)
        if asked.error is None and asked.value:
            found.minutes, found.minutes_market = asked.value, market
            break
        tried.append(f"{market} {asked.error or 'OK, 0행'}")
    found.minutes_error = "; ".join(tried) or None
    return found


def latest_trading_day(calendar: MarketCalendar, now: datetime) -> date:
    """The last trading day whose session had closed at ``now``."""
    today = now.astimezone(KST).date()
    session = calendar.session(today)
    if session is not None and now >= session.close:
        return today
    return calendar.previous_trading_day(today)


def _kind(product: Product) -> str:
    return f"{product.underlying} {product.leverage:+d}x {STRUCTURES[product.structure]}"


def legend(products: Sequence[Product]) -> str:
    """The products by stock, leverage and structure, as the lines below name them by code."""
    kinds: dict[str, list[str]] = {}
    for product in products:
        kinds.setdefault(_kind(product), []).append(product.code)
    return "상품: " + " · ".join(f"{kind} {', '.join(codes)}" for kind, codes in kinds.items())


def grouped(texts: Sequence[tuple[str, str]]) -> list[str]:
    """Products' lines (code, text), one per distinct text with the codes that share it."""
    groups: dict[str, list[str]] = {}
    for code, text in texts:
        groups.setdefault(text, []).append(code)
    lines = []
    for text, codes in groups.items():
        if len(codes) == len(texts) > 1:
            head = f"{len(codes)}종 모두"
        else:
            head = ", ".join(codes) + (f" ({len(codes)}종)" if len(codes) > 1 else "")
        lines.append(f"  {head}: {text}")
    return lines


def _looks(rows: Sequence[Mapping[str, Any]], names: Sequence[str]) -> str:
    looks = flags(rows)
    return ", ".join(f"{name} {looks.get(name, MISSING)}" for name in names)


def _counts(counter: Counter[str]) -> str:
    return " · ".join(f"{k} {n}" for k, n in counter.items()) if counter else "없음"


def _same_counts(results: Sequence[bool | None]) -> str:
    equal = sum(r is True for r in results)
    different = sum(r is False for r in results)
    missing = sum(r is None for r in results)
    return f"같음 {equal} · 다름 {different} · 견줄 것 없음 {missing}"


def price_lines(found: Sequence[Found]) -> list[str]:
    """(a): each product's answer and fields, the values counted, the product list's check."""
    lines = [f"1. (a) {PRICE.tr} {PRICE.title} (J): 오늘 값만(날짜를 받지 않는다)"]
    lines += grouped(
        [
            (
                f.product.code,
                str(f.price_error) if f.price is None else f"OK · {_looks([f.price], PRICE_FIELDS)}",
            )
            for f in found
        ]
    )
    answered = [f.price for f in found if f.price is not None]
    lines.append(
        f"  값 있음({len(found)}종 중): "
        + " · ".join(f"{name} {sum(has_value(p.get(name)) for p in answered)}" for name in PRICE_FIELDS)
    )
    leverage: list[bool | None] = []
    listed: list[bool | None] = []
    differ: list[str] = []
    for f in found:
        multiple = _number(f.price.get("etf_trc_ert_mltp")) if f.price is not None else None
        leverage.append(None if multiple is None else multiple == f.product.leverage)
        day = day_of(f.price, "stck_lstn_date") if f.price is not None else None
        listed.append(None if day is None else day == f.product.listed)
        what = [n for n, r in (("배율", leverage[-1]), ("상장일", listed[-1])) if r is False]
        if what:
            differ.append(f"{f.product.code}({'·'.join(what)})")
    lines.append(
        f"  상품 목록 확인: etf_trc_ert_mltp = 배율 L {_same_counts(leverage)}"
        f" · stck_lstn_date = 상장일 {_same_counts(listed)}"
    )
    if differ:
        lines.append(f"  다른 상품: {', '.join(differ)}")
    shown = next((f for f in found if f.price is not None), None)
    if shown is not None and shown.price is not None:
        lines.append(f"  필드({shown.product.code}): {describe(flags([shown.price]))}")
    return lines


def daily_lines(found: Sequence[Found], calendar: MarketCalendar) -> list[str]:
    """(b) by day: each product's rows, how far back, the trading days missing."""
    lines = [f"2. (b) {DAILY.tr} {DAILY.title} (J, {HISTORY_FROM}부터, 한 상품에 {MAX_PAGES}번까지)"]
    reach = Counter[str]()
    texts: list[tuple[str, str]] = []
    for f in found:
        if not f.daily:
            texts.append((f.product.code, f.daily_error or "OK, 0행"))
            reach["답 없음"] += 1
            continue
        days = sorted(f.daily)
        valued = [d for d in days if has_value(f.daily[d].get("nav"))]
        listed = f.product.listed
        before = sum(d < listed for d in days)
        reaches = bool(valued) and min(valued) <= listed
        reach["상장일까지 감" if reaches else "상장일까지 못 감"] += 1
        expected = calendar.trading_days(listed, days[-1])
        missing = sorted(set(expected) - set(days)) if expected else None
        gap = "거래 일정 없음" if missing is None else f"상장일부터 빠진 거래일 {len(missing)}"
        if missing:
            gap += f"({', '.join(d.isoformat() for d in missing[:5])}{' 외' if len(missing) > 5 else ''})"
        looks = _looks(list(f.daily.values()), ("nav",))
        reached = f"상장일 {listed}까지 감" if reaches else "상장일까지 못 감"
        text = (
            f"{len(days)}행({f.daily_calls}번 조회) {days[0]}..{days[-1]},"
            f" {order_of(f.daily_order)} · {looks} · nav 값 있는 날 {len(valued)}"
            + (f"(첫날 {valued[0]})" if valued else "")
            + f" · {reached} · {gap} · 상장 전 행 {before}"
        )
        if f.daily_more:
            text += f" · tr_cont {f.daily_more}"
        if f.daily_error is not None:
            text += f" · 그 뒤 {f.daily_error}"
        texts.append((f.product.code, text))
    lines += grouped(texts)
    lines.append(f"  모두: {_counts(reach)}")
    shown = next((f for f in found if f.daily), None)
    if shown is not None:
        lines.append(f"  필드({shown.product.code}): {describe(flags(list(shown.daily.values())))}")
    return lines


def trend_lines(found: Sequence[Found]) -> list[str]:
    """(b) of the product: each product's objects and the NAV fields."""
    lines = [f"3. (b) {TREND.tr} {TREND.title} (J): 오늘 값만"]
    texts: list[tuple[str, str]] = []
    for f in found:
        if f.trend is None:
            texts.append((f.product.code, str(f.trend_error)))
            continue
        parts = " · ".join(f"{key} {len(part)}개 필드" for key, part in f.trend.items())
        texts.append((f.product.code, f"OK · {parts} · {_looks([f.trend_values], TREND_FIELDS)}"))
    lines += grouped(texts)
    answered = [f.trend_values for f in found if f.trend is not None]
    lines.append(
        f"  값 있음({len(found)}종 중): "
        + " · ".join(f"{name} {sum(has_value(t.get(name)) for t in answered)}" for name in TREND_FIELDS)
    )
    shown = next((f for f in found if f.trend), None)
    if shown is not None and shown.trend is not None:
        described = "; ".join(f"{key} {describe(flags([part]))}" for key, part in shown.trend.items())
        lines.append(f"  필드({shown.product.code}): {described}")
    return lines


def minute_lines(found: Sequence[Found]) -> list[str]:
    """(b) by minute: each product's rows and their times."""
    first, then = MINUTE_MARKETS
    lines = [
        f"4. (b) {MINUTES.tr} {MINUTES.title} (시장 {first}, 안 되면 {then};"
        f" FID_HOUR_CLS_CODE {MINUTE_PERIOD} = 1분): 최근 30행, 날짜 필드는 없다"
    ]
    texts: list[tuple[str, str]] = []
    for f in found:
        if not f.minutes:
            texts.append((f.product.code, str(f.minutes_error)))
            continue
        times = sorted(t for row in f.minutes if (t := _hhmm(row.get(MINUTE_TIME))) is not None)
        span = f" · {MINUTE_TIME} {times[0]}..{times[-1]}" if times else f" · {MINUTE_TIME} 없음"
        text = f"{f.minutes_market} OK, {len(f.minutes)}행{span} · {_looks(f.minutes, ('nav',))}"
        texts.append((f.product.code, text + (f" (먼저 {f.minutes_error})" if f.minutes_error else "")))
    lines += grouped(texts)
    shown = next((f for f in found if f.minutes), None)
    if shown is not None:
        lines.append(f"  필드({shown.product.code}): {describe(flags(shown.minutes))}")
    return lines


def _hhmm(value: Any) -> str | None:
    text = str(value or "").strip()
    if len(text) != 6 or not text.isdigit():
        return None
    return f"{text[:2]}:{text[2:4]}"


def _which_day(value: Any, daily: Mapping[date, Mapping[str, Any]], latest: date, previous: date) -> str:
    """Which day's daily NAV ``value`` equals: the latest trading day, the one before, both."""
    if not has_value(value) or not daily:
        return "견줄 것 없음"
    on_latest = latest in daily and same(value, daily[latest].get("nav"))
    on_previous = previous in daily and same(value, daily[previous].get("nav"))
    if on_latest and on_previous:
        return "둘 다(같은 값)"
    if on_latest:
        return "마지막 거래일"
    if on_previous:
        return "그 전 거래일"
    if any(same(value, row.get("nav")) for row in daily.values()):
        return "다른 날"
    return "어느 날과도 다름"


def timing_lines(found: Sequence[Found], now: datetime, latest: date, previous: date) -> list[str]:
    """When the latest value is in, as far as this one look shows."""
    asked = now.astimezone(KST).strftime("%Y-%m-%d %H:%M")
    lines = [f"5. 그날 값이 언제 나오나({asked} KST에 본 것, 마지막 거래일 {latest}, 그 전 {previous})"]
    newest = Counter[str]()
    for f in found:
        if not f.daily:
            newest["행 없음"] += 1
        elif max(f.daily) in (latest, previous):
            newest["마지막 거래일" if max(f.daily) == latest else "그 전 거래일"] += 1
        else:
            newest["다른 날"] += 1
    lines.append(f"  (b) 일별의 가장 최근 행: {_counts(newest)}")
    for title, values in (
        ("(a) nav", [f.price.get("nav") if f.price else None for f in found]),
        ("(a) prdy_last_nav", [f.price.get("prdy_last_nav") if f.price else None for f in found]),
        ("(b) 종목 nav", [f.trend_values.get("nav") for f in found]),
        ("(b) 종목 prdy_clpr_nav", [f.trend_values.get("prdy_clpr_nav") for f in found]),
    ):
        days = Counter(_which_day(v, f.daily, latest, previous) for v, f in zip(values, found, strict=True))
        lines.append(f"  {title} = (b) 일별 nav의 날: {_counts(days)}")
    ends = Counter(
        max(times)
        for f in found
        if (times := [t for row in f.minutes if (t := _hhmm(row.get(MINUTE_TIME))) is not None])
    )
    lines.append(f"  (b) 분별 마지막 시각: {_counts(Counter(dict(sorted(ends.items()))))}")
    lines.append("  (a) 순자산 총액이 어느 날의 NAV로 맞는지는 6절에 있다.")
    lines.append(
        "  이 한 번으로는 모른다(두 번째 관찰이 필요하다): 장 전(08:00–09:00)에 (a)·(b)가"
        " 전 거래일 값을 주는지, 그날 값이 몇 시에 처음 바뀌는지. 예약 작업은 밤(22:30–05:30)에만 돈다."
    )
    return lines


def _agrees(total: float, units: float, nav: float, scale: float) -> bool:
    computed = units * nav
    return abs(total * scale - computed) <= max(TOLERANCE * abs(computed), scale / 2)


def agreement_lines(found: Sequence[Found]) -> list[str]:
    """(a)'s totals against (c), product by product: equal in which unit, different, missing."""
    lines = [
        "6. (a) 순자산 총액과 (c) 상장 주수 × NAV (개수만. 같음 = 단위를 맞춰 "
        f"{TOLERANCE:.1%} 안, 또는 그 단위의 반 안)"
    ]
    for total_name, units_name, nav_name in PAIRS:
        counts = Counter[str]({unit: 0 for unit, _ in UNITS})
        for f in found:
            values = f.price or {}
            total, units, nav = (_number(values.get(n)) for n in (total_name, units_name, nav_name))
            if total is None or units is None or nav is None:
                counts["견줄 것 없음"] += 1
                continue
            unit = next((name for name, scale in UNITS if _agrees(total, units, nav, scale)), "다름")
            counts[unit] += 1
        lines.append(f"  {total_name} = {units_name} × {nav_name}: {_counts(counts)}")
    lines.append(
        "  (c)의 지난날: 상장 주수는 (a)의 오늘 값뿐이다(날마다의 주수를 주는 KIS 조회를 찾지 못했다)."
        " 지난날의 (c)는 오늘 주수 × 그날 NAV라, 설정·환매로 주수가 바뀐 날에는 맞지 않는다."
    )
    return lines


async def probe_nav(
    settings: Settings, now: datetime, out: Out = print, products: Sequence[Product] = PRODUCTS
) -> tuple[int, int]:
    """Ask each query for each product, print the report; the calls made, and how many failed
    (KIS's errors, and answers without what was asked for)."""
    # The cached token while it is valid: no early refresh beside the running collector.
    settings = replace(settings, token_refresh_margin_seconds=0.0)
    calendar = calendar_store(settings).calendar
    today = now.astimezone(KST).date()
    latest = latest_trading_day(calendar, now)
    previous = calendar.previous_trading_day(latest)
    by_symbol = Counter(p.underlying for p in products)
    out(
        f"KIS ETF 순자산 출처 확인 (#305) {now.astimezone(KST):%Y-%m-%d %H:%M} KST: 상품 {len(products)}종"
        f"({' · '.join(f'{s} {n}' for s, n in by_symbol.items())}), 상장 "
        + ", ".join(sorted({p.listed.isoformat() for p in products}))
    )
    out(legend(products))
    out(
        f"후보: (a) {PRICE.tr} {PRICE.title}의 순자산 총액 · (b) NAV 추이({DAILY.tr} {DAILY.title},"
        f" {TREND.tr} {TREND.title}, {MINUTES.tr} {MINUTES.title}) · (c) 상장 주수 × NAV(계산)."
        " KIS 예제 표에는 모두 실전계좌 조회다(모의계좌 없음)."
    )
    out(
        "시세 값은 없다(개수, 날짜, 시각, 필드 이름과 모양, 같음·다름만)."
        ' "값 있음" = 비어 있지 않고 0이 아님. 모양: int 정수 · dec 소수 · ± 음수 있음 · date · text'
        " · zero 모두 0 · empty 모두 빈 값 · missing 답에 없음."
    )
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    async with rest_session(timeout=timeout) as session:
        tokens, client = tokens_and_client(session, settings, lambda: datetime.now().astimezone())
        asker = _Asker(client)
        try:
            await tokens.get()
        except (KisError, aiohttp.ClientError, OSError, ValueError) as e:
            reason = kis_error(e) if isinstance(e, KisError) else f"error {type(e).__name__}"
            out("")
            out(f"멈춤: KIS 접근 토큰이 없다: {reason}")
            return 1, 1
        found = [await _ask_product(asker, product, today) for product in products]
    for section in (
        price_lines(found),
        daily_lines(found, calendar),
        trend_lines(found),
        minute_lines(found),
        timing_lines(found, now, latest, previous),
        agreement_lines(found),
    ):
        out("")
        for line in section:
            out(line)
    out("")
    out(f"KIS: {asker.calls}번 조회, {asker.failed}번 실패")
    return asker.calls, asker.failed
