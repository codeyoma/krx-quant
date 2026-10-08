"""Hand-made normalized events for the Replay Harness.

Rows go through the production normalizers, so the engine sees exactly the shape the
collector records.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Iterable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from aiohttp.test_utils import TestClient, TestServer

from krx_quant.api import create_app
from krx_quant.engine import Event, PredictionEngine
from krx_quant.feeds import BOOK, KRX_TRADE, KST, TRADE, Feed
from krx_quant.market_calendar import MarketCalendar
from krx_quant.replay import replay

SYMBOL = "005930"
DAY = date(2026, 10, 12)  # a Monday
CALENDAR = MarketCalendar({DAY: True})
FEE = 0.015 / 100


def at(hhmmss: str, day: date = DAY) -> datetime:
    """``"09:30:15"`` or ``"09:30:15.25"`` on ``day``, in KST."""
    return datetime.combine(day, time.fromisoformat(hhmmss), KST)


def _fields(feed: Feed, values: dict[str, str]) -> dict[str, str]:
    return dict.fromkeys(feed.columns, "0") | values


def book(when: datetime, bid: int, ask: int, symbol: str = SYMBOL) -> Event:
    """An integrated book update received at ``when``."""
    fields = _fields(
        BOOK,
        {
            "MKSC_SHRN_ISCD": symbol,
            "BSOP_HOUR": when.strftime("%H%M%S"),
            "BIDP1": str(bid),
            "ASKP1": str(ask),
        },
    )
    return (BOOK.tr_id, BOOK.normalize(fields, when.astimezone(UTC)))


def trade(
    when: datetime,
    price: int,
    qty: int = 1,
    *,
    bid: int = 0,
    ask: int = 0,
    traded: datetime | None = None,
    feed: Feed = TRADE,
    symbol: str = SYMBOL,
) -> Event:
    """A trade received at ``when``; ``traded`` is its exchange time (default ``when``)."""
    exchange_time = traded or when
    fields = _fields(
        feed,
        {
            "MKSC_SHRN_ISCD": symbol,
            "BSOP_DATE": exchange_time.strftime("%Y%m%d"),
            "STCK_CNTG_HOUR": exchange_time.strftime("%H%M%S"),
            "STCK_PRPR": str(price),
            "CNTG_VOL": str(qty),
            "BIDP1": str(bid),
            "ASKP1": str(ask),
        },
    )
    return (feed.tr_id, feed.normalize(fields, when.astimezone(UTC)))


def krx_trade(when: datetime, price: int, *, bid: int, ask: int, symbol: str = SYMBOL) -> Event:
    """A trade from the KRX-only feed, carrying KRX's best quotes."""
    return trade(when, price, bid=bid, ask=ask, feed=KRX_TRADE, symbol=symbol)


def steady_book(start: datetime, end: datetime, bid: int, ask: int, every: float = 10.0) -> list[Event]:
    """The same book every ``every`` seconds in ``[start, end]``."""
    events: list[Event] = []
    when = start
    while when <= end:
        events.append(book(when, bid, ask))
        when += timedelta(seconds=every)
    return events


def run(
    db: Path,
    events: Iterable[Event],
    until: datetime,
    *,
    calendar: MarketCalendar = CALENDAR,
    symbols: tuple[str, ...] = (SYMBOL,),
    **options: Any,
) -> PredictionEngine:
    ordered = sorted(events, key=lambda e: e[1]["received_at"])
    return replay(ordered, db=db, calendar=calendar, symbols=symbols, fee_rate=FEE, until=until, **options)


@contextlib.asynccontextmanager
async def api(engine: PredictionEngine) -> AsyncIterator[TestClient[Any, Any]]:
    async with TestClient(TestServer(create_app(engine))) as client:
        yield client


async def get_json(client: TestClient[Any, Any], path: str) -> Any:
    response = await client.get(path)
    assert response.status == 200, await response.text()
    return await response.json()
