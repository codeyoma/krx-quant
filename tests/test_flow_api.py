"""``GET /v1/symbols/{symbol}/flow`` (#306): a symbol's intraday flows of a day, from the
recorded program trading, member firms and investor estimates, a minute at a time; all within
the /v1 schema."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from aiohttp.test_utils import TestClient, TestServer

from krx_quant.api import create_app
from krx_quant.backfill import BAR_SCHEMA, stock_bars_path
from krx_quant.clock import FakeClock
from krx_quant.engine import PredictionEngine
from krx_quant.feeds import FOREIGN_ESTIMATE, MEMBER, MEMBER_REST, PROGRAM, Feed, RestFeed
from krx_quant.feeds.base import KST
from krx_quant.kinds import DIRECTION_KIND
from krx_quant.market_calendar import MarketCalendar
from krx_quant.predictions import PredictionStore
from krx_quant.recorder import ParquetRecorder
from krx_quant.replay import replay_into
from krx_quant.supply import KIS, write_supply
from tests.api_schema import check
from tests.replay_events import trade

FRIDAY, MONDAY = date(2026, 10, 16), date(2026, 10, 19)
CALENDAR = MarketCalendar()
SYMBOL = "005930"


def at(hhmmss: str, day: date = MONDAY) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmmss), KST)


def record(data: Path, rows: list[tuple[Feed | RestFeed, Feed | RestFeed, dict[str, Any]]]) -> None:
    """Rows as the collector records them: under the feed's events name, in its schema."""
    recorder = ParquetRecorder(data, flush_rows=1000, flush_seconds=3600)
    for feed, schema_feed, row in rows:
        name = feed.tr_id if isinstance(feed, Feed) else feed.events_feed
        full = dict.fromkeys(schema_feed.schema.names) | row | {"symbol": SYMBOL}
        received = full["received_at"].astimezone(KST).date()
        recorder.record("events", received, name, SYMBOL, full, schema_feed.schema)
    recorder.close()


def program(when: str, net: int, day: date = MONDAY, rest: bool = False, second: int = 1) -> tuple[Any, ...]:
    t = at(when, day)
    row = {
        "event_time": t,
        "received_at": t.replace(second=second),
        "buy_value": 1_000 + net,
        "sell_value": 1_000,
        "net_value": net,
        "net_qty": net // 100,
    }
    if not rest:
        row |= {"sell_order_qty": 0, "buy_order_qty": 0, "whole_net_qty": 0}
    return (PROGRAM, PROGRAM, row)


def member(when: str, foreign: int, day: date = MONDAY, rest: bool = False) -> tuple[Any, ...]:
    row: dict[str, Any] = {"received_at": at(when, day), "foreign_net_qty": foreign}
    for side in ("sell", "buy"):
        for k in range(1, 6):
            row |= {
                f"{side}_member_{k}_no": f"{k:03d}",
                f"{side}_member_{k}_name": f"{side}{k}",
                f"{side}_member_{k}_qty": 600 - 100 * k,
                f"{side}_member_{k}_foreign": k == 2,
            }
    row["sell_member_5_name"], row["sell_member_5_qty"] = "", 0  # four sellers that day
    return (MEMBER_REST if rest else MEMBER, MEMBER, row)


def estimate(
    when: str, slot: str, foreign: int | None, institution: int | None, day: date = MONDAY
) -> tuple[Any, ...]:
    return (
        FOREIGN_ESTIMATE,
        FOREIGN_ESTIMATE,
        {
            "received_at": at(when, day),
            "slot": slot,
            "foreign_net_qty": foreign,
            "institution_net_qty": institution,
            "total_net_qty": None,
        },
    )


def bars(data: Path, day: date, closes: dict[str, int]) -> None:
    rows = [
        {
            "symbol": SYMBOL,
            "bar_time": at(t, day),
            "venue": "UN",
            "open": c,
            "high": c,
            "low": c,
            "close": c,
            "volume": 1,
            "cum_value": c,
        }
        for t, c in closes.items()
    ]
    path = stock_bars_path(data, day, SYMBOL)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=BAR_SCHEMA), path)


