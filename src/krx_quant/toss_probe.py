"""The Toss part of ``krx-quant probe-supply`` (#235): what the Toss Securities Open API gives.

Toss is the daily supply data's secondary source, and the foreign holdings' primary one (#226's
decision; docs/toss-feeds.md). With its client set (``TOSS_CLIENT_ID``, ``TOSS_CLIENT_SECRET``),
each of its five daily trading-trend endpoints (investor trading with the foreign holdings,
short selling, credit, securities lending, program trades) is asked:

- each symbol's latest page (``COUNT`` records): OK or Toss's error, the records and the days
  they span, the wanted fields' flags;
- the first symbol's history, paging back by ``nextUntil`` (``MAX_PAGES`` calls at most, about
  twelve years): the oldest day reached, the gaps, and from which day each part of a record
  (``individual``, ``foreignerHolding``, ...) stops being null. Today's record is provisional:
  Toss fills the individuals, the holdings and some more only in the evening;
- the KIS answers on the days both have (``kis``, the first stock's latest KIS rows by TR and
  market): how many days have equal values, field by field.

It prints no value either: names, counts, days and flags only. Calls are ``PACE`` seconds apart
(5 a second, half the endpoints' group limit), the token is the one the client's processes share
(``toss.py``), and the part asks nothing after ``BUDGET``. An address Toss does not allow stops
the part at once, saying so plainly.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import aiohttp

from krx_quant.config import Settings
from krx_quant.probe_fields import MISSING, day_of, describe, flags, order_of, same
from krx_quant.toss import IP_MESSAGE, STOCKS, TOKEN_FILE, TossClient, TossError, TossTokens

PACE = 0.2  # seconds between calls: 5 a second; the group (STOCK_TRADING_TREND) allows 10
COUNT = 100  # records a page, Toss's most
MAX_PAGES = 30  # a history's calls: 3,000 trading days, about twelve years
GAP_DAYS = 10  # calendar days between two records that count as a gap (holidays are shorter)
MAX_FAILED_IN_A_ROW = 10
BUDGET = timedelta(minutes=8)  # the Toss part asks nothing more after this

Out = Callable[[str], None]
KisRows = Mapping[tuple[str, str], Sequence[Mapping[str, Any]]]


@dataclass(frozen=True)
class TossSource:
    """A Toss trading-trend endpoint: ``GET /api/v1/stocks/{symbol}/<name>``."""

    name: str
    title: str  # Toss's name for it
    serves: str  # the indicators it would give
    wanted: tuple[str, ...]  # the fields that carry them (dotted paths)
    parts: tuple[str, ...] = ()  # parts of a record that may be null: from when they are not


INVESTOR = TossSource(
    name="investor-trading",
    title="투자자별 매매동향",
    serves="일별 개인·기관·외국인 순매수량, 일별 외국인 보유수량·지분율(주 출처)",
    wanted=(
        "individual.netBuyVolume",
        "institution.netBuyVolume",
        "foreigner.netBuyVolume",
        "foreignerHolding.holdingQuantity",
        "foreignerHolding.limitQuantity",
        "foreignerHolding.holdingRate",
    ),
    parts=(
        "individual",
        "foreigner",
        "institution",
        "institution.breakdown",
        "otherCorporation",
        "foreignerHolding",
        "cfd",
    ),
)
SHORT_SELLING = TossSource(
    name="short-selling",
    title="공매도 동향",
    serves="일별 공매도 거래량",
    wanted=("shortSellingVolume", "shortSellingVolumeRate"),
    parts=("shortSellingVolumeRate", "shortSellingAmountRate"),
)
CREDIT = TossSource(
    name="credit-trades",
    title="신용거래 동향",
    serves="일별 신용잔고 수량",
    wanted=("marginLoan.balanceQuantity", "marginLoan.balanceRate"),
    parts=("marginLoan", "stockLoan"),
)
LENDING = TossSource(
    name="securities-lending",
    title="대차거래 동향",
    serves="일별 대차잔고 수량",
    wanted=("balanceQuantity",),
)
PROGRAM = TossSource(
    name="program-trades",
    title="프로그램매매 동향",
    serves="8종 밖(KRX만 집계)",
    wanted=("arbitrage.netBuyVolume", "nonArbitrage.netBuyVolume"),
    parts=("arbitrage", "nonArbitrage"),
)
TOSS_SOURCES: tuple[TossSource, ...] = (INVESTOR, SHORT_SELLING, CREDIT, LENDING, PROGRAM)

_ABSENT = object()


def at(record: Mapping[str, Any], path: str) -> Any:
    """The value at a dotted path; ``_ABSENT`` if the path is not there (None if it is null)."""
    node: Any = record
    for key in path.split("."):
        if not isinstance(node, Mapping) or key not in node:
            return _ABSENT
        node = node[key]
    return node


def leaves(record: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """A record's values by dotted path; a null object stays under its own name."""
    out: dict[str, Any] = {}
    for key, value in record.items():
        name = f"{prefix}{key}"
        if isinstance(value, Mapping):
            out.update(leaves(value, f"{name}."))
        else:
            out[name] = value
    return out


