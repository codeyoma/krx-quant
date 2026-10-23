"""The single-stock ETFs' net assets from the KRX Open API (#305): its ETF daily trading service
(``etf_bydd_trd``, ETF 일별매매정보) gives every day's, so the closing flow's past days have their A,
and the day's demand has the previous trading day's from 08:05.

**What the server measured** (``probe-krx-etf-305``, 2026-10-23 23:57, docs/krx-openapi.md):

- one call a day (``basDd``, the key in the ``AUTH_KEY`` header) gives every ETF of that day in
  ``OutBlock_1``; the 16 products' rows carry their code in ``ISU_CD`` as it is, every field
  filled, no thousands commas and no ``-``; ``BAS_DD`` is the day asked on every product row;
- rows on every trading day asked from the listing day (2026-05-27) on, none on the day before;
- ``INVSTASST_NETASST_TOTAMT`` (순자산총액) is in won: where it matched ``LIST_SHRS × NAV`` (169 of
  400 rows, within 0.5%) it did in won and in no other unit; ``NAV`` is the row's day's (KIS's daily
  NAV of that day on all 400);
- at 23:57 the day's rows were not in yet and the day before's were (KRX's FAQ: from 08:00 on the
  next business day).

**A.** Day t's demand reads ``INVSTASST_NETASST_TOTAMT`` of t−1 as KRX gives it (``net_assets``):
the fund's own net assets at t−1's close, which its exposure was set to then. Not ``LIST_SHRS ×
NAV``: ``LIST_SHRS`` is the units listed on the exchange that day (``MKTCAP ÷ TDD_CLSPRC`` equals it
on all 400 rows), and it changes from one trading day to the next on 31 of 48 pairs. The other 231
rows are taken to be the days whose creations or redemptions the fund counted that day but the
exchange listed on another (inferred: the scheduled job ``check-krx-net-assets-305`` counts which
day's units the total matches, ``closing_flow_check.check_krx_net_assets``).

**When.**

- At the collector's start: every trading day from the products' listing whose rows are out (from
  08:00 on the trading day after it, ``published``) and not kept, one call a day ``PACE`` apart:
  about 100 calls the first time.
- On each trading day from ``ASK_FROM`` (08:05): the same, which takes in the trading day before;
  until that day is in, it alone again every ``RETRY`` (5 minutes) to the closing auction (15:20).
  Not in by then: the day's 15:20 demand reads KIS's net assets (``net_assets.SOURCES``, product by
  product), and the day is asked again the next morning, for the past days.
- A day is kept once the file holds a KRX row of it: KRX gives final values only (FAQ). A product
  without any KRX row (one added to ``products.PRODUCTS`` later) has its days asked again.
- Calls stop for the run after ``MAX_FAILED`` failures in a row (a refused key is refused for every
  day); the next run asks again.

**Checked.** Each day kept is set against KIS's row of the same day where the file has one
(``against_kis``): over ``FLAG`` (0.5%) or half an 억 (KIS's unit) apart is flagged in the log.

**Logged** as counts, dates, times and product codes, never a value: the lines go to Grafana Cloud
Loki (#358), a third party, and KRX's terms forbid passing its data on (docs/design.md 4.1). KRX's
rows stay in ``state/closing_flow/net_assets.parquet``, which the archive never uploads.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import aiohttp

from krx_quant.feeds.base import KST, Row
from krx_quant.krx_etf_probe import (
    BLOCK,
    DAY_FIELD,
    DAY_PARAM,
    HINTS,
    KEY_HEADER,
    NAV,
    PATH,
    SHARES,
    TOTAL,
    number,
)
from krx_quant.loki import TEMPLATE_ONLY
from krx_quant.market_calendar import MarketCalendar
from krx_quant.net_assets import KIS, KRX, UNIT, demand_day, keep_rows, read_net_assets
from krx_quant.probe_fields import day_of
from krx_quant.products import Product

log = logging.getLogger(__name__)

CODE_FIELD = "ISU_CD"  # the product's code as it is (probe-krx-etf-305)
PUBLISHED_AT = time(8, 0)  # a day's rows are out from then on the next business day (KRX FAQ)
ASK_FROM = time(8, 5)
RETRY = timedelta(minutes=5)
PACE = 1.0  # seconds between calls: KRX sets no per-second limit (FAQ); asked gently anyway
MAX_FAILED = 3
REQUEST_TIMEOUT = 30.0  # seconds a call may take
FLAG = 0.005  # KRX's and KIS's net assets this share apart (or half an 억) are flagged


class KrxError(Exception):
    """A call that gave no usable answer; its text carries no value of KRX's."""


