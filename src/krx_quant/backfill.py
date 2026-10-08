"""Backfiller: one-minute bars from KIS REST for every trading day of the past year.

Order book and tick history cannot be queried anywhere, so only the collector's own
recording has them and their gaps stay unfillable. One-minute bars can be queried for
about a year back (``FHKST03010230``, at most 120 bars per query), so the Backfiller keeps
the ``bars`` layer complete: its first run fills a year for each symbol, and later runs
fill whichever trading days are still missing (outages, new symbols).

A day is fetched only once all its sessions are over, always whole, and written as one
file that replaces any earlier one. So a day's bars are either complete or absent, and
filling a day again gives the same file. A day KIS answers with no bars at all is not
stored, so the next run asks again. The queries share the collector's REST limiter.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path

import aiohttp
import pyarrow as pa
import pyarrow.parquet as pq

from krx_quant.auth import tokens_and_client
from krx_quant.config import Settings
from krx_quant.feeds.base import EVENT_TIME, KST, Row, kst, kst_day, to_int
from krx_quant.kis import KisError, RestClient
from krx_quant.market_calendar import CalendarStore, MarketCalendar, calendar_store
from krx_quant.ratelimit import RateLimiter

log = logging.getLogger(__name__)

MINUTE_BARS_TR = "FHKST03010230"
MINUTE_BARS_PATH = "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice"
BARS_PER_QUERY = 120
# KIS market code -> venue recorded, in the order tried. Integrated KRX + NXT bars
# (08:00–20:00) first, like the real-time feeds; KIS has none for what NXT does not trade
# (ETFs), which take KRX bars instead.
MARKETS = {"UN": "UN", "J": "KRX"}
# The KRX after-market and NXT close at 20:00; a day's bars are final after that.
BARS_FINAL_AT = time(20, 30)
# Queries walk back through a day from here, newest first.
_DAY_END = "235959"
# Consecutive failed days after which a run gives up until the next one.
_MAX_FAILURES = 3

BAR_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("bar_time", EVENT_TIME),
        ("venue", pa.string()),
        ("open", pa.int64()),
        ("high", pa.int64()),
        ("low", pa.int64()),
        ("close", pa.int64()),
        ("volume", pa.int64()),
        ("cum_value", pa.int64()),
    ]
)


def bars_symbols(settings: Settings) -> tuple[str, ...]:
    """Stocks and the KRX-listed ETFs: both have KIS minute bars."""
    return settings.symbols + tuple(s for s in settings.etf_symbols if s not in settings.symbols)


class Backfiller:
    def __init__(
        self,
        client: RestClient,
        calendar: MarketCalendar,
        data_dir: Path,
        symbols: Sequence[str],
        history_days: int,
        max_per_second: float,
    ) -> None:
        self._client = client
        self._calendar = calendar
        self._data_dir = data_dir
        self._symbols = tuple(symbols)
        self._history = timedelta(days=history_days)
        # Evenly spaced under the shared REST limit: KIS refuses bursts (docs/kis-feeds.md),
        # and the polls need room.
        self._pace = RateLimiter(1, 1 / max_per_second, margin=0)

    async def run(self, now: datetime) -> int:
        """Fill every finished trading day of the window that has no bars yet, newest first;
        return how many symbol-days were filled.

        A day that fails is logged and left for the next run. After a few failures in a
        row the run stops, so an unreachable KIS is not queried for a whole year of days.
        """
        local = now.astimezone(KST)
        today = local.date()
        last = today if local.time() >= BARS_FINAL_AT else today - timedelta(days=1)
        days = self._calendar.trading_days(today - self._history, last)
        todo = [(s, d) for d in reversed(days) for s in self._symbols if not self._path(s, d).exists()]
        if not todo:
            return 0
        log.info("backfilling one-minute bars for %d symbol-days", len(todo))
        filled = failures = 0
        for symbol, day in todo:
            try:
                found = await self.fill(symbol, day)
            except (KisError, aiohttp.ClientError, OSError, ValueError, KeyError, TimeoutError) as e:
                failures += 1
                log.warning("one-minute bars for %s on %s not filled: %s", symbol, day, e)
                if failures >= _MAX_FAILURES:
                    log.error("backfill stopped after %d failures in a row; the next run retries", failures)
                    break
                continue
            failures = 0
            if found:
                filled += 1
            else:
                log.info("no one-minute bars for %s on %s yet; the next run asks again", symbol, day)
        log.info("backfilled %d of %d symbol-days", filled, len(todo))
        return filled

    async def fill(self, symbol: str, day: date) -> bool:
        """Fetch ``day``'s bars for ``symbol`` and replace whatever is stored.

        Return False, storing nothing, when KIS has no bars for the day.
        """
        bars = await self._fetch(symbol, day)
        if not bars:
            return False
        path = self._path(symbol, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        pq.write_table(pa.Table.from_pylist(bars, schema=BAR_SCHEMA), tmp)
        tmp.rename(path)
        return True

    async def _fetch(self, symbol: str, day: date) -> list[Row]:
        """The day's bars, oldest first, from the first market that has any."""
        for market in MARKETS:
            bars = await self._fetch_market(symbol, day, market)
            if bars:
                return bars
        return []

    async def _fetch_market(self, symbol: str, day: date, market: str) -> list[Row]:
        """Queries walk back from the day's end, 120 bars at a time."""
        bars: dict[datetime, Row] = {}
        ymd = day.strftime("%Y%m%d")
        hour = _DAY_END
        while True:
            params = {
                "FID_COND_MRKT_DIV_CODE": market,
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_HOUR_1": hour,
                "FID_INPUT_DATE_1": ymd,
                "FID_PW_DATA_INCU_YN": "N",  # that day only
                "FID_FAKE_TICK_INCU_YN": "",
            }
            await self._pace.acquire()
            body = await self._client.get(MINUTE_BARS_TR, MINUTE_BARS_PATH, params)
            page = body.get("output2") or []
            # KIS pads an empty answer with blank rows; keep this day's bars only.
            rows = [_bar(symbol, day, MARKETS[market], r) for r in page if r.get("stck_bsop_date") == ymd]
            new = [r for r in rows if r["bar_time"] not in bars]
            bars.update((r["bar_time"], r) for r in new)
            if len(page) < BARS_PER_QUERY or not new:
                break
            earliest = min(r["bar_time"] for r in new)
            if earliest.time() == time(0, 0):
                break
            hour = (earliest - timedelta(minutes=1)).strftime("%H%M%S")
        return [bars[t] for t in sorted(bars)]

    def _path(self, symbol: str, day: date) -> Path:
        return (
            self._data_dir
            / "bars"
            / f"date={day.isoformat()}"
            / f"feed={MINUTE_BARS_TR}"
            / f"symbol={symbol}"
            / "bars.parquet"
        )


