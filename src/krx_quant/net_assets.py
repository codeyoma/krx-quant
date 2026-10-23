"""The single-stock leveraged and inverse ETFs' net assets (#305): A, a product's net assets on
the previous trading day, in the closing flow's demand A·(L² − L)·r.

**Source.** KIS's ETF/ETN current price (``FHPST02400000``, market ``J``), its net asset total
``etf_ntas_ttam`` in 억원 (``UNIT``), stored in won. The server's probe (``probe-nav-305``,
2026-10-21 23:50, docs/kis-feeds.md "ETF 순자산 (#305)") found:

- every one of the 16 products answered, with ``etf_ntas_ttam``, ``prdy_last_nav`` and
  ``lstn_stcn`` (the listed units) given (not 0);
- ``etf_ntas_ttam`` equal to ``lstn_stcn × prdy_last_nav`` in 억원 on 14 of them (within 0.5% or
  half an 억), and on none to ``lstn_stcn × nav``: it is the net assets of ``prdy_last_nav``'s
  day. ``prdy_last_nav`` equalled the daily NAV (``FHPST02440200``) of the trading day before
  the last one on all 16, and ``nav`` the last one's;
- KIS has no query of the past days' net assets or units: the total is today's figure only.

So A is the total as KIS gives it, not the product computed in won: on the two products where
they differed, the units had likely changed (a creation or redemption) while the total still
held the day before's; the total's rounding (half an 억) is far below what the demand's ratio
can tell. The user chose KRX's Open API (``etf_bydd_trd``) for the past days (#305, 2026-10-22):
its rows (``krx_net_assets``, source ``krx``) are read before KIS's (``SOURCES``), product by
product, so KIS's stands in only where KRX's is not in.

**Its day.** A row is keyed by the trading day the net assets are of, never by when they were
fetched: the trading day before the fetch's date whose daily NAV equals the answer's
``prdy_last_nav`` (exactly one, else nothing is stored: two equal NAVs could not be told
apart). The fetch's own date is left out: before its session KIS's daily rows may hold a row of
it. A day t's 15:20 demand reads the row of the trading day before t (``net_assets_for``).

**When.** At the collector's start and daily at 20:30, every product (#305); and on a trading
day from ``RETRY_FROM`` (06:00) to its closing auction, every ``RETRY`` (10 minutes), the
products still lacking the previous trading day's row, until it is in. The retries are there
because when KIS moves to the new day is not measured: at 23:50 its answer was still the day
before's (``prdy_last_nav`` the NAV before the last), so the 20:30 fetch gives day t's A again,
not day t+1's, and whether the morning start (06:00 or 07:20) comes after the move is not known.
Each row keeps when its value was first fetched (``fetched_at``): the first row of a day shows
when KIS moved.

**Values.** A total of 0 or empty is no figure (#295's rule): no row. A value fetched again is
kept as it is (its first ``fetched_at``); a different one replaces it.

**Kept** in ``state/closing_flow/net_assets.parquet``: a row per product, day and source,
rewritten whole, with KRX's listed units and NAV beside its net assets (KIS's rows have none).
The state folder, not the data directory: the KRX Open API's data stays on the server (its terms
forbid passing it on, docs/design.md 4.1), and the archive uploads ``data/`` alone.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import pyarrow as pa
import pyarrow.parquet as pq

from krx_quant.feeds.base import EVENT_TIME, KST, Row
from krx_quant.kis import KisError, RestClient
from krx_quant.market_calendar import MarketCalendar
from krx_quant.nav_probe import DAILY, DAILY_DATE, PRICE, one_object, row_list
from krx_quant.probe_fields import day_of, present, same
from krx_quant.products import Product
from krx_quant.ratelimit import RateLimiter

log = logging.getLogger(__name__)

KIS, KRX = "kis", "krx"
# The sources read, product by product, the first one's row first: KRX's Open API, then KIS (#305).
SOURCES: tuple[str, ...] = (KRX, KIS)
UNIT = 100_000_000  # won in KIS's ``etf_ntas_ttam``: 억원 (probe-nav-305)
NAV_DAYS = timedelta(days=14)  # the daily NAV asked: the two weeks to the fetch's date
EVENING = time(20, 30)  # with the history task's run (``backfill.BARS_FINAL_AT``)
RETRY_FROM = time(6, 0)
RETRY = timedelta(minutes=10)
FILE = "net_assets.parquet"

SCHEMA = pa.schema(
    [
        ("code", pa.string()),
        ("date", pa.date32()),  # the trading day the net assets are of
        ("source", pa.string()),
        ("net_assets", pa.int64()),  # won
        ("fetched_at", EVENT_TIME),  # when this value was first fetched
        ("shares", pa.int64()),  # KRX's listed units (``LIST_SHRS``) that day; null for KIS's rows
        ("nav", pa.float64()),  # KRX's NAV that day; null for KIS's rows
    ]
)


def net_assets_path(state_dir: Path) -> Path:
    return state_dir / "closing_flow" / FILE


def read_net_assets(state_dir: Path) -> list[Row]:
    """Every stored row, by product, day and source; none without the file."""
    path = net_assets_path(state_dir)
    if not path.exists():
        return []
    rows: list[Row] = pq.read_table(path).to_pylist()
    return rows


def write_net_assets(state_dir: Path, rows: Sequence[Row]) -> None:
    """Replace the stored rows, whole or not at all, in order of product, day and source."""
    path = net_assets_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    ordered = sorted(rows, key=lambda r: (r["code"], r["date"], r["source"]))
    table = pa.Table.from_pylist([{n: r.get(n) for n in SCHEMA.names} for r in ordered], schema=SCHEMA)
    pq.write_table(table, tmp)
    tmp.rename(path)


def keep_rows(state_dir: Path, fetched: Sequence[Row]) -> int:
    """Add the fetched rows to the file; return how many changed. A row already held with the
    same values stays as it is (its first ``fetched_at``), another replaces it."""
    if not fetched:
        return 0
    rows = {(r["code"], r["date"], r["source"]): r for r in read_net_assets(state_dir)}
    changed = 0
    for row in fetched:
        key = (row["code"], row["date"], row["source"])
        held = rows.get(key)
        if held is not None and all(held.get(n) == row.get(n) for n in ("net_assets", "shares", "nav")):
            continue
        rows[key] = row
        changed += 1
    if changed:
        write_net_assets(state_dir, list(rows.values()))
    return changed


def source_of(rows: Sequence[Mapping[str, Any]]) -> str | None:
    """The sources of the net assets in ``rows`` (each with ``net_assets_source``, None without
    net assets), in ``SOURCES``' order joined by ``+``: ``krx``, ``kis``, ``krx+kis``; None if
    none has net assets."""
    found = {r["net_assets_source"] for r in rows if r.get("net_assets_source") is not None}
    ordered = [s for s in SOURCES if s in found] + sorted(found - set(SOURCES))
    return "+".join(ordered) or None


def net_assets_on(rows: Sequence[Row], day: date) -> dict[str, Row]:
    """Each product's row of ``day``, from the first source of ``SOURCES`` that has it."""
    found: dict[str, Row] = {}
    for source in SOURCES:
        for row in rows:
            if row["date"] == day and row["source"] == source:
                found.setdefault(row["code"], row)
    return found


