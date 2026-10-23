"""KRX 마감 흐름 (#305): the closing rebalancing demand of the single-stock ±2x ETFs on a symbol,
and what the closing auction and the next day did after it.

**Demand.** An L-times ETF must trade at the close, for its exposure to stay L times its net
assets, d = A × (L² − L) × r (#305): 2·A·r at 2x and 6·A·r at −2x, both the way the stock moved.

- A: the product's net assets on the previous trading day (``net_assets.net_assets_for``);
- r: the symbol's reference price (the integrated mid-point, ``reference.py``) at a minute over
  the previous trading day's KRX close (the closing auction's price, ``auctions.days_of``, as the
  briefing and ``open_to_close`` read it) − 1.

Asked during continuous trading, the answer is the minute's (the price just before it); from
the closing auction's start (15:20) on, the 15:20 one, which the collector stores a row per symbol
a day (``keep_demand``, ``state/closing_flow/demand.parquet``). It is the formula's value, not an
execution: since the FSC's request of 2026-07-28 the products may rebalance earlier. The Hong Kong
products and the ETNs are left out (``products.py``): the demand is the lower for it.

**By structure.** The futures-only products (선물형) rebalance in single-stock futures (to 15:45),
so their demand is not set against the closing auction. The spot and futures products (현물+선물형)
hold the stock and add futures, and how they split between the auction and the futures is not
known: all of theirs is set against it, so its ratio (``ratio``) is an upper bound. The total's
ratio (``ratio_total``) is given too. A product without its A is left out of the sums and named
(``missing``).

**Denominator.** The median closing auction value of the 20 trading days before the day
(``DENOMINATOR_DAYS``), of the days with one, with how many came from each source
(``denominator_sources``); None, with ``NO_AUCTION_VALUE`` as the reason, when none of them has
one. A day's value (``auction_value``), the first of:

- ``recorded``: its closing auction's trades in the recording, the KRX trades (``H0STCNT0``) from
  the closing auction's start to a minute after the close, price × quantity (from 2026-10-12, but a
  day the connection was cut around 15:30, #344);
- ``krx_bar``: KRX's (``J``) closing auction bar kept by ``auctions`` (the 15:30 bar's volume ×
  close, as traded);
- ``integrated_bar``: the backfill's integrated (``UN``) closing auction bar (the same, its last
  bar from 15:20 to 15:30), where KRX's is not kept.

The bars were measured first (``check-closing-flow-305``, 2026-10-23, CODING_STANDARDS.md
"External data is probed before it is used"): on all 9 recorded days (2026-10-12 to 10-23) of the
three symbols both bars' volume × close equalled the recorded value (ratio 1.000) and their closes
the recorded print. The KRX bar comes first as the auction is KRX's (``open_to_close`` reads that
bar's close too, #317).

**The days.** Each day of the backfill's bars that follows a day with them (no day missing
between), as #270's ``structure-check`` section 2 reads them, with the same functions
(``structure_check.auction_days``, ``period_of``, ``same_share``, ``opposite_share``): the closing
auction's move (close ÷ the last price before it − 1), the day's flow to 15:20 (that price ÷ the
day before's close − 1), the next day's gap and close-to-close. A day's demand is its stored 15:20
one (``live``); a day without one but with its A is computed from the bars (``bars``: r is the
day's flow). With KRX's net assets from the listing (``krx_net_assets``), every day from the day
after it (2026-05-28) has its A. ``net_assets_source`` says whose net assets the day's demand read
(``net_assets.source_of``: ``krx``, ``kis``, ``krx+kis``): the screen shows "한국거래소 통계정보" for
any but KIS's. A 15:20 row stored before the KRX source (#305) has none of its own: it read KIS's,
then the only source (``BEFORE_KRX``).

The auction went the demand's way when the two signs agree; a day without a demand (no A, or a
symbol without products such as 009150) compares the day's flow, as #270 does. The special
situations kept that day (``engine`` ``special_windows``: the symbol's VIs, the market's circuit
breakers and sidecars) are named, and the summary is given with them and without. Computed after
the history task's runs (at the collector's start and 20:30, after the auctions it keeps) and
after KRX's net assets come in (``keep_days``, one at a time), kept in
``state/closing_flow/days.json``; the answer's summary is over the days asked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import statistics
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from krx_quant.auctions import MINUTE_BAR as KRX_MINUTE_BAR
from krx_quant.auctions import read_auctions
from krx_quant.backfill import MINUTE_BARS_TR, read_bars
from krx_quant.daily import DayPrices as KrxDay
from krx_quant.daily import StoredDays
from krx_quant.feeds import KRX_TRADE
from krx_quant.feeds.base import EVENT_TIME, KST, Row
from krx_quant.macro import read_partition
from krx_quant.market_calendar import MarketCalendar, Session
from krx_quant.net_assets import KIS, net_assets_for, read_net_assets, source_of
from krx_quant.predictions import iso
from krx_quant.products import Product, Structure, products_of
from krx_quant.structure_check import (
    PERIODS,
    DayPrices,
    auction_days,
    bar_days,
    day_prices,
    opposite_share,
    period_of,
    same_share,
)

log = logging.getLogger(__name__)

DENOMINATOR_DAYS = 20
# Where a day's closing auction value came from, in the order tried (module doc "Denominator"): the
# recorded KRX trades, KRX's closing auction bar, the integrated one.
RECORDED, KRX_BAR, INTEGRATED_BAR = "recorded", "krx_bar", "integrated_bar"
VALUE_SOURCES = (RECORDED, KRX_BAR, INTEGRATED_BAR)
# Why a denominator is None: no day of the 20 before has a closing auction value; the days not
# computed yet (the answer's).
NO_AUCTION_VALUE, NOT_COMPUTED = "no_auction_value", "not_computed"
# The source of a 15:20 row's net assets stored before it kept one: KIS's, the only one then.
BEFORE_KRX = KIS
LIVE, BARS = "live", "bars"  # where a day's demand came from: the 15:20 store, the bars
STORED = "stored"  # the answer's 15:20 demand read back
# The 15:20 demand is stored this long after the closing auction's start: the quotes received
# before it are in by then (the reference price reads those received before 15:20).
STORE_AFTER = timedelta(seconds=5)
AFTER_CLOSE = timedelta(minutes=1)  # the recorded print's window ends here (``auctions.recorded_prints``)
DEMAND_FILE, DAYS_FILE = "demand.parquet", "days.json"
# The days file's layout: one written before (#305's KRX net assets and the bars' denominator, 2)
# is not read, as its rows lack fields; the next computation replaces it.
DAYS_VERSION = 2

DEMAND_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("date", pa.date32()),
        ("at", EVENT_TIME),  # the closing auction's start: the price is the one just before it
        ("price", pa.float64()),  # the reference price
        ("previous_close", pa.float64()),  # the previous trading day's KRX close
        ("r", pa.float64()),
        ("demand", pa.int64()),  # won, + buy, − sell; null: no product had its A
        ("spot_futures", pa.int64()),
        ("futures", pa.int64()),
        ("products", pa.int64()),  # the products counted (with their A)
        ("missing", pa.int64()),  # the products without their A
        ("net_assets_source", pa.string()),  # whose A (``source_of``); null on rows stored before it
    ]
)


def coefficient(product: Product) -> int:
    """L² − L: 2 at 2x, 6 at −2x."""
    return product.leverage**2 - product.leverage


@dataclass(frozen=True)
class Demand:
    """The products' demands at one r, and their sums (won); a sum is None without a product of
    its kind with its A, or without r."""

    products: list[Row]
    total: int | None
    spot_futures: int | None
    futures: int | None
    missing: list[str]  # the products without their A


def demand_of(products: Sequence[Product], held: Mapping[str, Row], r: float | None) -> Demand:
    """Each product's demand A × (L² − L) × r with its A of ``held`` (``net_assets_for``)."""
    rows: list[Row] = []
    sums: dict[Structure, int | None] = {Structure.SPOT_FUTURES: None, Structure.FUTURES: None}
    for product in products:
        row = held.get(product.code)
        assets = None if row is None else int(row["net_assets"])
        demand = None if assets is None or r is None else round(assets * coefficient(product) * r)
        if demand is not None:
            sums[product.structure] = (sums[product.structure] or 0) + demand
        rows.append(
            {
                "code": product.code,
                "leverage": product.leverage,
                "structure": product.structure.value,
                "coefficient": coefficient(product),
                "net_assets": assets,
                "net_assets_date": None if row is None else row["date"].isoformat(),
                "net_assets_source": None if row is None else row["source"],
                "demand": demand,
            }
        )
    spot, futures = sums[Structure.SPOT_FUTURES], sums[Structure.FUTURES]
    total = None if spot is None and futures is None else (spot or 0) + (futures or 0)
    missing = [p.code for p in products if p.code not in held]
    return Demand(rows, total, spot, futures, missing)


