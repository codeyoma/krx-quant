"""Daily supply data for the chart's lower indicators (#226): one value a day of eight
indicators for each predicted symbol.

The eight (``FIELDS``): the short-sale volume, the net buys of individuals, institutions and
foreigners, the foreign holdings and ownership ratio (%), and the stock lending and credit
balances, all in shares but the ratio. The holdings of institutions and individuals were
dropped: no source has them (#226's decision).

Sources (#226's decision; docs/kis-feeds.md "일별 수급 (#226)", docs/toss-feeds.md). KIS is
the primary source, the Toss Securities Open API the secondary one, and Toss is the primary
source of the foreign holdings and ratio (KIS has today's holdings only). The server's probe
(``probe-supply``, 2026-10-16) found where the two agree, and a day KIS lacks is filled from
Toss only there:

- the short-sale volume: KIS ``FHPST04830000`` KRX (``J``) ``ssts_cntg_qty``; Toss
  ``short-selling`` ``shortSellingVolume`` equalled it on all 100 days compared;
- the net buys: KIS ``FHPTJ04160001`` integrated (``UN``, KRX + NXT) ``prsn_ntby_qty``,
  ``orgn_ntby_qty``, ``frgn_ntby_qty``; Toss ``investor-trading`` ``individual`` and
  ``institution`` equalled the first two on all 30 days. Toss's ``foreigner`` is the
  registered foreigners only (KIS's ``frgn_reg_ntby_qty``), not KIS's foreigners, so the
  foreign net buy has no secondary. KIS answers 0 shares for a net buy it has no integrated
  figure of: the days before NXT opened (2025-03-04). Such a 0 is kept as none (``no_figure``:
  before NXT opened, and on a day all three are 0), so the day takes Toss's value where Toss
  has one; a 0 after NXT opened beside another class's figure is a real 0 and stays;
- the foreign holdings and ratio: Toss ``investor-trading`` ``foreignerHolding``
  (``holdingQuantity``; ``holdingRate`` × 100, of the listed shares). KIS's are today's one
  value and a ratio of the foreign limit: none is used;
- the lending balance: KIS ``HHPST074500C0`` ``rmnd_stcn``; Toss ``securities-lending``
  ``balanceQuantity`` equalled it on all 100 days;
- the credit balance: KIS ``FHPST04760000`` ``whol_loan_rmnd_stcn`` by its ``stlm_date``,
  Toss ``credit-trades`` ``marginLoan.balanceQuantity`` by its ``date``: equal on all 30 days
  on those dates (on none by KIS's ``deal_date``). A day's credit balance is the one dated so.

When: the collector's history task runs this after the day candles (``backfill.keep_history``):
at start and daily at 20:30, when the day is over. Each run asks every query from the last
finished trading day (today from 20:30 on, as the candles) back to the newest day it holds,
a page at least, and replaces the days it gets: the values KIS or Toss revise later (Toss's
foreign holdings the next business morning) are taken again by the runs after. Toss's record
of the run's own day is provisional (filled in the evening, docs/toss-feeds.md) and is not
taken; the next run takes it, the next morning's start (the 06:00 deploy or the 07:20 restart)
on a trading day. The first run takes ``HISTORY_YEARS`` of each, as far as the source goes
(Toss: from 2019, lending 2021, credit 2023; KIS further).

Storage: the engine's private state, not the data directory, as Toss's data is for the user's
own trading only and must not be passed on (its terms, docs/toss-feeds.md): it is not
uploaded (the archive uploads ``data/`` alone), and the Engine API serves it to the user's own
screen only. Each source keeps its own file, ``state/supply/symbol=<symbol>/kis.parquet`` and
``toss.parquet``, a row a day, oldest first, rewritten whole; the answer merges them
(``stored_supply``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import pyarrow as pa
import pyarrow.parquet as pq

from krx_quant.candles import last_finished
from krx_quant.config import Settings
from krx_quant.feeds.base import KST, Row
from krx_quant.kis import KisError, RestClient
from krx_quant.market_calendar import MarketCalendar
from krx_quant.ratelimit import RateLimiter
from krx_quant.toss import STOCKS, TossClient, TossError, TossTokens

log = logging.getLogger(__name__)

FIELDS = (
    "short_volume",
    "individual_net",
    "institution_net",
    "foreign_net",
    "foreign_holding",
    "foreign_ratio",
    "lending_balance",
    "credit_balance",
)
KIS, TOSS = "kis", "toss"
# Each field's sources, the first one's value first: a day it lacks takes the next one's.
SOURCES: Mapping[str, tuple[str, ...]] = {
    "short_volume": (KIS, TOSS),
    "individual_net": (KIS, TOSS),
    "institution_net": (KIS, TOSS),
    "foreign_net": (KIS,),
    "foreign_holding": (TOSS,),
    "foreign_ratio": (TOSS,),
    "lending_balance": (KIS, TOSS),
    "credit_balance": (KIS, TOSS),
}
# KIS's net buys (``INVESTORS``). Before NXT opened its integrated query answers 0 shares where
# it has no figure: the structure check (#270, 2026-10-18) found 2,452 stored days of each
# symbol's foreign net buy and only 395 of them other than 0, the same count for all three
# symbols, about the trading days since 2025-03-04 (396 by the holidays known here). The zeros
# are the days before; whether KIS also gave the individual and institution net buys as 0 then
# was not measured (only the foreign one was). So a 0 is kept as no figure (None), as fetched
# and as read from a file written before (``no_figure``), on two conditions:
# - on a day before NXT opened (``NXT_OPENED``), each net buy of 0;
# - on any day, the three at once at 0: no stock that traded has that, and it is the same
#   answer as before NXT (a day at the boundary, a halt).
# After NXT opened, one class at 0 beside another's figure is a real 0 (an institution can buy
# and sell as many shares in a day) and stays.
NET_BUYS = ("individual_net", "institution_net", "foreign_net")
NXT_OPENED = date(2025, 3, 4)  # NXT's first trading day: KIS's integrated figures begin
HISTORY_YEARS = 10  # the first run's depth: about 2,450 trading days, the day view's 2,000 and more
_MAX_CALLS = 400  # a query's pages in one run: 10 years of 30-row pages is 83
_WINDOW = timedelta(days=365)  # a ranged KIS query's span, ending at the day asked
TOSS_COUNT = 100  # records a Toss page, its most

KIS_SCHEMA = pa.schema([("date", pa.date32())] + [(f, pa.int64()) for f in FIELDS if KIS in SOURCES[f]])
TOSS_SCHEMA = pa.schema(
    [("date", pa.date32())]
    + [(f, pa.float64() if f == "foreign_ratio" else pa.int64()) for f in FIELDS if TOSS in SOURCES[f]]
)
SCHEMAS = {KIS: KIS_SCHEMA, TOSS: TOSS_SCHEMA}

_QUOTATIONS = "/uapi/domestic-stock/v1/quotations"


def _ymd(day: date) -> str:
    return day.strftime("%Y%m%d")


@dataclass(frozen=True)
class KisQuery:
    """A KIS daily query and the fields it gives (docs/kis-feeds.md "일별 수급 (#226)")."""

    tr: str
    path: str
    columns: Mapping[str, str]  # the field here -> KIS's
    rows_key: str
    date_field: str
    params: Callable[[str, date], dict[str, str]]  # (symbol, the last day asked) -> params


SHORT_SALE = KisQuery(
    tr="FHPST04830000",
    path=f"{_QUOTATIONS}/daily-short-sale",
    columns={"short_volume": "ssts_cntg_qty"},
    rows_key="output2",
    date_field="stck_bsop_date",
    # The newest 100 days of the year to the day asked.
    params=lambda symbol, end: {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": symbol,
        "FID_INPUT_DATE_1": _ymd(end - _WINDOW),
        "FID_INPUT_DATE_2": _ymd(end),
    },
)
INVESTORS = KisQuery(
    tr="FHPTJ04160001",
    path=f"{_QUOTATIONS}/investor-trade-by-stock-daily",
    columns={
        "individual_net": "prsn_ntby_qty",
        "institution_net": "orgn_ntby_qty",
        "foreign_net": "frgn_ntby_qty",
    },
    rows_key="output2",
    date_field="stck_bsop_date",
    # The 30 days to the day asked, integrated (KRX + NXT) as Toss's.
    params=lambda symbol, end: {
        "FID_COND_MRKT_DIV_CODE": "UN",
        "FID_INPUT_ISCD": symbol,
        "FID_INPUT_DATE_1": _ymd(end),
        "FID_ORG_ADJ_PRC": "",
        "FID_ETC_CLS_CODE": "",
    },
)
LENDING = KisQuery(
    tr="HHPST074500C0",
    path=f"{_QUOTATIONS}/daily-loan-trans",
    columns={"lending_balance": "rmnd_stcn"},
    rows_key="output1",
    date_field="bsop_date",
    # MRKT_DIV_CLS_CODE 3: one stock; the newest 100 days of the year to the day asked.
    params=lambda symbol, end: {
        "MRKT_DIV_CLS_CODE": "3",
        "MKSC_SHRN_ISCD": symbol,
        "START_DATE": _ymd(end - _WINDOW),
        "END_DATE": _ymd(end),
        "CTS": "",
    },
)
CREDIT = KisQuery(
    tr="FHPST04760000",
    path=f"{_QUOTATIONS}/daily-credit-balance",
    columns={"credit_balance": "whol_loan_rmnd_stcn"},
    rows_key="output",
    # The day the balance is of. The server's probe (#226, 2026-10-16) found Toss's credit balance
    # equal to this one by stlm_date on 30 of 30 days, and by deal_date (two trading days before)
    # on none: keyed by deal_date, KIS's balances would land two days early, over Toss's.
    date_field="stlm_date",
    # The 30 settlement days to the day asked.
    params=lambda symbol, end: {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_COND_SCR_DIV_CODE": "20476",
        "FID_INPUT_ISCD": symbol,
        "FID_INPUT_DATE_1": _ymd(end),
    },
)
KIS_QUERIES = (SHORT_SALE, INVESTORS, LENDING, CREDIT)


@dataclass(frozen=True)
class TossQuery:
    """A Toss daily trading-trend endpoint (``GET /api/v1/stocks/{symbol}/<name>``) and the
    fields it gives, by a dotted path in its records."""

    name: str
    columns: Mapping[str, str]  # the field here -> the record's path


TOSS_INVESTORS = TossQuery(
    "investor-trading",
    {
        "individual_net": "individual.netBuyVolume",
        "institution_net": "institution.netBuyVolume",
        "foreign_holding": "foreignerHolding.holdingQuantity",
        "foreign_ratio": "foreignerHolding.holdingRate",
    },
)
TOSS_SHORT_SALE = TossQuery("short-selling", {"short_volume": "shortSellingVolume"})
TOSS_LENDING = TossQuery("securities-lending", {"lending_balance": "balanceQuantity"})
TOSS_CREDIT = TossQuery("credit-trades", {"credit_balance": "marginLoan.balanceQuantity"})
TOSS_QUERIES = (TOSS_INVESTORS, TOSS_SHORT_SALE, TOSS_LENDING, TOSS_CREDIT)


def supply_path(state_dir: Path, symbol: str, source: str) -> Path:
    """A symbol's file of one source's values under the state directory."""
    return state_dir / "supply" / f"symbol={symbol}" / f"{source}.parquet"


def read_supply(state_dir: Path, symbol: str, source: str) -> list[Row]:
    """A source's stored days, oldest first; none if there is no file. KIS's net buys of 0 are
    none (``no_figure``: a file written before kept them)."""
    path = supply_path(state_dir, symbol, source)
    if not path.exists():
        return []
    rows: list[Row] = pq.read_table(path).to_pylist()
    return [no_figure(r, r["date"]) for r in rows] if source == KIS else rows


def no_figure(row: Row, day: date) -> Row:
    """KIS's ``row`` of ``day`` with its net buys of 0 shares that are KIS's "no figure" as none:
    each before NXT opened, and all three on a day they are all 0 (``NET_BUYS``)."""
    zeros = [f for f in NET_BUYS if row.get(f) == 0]
    if zeros and (day < NXT_OPENED or len(zeros) == len(NET_BUYS)):
        return {**row, **dict.fromkeys(zeros)}
    return row


def write_supply(state_dir: Path, symbol: str, source: str, rows: Sequence[Row]) -> None:
    """Replace a source's stored days, whole or not at all."""
    path = supply_path(state_dir, symbol, source)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    schema = SCHEMAS[source]
    full = [{name: row.get(name) for name in schema.names} for row in rows]
    pq.write_table(pa.Table.from_pylist(full, schema=schema), tmp)
    tmp.rename(path)


