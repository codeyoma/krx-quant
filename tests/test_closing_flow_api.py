"""``GET /v1/symbols/{symbol}/closing-flow`` and ``/closing-flow/days`` (#305): the single-stock
ETFs' closing rebalancing demand A·(L² − L)·r and what the closing auction and the next day did.

Input: a fixture state folder (the products' net assets, stored 15:20 demands, the days computed
as the collector computes them) and data folder (the backfill's one-minute bars, the kept KRX
closing prices, recorded KRX trades), with an engine fed integrated quotes. Every number is made
up. Output: the two answers, within the /v1 schema, and the 15:20 row the collector stores.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from aiohttp.test_utils import TestClient, TestServer

from krx_quant.api import create_app
from krx_quant.auctions import MINUTE_BAR, write_auctions
from krx_quant.backfill import BAR_SCHEMA, stock_bars_path
from krx_quant.clock import FakeClock
from krx_quant.closing_flow import keep_days, keep_demand, read_demand, store_closing_demand, write_demand
from krx_quant.engine import PredictionEngine
from krx_quant.feeds import KRX_TRADE
from krx_quant.feeds.base import KST
from krx_quant.kinds import DIRECTION_KIND
from krx_quant.market_calendar import MarketCalendar
from krx_quant.net_assets import KIS, KRX, write_net_assets
from krx_quant.predictions import PredictionStore
from krx_quant.products import products_of
from krx_quant.recorder import ParquetRecorder
from krx_quant.replay import replay_into
from krx_quant.special import Kind, Window
from krx_quant.structure_check import PERIODS, structure_check
from tests.api_schema import check
from tests.replay_events import book

HANGUL_DAY = date(2026, 10, 9)
CALENDAR = MarketCalendar(
    {
        d: d.weekday() < 5 and d != HANGUL_DAY
        for d in (date(2026, 6, 1) + timedelta(days=i) for i in range(200))
    }
)
MONDAY, THURSDAY = date(2026, 10, 12), date(2026, 10, 8)  # the day asked, and the trading day before
SYMBOLS = ("005930", "009150")
SAMSUNG = products_of("005930")
FEE = 0.00015
NOT_KEPT = "0198B0"  # a futures-only 2x product whose net assets are not kept


def at(hhmm: str, day: date = MONDAY) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm), KST)


def engine_at(tmp_path: Path, now: datetime) -> tuple[PredictionEngine, FakeClock]:
    clock = FakeClock(now)
    engine = PredictionEngine(
        PredictionStore(tmp_path / "e.sqlite3"),
        CALENDAR,
        SYMBOLS,
        FEE,
        clock,
        algorithms=(),
        kinds=(DIRECTION_KIND,),
        kind_algorithms=(),
    )
    return engine, clock


def quotes(start: datetime, end: datetime, bid: int, ask: int, symbol: str = "005930") -> list[Any]:
    out, when = [], start
    while when <= end:
        out.append(book(when, bid, ask, symbol))
        when += timedelta(seconds=10)
    return out


def net_assets(state: Path, day: date, assets: dict[str, int]) -> None:
    rows = [
        {"code": code, "date": day, "source": KIS, "net_assets": value, "fetched_at": at("07:30", day)}
        for code, value in assets.items()
    ]
    write_net_assets(state, rows)


def closes(data: Path, symbol: str, days: dict[date, int]) -> None:
    """The kept KRX closing prices (``auctions``), the previous close the demand's r is over."""
    rows = {
        d: {"symbol": symbol, "date": d, "open": c, "close": c, "source": MINUTE_BAR} for d, c in days.items()
    }
    write_auctions(data, symbol, rows)


def bars(data: Path, symbol: str, day: date, open_: int, pre_close: int, close: int, volume: int) -> None:
    """A day's one-minute bars as the backfill stores them: the open, 20 minutes before the
    auction, the last minute before it, and the closing auction's print."""
    prices = (("09:00", open_, 10), ("14:59", open_, 10), ("15:19", pre_close, 10), ("15:30", close, volume))
    rows = [
        {
            "symbol": symbol,
            "bar_time": at(t, day),
            "venue": "UN",
            "open": p,
            "high": p,
            "low": p,
            "close": p,
            "volume": v,
            "cum_value": 0,
        }
        for t, p, v in prices
    ]
    path = stock_bars_path(data, day, symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=BAR_SCHEMA), path)