def engine_at(tmp_path: Path, now: datetime) -> tuple[PredictionEngine, FakeClock]:
    clock = FakeClock(now)
    engine = PredictionEngine(
        PredictionStore(tmp_path / "e.sqlite3"),
        CALENDAR,
        (SYMBOL,),
        0.00015,
        clock,
        algorithms=(),
        kinds=(DIRECTION_KIND,),
        kind_algorithms=(),
    )
    return engine, clock


async def get(app: Any, path: str, status: int = 200) -> Any:
    async with TestClient(TestServer(app)) as client:
        response = await client.get(path)
        assert response.status == status, await response.text()
        body = await response.json()
    check("flow" if status == 200 else "error", body)
    return body


async def test_a_past_day_s_flows_a_minute_at_a_time(tmp_path: Path) -> None:
    data, state = tmp_path / "data", tmp_path / "state"
    record(
        data,
        [
            program("09:00:10", 500, FRIDAY),
            program("09:00:40", 700, FRIDAY),  # the minute's last wins
            program("09:02:05", 900, FRIDAY, rest=True),  # by the REST stand-in
            program("10:00:00", 4_000, FRIDAY),
            member("09:00:20", -30, FRIDAY),
            member("09:01:20", -50, FRIDAY, rest=True),
            member("15:00:00", 80, FRIDAY),
            estimate("09:26:00", "1", None, None, FRIDAY),  # polled before anything was entered
            estimate("09:31:00", "1", 1_200, None, FRIDAY),
            estimate("10:01:00", "1", 1_500, None, FRIDAY),
            estimate("10:01:00", "2", None, -300, FRIDAY),
            program("10:00:00", 2_000, date(2026, 10, 15)),  # the day before, for the same-time totals
        ],
    )
    bars(data, FRIDAY, {"09:00:00": 81_000, "09:01:00": 81_100, "09:02:00": 81_200, "15:30:00": 82_000})
    write_supply(state, SYMBOL, KIS, [{"date": FRIDAY, "foreign_net": 1_800, "institution_net": None}])
    engine, _ = engine_at(tmp_path, at("08:00"))

    body = await get(
        create_app(engine, data_dir=data, state_dir=state), f"/v1/symbols/{SYMBOL}/flow?day=2026-10-16"
    )

    assert (body["day"], body["recorded"]) == ("2026-10-16", True)
    assert (body["open"], body["close"]) == ("2026-10-16T09:00:00+09:00", "2026-10-16T15:30:00+09:00")
    minutes = {m["at"][11:16]: m for m in body["minutes"]}
    assert len(minutes) == 391, "09:00 to 15:30, the closing print's minute too"
    assert (minutes["09:00"]["program_net_value"], minutes["09:00"]["program_source"]) == (700, "ws")
    assert minutes["09:00"]["program_buy_value"] == 1_700
    assert minutes["09:01"]["program_net_value"] is None, "no record that minute: empty, not carried on"
    assert (minutes["09:02"]["program_net_value"], minutes["09:02"]["program_source"]) == (900, "rest")
    assert (minutes["09:00"]["member_foreign_net_qty"], minutes["09:00"]["member_source"]) == (-30, "ws")
    assert (minutes["09:01"]["member_foreign_net_qty"], minutes["09:01"]["member_source"]) == (-50, "rest")
    assert [minutes[t]["price"] for t in ("09:00", "09:02", "09:03", "15:30")] == [
        81_000,
        81_200,
        None,
        82_000,
    ]
    assert body["estimates"] == [
        {
            "slot": "1",
            "received_at": "2026-10-16T09:31:00+09:00",
            "foreign_net_qty": 1_500,
            "institution_net_qty": None,
        },
        {
            "slot": "2",
            "received_at": "2026-10-16T10:01:00+09:00",
            "foreign_net_qty": None,
            "institution_net_qty": -300,
        },
    ]
    assert body["members"]["received_at"] == "2026-10-16T15:00:00+09:00" and body["members"]["source"] == "ws"
    assert [m["name"] for m in body["members"]["sell"]] == ["sell1", "sell2", "sell3", "sell4"]
    assert [m["foreign"] for m in body["members"]["buy"]] == [False, True, False, False, False]
    assert body["totals"] == {
        "program_net_value": 4_000,
        "member_foreign_net_qty": 80,
        "estimate_foreign_net_qty": 1_500,
        "estimate_institution_net_qty": -300,
    }
    previous = body["previous_day_same_time"]
    assert (previous["day"], previous["at"]) == ("2026-10-15", "2026-10-15T15:30:00+09:00"), (
        "a past day: its close"
    )
    assert previous["program_net_value"] == 2_000 and previous["member_foreign_net_qty"] is None
    assert body["final"] == {"foreign_net": 1_800, "institution_net": None}