def merged(kis: Sequence[Row], toss: Sequence[Row]) -> list[Row]:
    """The days of both sources, oldest first, each field from its first source that has it
    that day (``SOURCES``); a day with no value at all is left out."""
    by_source = {KIS: {r["date"]: r for r in kis}, TOSS: {r["date"]: r for r in toss}}
    days: list[Row] = []
    for day in sorted(by_source[KIS].keys() | by_source[TOSS].keys()):
        row: Row = {"date": day}
        for name in FIELDS:
            row[name] = next(
                (
                    value
                    for source in SOURCES[name]
                    if (value := by_source[source].get(day, {}).get(name)) is not None
                ),
                None,
            )
        if any(row[name] is not None for name in FIELDS):
            days.append(row)
    return days


def stored_supply(state_dir: Path, symbol: str, first: date, last: date) -> list[Row]:
    """The stored days from ``first`` to ``last`` (both in), oldest first, the sources merged."""
    days = merged(read_supply(state_dir, symbol, KIS), read_supply(state_dir, symbol, TOSS))
    return [d for d in days if first <= d["date"] <= last]


def _whole(value: Any) -> int | None:
    """A count as KIS or Toss sends it (a string, maybe signed); None when blank or absent."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return int(float(text)) if "." in text else int(text)


def _percent(value: Any) -> float | None:
    """Toss's ratio (``0.5089``) as a percentage (50.89)."""
    if value is None or not str(value).strip():
        return None
    return round(float(value) * 100, 4)