def trading_days_before(day: date, n: int) -> list[date]:
    days = []
    for _ in range(n):
        day = CALENDAR.previous_trading_day(day)
        days.append(day)
    return sorted(days)


async def get(app: Any, path: str, definition: str, status: int = 200) -> Any:
    async with TestClient(TestServer(app)) as client:
        response = await client.get(path)
        assert response.status == status, await response.text()
        body = await response.json()
    check(definition if status == 200 else "error", body)
    return body


ASSETS = {p.code: (i + 1) * 10_000_000_000 for i, p in enumerate(SAMSUNG) if p.code != NOT_KEPT}


async def test_the_minute_s_demand_by_product_and_structure_and_its_ratio(tmp_path: Path) -> None:
    """Input: Thursday's net assets of seven of 005930's eight products, its KRX close 80,000, the
    20 days before Monday with recorded closing auction values 1..20 × 10억 (their median 10.5 ×
    10억; the bars' auction volume is not used), and
    Monday's quotes around 81,000 at 10:30: r = 1.25%. Output: each product's A·(L² − L)·r, 2·A·r
    at 2x and 6·A·r for the inverse 0193L0, the sums by structure, the missing product, ratios."""
    data, state = tmp_path / "data", tmp_path / "state"
    net_assets(state, THURSDAY, ASSETS)
    closes(data, "005930", {THURSDAY: 80_000})
    for n, day in enumerate(trading_days_before(MONDAY, 21)):
        bars(data, "005930", day, 80_000, 80_000, 80_000, 999_999)
        if n:  # n × 10억 (the first: none)
            record_print(data, "005930", day, 80_000, n * 12_500)
    engine, clock = engine_at(tmp_path, at("09:00"))
    await keep_days(data, state, CALENDAR, SYMBOLS, engine.store.special_windows, at("06:00"))
    await replay_into(engine, clock, quotes(at("10:00"), at("10:30"), 80_950, 81_050), until=at("10:30:20"))

    body = await get(
        create_app(engine, data_dir=data, state_dir=state, names={"0193W0": "가 2X"}),
        "/v1/symbols/005930/closing-flow",
        "closing_flow",
    )

    assert (body["day"], body["at"], body["source"]) == ("2026-10-12", "2026-10-12T10:30:00+09:00", "live")
    assert (body["price"], body["previous_close"]) == (81_000, 80_000)
    assert body["r"] == pytest.approx(0.0125)
    by_code = {p["code"]: p for p in body["products"]}
    assert by_code["0193W0"] == {
        "code": "0193W0",
        "name": "가 2X",
        "leverage": 2,
        "structure": "spot_futures",
        "coefficient": 2,
        "net_assets": 10_000_000_000,
        "net_assets_date": "2026-10-08",
        "net_assets_source": "kis",
        "demand": 250_000_000,  # 2 × 100억 × 1.25%: a buy
    }
    inverse = by_code["0193L0"]
    assert (inverse["coefficient"], inverse["net_assets"], inverse["demand"]) == (
        6,
        80_000_000_000,
        6_000_000_000,
    )
    assert (by_code[NOT_KEPT]["net_assets"], by_code[NOT_KEPT]["demand"]) == (None, None)
    assert body["missing"] == [NOT_KEPT]
    # Spot and futures: 0193W0, 0195R0, 0194M0, 0192M0, 0193K0 at 100–500억; futures: 0194N0 at 600억
    # and the inverse 0193L0 at 800억 (0198B0 not kept).
    assert body["spot_futures"] == 2 * (1 + 2 + 3 + 4 + 5) * 10_000_000_000 * 0.0125
    assert body["futures"] == (2 * 6 + 6 * 8) * 10_000_000_000 * 0.0125
    assert body["demand"] == body["spot_futures"] + body["futures"]
    assert (body["denominator"], body["denominator_days"], body["denominator_reason"]) == (
        10_500_000_000,
        20,
        None,
    )
    assert body["ratio"] == pytest.approx(body["spot_futures"] / 10_500_000_000)
    assert body["ratio_total"] == pytest.approx(body["demand"] / 10_500_000_000)
    assert body["reason"] is None


