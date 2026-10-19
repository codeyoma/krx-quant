"""The KRX auctions' prices of each trading day (#264): the opening auction's and the closing
auction's, which the daily kind ``open_to_close`` runs between.

**Why.** The kind was answered and trained on the KRX day candle (``candles.krx_days_path``, KIS
market ``J``). On 2026-10-19 the daily check (``daily_check``, the scheduled job
``daily-check-264``) found its open equal to the recorded opening auction print and its close
unlike the closing auction's, on all three symbols. Since KRX's after-market opened
(``AFTER_MARKET_OPENED``, 2026-09-14, 16:00–20:00) the candle's close is most likely the
after-market's last price. The kind's day ends at the 15:30 closing auction, a price one can trade
at (the user's decision, #264), so its close is that auction's price.

**Where from.** KIS's day of one-minute bars (``FHKST03010230``) of the KRX market alone (``J``).
A bar is labelled by the minute it starts and an auction's print is its first trade, so the
closing auction's price is the close of the first bar from the session's close within
``PRINT_WITHIN`` (10 minutes: the post-close trades at the closing price begin at 15:40), and the
opening auction's the open of the bar of the open's minute (``opening_bar``: a later bar's open is
a continuous trade). Each is one query, the bars at or before ``PRINT_WITHIN`` less a second after
the time. Where KIS has no such
bar, the day's recorded KRX trades (``H0STCNT0``, recorded from 2026-10-12) give the prints
(``recorded_prints``, as the daily check reads them).

**Kept** in ``data/candles/period=day/symbol=<s>/auctions.parquet``, beside the KRX day candles,
a row a day: the two prices as traded (KIS does not adjust its minute bars; the opening one from the
recorded print where KIS has no opening bar, null if neither has it), and where they came
from (``source``: ``krx_minute_bar``, ``recorded_print``, or ``none`` for a day settled without a
closing price). The collector's history task keeps it (``AuctionKeeper``): at its start and at
20:30, every trading day of the last ``KEEP_DAYS`` (KIS keeps about a year of minute bars) not kept
yet, newest first; and on a trading day at ``CLOSE_AT`` (15:40) the day itself, so the day is
answered from 15:45. A day neither source has a closing price for is settled ``none`` once it is
over and KIS answered other bars of it, it is older than ``SETTLE_AFTER``, or it is before the
after-market (its candle's close stands, ``days_of``); otherwise it is asked again on the next run.

**The days** (``days_of``), read by the daily kinds live and in training alike (``daily.StoredDays``):
each KRX day candle with its close replaced by the kept closing auction price. The candle's prices
are adjusted (``candles``) and the auction's are not, so the close is the candle's open times the
auction's close over its open (the day's move between the auctions); on a day not adjusted since,
the two opens are the same and the close is the auction's as traded. The close is never set
against the adjusted open alone: a day kept without its opening auction price (no opening bar, no
recorded print) counts as one without a kept price. A day without a kept price keeps its candle's
close before the after-market and is left out from it (no answer, no features of it). Every ratio
the kind reads is then of one scale: a day's move is the two auctions' as traded, and the closes,
opens, highs and lows across days are all the candle's adjusted ones. A day kept past the newest
candle (the day itself, before the 20:30 candles) is the two auction prices alone, as traded: it is
the newest day, on the scale of the candles before it until an adjustment, and an adjustment has
the candles taken again, that day's with them.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
import pyarrow as pa
import pyarrow.parquet as pq

from krx_quant.backfill import MINUTE_BARS_PATH, MINUTE_BARS_TR
from krx_quant.candles import KRX_MARKET, candles_path
from krx_quant.feeds.base import KST, Row, kst, to_int
from krx_quant.kis import KisError, RestClient
from krx_quant.market_calendar import MarketCalendar, Session
from krx_quant.ratelimit import RateLimiter

if TYPE_CHECKING:
    from krx_quant.daily import DayPrices

log = logging.getLogger(__name__)

AFTER_MARKET_OPENED = date(2026, 9, 14)  # KRX's after-market (16:00–20:00) opened
# An auction's print is the first trade within this of its time: the post-close trades at the
# closing price begin at 15:40.
PRINT_WITHIN = timedelta(minutes=10)
# The opening auction's print is in the bar of the open's minute: after it, the trades are
# continuous (a day without that bar had no opening auction trade).
OPENING_BAR = timedelta(minutes=1)
# When the day's closing auction price is asked, KST, on a trading day.
CLOSE_AT = time(15, 40)
# How far back the keeper fills: KIS keeps about a year of minute bars.
KEEP_DAYS = timedelta(days=365)
# A day over that neither source has a closing price for is settled ``none`` after this.
SETTLE_AFTER = timedelta(days=7)
AUCTIONS_FILE = "auctions.parquet"
# Where a day's prices came from.
MINUTE_BAR = "krx_minute_bar"
RECORDED = "recorded_print"
NONE = "none"
DAY_CANDLE = "krx_day_candle"  # a day's close from its candle (before the after-market)
# Consecutive failed days after which a run gives up until the next one.
_MAX_FAILURES = 3

AUCTION_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("date", pa.date32()),
        ("open", pa.int64()),  # the opening auction's price as traded; null: none found
        ("close", pa.int64()),  # the closing auction's price as traded; null: settled ``none``
        ("source", pa.string()),
    ]
)


def auctions_path(data_dir: Path, symbol: str) -> Path:
    """A symbol's kept auction prices (#264), beside its day candles."""
    return candles_path(data_dir, "day", symbol).with_name(AUCTIONS_FILE)