def record_day(record: Mapping[str, Any]) -> date | None:
    return day_of(record, "date", "%Y-%m-%d")


def _date(text: str) -> date | None:
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def holding_ratio(record: Mapping[str, Any]) -> float | None:
    """foreignerHolding.holdingQuantity / limitQuantity x 100: how much of the foreign limit is
    held, as KIS's hts_frgn_ehrt (소진율) gives it."""
    try:
        held = float(at(record, "foreignerHolding.holdingQuantity"))
        limit = float(at(record, "foreignerHolding.limitQuantity"))
    except (TypeError, ValueError):
        return None
    return held / limit * 100 if limit > 0 else None


@dataclass
class Page:
    """What one call gave."""

    error: str | None = None
    records: list[dict[str, Any]] = field(default_factory=list)
    next_until: str | None = None
    odd: str = ""  # the result was not the records Toss documents

    def days(self) -> list[date]:
        return [d for r in self.records if (d := record_day(r)) is not None]


def parse(result: Any) -> Page:
    if not isinstance(result, Mapping) or not isinstance(result.get("records"), list):
        keys = ", ".join(sorted(result)) if isinstance(result, Mapping) else type(result).__name__
        return Page(odd=f"OK, but no records list (result: {keys})")
    records = [r for r in result["records"] if isinstance(r, dict)]
    until = result.get("nextUntil")
    return Page(records=records, next_until=str(until) if until else None)


def page_status(page: Page) -> str:
    if page.error is not None:
        return page.error
    if page.odd:
        return page.odd
    days = page.days()
    text = f"OK, {len(page.records)} records"
    if days:
        text += f", {min(days)}..{max(days)}, {order_of(days)}"
    if len(days) < len(page.records):
        text += f", {len(page.records) - len(days)} without a date"
    return text + f", nextUntil {page.next_until or 'null'}"