async def test_a_falling_day_sells_and_a_symbol_without_products_says_so(tmp_path: Path) -> None:
    data, state = tmp_path / "data", tmp_path / "state"
    net_assets(state, THURSDAY, ASSETS)
    closes(data, "005930", {THURSDAY: 80_000})
    closes(data, "009150", {THURSDAY: 150_000})
    engine, clock = engine_at(tmp_path, at("09:00"))
    events = [
        *quotes(at("10:00"), at("10:30"), 78_950, 79_050),  # r = −1.25%
        *quotes(at("10:00"), at("10:30"), 149_950, 150_050, "009150"),
    ]
    await replay_into(engine, clock, sorted(events, key=lambda e: e[1]["received_at"]), until=at("10:30:20"))
    app = create_app(engine, data_dir=data, state_dir=state)

    body = await get(app, "/v1/symbols/005930/closing-flow", "closing_flow")
    none = await get(app, "/v1/symbols/009150/closing-flow", "closing_flow")

    by_code = {p["code"]: p for p in body["products"]}
    assert by_code["0193W0"]["demand"] == -250_000_000, "2·A·r, the way the stock moved: a sell"
    assert by_code["0193L0"]["demand"] == -6_000_000_000, "6·A·r for the inverse: a sell too"
    assert body["demand"] < 0 and body["denominator"] is None and body["ratio"] is None
    assert body["denominator_reason"] == "not_computed", "no days computed yet"
    assert (none["products"], none["demand"], none["reason"]) == ([], None, "no_products")
    assert none["r"] == pytest.approx(0.0)


async def test_before_the_open_without_net_assets_and_after_the_close(tmp_path: Path) -> None:
    data, state = tmp_path / "data", tmp_path / "state"
    closes(data, "005930", {THURSDAY: 80_000})
    engine, clock = engine_at(tmp_path, at("08:40"))
    app = create_app(engine, data_dir=data, state_dir=state)

    early = await get(app, "/v1/symbols/005930/closing-flow", "closing_flow")
    assert (early["at"], early["demand"], early["reason"]) == (None, None, "before_open")
    assert early["missing"] == [p.code for p in SAMSUNG], "no net assets kept: all missing"

    await replay_into(engine, clock, quotes(at("10:00"), at("10:30"), 80_950, 81_050), until=at("10:30:20"))
    bare = await get(app, "/v1/symbols/005930/closing-flow", "closing_flow")
    assert bare["r"] == pytest.approx(0.0125) and bare["demand"] is None and bare["reason"] == "no_net_assets"

    # After the close: the 15:20 row stored that day.
    net_assets(state, THURSDAY, ASSETS)
    write_demand(
        state,
        [
            {
                "symbol": "005930",
                "date": MONDAY,
                "at": at("15:20"),
                "price": 82_000.0,
                "previous_close": 80_000.0,
                "r": 0.025,
                "demand": 1,
                "spot_futures": 1,
                "futures": 0,
                "products": 7,
                "missing": 1,
            }
        ],
    )
    clock.set(at("18:00"))
    late = await get(app, "/v1/symbols/005930/closing-flow", "closing_flow")
    assert (late["day"], late["at"], late["source"]) == ("2026-10-12", "2026-10-12T15:20:00+09:00", "stored")
    assert (late["price"], late["r"]) == (82_000, pytest.approx(0.025))
    assert {p["code"]: p["demand"] for p in late["products"]}["0193W0"] == 500_000_000

    await get(app, "/v1/symbols/000000/closing-flow", "closing_flow", 404)
    without = await get(create_app(engine), "/v1/symbols/005930/closing-flow", "closing_flow")
    assert without["previous_close"] is None and without["reason"] == "no_price"


class _Stop(Exception):
    pass