def net_assets_for(rows: Sequence[Row], calendar: MarketCalendar, day: date) -> dict[str, Row]:
    """The net assets a trading day's demand reads (A): each product's of the trading day before."""
    return net_assets_on(rows, calendar.previous_trading_day(day))


def demand_day(calendar: MarketCalendar, at: datetime) -> date:
    """The trading day whose closing demand comes next at ``at``: today until its closing
    auction, else the next trading day."""
    day = at.astimezone(KST).date()
    session = calendar.session(day)
    if session is not None and at < session.closing_auction:
        return day
    day += timedelta(days=1)
    while not calendar.is_trading_day(day):
        day += timedelta(days=1)
    return day


def _number(value: Any) -> float | None:
    """A number KIS sent, not empty and not 0 (KIS's 0 may be no figure, #295); None otherwise."""
    if not present(value):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if number != 0 else None


def nav_day(daily: Sequence[Mapping[str, Any]], nav: Any, fetched_on: date) -> date | None:
    """The day before ``fetched_on`` whose daily NAV is ``nav``: exactly one, else None."""
    days = {
        d
        for row in daily
        if (d := day_of(row, DAILY_DATE)) is not None and d < fetched_on and same(row.get("nav"), nav)
    }
    return next(iter(days)) if len(days) == 1 else None


