"""Day, week, month and year candles from KIS's period chart query, kept on the server for
the chart (#111, #112).

The chart's day, week, month and year views scroll back as far as KIS has candles, so the
collector keeps every predicted symbol's whole history of finished candles of each period
(``FHKST03010100``, at most 100 a query). The first run takes all of it, paging back from
the last finished trading day; later runs ask from the last stored candle on and add what
is new. Prices are adjusted (``FID_ORG_ADJ_PRC=0``), as on a broker's chart: when KIS
adjusts the past (a split, say), the last stored candle no longer matches KIS's, and the
whole history is taken again.

A candle is stored by the day its period starts (KST): the day itself, the week's Monday,
the month's 1st, the year's 1 January, whatever day KIS labels it with. Only finished
candles are stored: a day once over (after the 20:30 backfill), a week, month or year once
the last finished trading day is in a later one. The screen makes the rest (today, and the
days of a period not stored yet) from the day candles and the day's one-minute bars.

The market is the integrated one (KRX + NXT) where KIS has it, else KRX's, as for the
one-minute bars; a symbol keeps the market of its first history. The queries share the
backfill's pace inside the REST limit.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path

import aiohttp
import pyarrow as pa
import pyarrow.parquet as pq

from krx_quant.backfill import BARS_FINAL_AT, MARKETS
from krx_quant.feeds.base import KST, Row, to_int
from krx_quant.kis import KisError, RestClient
from krx_quant.market_calendar import MarketCalendar
from krx_quant.ratelimit import RateLimiter

log = logging.getLogger(__name__)

CANDLES_TR = "FHKST03010100"
CANDLES_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
CANDLES_PER_QUERY = 100
# The periods kept, by the name the Engine API takes and KIS's code for it.
PERIODS = {"day": "D", "week": "W", "month": "M", "year": "Y"}
# Paging back from the first day KIS could have: it answers the newest candles in a range.
_FIRST_DAY = date(1900, 1, 1)
# A history is at most this many queries (60,000 candles); more means KIS keeps repeating.
_MAX_QUERIES = 600

CANDLE_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("date", pa.date32()),
        ("venue", pa.string()),  # UN (integrated KRX + NXT) or KRX, as for the one-minute bars
        ("open", pa.int64()),
        ("high", pa.int64()),
        ("low", pa.int64()),
        ("close", pa.int64()),
        ("volume", pa.int64()),
        ("value", pa.int64()),  # traded value in won
    ]
)


def period_start(day: date, period: str) -> date:
    """The day the ``period`` holding ``day`` starts: the day, its week's Monday, its month's
    1st or its year's 1 January."""
    if period == "week":
        return day - timedelta(days=day.weekday())
    if period == "month":
        return day.replace(day=1)
    if period == "year":
        return date(day.year, 1, 1)
    return day


def finished(period: str, start: date, last: date) -> bool:
    """Whether the candle of ``period`` starting ``start`` is final once ``last`` (the last
    finished trading day) is: a day up to it, a longer period before the one holding it."""
    return start <= last if period == "day" else start < period_start(last, period)


def candles_path(data_dir: Path, period: str, symbol: str) -> Path:
    """A symbol's candles of ``period`` under the data directory (and in the Object Store)."""
    return data_dir / "candles" / f"period={period}" / f"symbol={symbol}" / "candles.parquet"


def read_candles(data_dir: Path, period: str, symbol: str) -> list[Row]:
    """The stored candles, oldest first; none if there is no file."""
    path = candles_path(data_dir, period, symbol)
    if not path.exists():
        return []
    rows: list[Row] = pq.read_table(path).to_pylist()
    return rows