async def test_the_collector_stores_the_demand_at_the_closing_auction_s_start(tmp_path: Path) -> None:
    """Input: quotes around 81,000 until 15:19:50 and around 83,000 from 15:20:01 (the auction's
    book). Output: the row stored 5 seconds after 15:20 is at 15:20 with the price just before it."""
    data, state = tmp_path / "data", tmp_path / "state"
    net_assets(state, THURSDAY, ASSETS)
    closes(data, "005930", {THURSDAY: 80_000})
    engine, clock = engine_at(tmp_path, at("15:00"))
    events = [
        *quotes(at("15:10"), at("15:19:50"), 80_950, 81_050),
        *quotes(at("15:20:01"), at("15:20:05"), 82_950, 83_050),
    ]
    await replay_into(engine, clock, events, until=at("15:10"))
    woke: list[datetime] = []

    async def sleep(seconds: float) -> None:
        target = clock.now() + timedelta(seconds=seconds)
        if target > at("15:21"):
            raise _Stop  # the next day's auction: done
        later = [e for e in events if e[1]["received_at"] > clock.now()]
        await replay_into(engine, clock, later, until=target)
        woke.append(clock.now())

    store = lambda moment: store_closing_demand(  # noqa: E731
        engine.mid_at, data, state, CALENDAR, SYMBOLS, moment
    )
    with pytest.raises(_Stop):
        await keep_demand(store, CALENDAR, clock.now, sleep)

    assert woke == [at("15:20:05")]
    [row] = read_demand(state)
    assert (row["symbol"], row["date"], row["at"]) == ("005930", MONDAY, at("15:20"))
    assert (row["price"], row["previous_close"], row["r"]) == (81_000, 80_000, pytest.approx(0.0125))
    assert (row["products"], row["missing"]) == (7, 1)
    assert row["spot_futures"] == 2 * 15 * 10_000_000_000 * 0.0125
    assert row["demand"] == row["spot_futures"] + row["futures"]


# The days: ten trading days across the FSC's request (07-31), each as (open, last price before
# the auction, the auction's print). The first has no day before it and is no row.
DAYS = {
    date(2026, 7, 27): (80_000, 80_500, 80_600),
    date(2026, 7, 28): (80_700, 81_000, 81_200),
    date(2026, 7, 29): (81_100, 80_800, 80_700),
    date(2026, 7, 30): (80_900, 81_500, 81_400),
    date(2026, 7, 31): (81_300, 81_000, 81_100),
    date(2026, 8, 3): (81_200, 81_600, 81_800),
    date(2026, 8, 4): (81_700, 81_300, 81_300),
    date(2026, 8, 5): (81_400, 81_900, 82_000),
    date(2026, 8, 6): (81_900, 81_500, 81_400),
    date(2026, 8, 7): (81_500, 81_700, 81_900),
}
UNRECORDED_DAY, VI_DAY, LIVE_DAY = date(2026, 8, 3), date(2026, 7, 29), date(2026, 8, 6)


def record_print(data: Path, symbol: str, day: date, price: int, qty: int) -> None:
    """KRX trades as the collector records them: one in continuous trading, the closing print."""
    recorder = ParquetRecorder(data, flush_rows=1000, flush_seconds=3600)
    for when, p, q in ((at("15:10", day), price - 100, 7), (at("15:30:12", day), price, qty)):
        row = dict.fromkeys(KRX_TRADE.schema.names) | {
            "symbol": symbol,
            "event_time": when,
            "received_at": when.astimezone(UTC),
            "venue": "KRX",
            "price": p,
            "qty": q,
        }
        recorder.record("events", day, KRX_TRADE.tr_id, symbol, row, KRX_TRADE.schema)
    recorder.close()


def section_two(text: str, symbol: str) -> dict[str, str]:
    """``structure-check``'s section 2 lines of ``symbol``, by period."""
    block = text.split("2. 마감 단일가")[1].split("\n\n")[0]
    found = {}
    for line in block.splitlines():
        if line.strip().startswith(f"{symbol} "):
            name, rest = line.strip().removeprefix(f"{symbol} ").split(": ", 1)
            found[name] = rest
    return found


def shown(share: float | None, n: int) -> str:
    return "없음" if share is None else f"{100 * share:.1f}% (n={n})"


