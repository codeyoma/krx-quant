"""``krx-quant probe-supply``: where KIS gives the eight daily supply indicators (#226).

Before the engine keeps daily supply data for the chart (#226: short selling, the net buys of
individuals, institutions and foreigners, holdings, the foreign ownership ratio, the stock
lending and credit balances), the KIS queries the survey found (docs/kis-feeds.md, "일별
수급") are asked on the server, read-only and beside the collector:

- for each configured stock and one ETF, the latest day and a day about a year back: OK or
  KIS's error, the rows one call gives and the days they span, in which order;
- which fields an answer carries: each empty, zero, or whole numbers (counts, amounts) or
  decimals (ratios), and whether some are negative;
- how far back each dated query goes for the first stock: paging back a bounded number of
  calls, then bisecting for the oldest day if it still goes on; and KIS's continuation
  header (``tr_cont``) where it sends one;
- whether the sources agree on the days they share (counts of equal values only).

It prints no market value: the report goes on a public issue (the scheduled job
``probe-supply-226``). Calls are spaced ``PACE`` seconds apart, under the REST limit the
collector shares, and the cached access token is used while it is valid (no early refresh).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any

import aiohttp

from krx_quant.auth import tokens_and_client
from krx_quant.config import Settings
from krx_quant.kis import KisError, RestClient

PACE = 0.5  # seconds between calls
WINDOW = timedelta(days=365)  # a ranged query asks this far back from its last day
YEAR = timedelta(days=365)  # "a year back"
MAX_PAGES = 25  # a history pages back at most this many calls, then bisects
EMPTY_WINDOWS = 3  # a ranged history goes on past this many empty windows in a row (a gap)
MAX_BISECT = 20
EARLIEST = date(1980, 1, 1)  # the bisection's floor
MAX_SYMBOLS = 5  # configured stocks asked
MAX_FAILED_IN_A_ROW = 20  # then KIS is down or refusing: the rest is not asked

Out = Callable[[str], None]
Query = Callable[[str, str, date], dict[str, str]]


def _ymd(day: date) -> str:
    return day.strftime("%Y%m%d")


def short_sale_query(market: str, symbol: str, end: date) -> dict[str, str]:
    return {
        "FID_COND_MRKT_DIV_CODE": market,
        "FID_INPUT_ISCD": symbol,
        "FID_INPUT_DATE_1": _ymd(end - WINDOW),
        "FID_INPUT_DATE_2": _ymd(end),
    }


def investor_daily_query(market: str, symbol: str, end: date) -> dict[str, str]:
    return {
        "FID_COND_MRKT_DIV_CODE": market,
        "FID_INPUT_ISCD": symbol,
        "FID_INPUT_DATE_1": _ymd(end),
        "FID_ORG_ADJ_PRC": "",
        "FID_ETC_CLS_CODE": "",
    }


def stock_query(market: str, symbol: str, end: date) -> dict[str, str]:
    """A query of today only (the date is not asked)."""
    return {"FID_COND_MRKT_DIV_CODE": market, "FID_INPUT_ISCD": symbol}


def daily_price_query(market: str, symbol: str, end: date) -> dict[str, str]:
    return {**stock_query(market, symbol, end), "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "1"}


def loan_query(market: str, symbol: str, end: date) -> dict[str, str]:
    return {
        "MRKT_DIV_CLS_CODE": market,
        "MKSC_SHRN_ISCD": symbol,
        "START_DATE": _ymd(end - WINDOW),
        "END_DATE": _ymd(end),
        "CTS": "",
    }


def credit_query(market: str, symbol: str, end: date) -> dict[str, str]:
    return {
        "FID_COND_MRKT_DIV_CODE": market,
        "FID_COND_SCR_DIV_CODE": "20476",
        "FID_INPUT_ISCD": symbol,
        "FID_INPUT_DATE_1": _ymd(end),
    }


@dataclass(frozen=True)
class Source:
    """A KIS query the survey found (docs/kis-feeds.md, "일별 수급 (#226)")."""

    tr: str
    path: str
    title: str  # KIS's name for it
    serves: str  # the indicators it would give
    wanted: tuple[str, ...]  # the fields that carry them
    markets: tuple[str, ...]  # all asked for the first stock's latest day; else the first
    query: Query  # (market, symbol, the last day asked) -> params
    takes_date: bool  # a past day can be asked: a year back, and paging back
    ranged: bool = False  # asks [end - WINDOW, end]; else the rows up to one day
    rows_key: str | None = "output2"  # where its daily rows are; None: one object (a snapshot)
    date_field: str = "stck_bsop_date"
    other_dates: tuple[str, ...] = ()  # other day fields whose span is reported


QUOTATIONS = "/uapi/domestic-stock/v1/quotations"
SHORT_SALE = Source(
    tr="FHPST04830000",
    path=f"{QUOTATIONS}/daily-short-sale",
    title="국내주식 공매도 일별추이",
    serves="일별 공매도 거래량",
    wanted=("ssts_cntg_qty", "ssts_vol_rlim"),
    markets=("J", "UN", "NX"),
    query=short_sale_query,
    takes_date=True,
    ranged=True,
)
INVESTOR_DAILY = Source(
    tr="FHPTJ04160001",
    path=f"{QUOTATIONS}/investor-trade-by-stock-daily",
    title="종목별 투자자매매동향(일별)",
    serves="일별 개인·기관·외국인 순매수량",
    wanted=("prsn_ntby_qty", "orgn_ntby_qty", "frgn_ntby_qty"),
    markets=("UN", "J", "NX"),
    query=investor_daily_query,
    takes_date=True,
)
INVESTOR = Source(
    tr="FHKST01010900",
    path=f"{QUOTATIONS}/inquire-investor",
    title="주식현재가 투자자",
    serves="일별 개인·기관·외국인 순매수량 (최근만)",
    wanted=("prsn_ntby_qty", "orgn_ntby_qty", "frgn_ntby_qty"),
    markets=("J", "NX", "UN"),
    query=stock_query,
    takes_date=False,
    rows_key="output",
)
DAILY_PRICE = Source(
    tr="FHKST01010400",
    path=f"{QUOTATIONS}/inquire-daily-price",
    title="주식현재가 일자별",
    serves="일별 외국인 지분율 (HTS 외국인 소진율, 최근 30거래일)",
    wanted=("hts_frgn_ehrt", "frgn_ntby_qty"),
    markets=("UN", "J", "NX"),
    query=daily_price_query,
    takes_date=False,
    rows_key="output",
)
PRICE = Source(
    tr="FHKST01010100",
    path=f"{QUOTATIONS}/inquire-price",
    title="주식현재가 시세",
    serves="외국인 보유수량·지분율 (오늘 한 값)",
    wanted=("frgn_hldn_qty", "hts_frgn_ehrt", "lstn_stcn"),
    markets=("UN", "J"),
    query=stock_query,
    takes_date=False,
    rows_key=None,
)
LOAN = Source(
    tr="HHPST074500C0",
    path=f"{QUOTATIONS}/daily-loan-trans",
    title="종목별 일별 대차거래추이",
    serves="일별 대차잔고 수량",
    wanted=("rmnd_stcn",),
    markets=("3",),  # MRKT_DIV_CLS_CODE 3: one stock (1 KOSPI, 2 KOSDAQ)
    query=loan_query,
    takes_date=True,
    ranged=True,
    rows_key="output1",
    date_field="bsop_date",
)
CREDIT = Source(
    tr="FHPST04760000",
    path=f"{QUOTATIONS}/daily-credit-balance",
    title="국내주식 신용잔고 일별추이",
    serves="일별 신용잔고 수량",
    wanted=("whol_loan_rmnd_stcn", "whol_loan_rmnd_rate"),
    markets=("J", "UN"),
    query=credit_query,
    takes_date=True,
    rows_key="output",
    # FID_INPUT_DATE_1 is a settlement day, so the rows are paged by theirs; deal_date is
    # the trading day.
    date_field="stlm_date",
    other_dates=("deal_date",),
)
SOURCES: tuple[Source, ...] = (SHORT_SALE, INVESTOR_DAILY, INVESTOR, DAILY_PRICE, PRICE, LOAN, CREDIT)

# Each source's answers for the first stock are compared on the days they share: (source,
# market), (source, market), the fields compared.
COMPARISONS: tuple[tuple[tuple[Source, str], tuple[Source, str], tuple[str, ...]], ...] = (
    ((INVESTOR_DAILY, "J"), (INVESTOR, "J"), INVESTOR.wanted),
    ((INVESTOR_DAILY, "UN"), (INVESTOR_DAILY, "J"), INVESTOR.wanted),
    ((DAILY_PRICE, "J"), (INVESTOR_DAILY, "J"), ("frgn_ntby_qty",)),
    ((DAILY_PRICE, "UN"), (DAILY_PRICE, "J"), ("hts_frgn_ehrt", "frgn_ntby_qty")),
)

EMPTY, ZERO, MISSING = "empty", "zero", "missing"
FLAG_ORDER = ("int", "int±", "dec", "dec±", "date", "text", ZERO, EMPTY)
_NUMBER = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)")


def _present(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def flag(name: str, values: Sequence[Any]) -> str:
    """What a field's values look like, without them: ``empty``, ``zero``, ``date``, ``int``
    (whole numbers: counts, amounts), ``dec`` (decimals: ratios) or ``text``; a number field
    with a negative value gets ``±``."""
    present = [str(v).strip() for v in values if _present(v)]
    if not present:
        return EMPTY
    if "date" in name:
        return "date"
    if not all(_NUMBER.fullmatch(v) for v in present):
        return "text"
    if all(float(v) == 0 for v in present):
        return ZERO
    kind = "dec" if any("." in v for v in present) else "int"
    return kind + ("±" if any(v.startswith("-") for v in present) else "")


def flags(rows: Sequence[dict[str, Any]]) -> dict[str, str]:
    """Each field of the rows (any row having it), flagged over all of them."""
    names = dict.fromkeys(name for row in rows for name in row)
    return {name: flag(name, [row.get(name) for row in rows]) for name in names}


def describe(fields: dict[str, str]) -> str:
    """The fields grouped by flag: ``int: a, b · dec: c · empty: d``."""
    groups = {f: sorted(n for n, g in fields.items() if g == f) for f in FLAG_ORDER}
    return " · ".join(f"{f}: {', '.join(names)}" for f, names in groups.items() if names) or "none"


def day_of(row: dict[str, Any], name: str) -> date | None:
    try:
        return datetime.strptime(str(row.get(name) or ""), "%Y%m%d").date()
    except ValueError:
        return None


@dataclass
class Answer:
    """What one call gave, as the report uses it."""

    error: str | None = None
    key: str = "-"  # where the rows were in the body
    rows: list[dict[str, Any]] = field(default_factory=list)  # its rows with any value
    head: dict[str, Any] = field(default_factory=dict)  # the object beside them (output1)
    head_key: str = ""
    more: str = ""  # KIS's tr_cont header
    body_keys: tuple[str, ...] = ()  # set when the rows were not where they were looked for

    def days(self, source: Source) -> list[date]:
        """The rows' days, in the answer's order."""
        return [d for row in self.rows if (d := day_of(row, source.date_field)) is not None]


def parse(source: Source, body: dict[str, Any], more: str) -> Answer:
    """The rows (or the one object of a snapshot) of a successful answer."""
    if source.rows_key is None:
        for key in ("output", "output1"):
            value = body.get(key)
            if isinstance(value, list) and len(value) == 1:  # an object wrapped in a list
                value = value[0]
            if isinstance(value, dict):
                rows = [value] if any(_present(v) for v in value.values()) else []
                return Answer(key=key, rows=rows, more=more)
        return Answer(more=more, body_keys=tuple(sorted(body)))
    for key in (source.rows_key, "output2", "output1", "output"):
        value = body.get(key)
        if isinstance(value, list):
            rows = [r for r in value if isinstance(r, dict) and any(_present(v) for v in r.values())]
            head_key = next(
                (k for k in ("output1", "output") if k != key and isinstance(body.get(k), dict)), ""
            )
            head = body[head_key] if head_key else {}
            return Answer(key=key, rows=rows, head=head, head_key=head_key, more=more)
    return Answer(more=more, body_keys=tuple(sorted(body)))


_HTTP = re.compile(r"HTTP (\d+)")
_CODE = re.compile(r"'(?:msg_cd|error_code)': '([^']*)'")
_MESSAGE = re.compile(r"'(?:msg1|error_description)': ['\"]([^'\"]*)")


def kis_error(error: KisError) -> str:
    """KIS's refusal, by its status, code and message only (the rest of the body is left out)."""
    text = str(error)
    parts = [
        f"HTTP {m.group(1)}" if (m := _HTTP.search(text)) else "",
        m.group(1) if (m := _CODE.search(text)) else "",
        m.group(1).strip() if (m := _MESSAGE.search(text)) else "",
    ]
    return "error " + (" ".join(p for p in parts if p) or "(no code)")