def _at(record: Mapping[str, Any], path: str) -> Any:
    """The value at a dotted path of a Toss record; None if absent or null."""
    node: Any = record
    for key in path.split("."):
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node


def _day(text: Any, form: str) -> date | None:
    try:
        return datetime.strptime(str(text or ""), form).date()
    except ValueError:
        return None


def _updated(stored: Sequence[Row], fetched: Mapping[date, Row], floor: date) -> list[Row]:
    """The stored days with the fetched ones' fields replaced or added, from ``floor`` on,
    oldest first; a day left with no value is dropped."""
    days = {r["date"]: dict(r) for r in stored}
    for day, values in fetched.items():
        days.setdefault(day, {"date": day}).update(values)
    return [
        days[d]
        for d in sorted(days)
        if d >= floor and any(v is not None for k, v in days[d].items() if k != "date")
    ]


def _newest(rows: Sequence[Row], columns: Sequence[str]) -> date | None:
    """The newest day holding any of ``columns``."""
    return max((r["date"] for r in rows if any(r.get(c) is not None for c in columns)), default=None)


def _refused(error: TossError) -> bool:
    """Toss refused the address or the client (its token): no other call would answer."""
    return error.ip_not_allowed or error.status in (401, 403)


def history_floor(last: date) -> date:
    """The oldest day kept: ``HISTORY_YEARS`` before ``last``."""
    try:
        return last.replace(year=last.year - HISTORY_YEARS)
    except ValueError:  # 29 February
        return last.replace(year=last.year - HISTORY_YEARS, day=28)


