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

from krx_quant.algorithms import ALGORITHMS
from krx_quant.api import create_app
from krx_quant.baselines import BASELINES
from krx_quant.consensus import NAMES as CONSENSUS
from krx_quant.engine import Event, PredictionEngine
from krx_quant.feeds import (
    BOOK,
    INDEX,
    KOSPI200_FUTURES,
    KRX_TRADE,
    KST,
    TRADE,
    US_TRADE,
    VI_STATUS,
    Feed,
)
from krx_quant.market_calendar import MarketCalendar
from krx_quant.replay import replay

SYMBOL = "005930"
# What predicts at a bar close; the consensus is recorded there too, empty (no_members)
# while no algorithm is active.
PREDICTING = {b.name for b in BASELINES} | {a.name for a in ALGORITHMS}
PREDICTORS = PREDICTING | set(CONSENSUS)
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


def vi(received: datetime, start: str, end: str | None = None, symbol: str = SYMBOL) -> Event:
    """A VI status answer received at ``received``: a VI from ``start`` (``"09:30:20"``),
    released at ``end``, or still on.
    """
    row = {
        "mksc_shrn_iscd": symbol,
        "vi_cls_code": "N" if end else "Y",
        "bsop_date": received.strftime("%Y%m%d"),
        "cntg_vi_hour": start.replace(":", ""),
        "vi_cncl_hour": end.replace(":", "") if end else "",
        "vi_kind_code": "2",
        "vi_prc": "81500",
        "vi_count": "1",
    }
    [event] = VI_STATUS.normalize({"output": [row]}, symbol, received.astimezone(UTC))
    return (VI_STATUS.tr_id, event)


def kospi(when: datetime, change_pct: float) -> Event:
    """A KOSPI index tick at ``when``, ``change_pct`` from the previous close."""
    fields = _fields(
        INDEX, {"bstp_cls_code": "0001", "bsop_hour": when.strftime("%H%M%S"), "prdy_ctrt": str(change_pct)}
    )
    return (INDEX.tr_id, INDEX.normalize(fields, when.astimezone(UTC)))


def kospi200_futures(when: datetime, change_pct: float) -> Event:
    """A KOSPI200 front-month futures trade at ``when``, ``change_pct`` from the previous close."""
    fields = _fields(
        KOSPI200_FUTURES,
        {"futs_shrn_iscd": "A01612", "bsop_hour": when.strftime("%H%M%S"), "futs_prdy_ctrt": str(change_pct)},
    )
    return (KOSPI200_FUTURES.tr_id, KOSPI200_FUTURES.normalize(fields, when.astimezone(UTC)))


def futures_trade(
    when: datetime, price: float, *, feed: Feed = KOSPI200_FUTURES, contract: str = "A01612"
) -> Event:
    """A front-month futures trade at ``when`` (KOSPI200 by default; USD with ``feed``)."""
    fields = _fields(
        feed,
        {
            "futs_shrn_iscd": contract,
            "bsop_hour": when.strftime("%H%M%S"),
            "futs_prpr": f"{price:.2f}",
            "last_cnqn": "1",
        },
    )
    return (feed.tr_id, feed.normalize(fields, when.astimezone(UTC)))


def index_tick(when: datetime, code: str, value: float) -> Event:
    """An index tick at ``when``."""
    fields = _fields(
        INDEX, {"bstp_cls_code": code, "bsop_hour": when.strftime("%H%M%S"), "prpr_nmix": f"{value:.2f}"}
    )
    return (INDEX.tr_id, INDEX.normalize(fields, when.astimezone(UTC)))


def us_trade(when: datetime, ticker: str, price: float) -> Event:
    """A US daytime trade at ``when`` (Korean time)."""
    fields = _fields(
        US_TRADE,
        {
            "RSYM": f"RBAQ{ticker}",
            "SYMB": ticker,
            "KYMD": when.strftime("%Y%m%d"),
            "KHMS": when.strftime("%H%M%S"),
            "LAST": f"{price:.4f}",
            "EVOL": "10",
            "MTYP": "1",
        },
    )
    return (US_TRADE.tr_id, US_TRADE.normalize(fields, when.astimezone(UTC)))


def every(start: datetime, end: datetime, seconds: float, make: Any) -> list[Event]:
    """``make(when)`` every ``seconds`` in ``[start, end]``."""
    events: list[Event] = []
    when = start
    while when <= end:
        events.append(make(when))
        when += timedelta(seconds=seconds)
    return events


async def run(
    db: Path,
    events: Iterable[Event],
    until: datetime,
    *,
    calendar: MarketCalendar = CALENDAR,
    symbols: tuple[str, ...] = (SYMBOL,),
    **options: Any,
) -> PredictionEngine:
    ordered = sorted(events, key=lambda e: e[1]["received_at"])
    return await replay(
        ordered, db=db, calendar=calendar, symbols=symbols, fee_rate=FEE, until=until, **options
    )


@contextlib.asynccontextmanager
async def api(engine: PredictionEngine) -> AsyncIterator[TestClient[Any, Any]]:
    async with TestClient(TestServer(create_app(engine))) as client:
        yield client


async def get_json(client: TestClient[Any, Any], path: str) -> Any:
    response = await client.get(path)
    assert response.status == 200, await response.text()
    return await response.json()
