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
    FOREIGN_ESTIMATE,
    INDEX,
    KOSPI200_FUTURES,
    KRX_TRADE,
    KST,
    MEMBER,
    PROGRAM,
    TRADE,
    US_TRADE,
    VI_STATUS,
    Feed,
)
from krx_quant.market_calendar import MarketCalendar
from krx_quant.prevalidation import REGISTERED_BEFORE
from krx_quant.replay import replay

SYMBOL = "005930"
# What predicts at a bar close; the consensus is recorded there too, empty (no_members)
# while no algorithm is active.
PREDICTING = {b.name for b in BASELINES} | {a.name for a in ALGORITHMS}
PREDICTORS = PREDICTING | set(CONSENSUS)
# Live, an algorithm registered after pre-validation existed waits for its report (#24).
LIVE_PREDICTORS = PREDICTORS - {a.name for a in ALGORITHMS if a.name not in REGISTERED_BEFORE}
DAY = date(2026, 10, 12)  # a Monday
CALENDAR = MarketCalendar({DAY: True})
FEE = 0.015 / 100


def at(hhmmss: str, day: date = DAY) -> datetime:
    """``"09:30:15"`` or ``"09:30:15.25"`` on ``day``, in KST."""
    return datetime.combine(day, time.fromisoformat(hhmmss), KST)


def _fields(feed: Feed, values: dict[str, str]) -> dict[str, str]:
    return dict.fromkeys(feed.columns, "0") | values


def book(
    when: datetime,
    bid: int,
    ask: int,
    symbol: str = SYMBOL,
    *,
    bid_qty: int = 0,
    ask_qty: int = 0,
    deeper: tuple[int, int] = (0, 0),
) -> Event:
    """An integrated book update received at ``when``, with ``bid_qty`` and ``ask_qty`` at
    the best quotes and ``deeper`` (bid, ask) sizes at each of levels 2–10."""
    sizes = {f"BIDP_RSQN{i}": str(deeper[0]) for i in range(2, 11)} | {
        f"ASKP_RSQN{i}": str(deeper[1]) for i in range(2, 11)
    }
    fields = _fields(
        BOOK,
        {
            "MKSC_SHRN_ISCD": symbol,
            "BSOP_HOUR": when.strftime("%H%M%S"),
            "BIDP1": str(bid),
            "ASKP1": str(ask),
            "BIDP_RSQN1": str(bid_qty),
            "ASKP_RSQN1": str(ask_qty),
            **sizes,
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
    side: str = "",
) -> Event:
    """A trade received at ``when``; ``traded`` is its exchange time (default ``when``);
    ``side`` the aggressor, ``"buy"`` or ``"sell"`` (else unknown)."""
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
            "CNTG_CLS_CODE": {"buy": "1", "sell": "5"}.get(side, ""),
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


def with_sides(events: Iterable[Event], run: int = 2) -> list[Event]:
    """``events`` with the integrated trades' aggressor sides: ``run`` buys, then ``run``
    sells, in turn."""
    out: list[Event] = []
    trades = 0
    for feed, row in events:
        if feed == TRADE.tr_id:
            row = {**row, "side": "buy" if (trades // run) % 2 == 0 else "sell"}
            trades += 1
        out.append((feed, row))
    return out


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


def program(when: datetime, net_value: int, symbol: str = SYMBOL) -> Event:
    """The day's program trading so far, net buying ``net_value``, received at ``when``."""
    fields = _fields(
        PROGRAM,
        {"MKSC_SHRN_ISCD": symbol, "STCK_CNTG_HOUR": when.strftime("%H%M%S"), "NTBY_TR_PBMN": str(net_value)},
    )
    return (PROGRAM.tr_id, PROGRAM.normalize(fields, when.astimezone(UTC)))


def foreign_members(when: datetime, net_qty: int, symbol: str = SYMBOL) -> Event:
    """The top-5 member firms, the foreign ones net buying ``net_qty`` so far today."""
    fields = _fields(MEMBER, {"MKSC_SHRN_ISCD": symbol, "GLOB_NTBY_QTY": str(net_qty)})
    return (MEMBER.tr_id, MEMBER.normalize(fields, when.astimezone(UTC)))


def estimates(
    when: datetime, slots: dict[str, tuple[int | None, int | None]], symbol: str = SYMBOL
) -> list[Event]:
    """One poll of the investor estimates: (foreign, institution) net buying by time slot;
    None is a blank (not entered in that slot)."""
    body = {
        "output2": [
            {
                "bsop_hour_gb": slot,
                "frgn_fake_ntby_qty": _blank(f),
                "orgn_fake_ntby_qty": _blank(i),
                "sum_fake_ntby_qty": _blank((f or 0) + (i or 0)),
            }
            for slot, (f, i) in slots.items()
        ]
    }
    rows = FOREIGN_ESTIMATE.normalize(body, symbol, when.astimezone(UTC))
    return [(FOREIGN_ESTIMATE.tr_id, row) for row in rows]


def _blank(value: int | None) -> str:
    return "" if value is None else str(value)


def flows_morning(start: str = "09:00:30", end: str = "10:30:00", symbol: str = SYMBOL) -> list[Event]:
    """Programs and foreign firms buying and selling in turn, and estimates twice."""
    events: list[Event] = []
    when, net, members = at(start), 0, 0
    step = 0
    while when <= at(end):
        step += 1
        net += 50_000_000 * ((step * 31) % 7 - 3)
        members += 200 * ((step * 17) % 5 - 2)
        events += [program(when, net, symbol), foreign_members(when + timedelta(seconds=1), members, symbol)]
        when += timedelta(seconds=23)
    events += estimates(at("09:31:00"), {"1": (12_000, -3_000)}, symbol)
    events += estimates(at("10:01:00"), {"2": (20_000, 5_000), "1": (12_000, -3_000)}, symbol)
    return events


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