def _bar(symbol: str, day: date, venue: str, r: dict[str, str]) -> Row:
    return {
        "symbol": symbol,
        # KIS labels a bar by the minute it starts (09:00 holds the opening auction).
        "bar_time": kst(day, r["stck_cntg_hour"]),
        "venue": venue,
        "open": to_int(r["stck_oprc"]),
        "high": to_int(r["stck_hgpr"]),
        "low": to_int(r["stck_lwpr"]),
        "close": to_int(r["stck_prpr"]),
        "volume": to_int(r["cntg_vol"]),
        # KIS reports traded value cumulated over the day, not per bar.
        "cum_value": to_int(r["acml_tr_pbmn"]),
    }


def backfiller_for(settings: Settings, client: RestClient, store: CalendarStore) -> Backfiller:
    return Backfiller(
        client,
        store.calendar,
        settings.data_dir,
        bars_symbols(settings),
        settings.backfill_days,
        settings.backfill_max_per_second,
    )


async def keep_history(
    settings: Settings, client: RestClient, store: CalendarStore, now: Callable[[], datetime]
) -> None:
    """The collector's history task: at start, then daily once the day's bars are final,
    refresh the calendar and fill missing one-minute bars. Runs until cancelled.
    """
    backfiller = backfiller_for(settings, client, store)
    while True:
        started = now()
        try:
            await store.refresh(client, kst_day(started))
            await backfiller.run(started)
        except Exception:
            log.exception("calendar refresh or backfill failed")
        # The next final time after this run began: a run that started just before 20:30
        # and ended after it goes again at once for the day it could not take yet.
        await asyncio.sleep(max(0.0, (_next(BARS_FINAL_AT, started) - now()).total_seconds()))


def _next(at: time, after: datetime) -> datetime:
    local = after.astimezone(KST)
    target = datetime.combine(local.date(), at, KST)
    return target if target > local else target + timedelta(days=1)


async def run_backfill(settings: Settings, *, clock: Callable[[], datetime] | None = None) -> int:
    """One backfill run outside the collector: refresh the calendar, fill missing days.

    The collector runs the same backfill itself; run this only while it is stopped,
    because both would spend the app key's one REST limit.
    """
    now = clock or (lambda: datetime.now(KST))
    async with aiohttp.ClientSession() as session:
        _, client = tokens_and_client(session, settings, now)
        store = calendar_store(settings)
        await store.refresh(client, kst_day(now()))
        return await backfiller_for(settings, client, store).run(now())