def _ratio(value: int | None, denominator: float | None) -> float | None:
    return None if value is None or not denominator else value / denominator


AuctionValue = tuple[float, str]  # a day's closing auction value and its source


def denominator(
    values: Mapping[date, AuctionValue], calendar: MarketCalendar, day: date
) -> tuple[float | None, int, dict[str, int]]:
    """The median closing auction value of the ``DENOMINATOR_DAYS`` trading days before ``day``,
    of those with one, how many had one, and how many of them came from each source."""
    found, sources, before = [], dict.fromkeys(VALUE_SOURCES, 0), day
    for _ in range(DENOMINATOR_DAYS):
        before = calendar.previous_trading_day(before)
        if (kept := values.get(before)) is not None:
            found.append(kept[0])
            sources[kept[1]] = sources.get(kept[1], 0) + 1
    return (statistics.median(found) if found else None), len(found), sources


def auction_value(
    data_dir: Path, symbol: str, session: Session, prices: DayPrices, krx: Row | None
) -> tuple[float | None, str | None]:
    """The day's closing auction value and its source (module doc "Denominator"): the recorded
    trades', else KRX's kept closing bar's (``krx``, the day's ``auctions`` row), else the
    integrated bar's; None without any."""
    recorded = recorded_auction_value(data_dir, session.day, symbol, session.closing_auction, session.close)
    if recorded is not None:
        return recorded, RECORDED
    if krx is not None and krx["source"] == KRX_MINUTE_BAR and krx.get("close_volume") and krx.get("close"):
        return float(krx["close_volume"] * krx["close"]), KRX_BAR
    if prices.auction_volume and prices.close:
        return float(prices.auction_volume * prices.close), INTEGRATED_BAR
    return None, None