async def test_today_s_flows_read_the_engine_s_bars_and_the_totals_by_now(tmp_path: Path) -> None:
    data = tmp_path / "data"
    record(
        data,
        [
            program("09:01:00", 300),
            program("09:20:00", 600),
            program("09:10:00", 100, FRIDAY),
            program("09:30:00", 999, FRIDAY),  # after Monday's clock: not counted
            estimate("09:20:00", "1", 500, None, FRIDAY),
            estimate("09:40:00", "1", 900, None, FRIDAY),  # a revision after it: not seen at 09:25
        ],
    )
    engine, clock = engine_at(tmp_path, at("08:59"))
    events = [trade(at("09:00:30"), 81_000, symbol=SYMBOL), trade(at("09:01:30"), 81_300, symbol=SYMBOL)]
    await replay_into(engine, clock, events, until=at("09:25"))

    body = await get(create_app(engine, data_dir=data), f"/v1/symbols/{SYMBOL}/flow")

    assert body["day"] == "2026-10-19"
    minutes = {m["at"][11:16]: m for m in body["minutes"]}
    assert (minutes["09:00"]["price"], minutes["09:01"]["price"]) == (81_000, 81_300), (
        "today: the engine's bars"
    )
    assert body["totals"]["program_net_value"] == 600
    previous = body["previous_day_same_time"]
    assert previous["at"] == "2026-10-16T09:25:00+09:00" and previous["program_net_value"] == 100
    # Friday's slot came in at 09:20 and was revised at 09:40: at 09:25 the first figure was known.
    assert previous["estimate_foreign_net_qty"] == 500
    assert body["final"] is None and body["members"] is None and body["estimates"] == []


async def test_a_day_without_recordings_or_trading(tmp_path: Path) -> None:
    engine, _ = engine_at(tmp_path, at("12:00", date(2026, 10, 18)))  # a Sunday
    app = create_app(engine, data_dir=tmp_path / "data")

    body = await get(app, f"/v1/symbols/{SYMBOL}/flow")
    assert body["day"] == "2026-10-16", "a closed day: the last trading day's"
    assert body["recorded"] is False and body["members"] is None
    assert all(m["program_net_value"] is None and m["price"] is None for m in body["minutes"])
    await get(app, f"/v1/symbols/{SYMBOL}/flow?day=2026-10-17", 404)
    await get(app, f"/v1/symbols/{SYMBOL}/flow?day=2026-10-1", 400)
    await get(app, "/v1/symbols/000000/flow", 404)

    without = await get(create_app(engine), f"/v1/symbols/{SYMBOL}/flow")
    assert without["recorded"] is False and without["previous_day_same_time"]["program_net_value"] is None
    assert datetime.fromisoformat(without["as_of"]).astimezone(UTC) == at(
        "12:00", date(2026, 10, 18)
    ).astimezone(UTC)