def _ymd(day: date) -> str:
    return day.strftime("%Y%m%d")


class NetAssetKeeper:
    """Fetches the products' net assets from KIS and keeps them (module doc)."""

    def __init__(
        self,
        client: RestClient,
        calendar: MarketCalendar,
        state_dir: Path,
        products: Sequence[Product],
        pace: RateLimiter,
    ) -> None:
        self._client = client
        self.calendar = calendar
        self._state_dir = state_dir
        self.products = tuple(products)
        self._pace = pace  # spaced as the history task's queries, inside the app key's REST limit

    def lacking(self, day: date) -> list[str]:
        """The products whose net assets ``day``'s demand needs and the file does not hold."""
        held = net_assets_for(read_net_assets(self._state_dir), self.calendar, day)
        return [p.code for p in self.products if p.code not in held]

    async def run(self, now: datetime, codes: Sequence[str] | None = None) -> int:
        """Fetch the net assets of every product (of ``codes`` only, if given) and keep them;
        return how many rows changed. A product that fails is logged and left as it was."""
        fetched: list[Row] = []
        for product in self.products:
            if codes is not None and product.code not in codes:
                continue
            try:
                row = await self._fetch(product, now)
            except (KisError, aiohttp.ClientError, OSError, ValueError, KeyError, TimeoutError) as e:
                log.warning("net assets of %s not fetched: %s", product.code, e)
                continue
            if row is not None:
                fetched.append(row)
        return self._keep(fetched)

    async def _fetch(self, product: Product, now: datetime) -> Row | None:
        """The product's net assets as KIS gives them now, keyed by their day; None without."""
        codes = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": product.code}
        await self._pace.acquire()
        price = one_object(await self._client.get(PRICE.tr, PRICE.path, codes))
        if price is None:
            raise ValueError(f"{PRICE.tr} answered no object")
        total, nav = _number(price.get("etf_ntas_ttam")), price.get("prdy_last_nav")
        if total is None or _number(nav) is None:
            log.info("net assets of %s: no figure now (total or previous NAV empty or 0)", product.code)
            return None
        today = now.astimezone(KST).date()
        params = {**codes, "FID_INPUT_DATE_1": _ymd(today - NAV_DAYS), "FID_INPUT_DATE_2": _ymd(today)}
        await self._pace.acquire()
        daily = row_list(await self._client.get(DAILY.tr, DAILY.path, params))
        day = nav_day(daily or [], nav, today)
        if day is None:
            log.warning("net assets of %s: the previous NAV is no one day's of the daily NAV", product.code)
            return None
        return {
            "code": product.code,
            "date": day,
            "source": KIS,
            "net_assets": round(total * UNIT),
            "fetched_at": now.astimezone(KST).replace(microsecond=0),
        }

    def _keep(self, fetched: Sequence[Row]) -> int:
        return keep_rows(self._state_dir, fetched)


def next_ask(
    calendar: MarketCalendar, after: datetime, lacking: Callable[[date], Sequence[str]]
) -> tuple[datetime, tuple[str, ...] | None]:
    """When the keeper asks next after ``after``, and which products (None: every one): the
    next 20:30, or before it, on the next demand's day, every ``RETRY`` from ``RETRY_FROM`` to
    its closing auction, the products lacking that day's net assets."""
    local = after.astimezone(KST)
    evening = datetime.combine(local.date(), EVENING, KST)
    if evening <= local:
        evening += timedelta(days=1)
    day = demand_day(calendar, after)
    session = calendar.session(day)
    retry = max(local + RETRY, datetime.combine(day, RETRY_FROM, KST))
    if session is not None and retry < min(session.closing_auction, evening):
        codes = tuple(lacking(day))
        if codes:
            return retry, codes
    return evening, None


async def keep_net_assets(
    keeper: NetAssetKeeper,
    now: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """The collector's net-asset task (module doc "When"), until cancelled. The sleep is a
    parameter for tests."""
    codes: tuple[str, ...] | None = None  # at start: every product
    while True:
        started = now()
        try:
            changed = await keeper.run(started, codes)
            if changed:
                log.info("kept %d net asset rows", changed)
        except Exception:
            log.exception("the net assets failed")
        when, codes = next_ask(keeper.calendar, max(started, now()), keeper.lacking)
        await sleep(max(0.0, (when - now()).total_seconds()))