def recorded_auction_value(
    data_dir: Path, day: date, symbol: str, close_from: datetime, close: datetime
) -> float | None:
    """The day's closing auction value in its recorded KRX trades: price × quantity of the trades
    from the closing auction's start to a minute after the close (by exchange time); None without
    a recorded trade there."""
    partition = (
        data_dir / "events" / f"date={day.isoformat()}" / f"feed={KRX_TRADE.tr_id}" / f"symbol={symbol}"
    )
    table = read_partition(partition, ("event_time", "price", "qty"))
    if table is None:
        return None
    kind, times = table.schema.field("event_time").type, table["event_time"]
    inside = pc.and_(
        pc.greater_equal(times, pa.scalar(close_from, kind)),
        pc.less(times, pa.scalar(close + AFTER_CLOSE, kind)),
    )
    window = table.filter(inside)
    value = pc.sum(pc.multiply(window["price"], window["qty"])).as_py()
    return float(value) if value else None


# Stored 15:20 demand


def demand_path(state_dir: Path) -> Path:
    return state_dir / "closing_flow" / DEMAND_FILE


def read_demand(state_dir: Path) -> list[Row]:
    path = demand_path(state_dir)
    if not path.exists():
        return []
    rows: list[Row] = pq.read_table(path).to_pylist()
    return rows


def write_demand(state_dir: Path, rows: Sequence[Row]) -> None:
    """Replace the stored 15:20 rows, whole or not at all, by day and symbol."""
    path = demand_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    ordered = sorted(rows, key=lambda r: (r["date"], r["symbol"]))
    table = pa.Table.from_pylist(
        [{n: r.get(n) for n in DEMAND_SCHEMA.names} for r in ordered], schema=DEMAND_SCHEMA
    )
    pq.write_table(table, tmp)
    tmp.rename(path)


