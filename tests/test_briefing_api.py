"""``GET /v1/briefing`` (#304): the pre-open briefing of a day, read from the files the engine
keeps, with the 08:50 ``open_to_close`` predictions and Cox's inputs; all within the /v1 schema.

The engine replays a Monday morning up to 08:51 with Cox fitted on made-up years (``daily_doubles``);
the night futures, the pre-market's trades, the book's expected prices, the Friday evening's bars
and the economic calendar are written as the collector records them.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from aiohttp.test_utils import TestClient, TestServer

from krx_quant.algorithms.cox import FEATURES, Cox
from krx_quant.api import create_app
from krx_quant.backfill import BAR_SCHEMA, stock_bars_path
from krx_quant.candles import read_krx_days
from krx_quant.clock import FakeClock
from krx_quant.daily import StoredDays, history_before
from krx_quant.daily_features import DAILY_FEATURES, DailyFiles, LiveFeatures, samples
from krx_quant.engine import Event, PredictionEngine
from krx_quant.feeds import BOOK, NIGHT_FUTURES, TRADE, Feed
from krx_quant.feeds.base import KST
from krx_quant.kinds import DIRECTION_KIND
from krx_quant.market_calendar import MarketCalendar
from krx_quant.open_to_close import OPEN_TO_CLOSE
from krx_quant.predictions import PredictionStore
from krx_quant.recorder import ParquetRecorder
from krx_quant.replay import replay_into
from tests.api_schema import check
from tests.daily_doubles import SYMBOLS, made_history
from tests.replay_events import trade as made_trade

FEE = 0.015 / 100
FRIDAY, MONDAY = date(2026, 10, 16), date(2026, 10, 19)
CALENDAR = MarketCalendar()


def at(hhmmss: str, day: date = MONDAY) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmmss), KST)


def row_of(feed: Feed, **values: Any) -> dict[str, Any]:
    return dict.fromkeys(feed.schema.names) | values


def trade(symbol: str, when: datetime, price: int, received: datetime | None = None) -> Event:
    """An integrated trade at ``when``, received then (or at ``received``), as normalized."""
    return made_trade(received or when, price, 10, traded=when, symbol=symbol)


def record(data: Path, events: list[Event]) -> None:
    feeds = {f.tr_id: f for f in (TRADE, BOOK, NIGHT_FUTURES)}
    recorder = ParquetRecorder(data, flush_rows=1000, flush_seconds=3600)
    for tr_id, row in events:
        day = row["received_at"].astimezone(KST).date()
        recorder.record("events", day, tr_id, row["symbol"], row, feeds[tr_id].schema)
    recorder.close()


def friday_close(data: Path, symbol: str) -> int:
    return int(next(r["close"] for r in read_krx_days(data, symbol) if r["date"] == FRIDAY))


async def briefed(tmp_path: Path, now: str = "08:51") -> tuple[dict[str, Any], PredictionEngine]:
    data, state = tmp_path / "data", tmp_path / "state"
    made_history(data, state, FRIDAY, 400, signal=0.5)
    model = Cox().fit(samples(OPEN_TO_CLOSE, DailyFiles(data, state), SYMBOLS, FEATURES, FEE))
    close = friday_close(data, "005930")
    morning = [
        trade("005930", at("08:05:00"), close + 100),  # the earlier window
        trade("005930", at("08:35:00"), close),  # KRX's pre-open trade at the last close: no window
        trade("005930", at("08:45:00"), close + 300),  # the later window: this one
        trade("005930", at("08:49:00"), close + 900, received=at("08:51:00")),  # received too late
    ]
    night = [
        (
            NIGHT_FUTURES.tr_id,
            row_of(
                NIGHT_FUTURES,
                symbol="A01612",
                event_time=t,
                received_at=t,
                price=p,
                change_pct=c,
                cum_volume=v,
            ),
        )
        for t, p, c, v in (
            (at("17:59:00", FRIDAY), 390.0, 0.0, 1),  # the day session's: not the night's
            (at("18:00:30", FRIDAY), 391.0, 0.26, 100),
            (at("23:10:00", FRIDAY), 395.5, 1.41, 900),
            (at("05:59:00", date(2026, 10, 17)), 393.0, 0.77, 1500),
        )
    ]
    # The venue code '1' is KRX's auction; '2' came outside KRX's auctions and, for one symbol, in
    # the opening auction's window too: NXT's, not the price the open settles at (#314).
    book = [
        (
            BOOK.tr_id,
            row_of(
                BOOK,
                symbol=symbol,
                event_time=t,
                received_at=t,
                expected_px=p,
                expected_qty=q,
                expected_exch_cls=venue,
            ),
        )
        for symbol, t, p, q, venue in (
            ("005930", at("08:29:00"), close - 500, 10, "1"),  # before the opening auction
            ("005930", at("08:31:00"), close + 200, 2_000, "1"),
            ("005930", at("08:59:50"), close + 400, 5_000, "1"),  # KRX's last before the open
            ("005930", at("08:59:55"), close + 900, 7_000, "2"),  # later, but not KRX's
            ("005930", at("09:00:05"), close + 700, 9_000, "1"),  # after the open
            ("005930", at("09:30:00"), 0, 0, "1"),  # continuous trading: no auction on
            ("000660", at("08:44:39"), close, 300, "2"),  # only NXT's in the window: none
            ("000660", at("08:55:00"), close, 300, "2"),
        )
    ]
    record(data, [*morning, *night, *book])
    bars = [
        {
            "symbol": "005930",
            "bar_time": at(t, FRIDAY),
            "venue": "UN",
            "open": p,
            "high": p,
            "low": p,
            "close": p,
            "volume": 1,
            "cum_value": p,
        }
        for t, p in (
            ("15:19:00", close - 100),
            ("15:40:00", close + 50),
            ("19:59:00", close + 600),
            ("20:00:00", close + 999),
        )
    ]
    path = stock_bars_path(data, FRIDAY, "005930")
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(bars, schema=BAR_SCHEMA), path)
    events = [
        {"id": ident, "at": t.isoformat(), "country": "US", "title": ident, "importance": "high"}
        for ident, t in (
            ("sunday", at("21:30:00", date(2026, 10, 18))),
            ("monday", at("21:30:00")),
            ("tuesday", at("08:00:00", date(2026, 10, 20))),
        )
    ]
    (data / "calendar").mkdir(parents=True)
    body = {"fetched_at": at("07:00:00").isoformat(), "countries": ["US"], "events": events}
    (data / "calendar" / "latest.json").write_text(json.dumps(body))

    clock = FakeClock(at("07:20"))
    engine = PredictionEngine(
        PredictionStore(tmp_path / "e.sqlite3"),
        CALENDAR,
        SYMBOLS,
        FEE,
        clock,
        algorithms=(),
        kinds=(DIRECTION_KIND,),
        kind_algorithms=(),
        models={"cox": model},
        daily=StoredDays(data),
        daily_algorithms=(Cox(),),
        daily_features=LiveFeatures(OPEN_TO_CLOSE, data, state, CALENDAR, SYMBOLS, FEE),
        daily_feature_names=DAILY_FEATURES,
    )
    await replay_into(engine, clock, sorted(morning, key=lambda e: e[1]["received_at"]), until=at(now))
    app = create_app(engine, data_dir=data, state_dir=state)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/v1/briefing")
        assert response.status == 200, await response.text()
        answer = await response.json()
        check("briefing", answer)
        bad = await client.get("/v1/briefing?day=2026-10-1")
        assert bad.status == 400
    return answer, engine


async def test_the_briefing_reads_the_night_the_pre_market_and_the_book(tmp_path: Path) -> None:
    body, _ = await briefed(tmp_path)
    data = tmp_path / "data"
    close = friday_close(data, "005930")

    assert (body["day"], body["previous_day"], body["trading_day"]) == ("2026-10-19", "2026-10-16", True)
    assert body["session"] == {
        "open": "2026-10-19T09:00:00+09:00",
        "close": "2026-10-19T15:30:00+09:00",
        "special": None,
    }
    night = body["night_futures"]
    assert night["recorded"] and night["contract"] == "A01612"
    assert (night["last"], night["change_pct"], night["high"], night["low"]) == (393.0, 0.77, 395.5, 391.0)
    assert night["volume"] == 1500 and night["last_trade_at"] == "2026-10-17T05:59:00+09:00"
    assert [m["at"] for m in night["minutes"]] == [
        "2026-10-16T18:00:00+09:00",
        "2026-10-16T23:10:00+09:00",
        "2026-10-17T05:59:00+09:00",
    ]

    samsung = body["symbols"][0]
    assert samsung["symbol"] == "005930" and samsung["previous_close"] == close
    assert samsung["after_hours"]["at"] == "2026-10-16T19:59:00+09:00", "the evening's last bar before 20:00"
    assert samsung["after_hours"]["price"] == close + 600
    assert samsung["premarket"]["window"] == "08:40-08:50" and samsung["premarket"]["price"] == close + 300
    assert math.isclose(samsung["premarket"]["change_pct"], 300 / close * 100)
    expected = samsung["expected_open"]
    assert (expected["price"], expected["qty"], expected["venue_code"]) == (close + 400, 5_000, "1")
    assert expected["at"] == "2026-10-19T08:59:50+09:00" and expected["final"] is False
    others = body["symbols"][1]
    assert others["expected_open"] is None, "NXT's expected prices are not KRX's expected open"
    assert others["premarket"] is None and others["after_hours"] is None

    supply = samsung["supply"]
    assert supply["day"] == "2026-10-16" and supply["days"] == 5
    assert supply["foreign_net"] is not None and supply["foreign_net_sum"] is not None

    assert [e["id"] for e in body["schedule"]["events"]] == ["monday"], "the day's events only"
    assert body["schedule"]["option_expiry"] is False and body["schedule"]["month_end"] is False
    assert [u["indicator"] for u in body["overnight"]["us"]] == ["sox", "nvda", "mu", "vix"]
    assert len(body["overnight"]["us"][0]["closes"]) == 5
    assert all(c["day"] < "2026-10-19" for c in body["overnight"]["us"][0]["closes"])
    assert body["overnight"]["macro"]["day"] is None, "no macro read recorded: nulls"


async def test_the_briefing_shows_cox_s_inputs_as_it_predicted_from_them(tmp_path: Path) -> None:
    body, engine = await briefed(tmp_path)
    data = tmp_path / "data"
    otc = body["open_to_close"]
    assert (otc["day"], otc["predicted_at"], otc["previous"]) == (
        "2026-10-19",
        "2026-10-19T08:50:00+09:00",
        False,
    )
    samsung = otc["symbols"][0]
    assert {p["algorithm"] for p in samsung["predictions"]} == {
        "cox",
        "otc-climate",
        "otc-flat",
        "otc-persist",
    }
    [inputs] = samsung["inputs"]  # Cox's: the baselines have none
    assert inputs["algorithm"] == "cox" and [i["name"] for i in inputs["inputs"]] == list(FEATURES)
    by_name = {i["name"]: i for i in inputs["inputs"]}

    # The pre-market price the briefing shows is the one Cox read (in sigmas of the day's band).
    history = history_before(StoredDays(data).days("005930"), MONDAY)
    band = OPEN_TO_CLOSE.band(history)  # type: ignore[misc]
    assert band is not None
    gap = (
        math.log(body["symbols"][0]["premarket"]["price"] / body["symbols"][0]["previous_close"]) / band.sigma
    )
    assert math.isclose(by_name["premarket_gap"]["value"], gap)

    # The parts add up to the forecast: softmax of (bias + the inputs' scores) against flat.
    [cox] = [p for p in samsung["predictions"] if p["algorithm"] == "cox"]
    up = inputs["bias"]["up"] + sum(i["scores"]["up"] for i in inputs["inputs"])
    down = inputs["bias"]["down"] + sum(i["scores"]["down"] for i in inputs["inputs"])
    total = math.exp(up) + 1 + math.exp(down)
    assert math.isclose(cox["output"]["p_up"], math.exp(up) / total)
    assert math.isclose(cox["output"]["p_down"], math.exp(down) / total)
    assert by_name["night_futures"]["value"] is not None, "Friday's night, from the recording"
    assert by_name["weekday"]["z"] is None, "the weekday's indicators have no single standardized value"
    rows = engine.store.kind_at_close("open_to_close", OPEN_TO_CLOSE.answer_version, "005930", at("08:50"))
    assert {r["algorithm"]: r["inputs"] is not None for r in rows} == {
        "cox": True,
        "otc-climate": False,
        "otc-flat": False,
        "otc-persist": False,
    }


async def test_before_the_day_s_predictions_the_briefing_shows_the_last_day_s(tmp_path: Path) -> None:
    data, state = tmp_path / "data", tmp_path / "state"
    made_history(data, state, FRIDAY, 400)
    clock = FakeClock(at("07:20", FRIDAY))
    engine = PredictionEngine(
        PredictionStore(tmp_path / "e.sqlite3"),
        CALENDAR,
        SYMBOLS,
        FEE,
        clock,
        algorithms=(),
        kinds=(DIRECTION_KIND,),
        kind_algorithms=(),
        daily=StoredDays(data),
    )
    await replay_into(engine, clock, [], until=at("08:51", FRIDAY))
    clock.set(at("08:00"))  # Monday, before its predictions
    async with TestClient(TestServer(create_app(engine, data_dir=data, state_dir=state))) as client:
        body = await (await client.get("/v1/briefing")).json()
        check("briefing", body)
        otc = body["open_to_close"]
        assert (otc["day"], otc["previous"]) == ("2026-10-16", True)
        assert all(s["inputs"] == [] for s in otc["symbols"]), "the baselines have no inputs"
        assert body["night_futures"]["recorded"] is False and body["night_futures"]["minutes"] == []
        assert [s["premarket"] for s in body["symbols"]] == [None, None, None]
        saturday = await (await client.get("/v1/briefing?day=2026-10-17")).json()
        check("briefing", saturday)
        assert saturday["trading_day"] is False and saturday["session"] is None


async def test_without_data_the_briefing_is_nulls(tmp_path: Path) -> None:
    clock = FakeClock(at("07:20"))
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
    async with TestClient(TestServer(create_app(engine))) as client:
        body = await (await client.get("/v1/briefing")).json()
    check("briefing", body)
    assert body["open_to_close"] is None, "no daily kind"
    assert body["symbols"][0] == {
        "symbol": "005930",
        "previous_close": None,
        "after_hours": None,
        "premarket": None,
        "expected_open": None,
        "supply": {
            "day": "2026-10-16",
            "foreign_net": None,
            "institution_net": None,
            "days": 0,
            "foreign_net_sum": None,
            "institution_net_sum": None,
        },
    }
    assert body["schedule"]["events"] == []
    assert datetime.fromisoformat(body["as_of"]).astimezone(UTC) == at("07:20").astimezone(UTC)
