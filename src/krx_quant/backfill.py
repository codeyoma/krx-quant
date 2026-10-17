"""Backfiller: one-minute bars from KIS REST for every trading day of the past year.

Order book and tick history cannot be queried anywhere, so only the collector's own
recording has them and their gaps stay unfillable. One-minute bars can be queried for
about a year back, so the Backfiller keeps the ``bars`` layer complete: its first run fills
a year for each series, and later runs fill whichever trading days are still missing
(outages, new symbols). The series:

- each stock and KRX-listed ETF (``FHKST03010230``, at most 120 bars per query);
- KOSPI200 futures and USD futures, each one continuous series of every day's nearest
  month, rolled on its last trading day (``FHKIF03020200``, at most 102 bars per query;
  ``contracts``). Each bar keeps the contract it came from.

A day is fetched only once all its sessions are over, always whole, and written as one
file that replaces any earlier one. So a day's bars are either complete or absent, and
filling a day again gives the same file. A day KIS answers with no bars at all is not
stored, so the next run asks again. The queries share the collector's REST limiter.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import partial
from pathlib import Path
from typing import ClassVar

import aiohttp
import pyarrow as pa
import pyarrow.parquet as pq

from krx_quant.auth import tokens_and_client
from krx_quant.config import Settings
from krx_quant.contracts import KOSPI200, USD, Product, front_month
from krx_quant.features import SLOT_RETURN_DAYS, day_slot_ranges, day_slot_returns
from krx_quant.feeds.base import EVENT_TIME, KST, Row, kst, kst_day, to_float, to_int
from krx_quant.kis import KisError, RestClient
from krx_quant.market_calendar import CalendarStore, MarketCalendar, calendar_store
from krx_quant.ratelimit import RateLimiter
from krx_quant.reference import Bar
from krx_quant.toss import TossClient

log = logging.getLogger(__name__)

MINUTE_BARS_TR = "FHKST03010230"
MINUTE_BARS_PATH = "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice"
BARS_PER_QUERY = 120
FUTURES_BARS_TR = "FHKIF03020200"
FUTURES_BARS_PATH = "/uapi/domestic-futureoption/v1/quotations/inquire-time-fuopchartprice"
FUTURES_BARS_PER_QUERY = 102
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

FUTURES_BAR_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),  # the continuous series, e.g. KOSPI200F
        ("bar_time", EVENT_TIME),
        ("contract", pa.string()),  # the month the bar came from, e.g. A01612
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("volume", pa.int64()),
        ("cum_value", pa.int64()),
    ]
)


@dataclass(frozen=True)
class Query:
    """One way to ask KIS for a day of a series' bars, walked back from the day's end."""

    tr_id: str
    path: str
    params: Mapping[str, str]  # all but the day and the hour
    per_page: int  # the most bars KIS answers at once
    to_bar: Callable[[date, dict[str, str]], Row]  # a KIS row as the bar stored


@dataclass(frozen=True)
class StockBars:
    """A stock's or ETF's bars: integrated (KRX + NXT) where KIS has them, else KRX."""

    symbol: str
    feed: ClassVar[str] = MINUTE_BARS_TR
    schema: ClassVar[pa.Schema] = BAR_SCHEMA

    @property
    def name(self) -> str:
        return self.symbol

    def queries(self, day: date, calendar: MarketCalendar) -> list[Query]:
        """One per market, in the order tried."""
        return [
            Query(
                self.feed,
                MINUTE_BARS_PATH,
                {
                    "FID_COND_MRKT_DIV_CODE": market,
                    "FID_INPUT_ISCD": self.symbol,
                    "FID_PW_DATA_INCU_YN": "N",  # that day only
                    "FID_FAKE_TICK_INCU_YN": "",
                },
                BARS_PER_QUERY,
                partial(_bar, self.symbol, venue),
            )
            for market, venue in MARKETS.items()
        ]


@dataclass(frozen=True)
class FuturesBars:
    """A futures product's continuous bars: each day's from that day's month (``front_month``)."""

    product: Product
    feed: ClassVar[str] = FUTURES_BARS_TR
    schema: ClassVar[pa.Schema] = FUTURES_BAR_SCHEMA

    @property
    def name(self) -> str:
        return self.product.series

    def queries(self, day: date, calendar: MarketCalendar) -> list[Query]:
        contract = front_month(self.product, day, calendar)
        params = {
            "FID_COND_MRKT_DIV_CODE": self.product.market,
            "FID_INPUT_ISCD": contract,
            "FID_HOUR_CLS_CODE": "60",  # one-minute bars
            "FID_PW_DATA_INCU_YN": "N",  # that day only
            "FID_FAKE_TICK_INCU_YN": "N",
        }
        to_bar = partial(_futures_bar, self.product, contract)
        return [Query(self.feed, FUTURES_BARS_PATH, params, FUTURES_BARS_PER_QUERY, to_bar)]