def previous_close(days: Sequence[KrxDay], calendar: MarketCalendar, day: date) -> float | None:
    """The KRX close of the trading day before ``day`` (``auctions.days_of``'s days)."""
    before = calendar.previous_trading_day(day)
    return next((d.close for d in days if d.day == before), None)


def demand_row(
    symbol: str,
    day: date,
    at: datetime,
    price: float | None,
    close: float | None,
    held: Mapping[str, Row],
) -> Row | None:
    """The 15:20 row of ``symbol`` on ``day``; None without a product, a price or a close."""
    products = products_of(symbol)
    if not products or price is None or not close:
        return None
    r = price / close - 1
    found = demand_of(products, held, r)
    return {
        "symbol": symbol,
        "date": day,
        "at": at,
        "price": price,
        "previous_close": close,
        "r": r,
        "demand": found.total,
        "spot_futures": found.spot_futures,
        "futures": found.futures,
        "products": len(products) - len(found.missing),
        "missing": len(found.missing),
        "net_assets_source": source_of(found.products),
    }


def store_demand(state_dir: Path, rows: Sequence[Row]) -> None:
    """Keep the 15:20 rows: a symbol's row of a day replaces the one stored before."""
    if not rows:
        return
    kept = {(r["symbol"], r["date"]): r for r in read_demand(state_dir)}
    for row in rows:
        kept[(row["symbol"], row["date"])] = row
    write_demand(state_dir, list(kept.values()))


def store_closing_demand(
    mid: Callable[[str, datetime], float | None],
    data_dir: Path,
    state_dir: Path,
    calendar: MarketCalendar,
    symbols: Sequence[str],
    at: datetime,
) -> int:
    """Store each symbol's demand at ``at`` (the closing auction's start), with ``mid`` the
    engine's reference price just before it; return how many symbols were stored. A symbol
    without products is left out; one without a price or a previous close is logged."""
    day = at.astimezone(KST).date()
    krx_days = StoredDays(data_dir)
    held = net_assets_for(read_net_assets(state_dir), calendar, day)
    rows = []
    for symbol in symbols:
        if not products_of(symbol):
            continue
        close = previous_close(krx_days.days(symbol), calendar, day)
        row = demand_row(symbol, day, at, mid(symbol, at), close, held)
        if row is None:
            log.warning("no 15:20 closing demand of %s on %s: no price or previous close", symbol, day)
            continue
        rows.append(row)
    store_demand(state_dir, rows)
    return len(rows)


def next_closing_auction(calendar: MarketCalendar, after: datetime) -> datetime:
    """The first closing auction start after ``after``."""
    day = after.astimezone(KST).date()
    while True:
        session = calendar.session(day)
        if session is not None and session.closing_auction > after:
            return session.closing_auction
        day += timedelta(days=1)