async def test_the_days_measure_as_structure_check_and_add_the_demand(tmp_path: Path) -> None:
    data, state = tmp_path / "data", tmp_path / "state"
    for symbol in SYMBOLS:
        for i, (day, (o, pre, c)) in enumerate(DAYS.items()):
            bars(data, symbol, day, o, pre, c, 1_000 + 100 * i)
    for i, (day, (_, _, c)) in enumerate(DAYS.items()):
        if day != UNRECORDED_DAY:  # 005930's closing prints recorded but one day's
            record_print(data, "005930", day, c, 1_000 + 100 * i)
    days = list(DAYS)
    write_net_assets(
        state,
        [
            {
                "code": p.code,
                "date": d,
                "source": KIS,
                "net_assets": 10_000_000_000,
                "fetched_at": at("07:30", d),
            }
            for d in days[:-1]
            for p in SAMSUNG
        ],
    )
    write_demand(  # the stored 15:20 demand: a buy, on a day the bars' flow was down
        state,
        [
            {
                "symbol": "005930",
                "date": LIVE_DAY,
                "at": at("15:20", LIVE_DAY),
                "price": 82_100.0,
                "previous_close": 82_000.0,
                "r": 0.00122,
                "demand": 300_000_000,
                "spot_futures": 200_000_000,
                "futures": 100_000_000,
                "products": 8,
                "missing": 0,
            }
        ],
    )
    engine, _ = engine_at(tmp_path, at("09:00", date(2026, 8, 10)))
    start = at("10:00", VI_DAY)
    engine.store.keep_special(Window(Kind.VI, "005930", start, start + timedelta(minutes=2)), start)
    await keep_days(
        data, state, CALENDAR, SYMBOLS, engine.store.special_windows, at("20:40", date(2026, 8, 7))
    )
    app = create_app(engine, data_dir=data, state_dir=state)

    control = await get(app, "/v1/symbols/009150/closing-flow/days", "closing_flow_days")
    samsung = await get(
        app, "/v1/symbols/005930/closing-flow/days?from=2026-07-01&to=2026-08-31", "closing_flow_days"
    )

    # 009150 (no products): every day compares the day's flow, as structure-check does.
    rows = {r["date"]: r for r in control["days"]}
    assert sorted(rows) == [d.isoformat() for d in days[1:]], "the first day has no day before it"
    d = rows["2026-07-29"]
    assert d["auction_bp"] == pytest.approx((80_700 / 80_800 - 1) * 1e4)
    assert d["flow_bp"] == pytest.approx((80_800 / 81_200 - 1) * 1e4)
    assert d["next_gap_bp"] == pytest.approx((80_900 / 80_700 - 1) * 1e4)
    assert d["next_close_bp"] == pytest.approx((81_400 / 80_700 - 1) * 1e4)
    assert (d["compared_with"], d["agree"], d["demand"], d["net_assets_source"]) == ("flow", True, None, None)
    # No recording of 009150: each day's value from the integrated bar (no KRX bar kept either).
    assert (d["auction_value"], d["auction_value_source"]) == (80_700 * 1_200, "integrated_bar")
    assert (d["denominator"], d["denominator_days"], d["denominator_reason"]) == (
        (80_600 * 1_000 + 81_200 * 1_100) / 2,
        2,
        None,
    )
    assert d["denominator_sources"] == {"recorded": 0, "krx_bar": 0, "integrated_bar": 2}
    first = rows["2026-07-28"]
    assert (first["denominator_days"], first["denominator"]) == (1, 80_600 * 1_000)
    assert rows["2026-08-04"]["agree"] is None, "the auction did not move"
    assert rows["2026-08-07"]["next_gap_bp"] is None, "no next day yet"
    text = structure_check(data, state, CALENDAR, ("009150",), fee_rate=FEE)
    lines = section_two(text, "009150")
    summary = {p["period"]: p for p in control["summary"]["periods"]}
    assert set(summary) == set(lines) == {PERIODS[1][0], PERIODS[2][0]}
    for name, line in lines.items():
        cell = summary[name]["all"]
        assert line.startswith(f"{cell['days']}일 · {cell['auction_bp_median']:.1f}bp")
        assert f"그날 흐름과 {shown(cell['agree'], cell['agree_n'])}" in line
        assert f"다음날 갭 반대 {shown(cell['next_gap_opposite'], cell['next_gap_n'])}" in line
        assert f"다음날 종가 반대 {shown(cell['next_close_opposite'], cell['next_close_n'])}" in line

    # 005930: the demand from the net assets and the bars' flow, the stored one on its day.
    rows = {r["date"]: r for r in samsung["days"]}
    bars_day = rows["2026-08-05"]
    flow = 81_900 / 81_300 - 1
    assert bars_day["demand_source"] == "bars"
    assert bars_day["spot_futures"] == 5 * round(2 * 10_000_000_000 * flow)
    assert bars_day["futures"] == 2 * round(2 * 10_000_000_000 * flow) + round(6 * 10_000_000_000 * flow)
    assert (bars_day["auction_value"], bars_day["auction_value_source"]) == (82_000 * 1_700, "recorded")
    earlier = sorted((1_000 + 100 * i) * c for i, (d, (o, pre, c)) in enumerate(DAYS.items()) if i < 7)
    assert (bars_day["denominator"], bars_day["denominator_days"]) == (earlier[3], 7)
    assert bars_day["denominator_sources"] == {"recorded": 6, "krx_bar": 0, "integrated_bar": 1}
    assert bars_day["ratio"] == pytest.approx(bars_day["spot_futures"] / bars_day["denominator"])
    assert bars_day["net_assets_source"] == "kis"
    unrecorded = rows[UNRECORDED_DAY.isoformat()]
    assert (unrecorded["auction_value"], unrecorded["auction_value_source"]) == (
        81_800 * 1_500,
        "integrated_bar",
    ), "no recorded print, no KRX bar kept: the integrated bar's volume × close"
    live = rows["2026-08-06"]
    assert (live["demand_source"], live["demand"], live["compared_with"]) == ("live", 300_000_000, "demand")
    assert live["net_assets_source"] == "kis", "stored before the KRX source: KIS's, the only one then"
    assert live["agree"] is False, "a buy, and the auction went down"
    assert rows["2026-07-29"]["special"] == ["vi"]
    periods = {p["period"]: p for p in samsung["summary"]["periods"]}
    after = periods[PERIODS[1][0]]
    assert (after["all"]["days"], after["without_special"]["days"]) == (3, 2)
    assert [t["all"]["days"] for t in samsung["summary"]["tertiles"]] == [3, 3, 3]
    assert samsung["computed_at"] == "2026-08-07T20:40:00+09:00"