class SupplyKeeper:
    """Keeps every predicted symbol's daily supply data, from KIS and (when set) Toss."""

    def __init__(
        self,
        client: RestClient,
        calendar: MarketCalendar,
        state_dir: Path,
        symbols: Sequence[str],
        pace: RateLimiter,
        toss: Callable[[], TossClient] | None = None,
    ) -> None:
        self._client = client
        self._calendar = calendar
        self._state_dir = state_dir
        self._symbols = tuple(symbols)
        # The backfill's (Backfiller.pace), as the candles': one spacing inside the REST limit.
        self._pace = pace
        # A new Toss client each run (a client renews its token once at most); None: not set.
        self._toss = toss

    async def run(self, now: datetime) -> int:
        """Bring every symbol's days up to the last finished trading day; return how many files
        changed. A query that fails is logged, and the others' days are kept."""
        last = last_finished(self._calendar, now)
        # Toss's record of today is provisional (null parts until the evening): the next run's.
        toss_last = min(last, now.astimezone(KST).date() - timedelta(days=1))
        floor = history_floor(last)
        changed = 0
        for symbol in self._symbols:
            changed += await self._update_kis(symbol, last, floor)
        if self._toss is not None:
            toss = self._toss()
            for symbol in self._symbols:
                try:
                    changed += await self._update_toss(toss, symbol, toss_last, floor)
                except TossError as e:  # the address or the client refused: none would answer
                    log.warning("Toss supply not read this run: %s", e)
                    break
        return changed

    async def _update_kis(self, symbol: str, last: date, floor: date) -> bool:
        stored = read_supply(self._state_dir, symbol, KIS)
        fetched: dict[date, Row] = {}
        for query in KIS_QUERIES:
            stop = _newest(stored, list(query.columns)) or floor
            try:
                got = await self._walk_kis(query, symbol, last, max(stop, floor))
            except (KisError, aiohttp.ClientError, OSError, ValueError, KeyError, TimeoutError) as e:
                log.warning("%s supply (%s) of %s not updated: %s", KIS, query.tr, symbol, e)
                continue
            for day, values in got.items():
                fetched.setdefault(day, {}).update(values)
        return self._save(symbol, KIS, stored, fetched, floor)

    async def _update_toss(self, toss: TossClient, symbol: str, last: date, floor: date) -> bool:
        stored = read_supply(self._state_dir, symbol, TOSS)
        fetched: dict[date, Row] = {}
        for query in TOSS_QUERIES:
            stop = _newest(stored, list(query.columns)) or floor
            try:
                got = await self._walk_toss(toss, query, symbol, last, max(stop, floor))
            except TossError as e:
                if _refused(e):
                    self._save(symbol, TOSS, stored, fetched, floor)
                    raise
                log.warning("%s supply (%s) of %s not updated: %s", TOSS, query.name, symbol, e)
                continue
            except (aiohttp.ClientError, OSError, ValueError, KeyError, TimeoutError) as e:
                log.warning("%s supply (%s) of %s not updated: %s", TOSS, query.name, symbol, e)
                continue
            for day, values in got.items():
                fetched.setdefault(day, {}).update(values)
        return self._save(symbol, TOSS, stored, fetched, floor)

    def _save(
        self, symbol: str, source: str, stored: list[Row], fetched: Mapping[date, Row], floor: date
    ) -> bool:
        if not fetched:
            return False
        rows = _updated(stored, fetched, floor)
        if rows == stored:
            return False
        write_supply(self._state_dir, symbol, source, rows)
        return True

    async def _walk_kis(self, query: KisQuery, symbol: str, last: date, stop: date) -> dict[date, Row]:
        """The days KIS gives up to ``last``, paging back until a page reaches ``stop`` (or
        brings nothing new)."""
        found: dict[date, Row] = {}
        end = last
        for _ in range(_MAX_CALLS):
            await self._pace.acquire()
            body = await self._client.get(query.tr, query.path, query.params(symbol, end))
            new: dict[date, Row] = {}
            # KIS pads an answer with blank rows; keep the dated ones.
            for row in body.get(query.rows_key) or []:
                day = _day(row.get(query.date_field), "%Y%m%d") if isinstance(row, Mapping) else None
                if day is None or day > last or day in found:
                    continue
                values = {name: _whole(row.get(field)) for name, field in query.columns.items()}
                new[day] = no_figure(values, day)
            if not new:
                break
            found.update(new)
            oldest = min(new)
            if oldest <= stop:
                break
            end = oldest - timedelta(days=1)
        else:
            raise ValueError(f"{symbol} {query.tr} still going after {_MAX_CALLS} calls")
        return found

    async def _walk_toss(
        self, toss: TossClient, query: TossQuery, symbol: str, last: date, stop: date
    ) -> dict[date, Row]:
        """The days Toss gives up to ``last``, paging back by ``nextUntil`` until a page
        reaches ``stop`` (or Toss has nothing older)."""
        found: dict[date, Row] = {}
        until = last
        for _ in range(_MAX_CALLS):
            params = {"count": str(TOSS_COUNT), "until": until.isoformat()}
            result = await toss.get(f"{STOCKS}/{symbol}/{query.name}", params)
            records = result.get("records") if isinstance(result, Mapping) else None
            new: dict[date, Row] = {}
            for record in records or []:
                day = _day(record.get("date"), "%Y-%m-%d") if isinstance(record, Mapping) else None
                if day is None or day > last or day in found:
                    continue
                new[day] = {
                    name: (_percent if name == "foreign_ratio" else _whole)(_at(record, path))
                    for name, path in query.columns.items()
                }
            if not new:
                break
            found.update(new)
            next_until = result.get("nextUntil") if isinstance(result, Mapping) else None
            if min(new) <= stop or not next_until:
                break
            until = date.fromisoformat(str(next_until))
        else:
            raise ValueError(f"{symbol} Toss {query.name} still going after {_MAX_CALLS} calls")
        return found


def toss_client_maker(
    session: aiohttp.ClientSession, settings: Settings, now: Callable[[], datetime]
) -> Callable[[], TossClient] | None:
    """A new Toss client on each call, sharing the client's one token (``toss.py``); None when
    the client is not set (``TOSS_CLIENT_ID``, ``TOSS_CLIENT_SECRET``)."""
    if not (settings.toss_client_id and settings.toss_client_secret):
        return None
    tokens = TossTokens(
        session,
        settings.toss_url,
        settings.toss_client_id,
        settings.toss_client_secret,
        settings.state_dir,
        now,
    )
    return lambda: TossClient(session, settings.toss_url, tokens)
