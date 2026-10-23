"""``krx-quant probe-krx-etf`` (#305): what the KRX Open API's ETF daily trading service
(``etf_bydd_trd``, ETF 일별매매정보) gives for the single-stock leveraged and inverse ETFs, before
the engine stores any of it (CODING_STANDARDS.md, "External data is probed before it is used").

The closing flow's demand of a product is A·(L² − L)·r, with A its net assets on the previous
trading day (#305). KIS gives the net asset total and the listed units for today only
(``probe-nav-305``, 2026-10-21), so the user chose KRX's service for the days before (#305,
2026-10-22). What its service page and FAQ (openapi.krx.co.kr, read 2026-10-22) say, none of it
measured yet:

- ``GET {KRX_OPENAPI_URL}/svc/apis/etp/etf_bydd_trd.json?basDd=YYYYMMDD``, the key in an
  ``AUTH_KEY`` header; one call gives every ETF listed that day, in the block ``OutBlock_1``;
- the fields ``DOCUMENTED``, every one a string, ``-`` when there is none: ``ISU_CD`` the short
  code, ``NAV``, ``MKTCAP``, ``TDD_CLSPRC``, ``INVSTASST_NETASST_TOTAMT`` (순자산총액) and
  ``LIST_SHRS`` (상장좌수, the stock master's for that day);
- days from 2010-01-04; a day from 08:00 on the next business day; 10,000 calls a key a day and
  no per-second limit; 401 for a key without the service approved, 403 for a wrong address.

For the products of ``products.PRODUCTS`` it asks the trading day before the listing, the listing
day (2026-05-27), the first trading day of each week after it and the last ``LAST_DAYS`` trading
days (about 30 calls), reading every field that comes back and assuming none, and says:

1. how each day answered: the body's keys, the rows, ``BAS_DD`` against the day asked, the
   errors (with what KRX's FAQ says a 401, 403 or 429 means);
2. the fields and their looks (``probe_fields.flags``; thousands commas and ``-`` counted
   apart), against the page's list, and how many product rows have a value in each wanted one;
3. which field carries the product codes, and how it writes them;
4. whether each product has a row on the listing day and every trading day asked after it;
5. when a day is in: the latest trading day and the one before, at the time asked;
6. whether ``LIST_SHRS`` changes between the days asked (next trading days, and further apart),
   and whether ``MKTCAP ÷ TDD_CLSPRC`` equals it;
7. whether ``INVSTASST_NETASST_TOTAMT`` equals ``LIST_SHRS × NAV``, and in which unit;
8. the same days against KIS: ``NAV`` against ``FHPST02440200``'s ``nav`` on each day asked, and
   the latest days' net assets, units and NAVs against ``FHPST02400000``'s ``etf_ntas_ttam``,
   ``lstn_stcn``, ``prdy_last_nav`` and ``nav``.

KRX's terms allow non-commercial use only and forbid giving the data to anyone else, so the
report carries no value (no price, NAV, amount or number of units), only counts, dates, field
names and looks, same or different: it goes on the public issue (the scheduled job
``probe-krx-etf-305``). The key goes in the header only, never in the address or the output.
KRX calls are spaced ``PACE`` apart; KIS's ``KIS_PACE``, with the cached access token while it
is valid. Nothing more is asked after ``BUDGET``.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from time import monotonic
from typing import Any

import aiohttp

from krx_quant.auth import tokens_and_client
from krx_quant.config import Settings
from krx_quant.feeds.base import KST
from krx_quant.kis import KisError, RestClient, rest_session
from krx_quant.market_calendar import MarketCalendar, calendar_store
from krx_quant.nav_probe import (
    DAILY,
    DAILY_DATE,
    PRICE,
    UNITS,
    Query,
    grouped,
    latest_trading_day,
    legend,
    one_object,
    row_list,
)
from krx_quant.probe_fields import day_of, describe, flags
from krx_quant.products import PRODUCTS, Product
from krx_quant.redaction import MASK
from krx_quant.supply_probe import REQUEST_TIMEOUT, kis_error

Out = Callable[[str], None]

SERVICE = "etf_bydd_trd"  # ETF 일별매매정보
PATH = f"/svc/apis/etp/{SERVICE}.json"
KEY_HEADER = "AUTH_KEY"
DAY_PARAM = "basDd"
PACE = 1.0  # seconds between KRX calls: KRX sets no per-second limit (FAQ); asked gently anyway
KIS_PACE = 0.5  # as probe-nav's
LAST_DAYS = 5  # the last trading days asked one after another: day-to-day changes, and timing
MAX_FAILED_IN_A_ROW = 3  # KRX: a key it refuses is refused for every day
KIS_FAILED_IN_A_ROW = 10
KIS_PAGES = 3  # KIS's daily NAV calls a product, at most (100 days a call)
BUDGET = timedelta(minutes=10)  # then nothing more is asked: the job's limit is 15 minutes
NAV_TOLERANCE = 0.005 + 1e-9  # half the second decimal (the page's NAV format is ###0.00)
TOLERANCE = 0.005  # the share two totals may differ by and still be the same
# The fields the service page lists, in its order (OutBlock_1, read 2026-10-22).
DOCUMENTED = (
    "BAS_DD",
    "ISU_CD",
    "ISU_NM",
    "TDD_CLSPRC",
    "CMPPREVDD_PRC",
    "FLUC_RT",
    "NAV",
    "TDD_OPNPRC",
    "TDD_HGPRC",
    "TDD_LWPRC",
    "ACC_TRDVOL",
    "ACC_TRDVAL",
    "MKTCAP",
    "INVSTASST_NETASST_TOTAMT",
    "LIST_SHRS",
    "IDX_IND_NM",
    "OBJ_STKPRC_IDX",
    "CMPPREVDD_IDX",
    "FLUC_RT_IDX",
)
BLOCK = "OutBlock_1"
DAY_FIELD = "BAS_DD"
NAV, TOTAL, SHARES, MKTCAP, CLOSE = "NAV", "INVSTASST_NETASST_TOTAMT", "LIST_SHRS", "MKTCAP", "TDD_CLSPRC"
WANTED = (NAV, TOTAL, SHARES, MKTCAP, CLOSE)
# What KRX's FAQ says each refusal means.
HINTS = {
    401: "KRX FAQ: 인증키만 받고 이 서비스를 이용 신청·승인받지 않았거나, 갱신 전 인증키, 오타·공백",
    403: "KRX FAQ: 주소·인증키의 오타, 샘플 경로(/svc/sample/apis), http",
    429: "KRX FAQ: 인증키의 하루 한도 10,000번을 넘었다, 자정에 초기화",
}
_GROUPED_NUMBER = re.compile(r"[+-]?\d{1,3}(,\d{3})+(\.\d+)?")
_SHORT = 60  # an error body's text values shown up to this length


def _ymd(day: date) -> str:
    return day.strftime("%Y%m%d")


def _clean(value: Any) -> str:
    """A KRX value as ``probe_fields`` reads it: ``-`` (none) as empty, a number's thousands
    commas dropped."""
    text = "" if value is None else str(value).strip()
    if text == "-":
        return ""
    return text.replace(",", "") if _GROUPED_NUMBER.fullmatch(text) else text


def number(value: Any) -> float | None:
    """A KRX value as a number; None when there is none (empty, ``-``), it is 0 (no figure, as
    KIS's 0, #295) or it is not a number."""
    text = _clean(value)
    if not text:
        return None
    try:
        found = float(text)
    except ValueError:
        return None
    return found if found != 0 and math.isfinite(found) else None


def code_form(value: Any, codes: frozenset[str]) -> tuple[str, str] | None:
    """(the product code, how the value writes it) if ``value`` is one of ``codes``: as it is, with
    an ``A`` before it (KRX's short code) or inside a 12-character standard code (ISIN)."""
    text = str(value or "").strip()
    if text in codes:
        return text, "6자리 그대로"
    if text[:1] == "A" and text[1:] in codes:
        return text[1:], "A 붙음"
    if len(text) == 12 and text[:2].isalpha() and text[3:9] in codes:
        return text[3:9], "12자리 표준코드 안"
    return None


def _by_unit() -> Counter[str]:
    """Counts by unit (``unit_of``), then the different and the missing, all shown."""
    return Counter[str]({name: 0 for name in (*(unit for unit, _ in UNITS), "다름", "견줄 것 없음")})


def unit_of(small: float, big: float) -> str:
    """The unit (``nav_probe.UNITS``) ``small`` gives ``big`` in: within ``TOLERANCE`` of it, or half
    the unit; "다름" if none."""
    for name, scale in UNITS:
        if abs(small * scale - big) <= max(TOLERANCE * abs(big), scale / 2):
            return name
    return "다름"


@dataclass
class Answer:
    """One day's answer: its rows (None: no usable answer), the body's keys, or the error."""

    day: date
    rows: list[dict[str, Any]] | None = None
    keys: tuple[str, ...] = ()
    error: str | None = None
    products: dict[str, dict[str, Any]] = field(default_factory=dict)  # code -> its row
    where: dict[str, tuple[str, str]] = field(default_factory=dict)  # code -> (field, form)
    duplicates: int = 0  # product rows of a code already found that day

    @property
    def has_rows(self) -> bool:
        """An answer with rows: the day is in."""
        return bool(self.rows)


def _summary(body: Any) -> str:
    """An error body without its values that are long or not text: its keys, and the short ones."""
    if not isinstance(body, dict):
        return type(body).__name__
    parts = []
    for key, value in body.items():
        if isinstance(value, (str, int)) and len(str(value)) <= _SHORT:
            parts.append(f"{key} {value}")
        else:
            parts.append(str(key))
    return ", ".join(parts) or "빈 객체"


def read_answer(day: date, status: int, text: str) -> Answer:
    """What a call's status and body say: the rows of its list (``OutBlock_1``, else the first), an
    empty answer, or why it is no usable one."""
    try:
        body = json.loads(text) if text.strip() else None
    except ValueError:
        body = None
        parsed = False
    else:
        parsed = body is not None
    if status != 200:
        hint = f" ({HINTS[status]})" if status in HINTS else ""
        shown = f", 본문: {_summary(body)}" if parsed else f", 본문 {len(text)}자"
        return Answer(day, error=f"HTTP {status}{hint}{shown}")
    if not parsed:
        return Answer(day, error=f"HTTP 200, JSON이 아니다(본문 {len(text)}자)")
    if isinstance(body, list):
        return Answer(day, rows=[r for r in body if isinstance(r, dict)], keys=("(본문이 목록)",))
    if not isinstance(body, dict):
        return Answer(day, error=f"HTTP 200, 읽을 것이 없다({type(body).__name__})")
    keys = tuple(body)
    lists = [k for k, v in body.items() if isinstance(v, list)]
    if not lists:
        return Answer(day, keys=keys, error=f"HTTP 200, 행 목록이 없다(본문: {_summary(body)})")
    key = BLOCK if BLOCK in lists else lists[0]
    return Answer(day, rows=[r for r in body[key] if isinstance(r, dict)], keys=keys)


def find_products(answer: Answer, products: Sequence[Product]) -> None:
    """Each product's row in ``answer``, found in any field (``code_form``)."""
    codes = frozenset(p.code for p in products)
    for row in answer.rows or ():
        for name, value in row.items():
            found = code_form(value, codes)
            if found is None:
                continue
            code, form = found
            if code in answer.products:
                answer.duplicates += 1
            else:
                answer.products[code] = row
                answer.where[code] = (name, form)
            break


class _Calls:
    """Calls to one source: spaced, counted, stopped after failures in a row or the deadline."""

    def __init__(self, pace: float, max_failed: int, deadline: float) -> None:
        self._pace = pace
        self._max_failed = max_failed
        self._deadline = deadline
        self.calls = 0
        self.failed = 0
        self._failed_in_a_row = 0

    async def turn(self) -> str | None:
        """Wait for the next call's turn; why not to call, if so."""
        if self._failed_in_a_row >= self._max_failed:
            return f"묻지 않음: {self._max_failed}번 잇달아 실패"
        if monotonic() > self._deadline:
            return f"묻지 않음: {int(BUDGET.total_seconds() // 60)}분이 지났다"
        if self.calls:
            await asyncio.sleep(self._pace)
        self.calls += 1
        return None

    def answered(self, ok: bool) -> None:
        if ok:
            self._failed_in_a_row = 0
        else:
            self.failed += 1
            self._failed_in_a_row += 1


class Krx:
    """The service's calls, the key in the header (never in the address)."""

    def __init__(self, session: aiohttp.ClientSession, settings: Settings, deadline: float) -> None:
        assert settings.krx_openapi_key
        self._session = session
        self._url = f"{settings.krx_openapi_url}{PATH}"
        self._key = settings.krx_openapi_key
        self.calls = _Calls(PACE, MAX_FAILED_IN_A_ROW, deadline)

    async def ask(self, day: date) -> Answer:
        why_not = await self.calls.turn()
        if why_not is not None:
            return Answer(day, error=why_not)
        try:
            async with self._session.get(
                self._url,
                params={DAY_PARAM: _ymd(day)},
                headers={KEY_HEADER: self._key},
                allow_redirects=False,
            ) as resp:
                status, text = resp.status, await resp.text(errors="replace")
        except (aiohttp.ClientError, OSError, ValueError) as e:  # OSError: timeouts too
            answer = Answer(day, error=f"error {type(e).__name__}")
        else:
            answer = read_answer(day, status, text)
        if answer.error is not None:
            answer.error = answer.error.replace(self._key, MASK)  # never shown, should KRX echo it
        self.calls.answered(answer.rows is not None)
        return answer


@dataclass
class KisFound:
    """What KIS said of one product: its current price's object, its daily NAV rows by day."""

    product: Product
    price: dict[str, Any] | None = None
    price_error: str | None = None
    daily: dict[date, Mapping[str, Any]] = field(default_factory=dict)
    daily_error: str | None = None


class Kis:
    """KIS's calls, as ``probe-nav`` asks them."""

    def __init__(self, client: RestClient, deadline: float) -> None:
        self._client = client
        self.calls = _Calls(KIS_PACE, KIS_FAILED_IN_A_ROW, deadline)

    async def ask(
        self, query: Query, params: dict[str, str], parse: Callable[[Mapping[str, Any]], Any]
    ) -> tuple[Any, str | None]:
        """``parse``'s reading of the answer, or the error; an answer it reads as None fails (#297)."""
        why_not = await self.calls.turn()
        if why_not is not None:
            return None, why_not
        try:
            body, _ = await self._client.get_page(query.tr, query.path, params)
        except KisError as e:
            error = kis_error(e)
        except (aiohttp.ClientError, OSError, ValueError) as e:
            error = f"error {type(e).__name__}"
        else:
            value = parse(body)
            if value is not None:
                self.calls.answered(True)
                return value, None
            error = f"OK, 읽을 것이 없다(본문 키: {', '.join(sorted(body))})"
        self.calls.answered(False)
        return None, error

    async def product(self, product: Product, first: date, today: date) -> KisFound:
        found = KisFound(product)
        codes = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": product.code}
        found.price, found.price_error = await self.ask(PRICE, dict(codes), one_object)
        end = today
        for _ in range(KIS_PAGES):
            params = {**codes, "FID_INPUT_DATE_1": _ymd(first), "FID_INPUT_DATE_2": _ymd(end)}
            rows, error = await self.ask(DAILY, params, row_list)
            if error is not None:
                found.daily_error = error if not found.daily else f"{error}, {len(found.daily)}행 뒤"
                break
            page = {
                d: row
                for row in rows
                if (d := day_of(row, DAILY_DATE)) is not None and d <= end and d not in found.daily
            }
            if not page:
                break
            found.daily.update(page)
            end = min(page) - timedelta(days=1)
            if end < first:
                break
        return found


def asked_days(calendar: MarketCalendar, listed: date, latest: date) -> tuple[date, list[date]]:
    """The trading day before ``listed``, and the days asked from it: ``listed``, the first trading
    day of each week after it, and the last ``LAST_DAYS`` trading days to ``latest``."""
    before = calendar.previous_trading_day(listed)
    days = calendar.trading_days(listed, latest) or [listed, latest]
    chosen = {before, listed, *days[-LAST_DAYS:]}
    weeks: set[tuple[int, int]] = set()
    for day in days:
        iso = day.isocalendar()
        if (iso.year, iso.week) not in weeks:
            weeks.add((iso.year, iso.week))
            chosen.add(day)
    return before, sorted(chosen)


def _counts(counter: Mapping[str, int]) -> str:
    return " · ".join(f"{k} {n}" for k, n in counter.items()) if counter else "없음"


def _dates(days: Sequence[date], most: int = 5) -> str:
    shown = ", ".join(d.isoformat() for d in days[:most])
    return shown + (" 외" if len(days) > most else "")


def answer_lines(answers: Sequence[Answer]) -> list[str]:
    """1: how each day answered."""
    lines = [f"1. 날마다의 답 (KRX {SERVICE}, {PACE:g}초 간격)"]
    ok = [a for a in answers if a.has_rows]
    empty = [a.day for a in answers if a.rows is not None and not a.rows]
    failed = [a for a in answers if a.rows is None]
    head = f"  행 있음 {len(ok)}일 · 빈 답(행 0) {len(empty)}일"
    if empty:
        head += f"({_dates(empty)})"
    lines.append(head + f" · 실패 {len(failed)}일")
    errors: dict[str, list[date]] = {}
    for a in failed:
        errors.setdefault(str(a.error), []).append(a.day)
    for error, days in errors.items():
        lines.append(f"  실패 {len(days)}일({_dates(days)}): {error}")
    keys = Counter(", ".join(a.keys) for a in answers if a.rows is not None)
    lines.append(f"  본문 키: {_counts(keys)}")
    if ok:
        sizes = sorted(len(a.rows or ()) for a in ok)
        lines.append(f"  행 수(그날 답한 모든 ETF): 가장 적은 날 {sizes[0]} · 가장 많은 날 {sizes[-1]}")
    keyed = Counter(_keyed(row, a.day) for a in ok for row in a.products.values())
    lines.append(f"  상품 행의 {DAY_FIELD} = 물은 날: {_counts(keyed)}")
    return lines


def _keyed(row: Mapping[str, Any], day: date) -> str:
    """Whether ``row``'s date field is the day asked."""
    if DAY_FIELD not in row:
        return "필드 없음"
    on = day_of(row, DAY_FIELD)
    if on is None:
        return "날짜 아님"
    return "같음" if on == day else "다름"


def field_lines(answers: Sequence[Answer]) -> list[str]:
    """2: the fields, their looks, against the page's list; the wanted fields' values counted."""
    product_rows = [row for a in answers for row in a.products.values()]
    rows = product_rows or [row for a in answers for row in a.rows or ()]
    which = f"상품 행 {len(rows)}개" if product_rows else f"상품 행이 없어 모든 행 {len(rows)}개"
    lines = [f"2. 필드 ({which})"]
    if not rows:
        lines.append("  행이 없다")
        return lines
    names = list(dict.fromkeys(name for row in rows for name in row))
    missing = [n for n in DOCUMENTED if n not in names]
    extra = [n for n in names if n not in DOCUMENTED]
    lines.append(
        f"  서비스 페이지의 {len(DOCUMENTED)}개와: 답에 있음 {len(DOCUMENTED) - len(missing)}"
        f" · 답에 없음 {len(missing)}"
        + (f"({', '.join(missing)})" if missing else "")
        + f" · 페이지에 없는 필드 {len(extra)}"
        + (f"({', '.join(extra)})" if extra else "")
    )
    lines.append(f"  모양: {describe(flags([{k: _clean(v) for k, v in row.items()} for row in rows]))}")
    commas = Counter(
        n for row in rows for n, v in row.items() if _GROUPED_NUMBER.fullmatch(str(v or "").strip())
    )
    dashes = Counter(n for row in rows for n, v in row.items() if str(v or "").strip() == "-")
    lines.append(f"  천 단위 쉼표가 있는 필드(행 수): {_counts(commas)}")
    lines.append(f'  "-"인 필드(행 수): {_counts(dashes)}')
    lines.append(
        f"  값 있음({len(rows)}행 중): "
        + " · ".join(f"{name} {sum(number(row.get(name)) is not None for row in rows)}" for name in WANTED)
    )
    return lines


def code_lines(answers: Sequence[Answer], products: Sequence[Product]) -> list[str]:
    """3: where the product codes are, and how they are written."""
    lines = ["3. 종목코드"]
    where: dict[str, Counter[tuple[str, str]]] = {}
    for a in answers:
        for code, place in a.where.items():
            where.setdefault(code, Counter())[place] += 1
    places = Counter(f"{c.most_common(1)[0][0][0]} {c.most_common(1)[0][0][1]}" for c in where.values())
    lines.append(f"  찾은 곳(상품 수): {_counts(places)}")
    mixed = sorted(code for code, c in where.items() if len(c) > 1)
    if mixed:
        lines.append(f"  날마다 다른 곳에 있는 상품: {', '.join(mixed)}")
    absent = [p.code for p in products if p.code not in where]
    lines.append("  찾지 못한 상품: " + (f"{', '.join(absent)} ({len(absent)}종)" if absent else "없음"))
    lines.append(f"  같은 날 같은 코드의 두 번째 행: {sum(a.duplicates for a in answers)}")
    return lines


def coverage_lines(answers: Sequence[Answer], products: Sequence[Product], before: date) -> list[str]:
    """4: each product's rows from its listing day on, over the days that answered with rows."""
    answered = [a for a in answers if a.has_rows]
    first = min(p.listed for p in products)
    after = [a for a in answered if a.day >= first]
    lines = [f"4. 상장일부터 행이 있나 (상장일과 그 뒤 물은 거래일 중 행이 온 {len(after)}일)"]
    texts: list[tuple[str, str]] = []
    for p in products:
        days = [a for a in after if a.day >= p.listed]
        missing = [a.day for a in days if p.code not in a.products]
        text = f"{len(days) - len(missing)}/{len(days)}일"
        if missing:
            text += f" · 없는 날 {_dates(missing)}"
        listed = next((a for a in days if a.day == p.listed), None)
        if listed is None:
            text += " · 상장일 답 없음"
        elif p.code not in listed.products:
            text += " · 상장일 행 없음"
        texts.append((p.code, text))
    lines += grouped(texts)
    on_before = next((a for a in answers if a.day == before), None)
    if on_before is None or on_before.rows is None:
        lines.append(f"  상장 전 거래일({before}): 답 없음")
    else:
        lines.append(f"  상장 전 거래일({before}): 상품 행 {len(on_before.products)}종")
    return lines


def timing_lines(answers: Sequence[Answer], now: datetime, latest: date, previous: date) -> list[str]:
    """5: whether the latest trading day and the one before are in, at the time asked."""
    asked = now.astimezone(KST).strftime("%Y-%m-%d %H:%M")
    lines = [f"5. 그날 값이 언제 나오나({asked} KST에 물은 것, 마지막 거래일 {latest}, 그 전 {previous})"]
    by_day = {a.day: a for a in answers}
    for title, day in (("마지막 거래일", latest), ("그 전 거래일", previous)):
        a = by_day.get(day)
        if a is None:
            text = "묻지 않음"
        elif a.rows is None:
            text = f"실패: {a.error}"
        elif not a.rows:
            text = "빈 답(행 0)"
        else:
            text = f"행 있음 · 상품 {len(a.products)}종"
        lines.append(f"  {title} {day}: {text}")
    lines.append(
        "  KRX FAQ: 영업일의 값은 다음 영업일 08:00부터 준다(금요일 것은 월요일 08:00). 이 한 번은 물은 때의"
        " 모습만 본다."
    )
    return lines


def _shares(row: Mapping[str, Any] | None) -> float | None:
    return None if row is None else number(row.get(SHARES))


def shares_lines(
    answers: Sequence[Answer], products: Sequence[Product], calendar: MarketCalendar
) -> list[str]:
    """6: whether the listed units change between the days asked; market cap ÷ close against them."""
    lines = [f"6. {SHARES}(상장좌수)가 날마다 바뀌나 (상품마다 행이 있는 물은 날의 이웃한 두 날)"]
    answered = sorted((a for a in answers if a.has_rows), key=lambda a: a.day)
    pairs = {"이웃한 거래일": Counter[str](), "사이에 거래일이 있는 날": Counter[str]()}
    changed: list[str] = []
    for p in products:
        days = [a for a in answered if p.code in a.products]
        moved = False
        for one, two in zip(days, days[1:], strict=False):
            next_day = calendar.trading_days(one.day, two.day) == [one.day, two.day]
            kind = "이웃한 거래일" if next_day else "사이에 거래일이 있는 날"
            earlier, later = _shares(one.products[p.code]), _shares(two.products[p.code])
            if earlier is None or later is None:
                pairs[kind]["견줄 것 없음"] += 1
            elif earlier == later:
                pairs[kind]["같음"] += 1
            else:
                pairs[kind]["바뀜"] += 1
                moved = True
        if moved:
            changed.append(p.code)
    for kind, counts in pairs.items():
        lines.append(
            f"  {kind}: 짝 {sum(counts.values())} · 바뀜 {counts['바뀜']} · 같음 {counts['같음']}"
            f" · 견줄 것 없음 {counts['견줄 것 없음']}"
        )
    lines.append(
        f"  한 번이라도 바뀐 상품: {len(changed)}종" + (f"({', '.join(changed)})" if changed else "")
    )
    check = Counter[str]({"정확히 같음": 0, f"{TOLERANCE:.1%} 안": 0, "다름": 0, "견줄 것 없음": 0})
    for a in answered:
        for row in a.products.values():
            cap, close, shares = number(row.get(MKTCAP)), number(row.get(CLOSE)), _shares(row)
            if cap is None or close is None or shares is None:
                check["견줄 것 없음"] += 1
            elif cap / close == shares:
                check["정확히 같음"] += 1
            elif abs(cap / close - shares) <= TOLERANCE * shares:
                check[f"{TOLERANCE:.1%} 안"] += 1
            else:
                check["다름"] += 1
    lines.append(f"  {MKTCAP} ÷ {CLOSE} = {SHARES} (상품 행): {_counts(check)}")
    return lines


def total_lines(answers: Sequence[Answer]) -> list[str]:
    """7: the net asset total against the listed units × the NAV, row by row."""
    lines = [
        f"7. {TOTAL} = {SHARES} × {NAV}"
        f" (상품 행. 같음 = 단위를 맞춰 {TOLERANCE:.1%} 안, 또는 그 단위의 반 안)"
    ]
    counts = _by_unit()
    for a in answers:
        for row in a.products.values():
            total, shares, nav = number(row.get(TOTAL)), _shares(row), number(row.get(NAV))
            if total is None or shares is None or nav is None:
                counts["견줄 것 없음"] += 1
                continue
            counts[unit_of(total, shares * nav)] += 1
    lines.append(f"  {_counts(counts)}")
    return lines


def _same(a: Any, b: Any, tolerance: float = 0.0) -> bool | None:
    x, y = number(a), number(b)
    if x is None or y is None:
        return None
    return abs(x - y) <= tolerance


def _same_counts(results: Sequence[bool | None]) -> str:
    return (
        f"같음 {sum(r is True for r in results)} · 다름 {sum(r is False for r in results)}"
        f" · 견줄 것 없음 {sum(r is None for r in results)}"
    )


def kis_lines(
    answers: Sequence[Answer], found: Sequence[KisFound], latest: date, previous: date
) -> list[str]:
    """8: KRX against KIS on the same days."""
    lines = [
        f"8. KIS와 같은 날 (상품마다 {PRICE.tr} {PRICE.title} 1번, {DAILY.tr} {DAILY.title}"
        f" 상장 전 거래일부터 오늘까지 100행씩)"
    ]
    errors = [(f.product.code, f"{PRICE.tr} {f.price_error}") for f in found if f.price_error]
    errors += [(f.product.code, f"{DAILY.tr} {f.daily_error}") for f in found if f.daily_error]
    navs = Counter[str]({"같음": 0, "다름": 0, "KIS에 그날 없음": 0, "값 없음": 0})
    differ: list[str] = []
    for f in found:
        for a in answers:
            row = a.products.get(f.product.code)
            if row is None:
                continue
            kis_row = f.daily.get(a.day)
            if kis_row is None:
                navs["KIS에 그날 없음"] += 1
                continue
            same = _same(row.get(NAV), kis_row.get("nav"), NAV_TOLERANCE)
            navs["값 없음" if same is None else "같음" if same else "다름"] += 1
            if same is False:
                differ.append(f"{f.product.code} {a.day}")
    lines.append(f"  {DAILY.tr} nav = KRX {NAV}(물은 날마다, 소수 둘째 자리의 반 안): {_counts(navs)}")
    if differ:
        lines.append(f"  다른 곳: {', '.join(differ[:5])}{' 외' if len(differ) > 5 else ''}")
    by_day = {a.day: a for a in answers}
    for title, day in (("그 전 거래일", previous), ("마지막 거래일", latest)):
        on = by_day.get(day)
        if on is None or not on.products:
            lines.append(f"  {PRICE.tr}(물은 때의 값)과 KRX {title} {day}: KRX 상품 행 없음")
            continue
        lines.append(f"  {PRICE.tr}(물은 때의 값)과 KRX {title} {day}:")
        units = _by_unit()
        shares: list[bool | None] = []
        prdy: list[bool | None] = []
        today: list[bool | None] = []
        for f in found:
            row, price = on.products.get(f.product.code), f.price
            if row is None or price is None:
                units["견줄 것 없음"] += 1
                shares.append(None)
                prdy.append(None)
                today.append(None)
                continue
            kis_total, krx_total = number(price.get("etf_ntas_ttam")), number(row.get(TOTAL))
            units[
                "견줄 것 없음" if kis_total is None or krx_total is None else unit_of(kis_total, krx_total)
            ] += 1
            shares.append(_same(price.get("lstn_stcn"), row.get(SHARES)))
            prdy.append(_same(price.get("prdy_last_nav"), row.get(NAV), NAV_TOLERANCE))
            today.append(_same(price.get("nav"), row.get(NAV), NAV_TOLERANCE))
        lines.append(f"    etf_ntas_ttam = {TOTAL}: {_counts(units)}")
        lines.append(f"    lstn_stcn = {SHARES}: {_same_counts(shares)}")
        lines.append(f"    prdy_last_nav = {NAV}: {_same_counts(prdy)}")
        lines.append(f"    nav = {NAV}: {_same_counts(today)}")
    if errors:
        lines.append("  KIS 실패:")
        lines += grouped(errors)
    return lines


async def _ask_kis(
    session: aiohttp.ClientSession,
    settings: Settings,
    products: Sequence[Product],
    first: date,
    today: date,
    deadline: float,
) -> tuple[list[KisFound], Kis | None, str | None]:
    """KIS's answers for each product; or why it was not asked."""
    if not (settings.kis_app_key and settings.kis_app_secret):
        return [], None, "KIS_APP_KEY·KIS_APP_SECRET이 없다"
    # The cached token while it is valid: no early refresh beside the running collector.
    settings = replace(settings, token_refresh_margin_seconds=0.0)
    tokens, client = tokens_and_client(session, settings, lambda: datetime.now().astimezone())
    try:
        await tokens.get()
    except (KisError, aiohttp.ClientError, OSError, ValueError) as e:
        reason = kis_error(e) if isinstance(e, KisError) else f"error {type(e).__name__}"
        return [], None, f"KIS 접근 토큰이 없다: {reason}"
    kis = Kis(client, deadline)
    return [await kis.product(p, first, today) for p in products], kis, None


async def probe_krx_etf(
    settings: Settings, now: datetime, out: Out = print, products: Sequence[Product] = PRODUCTS
) -> tuple[int, int]:
    """Ask the service for the days, and KIS for the products; print the report. The KRX calls
    made, and the days that answered with rows."""
    calendar = calendar_store(settings).calendar
    latest = latest_trading_day(calendar, now)
    previous = calendar.previous_trading_day(latest)
    first = min(p.listed for p in products)
    before, days = asked_days(calendar, first, latest)
    by_symbol = Counter(p.underlying for p in products)
    out(
        f"KRX Open API ETF 일별매매정보 확인 (#305) {now.astimezone(KST):%Y-%m-%d %H:%M} KST:"
        f" 상품 {len(products)}종({' · '.join(f'{s} {n}' for s, n in by_symbol.items())}), 상장 "
        + ", ".join(sorted({p.listed.isoformat() for p in products}))
    )
    out(legend(products))
    out(
        f"조회: GET {PATH}?{DAY_PARAM}=<날>, 인증키는 {KEY_HEADER} 헤더(주소와 출력에는 없다)."
        f" 서비스 페이지(2026-10-22에 읽음): 한 번에 그날의 모든 ETF, 블록 {BLOCK}, 필드 {len(DOCUMENTED)}개,"
        ' 값 없음은 "-". 서버에서 잰 것은 아래가 처음이다.'
    )
    last = days[-LAST_DAYS:]
    out(
        f"물은 날 {len(days)}일: 상장 전 거래일 {before} · 상장일 {first} · 그 뒤 주마다 첫 거래일"
        f" · 마지막 {len(last)}거래일 {last[0]}..{last[-1]}"
    )
    out(
        "시세 값은 없다(개수, 날짜, 필드 이름과 모양, 같음·다름만. KRX 약관: 비상업 이용, 제3자 제공 금지)."
        ' "값 있음" = 비어 있지 않고 "-"도 0도 아님. 모양: int 정수 · dec 소수 · ± 음수 있음 · date · text'
        ' · zero 모두 0 · empty 모두 빈 값("-" 포함) · missing 답에 없음.'
    )
    if not settings.krx_openapi_key:
        out("")
        out('멈춤: KRX_OPENAPI_KEY가 없다(서버 .env에 넣는다, docs/operations.md "KRX Open API (#305)")')
        return 0, 0
    deadline = monotonic() + BUDGET.total_seconds()
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    async with rest_session(timeout=timeout) as session:
        krx = Krx(session, settings, deadline)
        answers = [await krx.ask(day) for day in days]
        for answer in answers:
            find_products(answer, products)
        found: list[KisFound] = []
        kis: Kis | None = None
        why_not: str | None = "KRX에 상품 행이 없다"
        if any(a.products for a in answers):
            found, kis, why_not = await _ask_kis(
                session, settings, products, before, now.astimezone(KST).date(), deadline
            )
    sections = [
        answer_lines(answers),
        field_lines(answers),
        code_lines(answers, products),
        coverage_lines(answers, products, before),
        timing_lines(answers, now, latest, previous),
        shares_lines(answers, products, calendar),
        total_lines(answers),
        kis_lines(answers, found, latest, previous)
        if why_not is None
        else [f"8. KIS와 같은 날: 건너뜀({why_not})"],
    ]
    for section in sections:
        out("")
        for line in section:
            out(line)
    with_rows = sum(a.has_rows for a in answers)
    out("")
    tail = f"KRX: {krx.calls.calls}번 조회, {krx.calls.failed}번 실패, 행이 있는 날 {with_rows}"
    if kis is not None:
        tail += f" · KIS: {kis.calls.calls}번 조회, {kis.calls.failed}번 실패"
    out(tail)
    return krx.calls.calls, with_rows