async def test_days_without_a_computation_and_the_query_s_errors(tmp_path: Path) -> None:
    engine, _ = engine_at(tmp_path, at("09:00"))
    app = create_app(engine, state_dir=tmp_path / "state")

    body = await get(app, "/v1/symbols/005930/closing-flow/days", "closing_flow_days")
    assert (body["from"], body["to"], body["computed_at"], body["days"]) == (
        "2025-10-13",
        "2026-10-12",
        None,
        [],
    )
    assert body["summary"] == {"periods": [], "tertiles": []}
    await get(app, "/v1/symbols/005930/closing-flow/days?from=2026-10-1", "closing_flow_days", 400)
    await get(
        app, "/v1/symbols/005930/closing-flow/days?from=2026-10-12&to=2026-10-11", "closing_flow_days", 400
    )
    await get(app, "/v1/symbols/000000/closing-flow/days", "closing_flow_days", 404)


async def test_the_past_days_read_krx_s_net_assets_and_krx_s_closing_bar(tmp_path: Path) -> None:
    """#305, the KRX Open API's net assets and the bars' denominator. Input: the ten days' integrated
    bars of 005930 (auction volume 777) and no recording; KRX's net assets of every day, but one
    product's on 07-30 (KIS's then); the KRX closing bars kept by ``auctions`` with their volumes,
    but none on 08-04 and a volume of 0 on 08-05. Output: every day after the first has its demand
    from KRX's A and the bars (``krx``; ``krx+kis`` on 07-31, which reads 07-30's), and its closing
    auction value from KRX's bar (its volume × close), the integrated bar's where KRX's has none."""
    data, state = tmp_path / "data", tmp_path / "state"
    days = list(DAYS)
    kept = {}
    for i, (day, (o, pre, c)) in enumerate(DAYS.items()):
        bars(data, "005930", day, o, pre, c, 777)
        if day != date(2026, 8, 4):
            volume = 0 if day == date(2026, 8, 5) else 2_000 + i
            kept[day] = {"symbol": "005930", "date": day, "open": o, "close": c, "source": MINUTE_BAR}
            kept[day]["close_volume"] = volume
    write_auctions(data, "005930", kept)
    mixed, alone = date(2026, 7, 30), SAMSUNG[0].code
    write_net_assets(
        state,
        [
            {
                "code": p.code,
                "date": d,
                "source": KIS if (d, p.code) == (mixed, alone) else KRX,
                "net_assets": 10_000_000_000,
                "fetched_at": at("08:05", d),
                "shares": None,
                "nav": None,
            }
            for d in days
            for p in SAMSUNG
        ],
    )
    engine, _ = engine_at(tmp_path, at("09:00", date(2026, 8, 10)))
    await keep_days(data, state, CALENDAR, SYMBOLS, engine.store.special_windows, at("20:40", days[-1]))

    body = await get(
        create_app(engine, data_dir=data, state_dir=state),
        "/v1/symbols/005930/closing-flow/days?from=2026-07-01&to=2026-08-31",
        "closing_flow_days",
    )

    rows = {r["date"]: r for r in body["days"]}
    assert sorted(rows) == [d.isoformat() for d in days[1:]]
    assert {r["demand_source"] for r in rows.values()} == {"bars"}
    assert {d: r["net_assets_source"] for d, r in rows.items() if r["net_assets_source"] != "krx"} == {
        "2026-07-31": "krx+kis"
    }, "07-31 reads 07-30's: KIS's for the product KRX lacked"
    for i, (day, (_, _, c)) in enumerate(DAYS.items()):
        if i == 0:
            continue
        row = rows[day.isoformat()]
        if day in (date(2026, 8, 4), date(2026, 8, 5)):
            assert (row["auction_value"], row["auction_value_source"]) == (c * 777, "integrated_bar"), day
        else:
            assert (row["auction_value"], row["auction_value_source"]) == (c * (2_000 + i), "krx_bar"), day
    last = rows["2026-08-07"]
    assert last["denominator_sources"] == {"recorded": 0, "krx_bar": 7, "integrated_bar": 2}
    assert last["denominator_days"] == 9 and last["denominator_reason"] is None
    assert last["ratio"] == pytest.approx(last["spot_futures"] / last["denominator"])