def write_candles(data_dir: Path, period: str, symbol: str, rows: Sequence[Row]) -> None:
    """Replace the stored candles, whole or not at all."""
    path = candles_path(data_dir, period, symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    pq.write_table(pa.Table.from_pylist(list(rows), schema=CANDLE_SCHEMA), tmp)
    tmp.rename(path)


def stored_candles(
    data_dir: Path, period: str, symbol: str, before: datetime, limit: int, today: date
) -> tuple[str | None, list[Row]]:
    """The venue and the last ``limit`` stored candles that start before ``before`` (a candle
    starts at 00:00 KST of its period's first day), oldest first. The period holding ``today``
    is left out even once stored: the screen builds it from the day's one-minute bars."""
    rows = read_candles(data_dir, period, symbol)
    now = period_start(today, period)
    found = [r for r in rows if datetime.combine(r["date"], time(), KST) < before and r["date"] < now]
    return (rows[-1]["venue"] if rows else None), found[-limit:]


class CandleKeeper:
    def __init__(
        self,
        client: RestClient,
        calendar: MarketCalendar,
        data_dir: Path,
        symbols: Sequence[str],
        pace: RateLimiter,
    ) -> None:
        self._client = client
        self._calendar = calendar
        self._data_dir = data_dir
        self._symbols = tuple(symbols)
        # The backfill's own (Backfiller.pace): one spacing for both, inside the shared REST
        # limit (docs/kis-feeds.md), with no burst where one hands over to the other.
        self._pace = pace

    async def run(self, now: datetime) -> int:
        """Bring every symbol's candles of every period up to the last finished trading day;
        return how many files changed. A symbol that fails is logged and left for the next run."""
        last = last_finished(self._calendar, now)
        changed = 0
        for period in PERIODS:
            for symbol in self._symbols:
                try:
                    changed += await self.update(period, symbol, last)
                except (KisError, aiohttp.ClientError, OSError, ValueError, KeyError, TimeoutError) as e:
                    log.warning("%s candles of %s not updated: %s", period, symbol, e)
        return changed

    async def update(self, period: str, symbol: str, last: date) -> bool:
        """Add the finished candles up to ``last`` that are not stored yet; True if the file
        changed.

        The last stored candle is asked again every run, a new one due or not, so a past
        KIS adjusted (a split before the open, say) is taken again at once.
        """
        stored = read_candles(self._data_dir, period, symbol)
        if stored:
            newest = stored[-1]
            market = _market(newest["venue"])
            fresh = await self._walk(period, market, symbol, newest["date"], max(last, newest["date"]))
            again = next((r for r in fresh if r["date"] == newest["date"]), None)
            if again is not None and _prices(again) == _prices(newest):
                new = [r for r in fresh if r["date"] > newest["date"] and finished(period, r["date"], last)]
                if not new:
                    return False
                write_candles(self._data_dir, period, symbol, [*stored, *new])
                return True
            log.info(
                "%s candles of %s changed at %s (adjusted?): taking the whole history again",
                period,
                symbol,
                newest["date"],
            )
        history = [r for r in await self._history(period, symbol, last) if finished(period, r["date"], last)]
        if not history:
            log.info("no %s candles of %s at KIS yet; the next run asks again", period, symbol)
            return False
        write_candles(self._data_dir, period, symbol, history)
        return True

    async def _history(self, period: str, symbol: str, last: date) -> list[Row]:
        """The whole history from the first market that has any."""
        for market in MARKETS:
            rows = await self._walk(period, market, symbol, _FIRST_DAY, last)
            if rows:
                return rows
        return []

    async def _walk(self, period: str, market: str, symbol: str, first: date, last: date) -> list[Row]:
        """The candles KIS labels from ``first`` to ``last``, oldest first, paging back from
        ``last``; each by its period's first day."""
        candles: dict[date, Row] = {}
        end = last
        for _ in range(_MAX_QUERIES):
            params = {
                "FID_COND_MRKT_DIV_CODE": market,
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_DATE_1": first.strftime("%Y%m%d"),
                "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
                "FID_PERIOD_DIV_CODE": PERIODS[period],
                "FID_ORG_ADJ_PRC": "0",  # adjusted prices
            }
            await self._pace.acquire()
            body = await self._client.get(CANDLES_TR, CANDLES_PATH, params)
            # KIS pads an answer with blank rows; keep the dated ones in the range.
            labelled = [
                (d, _candle(symbol, MARKETS[market], period_start(d, period), r))
                for r in body.get("output2") or []
                if (d := _date(r)) is not None and first <= d <= last
            ]
            new = [(d, r) for d, r in labelled if r["date"] not in candles]
            candles.update((r["date"], r) for _, r in new)
            if not new:
                break
            earliest = min(d for d, _ in new)
            if period_start(earliest, period) <= first:  # back at the first's period: no more to ask
                break
            end = earliest - timedelta(days=1)
        else:
            raise ValueError(f"{symbol} {period} candles still going after {_MAX_QUERIES} queries")
        return [candles[d] for d in sorted(candles)]


def last_finished(calendar: MarketCalendar, now: datetime) -> date:
    """The last trading day that is over: today only once the day's bars are final (20:30)."""
    local = now.astimezone(KST)
    today = local.date()
    if calendar.is_trading_day(today) and local.time() >= BARS_FINAL_AT:
        return today
    return calendar.previous_trading_day(today)


def _market(venue: str) -> str:
    """The KIS market code a venue was stored from."""
    return next(code for code, v in MARKETS.items() if v == venue)


def _date(r: dict[str, str]) -> date | None:
    try:
        return datetime.strptime(r.get("stck_bsop_date") or "", "%Y%m%d").date()
    except ValueError:
        return None


def _candle(symbol: str, venue: str, start: date, r: dict[str, str]) -> Row:
    return {
        "symbol": symbol,
        "date": start,
        "venue": venue,
        "open": to_int(r["stck_oprc"]),
        "high": to_int(r["stck_hgpr"]),
        "low": to_int(r["stck_lwpr"]),
        "close": to_int(r["stck_clpr"]),
        "volume": to_int(r["acml_vol"]),
        "value": to_int(r["acml_tr_pbmn"]),
    }


def _prices(r: Row) -> tuple[int, int, int, int]:
    return (r["open"], r["high"], r["low"], r["close"])