def field_flags(records: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """The records' leaf fields, flagged (an object that is null somewhere and set elsewhere is
    described by its fields; one null everywhere is ``empty``)."""
    rows = [leaves(r) for r in records]
    found = flags(rows)
    return {n: f for n, f in found.items() if not any(m.startswith(f"{n}.") for m in found)}


def wanted(source: TossSource, page: Page) -> str:
    if page.error is not None or page.odd or not page.records:
        return ""
    fields = field_flags(page.records)

    def shown(name: str) -> str:
        if name in fields:
            return fields[name]
        parent = name.split(".")[0]
        return "null" if all(at(r, parent) is None for r in page.records) else MISSING

    return " · " + ", ".join(f"{name} {shown(name)}" for name in source.wanted)


@dataclass(frozen=True)
class Check:
    """A Toss field and the KIS field it should equal on the days both have."""

    source: TossSource
    label: str  # the Toss side, as the report names it
    value: Callable[[Mapping[str, Any]], Any]
    kis_tr: str
    kis_market: str
    kis_field: str
    kis_day: str = "stck_bsop_date"
    tolerance: float = 0.0


def _field(path: str, scale: float = 1.0) -> Callable[[Mapping[str, Any]], Any]:
    def value(record: Mapping[str, Any]) -> Any:
        found = at(record, path)
        if found is _ABSENT or found is None:
            return None
        if scale == 1.0:
            return found
        try:
            return float(found) * scale
        except (TypeError, ValueError):
            return None

    return value


RATIO = 0.011  # a ratio in % to two places, on either side: equal within a rounding step
INVESTOR_DAILY, DAILY_PRICE, PRICE = "FHPTJ04160001", "FHKST01010400", "FHKST01010100"
CHECKS: tuple[Check, ...] = (
    *(
        Check(INVESTOR, f"{who}.netBuyVolume", _field(f"{who}.netBuyVolume"), INVESTOR_DAILY, market, kis)
        for who, kis in (("individual", "prsn_ntby_qty"), ("institution", "orgn_ntby_qty"))
        for market in ("UN", "J")
    ),
    # Toss's foreigners are the registered ones; KIS's frgn_ntby_qty adds the others.
    *(
        Check(
            INVESTOR, "foreigner.netBuyVolume", _field("foreigner.netBuyVolume"), INVESTOR_DAILY, market, kis
        )
        for kis in ("frgn_ntby_qty", "frgn_reg_ntby_qty")
        for market in ("UN", "J")
    ),
    *(
        Check(
            INVESTOR,
            "foreignerHolding.holdingRate x100",
            _field("foreignerHolding.holdingRate", 100.0),
            DAILY_PRICE,
            market,
            "hts_frgn_ehrt",
            tolerance=RATIO,
        )
        for market in ("UN", "J")
    ),
    Check(
        INVESTOR,
        "holdingQuantity / limitQuantity x100",
        holding_ratio,
        DAILY_PRICE,
        "UN",
        "hts_frgn_ehrt",
        tolerance=RATIO,
    ),
    Check(
        SHORT_SELLING,
        "shortSellingVolume",
        _field("shortSellingVolume"),
        "FHPST04830000",
        "J",
        "ssts_cntg_qty",
    ),
    Check(
        LENDING, "balanceQuantity", _field("balanceQuantity"), "HHPST074500C0", "3", "rmnd_stcn", "bsop_date"
    ),
    *(
        Check(
            CREDIT,
            "marginLoan.balanceQuantity",
            _field("marginLoan.balanceQuantity"),
            "FHPST04760000",
            "J",
            "whol_loan_rmnd_stcn",
            day,
        )
        for day in ("deal_date", "stlm_date")
    ),
)


def compare(check: Check, records: Sequence[Mapping[str, Any]], kis: KisRows) -> str:
    """How many days both sides have, and on how many they are equal (counts only)."""
    rows = kis.get((check.kis_tr, check.kis_market))
    if not rows:
        return "no KIS rows"
    toss = {d: check.value(r) for r in records if (d := record_day(r)) is not None}
    theirs = {d: r.get(check.kis_field) for r in rows if (d := day_of(r, check.kis_day)) is not None}
    common = sorted(toss.keys() & theirs.keys())
    if not common:
        return "no common day"
    equal = sum(same(toss[d], theirs[d], check.tolerance) for d in common)
    return f"{len(common)} common days ({common[0]}..{common[-1]}); equal on {equal}"


def snapshot_checks(records: Sequence[Mapping[str, Any]], kis: KisRows) -> list[str]:
    """KIS's holdings of the moment (FHKST01010100) against Toss's daily ones: on which Toss
    days they are equal (days only)."""
    lines = []
    held = [
        (d, r)
        for r in records
        if (d := record_day(r)) is not None and isinstance(at(r, "foreignerHolding"), Mapping)
    ]
    held.sort(key=lambda pair: pair[0], reverse=True)
    for market in ("UN", "J"):
        rows = kis.get((PRICE, market))
        if not rows:
            continue
        row = rows[0]
        quantity = [
            d for d, r in held if same(at(r, "foreignerHolding.holdingQuantity"), row.get("frgn_hldn_qty"))
        ]
        try:
            ratio: float | None = float(row["frgn_hldn_qty"]) / float(row["lstn_stcn"]) * 100
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            ratio = None
        rates = [d for d, r in held if same(_field("foreignerHolding.holdingRate", 100.0)(r), ratio, RATIO)]
        newest = f" (Toss's newest day with holdings: {held[0][0]})" if held else ""
        lines.append(
            f"  {PRICE} {market} frgn_hldn_qty = foreignerHolding.holdingQuantity "
            f"on: {_days(quantity)}{newest}"
        )
        lines.append(
            f"  {PRICE} {market} frgn_hldn_qty / lstn_stcn x100 = foreignerHolding.holdingRate x100 "
            f"(within 0.01) on: {_days(rates)}"
        )
    return lines


def _days(days: Sequence[date]) -> str:
    if not days:
        return "no day"
    shown = ", ".join(str(d) for d in days[:3])
    return shown + (f" and {len(days) - 3} more" if len(days) > 3 else "")


class _TossProber:
    def __init__(self, client: TossClient, out: Out, today: date, budget: timedelta = BUDGET) -> None:
        self._client = client
        self._out = out
        self._today = today
        self._budget = budget
        self._deadline = time.monotonic() + budget.total_seconds()
        self.calls = 0
        self.failed = 0
        self._failed_in_a_row = 0
        self.stopped: str | None = None  # why nothing more is asked (an address Toss refuses)
        self.history: dict[str, list[dict[str, Any]]] = {}  # the first symbol's records, by source

    async def ask(self, source: TossSource, symbol: str, until: str | None = None) -> Page:
        if self.stopped is not None:
            return Page(error=f"not asked: {self.stopped}")
        if self._failed_in_a_row >= MAX_FAILED_IN_A_ROW:
            return Page(error=f"not asked: {MAX_FAILED_IN_A_ROW} calls in a row failed")
        if time.monotonic() >= self._deadline:
            minutes = self._budget.total_seconds() / 60
            return Page(error=f"not asked: the Toss part's {minutes:g}-minute budget is used up")
        self.calls += 1
        params = {"count": str(COUNT), **({"until": until} if until else {})}
        try:
            result = await self._client.get(f"{STOCKS}/{symbol}/{source.name}", params)
        except TossError as e:
            if e.ip_not_allowed:
                self.stopped = IP_MESSAGE
            page = Page(error=f"error {e}")
        except (aiohttp.ClientError, OSError, ValueError) as e:  # OSError: timeouts, the cache
            page = Page(error=f"error {type(e).__name__}")
        else:
            self._failed_in_a_row = 0
            return parse(result)
        self.failed += 1
        self._failed_in_a_row += 1
        return page

    async def source(self, n: int, source: TossSource, symbols: Sequence[str]) -> None:
        self._out(f"[T{n}] {source.name} {source.title}: GET {STOCKS}/{{symbol}}/{source.name}")
        self._out(f"  for {source.serves}; wanted: {', '.join(source.wanted)}")
        self._out(f"  latest (count {COUNT}):")
        first: Page | None = None
        shown: tuple[str, Page] | None = None
        for symbol in symbols:
            page = await self.ask(source, symbol)
            self._out(f"    {symbol}: {page_status(page)}{wanted(source, page)}")
            first = first or page
            if shown is None and page.records:
                shown = (symbol, page)
        if shown is not None:
            self._out(f"  fields ({shown[0]}): {describe(field_flags(shown[1].records))}")
        if first is not None:
            await self.page_back(source, symbols[0], first)

    async def page_back(self, source: TossSource, symbol: str, first: Page) -> None:
        """The first symbol's history, from its latest page back by nextUntil."""
        self._out(
            f"  history of {symbol} (count {COUNT}, paging back by nextUntil, at most {MAX_PAGES} calls):"
        )
        if first.error is not None or first.odd:
            self._out("    not paged: the latest page did not answer")
            return
        by_day = {d: r for r in first.records if (d := record_day(r)) is not None}
        calls, sizes, until, ended = 1, [len(first.records)], first.next_until, ""
        while until is not None and calls < MAX_PAGES:
            asked = _date(until)
            if asked is None or (by_day and asked >= min(by_day)):
                ended = f"nextUntil {until} does not go back: stopped"
                break
            page = await self.ask(source, symbol, until)
            calls += 1
            if page.error is not None or page.odd:
                ended = f"{page_status(page)}: stopped"
                break
            sizes.append(len(page.records))
            by_day.update((d, r) for r in page.records if (d := record_day(r)) is not None)
            until = page.next_until
        records = [by_day[d] for d in sorted(by_day, reverse=True)]
        self.history[source.name] = records
        counted = f"    {len(records)} records in {calls} calls (at most {max(sizes)} a call)"
        if not records:
            self._out(f"    no records in {calls} calls")
            return
        oldest = min(by_day)
        if ended:
            self._out(f"{counted}, back to {oldest}; {ended}")
        elif until is None:
            self._out(f"{counted}, oldest {oldest}: nextUntil null, nothing older")
        else:
            self._out(f"{counted}, back to {oldest}: still going after {MAX_PAGES} calls")
        self._out(f"    gaps over {GAP_DAYS} days: {_gaps(sorted(by_day))}")
        if source.parts:
            self._out("    not null:")
            for part in source.parts:
                self._out(f"      {part}: {_not_null(records, part)}")
        self._out(f"    {self._newest(source, records)}")

    def _newest(self, source: TossSource, records: Sequence[Mapping[str, Any]]) -> str:
        newest, day = records[0], record_day(records[0])
        nulls = [p for p in source.parts if at(newest, p) is None]
        null = f"; null in it: {', '.join(nulls)}" if nulls else ""
        if day == self._today:
            return (
                f"the newest record ({day}) is today's: provisional, Toss completes it in the evening{null}"
            )
        return f"the newest record is {day}: today's is not out (yet){null}"


def _gaps(days: Sequence[date]) -> str:
    gaps = [(a, b) for a, b in zip(days, days[1:], strict=False) if (b - a).days > GAP_DAYS]
    if not gaps:
        return "none"
    gaps.sort(key=lambda pair: (pair[1] - pair[0]).days, reverse=True)
    shown = ", ".join(f"{a + timedelta(days=1)}..{b - timedelta(days=1)}" for a, b in gaps[:3])
    return f"{len(gaps)} ({shown}{', ...' if len(gaps) > 3 else ''})"


def _not_null(records: Sequence[Mapping[str, Any]], part: str) -> str:
    """From which day a part stops being null: its first and last days with a value, and the
    null days after its first one."""
    days = [(d, at(r, part)) for r in records if (d := record_day(r)) is not None]
    set_days = sorted(d for d, v in days if v is not None and v is not _ABSENT)
    if not set_days:
        return "null on every day" if any(v is None for _, v in days) else "not in the records"
    first = set_days[0]
    nulls = sum(1 for d, v in days if d > first and (v is None or v is _ABSENT))
    counts = f"{len(set_days)} of {len(days)} days; null on {nulls} later day{'' if nulls == 1 else 's'}"
    return f"from {first} to {set_days[-1]} ({counts})"


async def probe_toss(
    session: aiohttp.ClientSession,
    settings: Settings,
    symbols: Sequence[str],
    today: date,
    kis: KisRows,
    out: Out = print,
    budget: timedelta = BUDGET,
) -> tuple[int, int]:
    """Ask each Toss source and print the report; the calls made, and how many failed."""
    out("Toss (secondary; foreign holdings primary, #235)")
    if not (settings.toss_client_id and settings.toss_client_secret):
        out("  skipped: TOSS_CLIENT_ID/TOSS_CLIENT_SECRET not set")
        return 0, 0
    tokens = TossTokens(
        session,
        settings.toss_url,
        settings.toss_client_id,
        settings.toss_client_secret,
        settings.state_dir,
        lambda: datetime.now().astimezone(),
    )
    try:
        await tokens.get()
    except TossError as e:
        out(f"  stopped: no Toss access token: {IP_MESSAGE if e.ip_not_allowed else e}")
        out("Toss: 1 call, 1 failed")
        return 1, 1
    except (aiohttp.ClientError, OSError, ValueError) as e:
        out(f"  stopped: no Toss access token: error {type(e).__name__}")
        out("Toss: 1 call, 1 failed")
        return 1, 1
    how = "issued a new one (none was cached, or it had expired)" if tokens.issued else "the cached one"
    out(f"  token: {how}; shared through state/{TOKEN_FILE}")
    prober = _TossProber(TossClient(session, settings.toss_url, tokens, PACE), out, today, budget)
    for n, source in enumerate(TOSS_SOURCES, 1):
        out("")
        await prober.source(n, source, symbols)
        if prober.stopped is not None:
            out(f"  stopped: {prober.stopped}")
            break
    out("")
    out(f"cross-checks with KIS ({symbols[0]}; Toss's history against KIS's latest answers, counts only):")
    for check in CHECKS:
        records = prober.history.get(check.source.name, [])
        kis_side = f"{check.kis_tr} {check.kis_market} {check.kis_field}"
        if check.kis_day != "stck_bsop_date":
            kis_side += f" by {check.kis_day}"
        within = " (within 0.01)" if check.tolerance else ""
        line = compare(check, records, kis) if records else "no Toss records"
        out(f"  {check.source.name} {check.label} vs {kis_side}{within}: {line}")
    for line in snapshot_checks(prober.history.get(INVESTOR.name, []), kis):
        out(line)
    out("")
    out(f"Toss: {prober.calls} calls, {prober.failed} failed; tokens issued: {tokens.issued}")
    return prober.calls, prober.failed