def _order(days: Sequence[date]) -> str:
    if len(set(days)) < 2:
        return "one day" if days else "no day"
    if list(days) == sorted(days, reverse=True):
        return "newest first"
    if list(days) == sorted(days):
        return "oldest first"
    return "unordered"


def status(source: Source, answer: Answer, end: date | None = None) -> str:
    """One call's line: OK or the error, the rows and their days."""
    if answer.error is not None:
        return answer.error
    if answer.body_keys:
        missing = "an object" if source.rows_key is None else "a row list"
        return f"OK, but not {missing} (body keys: {', '.join(answer.body_keys)})"
    if source.rows_key is None:
        return f"OK, one object ({answer.key})" if answer.rows else f"OK, empty ({answer.key})"
    days = answer.days(source)
    text = f"OK, {len(answer.rows)} rows ({answer.key})"
    if days:
        text += f", {min(days)}..{max(days)}, {_order(days)}"
    if len(days) < len(answer.rows):
        text += f", {len(answer.rows) - len(days)} without a day in {source.date_field}"
    for name in source.other_dates:
        others = [d for row in answer.rows if (d := day_of(row, name)) is not None]
        if others:
            text += f", {name} {min(others)}..{max(others)}"
    if end is not None and days and max(days) > end:
        text += f", rows after {end}"
    if answer.more:
        text += f", tr_cont {answer.more}"
    return text