Series = StockBars | FuturesBars


def bars_series(settings: Settings) -> list[Series]:
    """Stocks and the KRX-listed ETFs, then KOSPI200 and USD futures."""
    symbols = settings.symbols + tuple(s for s in settings.etf_symbols if s not in settings.symbols)
    return [*(StockBars(s) for s in symbols), FuturesBars(KOSPI200), FuturesBars(USD)]


class Backfiller:
    def __init__(
        self,
        client: RestClient,
        calendar: MarketCalendar,
        data_dir: Path,
        series: Sequence[Series],
        history_days: int,
        max_per_second: float,
    ) -> None:
        self._client = client
        self._calendar = calendar
        self._data_dir = data_dir
        self._series = tuple(series)
        self._history = timedelta(days=history_days)
        # Evenly spaced under the shared REST limit: KIS refuses bursts (docs/kis-feeds.md),
        # and the polls need room. The day candles keep the same pace (``candles``).
        self.pace = RateLimiter(1, 1 / max_per_second, margin=0)

    async def run(self, now: datetime) -> int:
        """Fill every finished trading day of the window that has no bars yet, newest first;
        return how many series-days were filled.

        A day that fails is logged and left for the next run. After a few failures in a
        row the run stops, so an unreachable KIS is not queried for a whole year of days.
        """
        local = now.astimezone(KST)
        today = local.date()
        last = today if local.time() >= BARS_FINAL_AT else today - timedelta(days=1)
        days = self._calendar.trading_days(today - self._history, last)
        todo = [(s, d) for d in reversed(days) for s in self._series if not self._path(s, d).exists()]
        if not todo:
            return 0
        log.info("backfilling one-minute bars for %d series-days", len(todo))
        filled = failures = 0
        for series, day in todo:
            try:
                found = await self.fill(series, day)
            except (KisError, aiohttp.ClientError, OSError, ValueError, KeyError, TimeoutError) as e:
                failures += 1
                log.warning("one-minute bars for %s on %s not filled: %s", series.name, day, e)
                if failures >= _MAX_FAILURES:
                    log.error("backfill stopped after %d failures in a row; the next run retries", failures)
                    break
                continue
            failures = 0
            if found:
                filled += 1
            else:
                log.info("no one-minute bars for %s on %s yet; the next run asks again", series.name, day)
        log.info("backfilled %d of %d series-days", filled, len(todo))
        return filled

    async def fill(self, series: Series, day: date) -> bool:
        """Fetch ``day``'s bars of ``series`` and replace whatever is stored.

        Return False, storing nothing, when KIS has no bars for the day.
        """
        bars = await self._fetch(series, day)
        if not bars:
            return False
        path = self._path(series, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        pq.write_table(pa.Table.from_pylist(bars, schema=series.schema), tmp)
        tmp.rename(path)
        return True

    async def _fetch(self, series: Series, day: date) -> list[Row]:
        """The day's bars, oldest first, from the first query that has any."""
        for query in series.queries(day, self._calendar):
            bars = await self._walk(query, day)
            if bars:
                return bars
        return []

    async def _walk(self, query: Query, day: date) -> list[Row]:
        """Queries walk back from the day's end, a page of bars at a time."""
        bars: dict[datetime, Row] = {}
        ymd = day.strftime("%Y%m%d")
        hour = _DAY_END
        while True:
            params = {**query.params, "FID_INPUT_DATE_1": ymd, "FID_INPUT_HOUR_1": hour}
            await self.pace.acquire()
            body = await self._client.get(query.tr_id, query.path, params)
            page = body.get("output2") or []
            # KIS pads an empty answer with blank rows; keep this day's bars only.
            rows = [query.to_bar(day, r) for r in page if r.get("stck_bsop_date") == ymd]
            new = [r for r in rows if r["bar_time"] not in bars]
            bars.update((r["bar_time"], r) for r in new)
            if len(page) < query.per_page or not new:
                break
            earliest = min(r["bar_time"] for r in new)
            if earliest.time() == time(0, 0):
                break
            hour = (earliest - timedelta(minutes=1)).strftime("%H%M%S")
        return [bars[t] for t in sorted(bars)]

    def _path(self, series: Series, day: date) -> Path:
        return self._data_dir / bars_key(day, series.feed, series.name)


def bars_key(day: date, feed: str, name: str) -> str:
    """Where a day's bars of a series are, under the data directory or the Object Store."""
    return f"bars/date={day.isoformat()}/feed={feed}/symbol={name}/bars.parquet"


def stock_bars_path(data_dir: Path, day: date, symbol: str) -> Path:
    """A stock's or ETF's bars of ``day`` under the data directory, whether filled or not."""
    return data_dir / bars_key(day, MINUTE_BARS_TR, symbol)


def read_bars(data_dir: Path, day: date, feed: str, name: str) -> list[Bar] | None:
    """A day's backfilled bars of a series, as stored (oldest first), if it has a file."""
    path = data_dir / bars_key(day, feed, name)
    if not path.exists():
        return None
    return [_stored_bar(row) for row in pq.read_table(path).to_pylist()]


def _stored_bar(row: Row) -> Bar:
    start = row["bar_time"]
    return Bar(
        start=start,
        open=row["open"],
        high=row["high"],
        low=row["low"],
        close=row["close"],
        volume=row["volume"],
        trades=0,  # the backfill has no trade counts
        first_at=start,
        last_at=start,
    )


class PreviousDay:
    """Each symbol's previous trading day (``features.PreviousDays``), from that day's
    backfilled bars under the data directory: its one-minute ranges by 30-minute slot (#181,
    ``features.day_slot_ranges``: the range's ``range_prev_day``) and its one-minute bars of
    continuous trading, from the open to the closing auction (#183: the barrier's width); empty
    without them. The same on a recorded day and a backfilled one: every finished trading day
    is backfilled (20:30). Read once per symbol and day; a file that cannot be read is logged
    and counts as none (the engine predicts on)."""

    def __init__(self, data_dir: Path, calendar: MarketCalendar) -> None:
        self._data_dir = data_dir
        self._calendar = calendar
        self._read: dict[tuple[str, date], tuple[dict[str, float], list[Bar]]] = {}
        self._slot_returns: dict[tuple[str, date], dict[str, float]] = {}  # of the day itself

    def __call__(self, symbol: str, day: date) -> Mapping[str, float]:
        return self._of(symbol, day)[0]

    def bars(self, symbol: str, day: date) -> Sequence[Bar]:
        return self._of(symbol, day)[1]

    def slot_returns(self, symbol: str, day: date) -> Sequence[Mapping[str, float]]:
        """The returns by 10-minute slot (#265) of the ``max(SLOT_RETURN_DAYS)`` trading days
        before ``day``, the latest first: each from its backfilled bars, read once; a day
        without them is empty. A day before the calendar knows none ends the list."""
        known = self._calendar.known()
        first = min(known) if known else None
        out: list[Mapping[str, float]] = []
        before = day
        for _ in range(max(SLOT_RETURN_DAYS)):
            before = self._calendar.previous_trading_day(before)
            if first is not None and before < first:
                break
            out.append(self._returns_of(symbol, before))
        return out

    def _returns_of(self, symbol: str, day: date) -> dict[str, float]:
        """A day's slot returns, read once; a day without its bars (or a session) is empty and
        asked again next time: the backfill may fill it while the engine runs."""
        if (symbol, day) in self._slot_returns:
            return self._slot_returns[(symbol, day)]
        session = self._calendar.session(day)
        try:
            bars = read_bars(self._data_dir, day, MINUTE_BARS_TR, symbol)
        except Exception:
            log.exception("cannot read %s's bars of %s; its slot returns go without them", symbol, day)
            bars = None
        if bars is None or session is None:
            return {}
        found = self._slot_returns[(symbol, day)] = day_slot_returns(bars, session)
        return found

    def _of(self, symbol: str, day: date) -> tuple[dict[str, float], list[Bar]]:
        if (symbol, day) not in self._read:
            before = self._calendar.previous_trading_day(day)
            session = self._calendar.session(before)
            try:
                bars = read_bars(self._data_dir, before, MINUTE_BARS_TR, symbol)
            except Exception:
                log.exception(
                    "cannot read %s's bars of %s; its range_prev_day and barrier width go without them",
                    symbol,
                    before,
                )
                bars = None
            if bars is None or session is None:
                self._read[(symbol, day)] = ({}, [])
            else:
                trading = sorted(
                    (b for b in bars if session.open <= b.start < session.closing_auction),
                    key=lambda b: b.start,
                )
                self._read[(symbol, day)] = (day_slot_ranges(bars, session), trading)
        return self._read[(symbol, day)]


def stored_bars(data_dir: Path, symbol: str, before: datetime, limit: int) -> list[Row]:
    """The last ``limit`` stored bars of a stock or ETF that start before ``before``, across
    days, oldest first (the chart scrolled back, #108). Days are read newest first, only
    as many as needed.
    """
    days = sorted(
        (date.fromisoformat(d.name.removeprefix("date=")) for d in (data_dir / "bars").glob("date=*")),
        reverse=True,
    )
    found: list[Row] = []
    for day in days:
        if len(found) >= limit:
            break
        path = stock_bars_path(data_dir, day, symbol)
        if day > kst_day(before) or not path.exists():
            continue
        bars = [r for r in pq.read_table(path).to_pylist() if r["bar_time"] < before]
        found = sorted(bars, key=lambda r: r["bar_time"]) + found
    return found[-limit:] if limit > 0 else []


def _bar(symbol: str, venue: str, day: date, r: dict[str, str]) -> Row:
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


def _futures_bar(product: Product, contract: str, day: date, r: dict[str, str]) -> Row:
    return {
        "symbol": product.series,
        "bar_time": kst(day, r["stck_cntg_hour"]),
        "contract": contract,
        "open": to_float(r["futs_oprc"]),
        "high": to_float(r["futs_hgpr"]),
        "low": to_float(r["futs_lwpr"]),
        "close": to_float(r["futs_prpr"]),
        "volume": to_int(r["cntg_vol"]),
        "cum_value": to_int(r["acml_tr_pbmn"]) * product.value_unit,  # in won, like stocks
    }


def backfiller_for(settings: Settings, client: RestClient, store: CalendarStore) -> Backfiller:
    return Backfiller(
        client,
        store.calendar,
        settings.data_dir,
        bars_series(settings),
        settings.backfill_days,
        settings.backfill_max_per_second,
    )


async def keep_history(
    settings: Settings,
    client: RestClient,
    store: CalendarStore,
    now: Callable[[], datetime],
    toss: Callable[[], TossClient] | None = None,
) -> None:
    """The collector's history task: at start, then daily once the day's bars are final,
    refresh the calendar, fill missing one-minute bars, add the new day candles (``candles``)
    and the daily supply data (``supply``, from KIS and, given ``toss``, Toss). Runs until
    cancelled.
    """
    from krx_quant.candles import CandleKeeper  # it builds on this module's markets
    from krx_quant.supply import SupplyKeeper

    backfiller = backfiller_for(settings, client, store)
    candles = CandleKeeper(client, store.calendar, settings.data_dir, settings.symbols, backfiller.pace)
    supply = SupplyKeeper(client, store.calendar, settings.state_dir, settings.symbols, backfiller.pace, toss)
    while True:
        started = now()
        try:
            await store.refresh(client, kst_day(started))
            await backfiller.run(started)
            await candles.run(started)
            await supply.run(started)
        except Exception:
            log.exception("calendar refresh, backfill, candles or supply failed")
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