async def keep_demand(
    store: Callable[[datetime], int],
    calendar: MarketCalendar,
    now: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """The collector's 15:20 task, until cancelled: at each trading day's closing auction start
    (``STORE_AFTER`` past it), ``store`` the demand at that moment. The sleep is for tests."""
    while True:
        at = next_closing_auction(calendar, now() - STORE_AFTER)
        await sleep(max(0.0, (at + STORE_AFTER - now()).total_seconds()))
        try:
            stored = store(at)
            log.info("stored the 15:20 closing demand of %d symbols", stored)
        except Exception:
            log.exception("the 15:20 closing demand failed")
        # Not again for the same auction, however early the sleep woke.
        while next_closing_auction(calendar, now() - STORE_AFTER) <= at:
            await sleep(1.0)


# The days


@dataclass(frozen=True)
class DayInput:
    """What a day of the table is made of."""

    prices: DayPrices  # #270's, of the day's bars
    auction_value: float | None
    auction_value_source: str | None  # one of ``VALUE_SOURCES``; None without a value
    special: tuple[str, ...] = ()  # the kinds of the special windows begun that day


def gather(
    data_dir: Path, calendar: MarketCalendar, symbol: str, special: Mapping[date, Sequence[str]]
) -> list[DayInput]:
    """Each day of the symbol's bars with #270's prices, its closing auction value and its special
    windows, oldest first."""
    found = []
    krx = read_auctions(data_dir, symbol)
    for day in bar_days(data_dir, [symbol], calendar):
        session = calendar.session(day)
        bars = read_bars(data_dir, day, MINUTE_BARS_TR, symbol)
        if session is None or not bars:
            continue
        prices = day_prices(bars, session)
        if prices is None:
            continue
        value, source = auction_value(data_dir, symbol, session, prices, krx.get(day))
        found.append(DayInput(prices, value, source, tuple(sorted(set(special.get(day, ()))))))
    return found


def _bp(x: float | None) -> float | None:
    return None if x is None else x * 1e4


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def day_rows(
    symbol: str,
    inputs: Sequence[DayInput],
    calendar: MarketCalendar,
    net_assets: Sequence[Row],
    demands: Sequence[Row],
) -> list[Row]:
    """The days of the table (module doc), oldest first."""
    products = products_of(symbol)
    by_day = {i.prices.day: i for i in inputs}
    values = {
        d: (i.auction_value, i.auction_value_source)
        for d, i in by_day.items()
        if i.auction_value is not None and i.auction_value_source is not None
    }
    stored = {r["date"]: r for r in demands if r["symbol"] == symbol}
    rows = []
    for a in auction_days([i.prices for i in inputs], calendar):
        given = by_day[a.day]
        base, base_days, base_sources = denominator(values, calendar, a.day)
        kept = stored.get(a.day)
        demand = spot = futures = None
        source = assets_source = None
        if kept is not None and kept["demand"] is not None:
            demand, spot, futures, source = kept["demand"], kept["spot_futures"], kept["futures"], LIVE
            assets_source = kept.get("net_assets_source") or BEFORE_KRX
        elif products:
            found = demand_of(products, net_assets_for(net_assets, calendar, a.day), a.flow)
            if found.total is not None:
                demand, spot, futures, source = found.total, found.spot_futures, found.futures, BARS
                assets_source = source_of(found.products)
        lead = demand if demand is not None else a.flow
        rows.append(
            {
                "date": a.day.isoformat(),
                "period": period_of(a.day),
                "demand": demand,
                "spot_futures": spot,
                "futures": futures,
                "demand_source": source,
                "net_assets_source": assets_source,
                "ratio": _ratio(spot, base),
                "ratio_total": _ratio(demand, base),
                "denominator": base,
                "denominator_days": base_days,
                "denominator_sources": base_sources,
                "denominator_reason": None if base is not None else NO_AUCTION_VALUE,
                "auction_value": given.auction_value,
                "auction_value_source": given.auction_value_source,
                "auction_bp": _bp(a.move),
                "auction_ticks": a.ticks,
                "flow_bp": _bp(a.flow),
                "compared_with": "demand" if demand is not None else "flow",
                "agree": None if lead == 0 or a.move == 0 else _sign(lead) == _sign(a.move),
                "next_gap_bp": _bp(a.next_gap),
                "next_close_bp": _bp(a.next_close),
                "special": list(given.special),
            }
        )
    return rows


def days_path(state_dir: Path) -> Path:
    return state_dir / "closing_flow" / DAYS_FILE


def write_days(state_dir: Path, body: Mapping[str, Any]) -> None:
    path = days_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(body, ensure_ascii=False))
    tmp.rename(path)


def read_days(state_dir: Path) -> dict[str, Any] | None:
    """The days kept; None without them, or kept in an older layout (``DAYS_VERSION``)."""
    path = days_path(state_dir)
    try:
        body: dict[str, Any] = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    return body if body.get("version") == DAYS_VERSION else None