def wanted(source: Source, answer: Answer) -> str:
    """The wanted fields' flags in an answer: `` · ssts_cntg_qty int``."""
    if answer.error is not None or not answer.rows:
        return ""
    fields = flags(answer.rows)
    return " · " + ", ".join(f"{name} {fields.get(name, MISSING)}" for name in source.wanted)


def _same(a: Any, b: Any) -> bool:
    if not (_present(a) and _present(b)):
        return False
    try:
        return float(a) == float(b)
    except ValueError:
        return str(a).strip() == str(b).strip()


def agreement(source_a: Source, a: Answer, source_b: Source, b: Answer, names: Sequence[str]) -> str:
    """How many shared days have equal values, field by field (counts only)."""
    by_day_a = {d: row for row in a.rows if (d := day_of(row, source_a.date_field)) is not None}
    by_day_b = {d: row for row in b.rows if (d := day_of(row, source_b.date_field)) is not None}
    common = sorted(by_day_a.keys() & by_day_b.keys())
    if not common:
        return "no common day"
    equal = ", ".join(
        f"{name} {sum(_same(by_day_a[d].get(name), by_day_b[d].get(name)) for d in common)}" for name in names
    )
    return f"{len(common)} common days ({common[0]}..{common[-1]}); equal on: {equal}"