def read_auctions(data_dir: Path, symbol: str) -> dict[date, Row]:
    """The kept auction prices by day; none if there is no file."""
    path = auctions_path(data_dir, symbol)
    if not path.exists():
        return {}
    return {r["date"]: r for r in pq.read_table(path).to_pylist()}


def write_auctions(data_dir: Path, symbol: str, rows: Mapping[date, Row]) -> None:
    """Replace the kept auction prices, whole or not at all."""
    path = auctions_path(data_dir, symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    table = pa.Table.from_pylist([rows[d] for d in sorted(rows)], schema=AUCTION_SCHEMA)
    pq.write_table(table, tmp)
    tmp.rename(path)


def days_of(candles: Sequence[Row], kept: Mapping[date, Row]) -> list[DayPrices]:
    """The KRX days (module doc): each candle with the closing auction's price as its close, a day
    without one before the after-market with its candle's, the days kept past the newest candle
    from the auctions alone; oldest first."""
    from krx_quant.daily import DayPrices  # daily reads this module's days

    out: dict[date, DayPrices] = {}
    for row in candles:
        day = DayPrices.of(row)
        if day is None:
            continue
        price = kept.get(day.day)
        if price is not None and price.get("close") and price.get("open"):
            # Both auctions as traded: their ratio onto the candle's adjusted open. A close without
            # the same source's open is never set against the adjusted open (#317 review): the
            # day has no kept price then.
            closed, opened = price["close"], price["open"]
            moved = float(closed) if opened == day.open else day.open * closed / opened
            out[day.day] = replace(day, close=moved, source=str(price["source"]))
        elif day.day < AFTER_MARKET_OPENED:
            out[day.day] = day
    newest = max((r["date"] for r in candles), default=None)
    for when, price in kept.items():
        if (newest is not None and when <= newest) or not price.get("open") or not price.get("close"):
            continue  # a day's candle comes after 20:30: before it, the auctions alone
        opened, closed = float(price["open"]), float(price["close"])
        out[when] = DayPrices(
            when, opened, max(opened, closed), min(opened, closed), closed, 0, str(price["source"])
        )
    return [out[d] for d in sorted(out)]


def last_closed(calendar: MarketCalendar, now: datetime) -> date:
    """The last trading day whose closing auction print is in: today from ``CLOSE_AT`` on."""
    local = now.astimezone(KST)
    today = local.date()
    session = calendar.session(today)
    if session is not None and local >= session.close + PRINT_WITHIN:
        return today
    return calendar.previous_trading_day(today)


def closes_pending(
    data_dir: Path, calendar: MarketCalendar, symbols: Sequence[str], now: datetime
) -> str | None:
    """Why the closing auction prices of the after-market's days are not all kept yet (every
    trading day from ``AFTER_MARKET_OPENED`` to the last closed one, every symbol, a price or a
    day settled ``none``); None once they are. Until then the days without one have no answer,
    so a simulation of the latest days or a walk-forward would read fewer days than there are."""
    days = calendar.trading_days(AFTER_MARKET_OPENED, last_closed(calendar, now))
    missing = []
    for symbol in symbols:
        kept = read_auctions(data_dir, symbol)
        lacking = [d for d in days if d not in kept]
        if lacking:
            missing.append(f"{symbol} {len(lacking)} days from {lacking[0]}")
    if not missing:
        return None
    return "closing auction prices not kept yet: " + ", ".join(missing)


def recorded_prints(
    data_dir: Path, day: date, symbol: str, session: Session
) -> tuple[int | None, int | None] | None:
    """The day's opening and closing auction prints of ``symbol`` in its recorded KRX trades (by
    their exchange time): the first trade from the open within ``PRINT_WITHIN``, the last from the
    closing auction's start to a minute after the close; None without a recording."""
    from krx_quant.feeds import KRX_TRADE
    from krx_quant.replay import recorded_events  # it reads the recording

    try:
        events = recorded_events(data_dir, day, [symbol], feeds={KRX_TRADE.tr_id})
        trades = [row for _, row in events if row["symbol"] == symbol and row.get("price")]
    except FileNotFoundError:
        return None
    timed = sorted(trades, key=lambda r: r["event_time"])
    opening = next(
        (r["price"] for r in timed if session.open <= r["event_time"] < session.open + PRINT_WITHIN),
        None,
    )
    closing = [
        r["price"]
        for r in timed
        if session.closing_auction <= r["event_time"] < session.close + timedelta(minutes=1)
    ]
    return opening, closing[-1] if closing else None


async def bars_page(
    client: RestClient, symbol: str, day: date, before: datetime, market: str = KRX_MARKET
) -> list[Row]:
    """The day's one-minute bars of ``market`` that start at or before ``before`` (KST), at most
    120 (``backfill.BARS_PER_QUERY``), oldest first: open, high, low, close and volume by bar time."""
    ymd = day.strftime("%Y%m%d")
    params = {
        "FID_COND_MRKT_DIV_CODE": market,
        "FID_INPUT_ISCD": symbol,
        "FID_PW_DATA_INCU_YN": "N",  # that day only
        "FID_FAKE_TICK_INCU_YN": "",
        "FID_INPUT_DATE_1": ymd,
        "FID_INPUT_HOUR_1": before.astimezone(KST).strftime("%H%M%S"),
    }
    body = await client.get(MINUTE_BARS_TR, MINUTE_BARS_PATH, params)
    rows = [
        {
            "bar_time": kst(day, r["stck_cntg_hour"]),
            "open": to_int(r["stck_oprc"]),
            "high": to_int(r["stck_hgpr"]),
            "low": to_int(r["stck_lwpr"]),
            "close": to_int(r["stck_prpr"]),
            "volume": to_int(r["cntg_vol"]),
        }
        for r in body.get("output2") or []
        if r.get("stck_bsop_date") == ymd  # KIS pads an empty answer with blank rows
    ]
    return sorted(rows, key=lambda r: r["bar_time"])


def print_bar(bars: Sequence[Row], at: datetime, within: timedelta = PRINT_WITHIN) -> Row | None:
    """The bar holding an auction's print at ``at``: the first from ``at`` within ``within``."""
    return next((b for b in bars if at <= b["bar_time"] < at + within), None)


def opening_bar(bars: Sequence[Row], session: Session) -> Row | None:
    """The bar of the open's minute (``OPENING_BAR``): the opening auction's print is its first
    trade. A later bar's open would be a continuous trade, not the auction (#317 review)."""
    return print_bar(bars, session.open, OPENING_BAR)


def _row(symbol: str, day: date, opened: Any, closed: Any, source: str) -> Row:
    return {"symbol": symbol, "date": day, "open": opened or None, "close": closed or None, "source": source}


class AuctionKeeper:
    """Keeps each symbol's auction prices (module doc), inside the history task's REST pace."""

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
        self._pace = pace  # the backfill's: one spacing for all the history task's queries

    async def run(self, now: datetime) -> int:
        """Keep every trading day of the last ``KEEP_DAYS`` up to the last closed one that is not
        kept yet, newest first; return how many were kept. A day that fails is logged and asked
        again on the next run; after a few failures in a row the symbol waits for it."""
        local = now.astimezone(KST)
        today = local.date()
        days = self._calendar.trading_days(today - KEEP_DAYS, last_closed(self._calendar, now))
        added = 0
        for symbol in self._symbols:
            kept = read_auctions(self._data_dir, symbol)
            todo = [d for d in reversed(days) if d not in kept]
            if not todo:
                continue
            new = failures = 0
            for day in todo:
                try:
                    row = await self.price(symbol, day, today)
                except (KisError, aiohttp.ClientError, OSError, ValueError, KeyError, TimeoutError) as e:
                    failures += 1
                    log.warning("auction prices of %s on %s not kept: %s", symbol, day, e)
                    if failures >= _MAX_FAILURES:
                        log.error("auction prices of %s stopped after %d failures in a row", symbol, failures)
                        break
                    continue
                failures = 0
                if row is not None:
                    kept[day] = row
                    new += 1
            if new:
                write_auctions(self._data_dir, symbol, kept)
                added += new
                log.info("kept the auction prices of %s on %d days", symbol, new)
        return added

    async def price(self, symbol: str, day: date, today: date) -> Row | None:
        """``day``'s auction prices of ``symbol``: from KRX's minute bars, else the recording, else
        settled ``none`` once it may be (module doc); None to ask again."""
        session = self._calendar.session(day)
        if session is None:
            return None
        closing = await self._page(symbol, day, session.close + PRINT_WITHIN - timedelta(seconds=1))
        bar = print_bar(closing, session.close)
        if bar is not None:
            opening = await self._page(symbol, day, session.open + PRINT_WITHIN - timedelta(seconds=1))
            first = opening_bar(opening, session)
            opened = None if first is None else first["open"]
            if opened is None:  # the recorded opening print, as traded too
                prints = recorded_prints(self._data_dir, day, symbol, session)
                opened = None if prints is None else prints[0]
            return _row(symbol, day, opened, bar["close"], MINUTE_BAR)
        prints = recorded_prints(self._data_dir, day, symbol, session)
        if prints is not None and prints[1] is not None:
            return _row(symbol, day, prints[0], prints[1], RECORDED)
        if day < today and (day < AFTER_MARKET_OPENED or closing or today - day > SETTLE_AFTER):
            log.warning("no closing auction price of %s on %s: settled without one", symbol, day)
            return _row(symbol, day, None, None, NONE)
        return None

    async def _page(self, symbol: str, day: date, before: datetime) -> list[Row]:
        await self._pace.acquire()
        return await bars_page(self._client, symbol, day, before)