def compute_days(
    data_dir: Path,
    state_dir: Path,
    calendar: MarketCalendar,
    symbols: Sequence[str],
    special: Mapping[str, Mapping[date, Sequence[str]]],
    now: datetime,
) -> dict[str, Any]:
    """Every symbol's days and closing auction values, as kept in the days file."""
    net_assets, demands = read_net_assets(state_dir), read_demand(state_dir)
    body: dict[str, Any] = {"version": DAYS_VERSION, "computed_at": iso(now), "symbols": {}}
    for symbol in symbols:
        inputs = gather(data_dir, calendar, symbol, special.get(symbol, {}))
        body["symbols"][symbol] = {
            "days": day_rows(symbol, inputs, calendar, net_assets, demands),
            "auction_values": {
                i.prices.day.isoformat(): {"value": i.auction_value, "source": i.auction_value_source}
                for i in inputs
                if i.auction_value is not None
            },
        }
    return body


SpecialReader = Callable[[str, datetime, datetime], Sequence[Mapping[str, Any]]]


def special_days(read: SpecialReader, symbol: str, first: date, last: date) -> dict[date, list[str]]:
    """The kinds of the special windows kept that begin on each day from ``first`` to ``last``."""
    rows = read(
        symbol, datetime.combine(first, time(), KST), datetime.combine(last + timedelta(days=1), time(), KST)
    )
    out: dict[date, list[str]] = defaultdict(list)
    for row in rows:
        start = datetime.fromisoformat(str(row["start"])).astimezone(KST).date()
        out[start].append(str(row["kind"]))
    return dict(out)


async def keep_days(
    data_dir: Path,
    state_dir: Path,
    calendar: MarketCalendar,
    symbols: Sequence[str],
    read_special: SpecialReader,
    now: datetime,
) -> None:
    """Compute and keep the days (after the history task's backfill): the special windows read
    here, on the store's thread, the files in a worker's."""
    first = now.astimezone(KST).date() - timedelta(days=400)  # the backfill's year, and more
    last = now.astimezone(KST).date()
    special = {s: special_days(read_special, s, first, last) for s in symbols}
    body = await asyncio.to_thread(compute_days, data_dir, state_dir, calendar, symbols, special, now)
    await asyncio.to_thread(write_days, state_dir, body)


# The answers


def _stats(rows: Sequence[Row]) -> dict[str, Any]:
    """A summary cell: the days, how often the auction went the demand's (or the flow's) way, and
    how often the next day's gap and close went against the auction, each with its count (#270's)."""

    def lead(r: Row) -> float:
        return float(r["demand"] if r["demand"] is not None else r["flow_bp"])

    agree = same_share([(lead(r), r["auction_bp"]) for r in rows])
    gap = opposite_share([(r["auction_bp"], r["next_gap_bp"]) for r in rows])
    after = opposite_share([(r["auction_bp"], r["next_close_bp"]) for r in rows])
    return {
        "days": len(rows),
        "auction_bp_median": statistics.median(abs(r["auction_bp"]) for r in rows) if rows else None,
        "agree": None if agree is None else agree[0],
        "agree_n": 0 if agree is None else agree[1],
        "next_gap_opposite": None if gap is None else gap[0],
        "next_gap_n": 0 if gap is None else gap[1],
        "next_close_opposite": None if after is None else after[0],
        "next_close_n": 0 if after is None else after[1],
    }


def _cell(rows: Sequence[Row]) -> dict[str, Any]:
    return {"all": _stats(rows), "without_special": _stats([r for r in rows if not r["special"]])}


def summary(rows: Sequence[Row]) -> dict[str, Any]:
    """The days' summary: by period (#270's) and by tertile of the ratio's size."""
    periods = []
    for name, first, end in PERIODS:
        chosen = [r for r in rows if r["period"] == name]
        if chosen:
            periods.append(
                {
                    "period": name,
                    "from": None if first is None else first.isoformat(),
                    "to": None if end is None else (end - timedelta(days=1)).isoformat(),
                    **_cell(chosen),
                }
            )
    rated = sorted((r for r in rows if r["ratio"] is not None), key=lambda r: abs(r["ratio"]))
    tertiles = []
    if len(rated) >= 3:
        size, extra = divmod(len(rated), 3)
        cut = 0
        for k in range(3):
            part = rated[cut : cut + size + (1 if k < extra else 0)]
            cut += len(part)
            tertiles.append(
                {
                    "tertile": k + 1,
                    "ratio_from": abs(part[0]["ratio"]),
                    "ratio_to": abs(part[-1]["ratio"]),
                    **_cell(part),
                }
            )
    return {"periods": periods, "tertiles": tertiles}