def ownership_check(answer: Answer) -> str:
    """Whether frgn_hldn_qty / lstn_stcn x 100 is hts_frgn_ehrt (no foreign limit below 100%)."""
    if answer.error is not None or not answer.rows:
        return "no answer"
    row = answer.rows[0]
    try:
        held, listed, ratio = (
            float(row["frgn_hldn_qty"]),
            float(row["lstn_stcn"]),
            float(row["hts_frgn_ehrt"]),
        )
    except (KeyError, TypeError, ValueError):
        return "cannot tell: frgn_hldn_qty, lstn_stcn or hts_frgn_ehrt is missing or not a number"
    if listed <= 0:
        return "cannot tell: lstn_stcn is zero"
    if abs(held / listed * 100 - ratio) <= 0.01:
        return "frgn_hldn_qty / lstn_stcn x 100 = hts_frgn_ehrt (within 0.01 points)"
    return "frgn_hldn_qty / lstn_stcn x 100 differs from hts_frgn_ehrt by more than 0.01 points"


class _Prober:
    def __init__(self, client: RestClient, out: Out) -> None:
        self._client = client
        self._out = out
        self.calls = 0
        self.failed = 0
        self._failed_in_a_row = 0
        self.first: dict[tuple[str, str], Answer] = {}  # the first stock's latest answers

    async def ask(self, source: Source, market: str, symbol: str, end: date, tr_cont: str = "") -> Answer:
        if self._failed_in_a_row >= MAX_FAILED_IN_A_ROW:
            return Answer(error=f"not asked: {MAX_FAILED_IN_A_ROW} calls in a row failed")
        if self.calls:
            await asyncio.sleep(PACE)
        self.calls += 1
        try:
            body, more = await self._client.get_page(
                source.tr, source.path, source.query(market, symbol, end), tr_cont
            )
        except KisError as e:
            answer = Answer(error=kis_error(e))
        except (aiohttp.ClientError, TimeoutError, ValueError) as e:
            answer = Answer(error=f"error {type(e).__name__}")
        else:
            self._failed_in_a_row = 0
            return parse(source, body, more)
        self.failed += 1
        self._failed_in_a_row += 1
        return answer

    async def source(
        self, n: int, source: Source, stocks: Sequence[str], etf: str | None, today: date
    ) -> None:
        self._out(f"[{n}] {source.tr} {source.title}: GET {source.path}")
        self._out(f"  for {source.serves}; wanted: {', '.join(source.wanted)}")
        await self.latest(source, stocks, etf, today)
        if not source.takes_date:
            self._out("  takes no date: a past day cannot be asked")
            return
        self._out(f"  a year back (asked to {today - YEAR}):")
        for symbol in [*stocks, *([etf] if etf else [])]:
            market = self._market(source, symbol == etf)
            answer = await self.ask(source, market, symbol, today - YEAR)
            self._out(
                f"    {symbol} {market}: {status(source, answer, today - YEAR)}{wanted(source, answer)}"
            )
        await self.history(source, stocks[0], source.markets[0], today)

    @staticmethod
    def _market(source: Source, etf: bool) -> str:
        """An ETF trades on KRX alone: J if the query takes it."""
        return "J" if etf and "J" in source.markets else source.markets[0]

    async def latest(self, source: Source, stocks: Sequence[str], etf: str | None, today: date) -> None:
        self._out(f"  latest (asked to {today}):")
        shown: tuple[str, Answer] | None = None
        for symbol in [*stocks, *([etf] if etf else [])]:
            markets = source.markets if symbol == stocks[0] else (self._market(source, symbol == etf),)
            for market in markets:
                answer = await self.ask(source, market, symbol, today)
                self._out(f"    {symbol} {market}: {status(source, answer, today)}{wanted(source, answer)}")
                if symbol == stocks[0]:
                    self.first[(source.tr, market)] = answer
                if shown is None and answer.rows:
                    shown = (f"{symbol} {market}", answer)
        if shown is not None:
            label, answer = shown
            self._out(f"  {answer.key} fields ({label}): {describe(flags(answer.rows))}")
            if answer.head:
                self._out(f"  beside them, {answer.head_key} fields: {describe(flags([answer.head]))}")

    async def history(self, source: Source, symbol: str, market: str, today: date) -> None:
        """Page back from today until nothing older comes; bisect for the oldest day if the
        pages run out first. A ranged query goes on past a few empty windows (a gap)."""
        self._out(f"  history of {symbol} {market} (paging back from {today}, at most {MAX_PAGES} calls):")
        end = today
        oldest: date | None = None
        total = calls = empty = 0
        sizes: list[int] = []
        first: Answer | None = None
        finished = False  # nothing older came
        while calls < MAX_PAGES:
            answer = await self.ask(source, market, symbol, end)
            calls += 1
            if answer.error is not None:
                self._out(f"    {answer.error}, after {total} rows in {calls} calls")
                return
            first = first or answer
            days = [d for d in answer.days(source) if d <= end]  # end is before the oldest so far
            if source.ranged:
                days = [d for d in days if d >= end - WINDOW]
            if not days:
                empty += 1
                if not source.ranged or oldest is None or empty > EMPTY_WINDOWS:
                    finished = True
                    break
                end -= WINDOW + timedelta(days=1)  # a gap? the window before
                continue
            if empty and oldest is not None:
                gap_to = oldest - timedelta(days=1)
                self._out(f"    a gap: no rows from {max(days) + timedelta(days=1)} to {gap_to}")
            empty = 0
            sizes.append(len(answer.rows))
            total += len(days)
            oldest = min(days)
            end = oldest - timedelta(days=1)
        counted = f"    {total} rows in {calls} calls (at most {max(sizes, default=0)} a call)"
        if oldest is None:
            self._out(f"    no rows in {calls} calls")
        elif finished:
            self._out(f"{counted}, oldest {oldest}: nothing older")
        elif empty:
            self._out(f"{counted}, oldest {oldest}; stopped among empty windows")
        else:
            self._out(f"{counted}, back to {oldest}: still going")
            await self.bisect(source, market, symbol, oldest)
        if first is not None and first.more in ("M", "F"):
            await self.continuation(source, market, symbol, today, first)

    async def bisect(self, source: Source, market: str, symbol: str, hi: date) -> None:
        """The oldest day with a row, between ``EARLIEST`` and ``hi`` (a day with one). A
        failed call counts as no rows, and is counted."""
        lo = EARLIEST
        calls = failed = 0

        async def rows_upto(day: date) -> list[date]:
            nonlocal calls, failed
            answer = await self.ask(source, market, symbol, day)
            calls += 1
            failed += answer.error is not None
            low = day - WINDOW if source.ranged else EARLIEST
            return [d for d in answer.days(source) if low <= d <= day]

        floor = await rows_upto(lo)
        while not floor and (hi - lo).days > 1 and calls < MAX_BISECT:
            mid = lo + timedelta(days=(hi - lo).days // 2)
            found = await rows_upto(mid)
            if found:
                hi = min(found)
            else:
                lo = mid
        failures = f", {failed} failed: counted as no rows" if failed else ""
        if floor:
            self._out(
                f"    rows even at the bisection's floor {EARLIEST}: oldest seen {min(floor)}{failures}"
            )
        elif (hi - lo).days <= 1:
            self._out(f"    oldest by bisection: {hi} ({calls} calls{failures})")
        else:
            self._out(f"    oldest by bisection: between {lo} and {hi} ({calls} calls, stopped{failures})")

    async def continuation(
        self, source: Source, market: str, symbol: str, today: date, first: Answer
    ) -> None:
        """KIS said there is more (tr_cont M or F): the next page by continuation, once."""
        answer = await self.ask(source, market, symbol, today, tr_cont="N")
        days, before = answer.days(source), first.days(source)
        older = bool(days and before and min(days) < min(before))
        said = "older rows" if older else "no older rows"
        self._out(
            f"    the first page said tr_cont {first.more}; the next (tr_cont N): {status(source, answer)}"
            + ("" if answer.error is not None else f", {said}")
        )

    def cross_checks(self, symbol: str) -> None:
        self._out(f"cross-checks of {symbol} (latest answers; counts of equal values only):")
        for (source_a, market_a), (source_b, market_b), names in COMPARISONS:
            a, b = self.first.get((source_a.tr, market_a)), self.first.get((source_b.tr, market_b))
            label = f"  {source_a.tr} {market_a} vs {source_b.tr} {market_b}:"
            if a is None or b is None or a.error is not None or b.error is not None:
                self._out(f"{label} not both answered")
                continue
            self._out(f"{label} {agreement(source_a, a, source_b, b, names)}")
        for market in PRICE.markets:
            answer = self.first.get((PRICE.tr, market))
            if answer is not None:
                self._out(f"  {PRICE.tr} {market}: {ownership_check(answer)}")


async def probe_supply(settings: Settings, today: date, out: Out = print) -> tuple[int, int]:
    """Ask each source and print the report; the calls made, and how many failed."""
    stocks = settings.symbols[:MAX_SYMBOLS]
    etf = next((s for s in settings.etf_symbols if s not in settings.symbols), None)
    # The cached token while it is valid: no early refresh beside the running collector.
    settings = replace(settings, token_refresh_margin_seconds=0.0)
    async with aiohttp.ClientSession() as session:
        tokens, client = tokens_and_client(session, settings, lambda: datetime.now().astimezone())
        await tokens.get()
        prober = _Prober(client, out)
        out(f"probe-supply (#226) {today}: stocks {', '.join(stocks)}; ETF {etf or '-'}")
        out("No market values below: field names, row counts, days and flags only.")
        out(
            "Flags: int whole numbers (counts, amounts) · dec decimals (ratios) · ± some negative · "
            "date · text · zero all zero · empty all empty · missing not in the answer."
        )
        for n, source in enumerate(SOURCES, 1):
            out("")
            await prober.source(n, source, stocks, etf, today)
        out("")
        prober.cross_checks(stocks[0])
        out("")
        out(f"{prober.calls} calls, {prober.failed} failed")
    return prober.calls, prober.failed