async def test_today_s_products_read_krx_s_net_assets_first(tmp_path: Path) -> None:
    """Source precedence: Thursday's net assets from KRX's Open API and from KIS; KRX's are read,
    KIS's only for the product KRX lacks."""
    data, state = tmp_path / "data", tmp_path / "state"
    rows = [
        {"code": code, "date": THURSDAY, "source": KIS, "net_assets": 1, "fetched_at": at("06:12", MONDAY)}
        for code in ASSETS
    ]
    rows += [
        {
            "code": code,
            "date": THURSDAY,
            "source": KRX,
            "net_assets": value,
            "fetched_at": at("08:05", MONDAY),
            "shares": 3_000_000,
            "nav": 12_345.67,
        }
        for code, value in ASSETS.items()
        if code != "0193W0"
    ]
    write_net_assets(state, rows)
    closes(data, "005930", {THURSDAY: 80_000})
    engine, clock = engine_at(tmp_path, at("09:00"))
    await replay_into(engine, clock, quotes(at("10:00"), at("10:30"), 80_950, 81_050), until=at("10:30:20"))

    body = await get(
        create_app(engine, data_dir=data, state_dir=state), "/v1/symbols/005930/closing-flow", "closing_flow"
    )

    by_code = {p["code"]: p for p in body["products"]}
    assert (by_code["0193W0"]["net_assets_source"], by_code["0193W0"]["net_assets"]) == ("kis", 1)
    assert {p["net_assets_source"] for c, p in by_code.items() if c not in ("0193W0", NOT_KEPT)} == {"krx"}
    assert by_code["0195R0"]["net_assets"] == ASSETS["0195R0"]
    assert body["missing"] == [NOT_KEPT]