def days_answer(symbol: str, kept: Mapping[str, Any] | None, first: date, last: date) -> dict[str, Any]:
    """``GET /v1/symbols/{symbol}/closing-flow/days``: the kept days from ``first`` to ``last``
    and their summary."""
    rows = []
    if kept is not None:
        rows = [
            r
            for r in kept["symbols"].get(symbol, {}).get("days", [])
            if first.isoformat() <= r["date"] <= last.isoformat()
        ]
    return {
        "symbol": symbol,
        "from": first.isoformat(),
        "to": last.isoformat(),
        "computed_at": None if kept is None else kept["computed_at"],
        "days": rows,
        "summary": summary(rows),
    }


def demand_moment(calendar: MarketCalendar, now: datetime) -> tuple[date, datetime | None]:
    """The day the answer is of (today if it trades, else the last trading day) and the moment its
    demand is at: None before its open, the minute during continuous trading, the closing
    auction's start from then on."""
    local = now.astimezone(KST)
    day = local.date()
    if not calendar.is_trading_day(day):
        day = calendar.previous_trading_day(day)
    session = calendar.session(day)
    assert session is not None
    if local < session.open:
        return day, None
    if local < session.closing_auction:
        return day, local.replace(second=0, microsecond=0)
    return day, session.closing_auction


def today_answer(
    symbol: str,
    day: date,
    at: datetime | None,
    live_price: float | None,
    now: datetime,
    calendar: MarketCalendar,
    krx_days: Sequence[KrxDay],
    net_assets: Sequence[Row],
    demands: Sequence[Row],
    kept: Mapping[str, Any] | None,
    names: Mapping[str, str],
) -> dict[str, Any]:
    """``GET /v1/symbols/{symbol}/closing-flow``: the day's demand at ``at`` (module doc)."""
    products = products_of(symbol)
    held = net_assets_for(net_assets, calendar, day)
    session = calendar.session(day)
    stored = next((r for r in demands if r["symbol"] == symbol and r["date"] == day), None)
    source: str | None = None
    price, close = None, previous_close(krx_days, calendar, day)
    if at is not None and session is not None and at >= session.closing_auction and stored is not None:
        price, close, source = stored["price"], stored["previous_close"], STORED
    elif live_price is not None:
        price, source = live_price, LIVE
    r = None if price is None or not close else price / close - 1
    found = demand_of(products, held, r)
    values: dict[date, AuctionValue] = {}
    if kept is not None:
        values = {
            date.fromisoformat(d): (v["value"], v["source"])
            for d, v in kept["symbols"].get(symbol, {}).get("auction_values", {}).items()
        }
    base, base_days, base_sources = denominator(values, calendar, day)
    reason = None
    if not products:
        reason = "no_products"
    elif at is None:
        reason = "before_open"
    elif price is None:
        reason = "no_price"
    elif not close:
        reason = "no_previous_close"
    elif found.total is None:
        reason = "no_net_assets"
    return {
        "symbol": symbol,
        "day": day.isoformat(),
        "as_of": iso(now),
        "at": None if at is None else iso(at),
        "source": source if r is not None else None,
        "price": price,
        "previous_close": close,
        "r": r,
        "demand": found.total,
        "spot_futures": found.spot_futures,
        "futures": found.futures,
        "ratio": _ratio(found.spot_futures, base),
        "ratio_total": _ratio(found.total, base),
        "denominator": base,
        "denominator_days": base_days,
        "denominator_sources": base_sources,
        "denominator_reason": None
        if base is not None
        else NOT_COMPUTED
        if kept is None
        else NO_AUCTION_VALUE,
        "products": [{**row, "name": names.get(row["code"])} for row in found.products],
        "missing": found.missing,
        "reason": reason,
    }