class KrxClient:
    """The service's calls: the key in the header, never in the address, a log line or an error."""

    def __init__(self, session: aiohttp.ClientSession, url: str, key: str) -> None:
        self._session = session
        self._url = f"{url}{PATH}"
        self._key = key

    async def rows(self, day: date) -> list[dict[str, Any]]:
        """Every ETF's row of ``day``: none while the day is not out yet."""
        try:
            async with self._session.get(
                self._url,
                params={DAY_PARAM: day.strftime("%Y%m%d")},
                headers={KEY_HEADER: self._key},
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                status, text = resp.status, await resp.text(errors="replace")
        except (aiohttp.ClientError, OSError, ValueError) as e:  # OSError: timeouts too
            raise KrxError(f"error {type(e).__name__}") from None
        if status != 200:
            raise KrxError(f"HTTP {status}" + (f" ({HINTS[status]})" if status in HINTS else ""))
        try:
            body = json.loads(text)
        except ValueError:
            raise KrxError("HTTP 200, not JSON") from None
        block = body.get(BLOCK) if isinstance(body, dict) else None
        if not isinstance(block, list):
            raise KrxError(f"HTTP 200, no {BLOCK}")
        return [r for r in block if isinstance(r, dict)]


def next_trading_day(calendar: MarketCalendar, day: date) -> date:
    day += timedelta(days=1)
    while not calendar.is_trading_day(day):
        day += timedelta(days=1)
    return day


def published(calendar: MarketCalendar, day: date, now: datetime) -> bool:
    """Whether ``day``'s rows are out at ``now``: from ``PUBLISHED_AT`` on the trading day after it."""
    return now >= datetime.combine(next_trading_day(calendar, day), PUBLISHED_AT, KST)


def latest_published(calendar: MarketCalendar, now: datetime) -> date:
    """The latest trading day whose rows are out at ``now``."""
    day = calendar.previous_trading_day(now.astimezone(KST).date() + timedelta(days=1))
    while not published(calendar, day, now):
        day = calendar.previous_trading_day(day)
    return day


def rows_of(
    answer: Sequence[Mapping[str, Any]], day: date, products: Sequence[Product], at: datetime
) -> list[Row]:
    """The products' rows of a day's answer, listed by then: the net assets in won as KRX gives
    them, with the listed units and NAV. A row of another day, or without net assets (empty, ``-``
    or 0: no figure, as KIS's 0, #295), is none; a code's second row is dropped."""
    listed = {p.code for p in products if p.listed <= day}
    found: dict[str, Row] = {}
    for row in answer:
        code = str(row.get(CODE_FIELD) or "").strip()
        if code not in listed or code in found or day_of(row, DAY_FIELD) != day:
            continue
        total, shares = number(row.get(TOTAL)), number(row.get(SHARES))
        if total is None:
            continue
        found[code] = {
            "code": code,
            "date": day,
            "source": KRX,
            "net_assets": round(total),
            "fetched_at": at.astimezone(KST).replace(microsecond=0),
            "shares": None if shares is None else round(shares),
            "nav": number(row.get(NAV)),
        }
    return list(found.values())


def against_kis(rows: Sequence[Row], day: date) -> tuple[int, list[str]]:
    """KRX's net assets of ``day`` against KIS's of the same day: how many products have both, and
    those over ``FLAG`` or half an 억 (KIS gives 억원) apart."""
    krx = {r["code"]: r["net_assets"] for r in rows if r["date"] == day and r["source"] == KRX}
    kis = {r["code"]: r["net_assets"] for r in rows if r["date"] == day and r["source"] == KIS}
    both = sorted(set(krx) & set(kis))
    flagged = [c for c in both if abs(krx[c] - kis[c]) > max(FLAG * abs(krx[c]), UNIT / 2)]
    return len(both), flagged


class KrxNetAssetKeeper:
    """Fetches the products' net assets from the KRX Open API and keeps them (module doc)."""

    def __init__(
        self,
        client: KrxClient,
        calendar: MarketCalendar,
        state_dir: Path,
        products: Sequence[Product],
        now: Callable[[], datetime],
        *,
        pace: float = PACE,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        after: Callable[[datetime], Awaitable[None]] | None = None,
    ) -> None:
        self._client = client
        self.calendar = calendar
        self._state_dir = state_dir
        self.products = tuple(products)
        self._now = now
        self._pace = pace
        self._sleep = sleep
        self._after = after  # once rows were kept: the closing flow's days computed again

    @property
    def first(self) -> date:
        return min(p.listed for p in self.products)

    def _krx_rows(self) -> list[Row]:
        return [r for r in read_net_assets(self._state_dir) if r["source"] == KRX]

    def lacks(self, day: date) -> bool:
        """Whether the file holds no KRX row of ``day``."""
        return all(r["date"] != day for r in self._krx_rows())

    def lacking(self, now: datetime) -> list[date]:
        """The trading days from the products' listing, out by ``now``, that the file holds no KRX
        row of; every one from a product's listing, for a product without any."""
        last = latest_published(self.calendar, now)
        if last < self.first:
            return []
        rows = self._krx_rows()
        held, codes = {r["date"] for r in rows}, {r["code"] for r in rows}
        new = [p.listed for p in self.products if p.code not in codes]
        return [
            d
            for d in self.calendar.trading_days(self.first, last)
            if d not in held or any(listed <= d for listed in new)
        ]

    async def run(self, days: Sequence[date] | None = None) -> int:
        """Fetch ``days`` (every lacking one if None) and keep their rows; return how many rows
        changed. A day that fails is logged and left for the next run."""
        asked = self.lacking(self._now()) if days is None else list(days)
        if not asked:
            return 0
        changed = came = empty = failed = in_a_row = 0
        for i, day in enumerate(asked):
            if i:
                await self._sleep(self._pace)
            try:
                answer = await self._client.rows(day)
            except KrxError as e:
                failed += 1
                in_a_row += 1
                log.warning("KRX net assets of %s not fetched: %s", day, e)
                if in_a_row >= MAX_FAILED:
                    log.warning("KRX net assets: stopped after %d failures in a row", in_a_row)
                    break
                continue
            in_a_row = 0
            at = self._now()
            rows = rows_of(answer, day, self.products, at)
            if not rows:
                empty += 1
                log.debug("KRX net assets of %s: not out yet (%d rows)", day, len(answer))
                continue
            came += 1
            changed += keep_rows(self._state_dir, rows)
            if days is not None or len(asked) == 1:
                log.info(
                    "KRX net assets of %s in at %s: %d products",
                    day,
                    at.astimezone(KST).strftime("%H:%M:%S"),
                    len(rows),
                )
            self._check(day)
        # A retry that found the day not out yet says so at DEBUG: every five minutes on a late day.
        log.log(
            logging.DEBUG if days is not None and not came and not failed else logging.INFO,
            "KRX net assets: %d days asked (%s – %s), %d in, %d without product rows, %d failed;"
            " %d rows kept",
            len(asked),
            asked[0],
            asked[-1],
            came,
            empty,
            failed,
            changed,
        )
        if changed and self._after is not None:
            await self._after(self._now())
        return changed

    def _check(self, day: date) -> None:
        compared, flagged = against_kis(read_net_assets(self._state_dir), day)
        if not compared:
            return
        if flagged:
            log.warning(
                "KRX net assets of %s against KIS's: %d compared, %d over %.1f%% (%s)",
                day,
                compared,
                len(flagged),
                FLAG * 100,
                ", ".join(flagged),
            )
        else:
            log.info(
                "KRX net assets of %s against KIS's: %d compared, 0 over %.1f%%", day, compared, FLAG * 100
            )


@dataclass(frozen=True)
class Ask:
    """When the keeper asks next, which days (None: every lacking one), and the day it gave up on
    for the day's demand (None: none)."""

    when: datetime
    days: tuple[date, ...] | None
    gave_up: date | None = None


def next_ask(calendar: MarketCalendar, after: datetime, lacks: Callable[[date], bool]) -> Ask:
    """After a run that ended at ``after``: on the next demand's day, ``ASK_FROM`` for every lacking
    day; from then, while the trading day before it is lacking, it alone every ``RETRY`` to the
    closing auction; then the next demand's day's ``ASK_FROM``."""
    day = demand_day(calendar, after)
    start = datetime.combine(day, ASK_FROM, KST)
    if after < start:
        return Ask(start, None)
    session = calendar.session(day)
    needed = calendar.previous_trading_day(day)
    tomorrow = Ask(datetime.combine(next_trading_day(calendar, day), ASK_FROM, KST), None)
    if session is None or not lacks(needed):
        return tomorrow
    retry = after + RETRY
    if retry < session.closing_auction:
        return Ask(retry, (needed,))
    return Ask(tomorrow.when, None, needed)


async def keep_krx_net_assets(
    keeper: KrxNetAssetKeeper,
    now: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """The collector's KRX net-asset task (module doc "When"), until cancelled. The sleep is a
    parameter for tests."""
    days: tuple[date, ...] | None = None  # at start: every lacking day
    while True:
        try:
            await keeper.run(days)
        except Exception:
            # The traceback stays on the server: its message may carry a value (Loki gets the line's
            # template and the exception's type, ``loki.TEMPLATE_ONLY``).
            log.exception("the KRX net assets failed", extra=TEMPLATE_ONLY)
        ask = next_ask(keeper.calendar, now(), keeper.lacks)
        if ask.gave_up is not None:
            log.warning(
                "KRX net assets of %s not in by the closing auction: the day's demand reads KIS's",
                ask.gave_up,
            )
        days = ask.days
        await sleep(max(0.0, (ask.when - now()).total_seconds()))
