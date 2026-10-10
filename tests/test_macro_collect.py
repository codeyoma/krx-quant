"""Macro collection: the sources over HTTP, a failing one, the alert and the regime recorded.

Input: a fake server answering as ECOS, FRED, Yahoo and Telegram do, and backfilled USD
futures bars. Output: the recorded readings and regime, the Telegram messages, and what
the Engine API shows of them (``/v1/macro``).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from krx_quant.api import create_app
from krx_quant.archive import partition_files
from krx_quant.backfill import FUTURES_BAR_SCHEMA, FUTURES_BARS_TR, bars_key
from krx_quant.config import Settings
from krx_quant.feeds import KST
from krx_quant.macro import MacroCollector, Source, day_regime, first_macro_run, keep_macro, macro_recorder
from krx_quant.market_calendar import MarketCalendar
from krx_quant.notify import Alerts
from krx_quant.recorder import ParquetRecorder
from tests.replay_events import at, get_json, run

DAY = date(2026, 10, 12)  # a Monday; the KRX day before is Friday the 9th
CALENDAR = MarketCalendar({DAY: True})
MORNING = datetime.combine(DAY, time(7, 30), KST)
NEW_YORK_FRIDAY_CLOSE = 1791576000  # 2026-10-09 16:00 New York


@dataclass
class Sources:
    """What the fake server answers, and what it was told."""

    down: set[str] = field(default_factory=set)
    sent: list[str] = field(default_factory=list)
    calls: dict[str, int] = field(default_factory=dict)


def ecos(request: web.Request) -> web.Response:
    item = request.match_info["item"]
    values = {"0000001": ("1380.0", "1382.8"), "010200000": ("2.61", "2.63")}[item]  # won +0.2%
    rows = [{"TIME": t, "DATA_VALUE": v} for t, v in zip(("20261008", "20261009"), values, strict=True)]
    return web.json_response({"StatisticSearch": {"list_total_count": 2, "row": rows}})


def fred(request: web.Request) -> web.Response:
    values = {"DGS10": "4.15", "DCOILWTICO": "71.2"}[request.query["series_id"]]
    return web.json_response(
        {"observations": [{"date": "2026-10-08", "value": "."}, {"date": "2026-10-09", "value": values}]}
    )


def yahoo(request: web.Request) -> web.Response:
    known = {"^SOX": [12800.0, 12608.0], "^VIX": [17.0, 18.5]}  # SOX -1.5%
    closes = known.get(request.match_info["symbol"], [100.0, 101.0])
    result = {
        "meta": {
            "exchangeTimezoneName": "America/New_York",
            "currentTradingPeriod": {
                "regular": {"start": NEW_YORK_FRIDAY_CLOSE - 23_400, "end": NEW_YORK_FRIDAY_CLOSE}
            },
        },
        "timestamp": [1791466200, 1791552600],  # 2026-10-08 and 2026-10-09, 09:30 New York
        "indicators": {"quote": [{"close": closes}]},
    }
    return web.json_response({"chart": {"result": [result], "error": None}})


def fake_server(sources: Sources) -> web.Application:
    def guarded(name: str, handler: Callable[[web.Request], web.Response]) -> Any:
        async def handle(request: web.Request) -> web.Response:
            sources.calls[name] = sources.calls.get(name, 0) + 1
            if name in sources.down:
                return web.Response(status=503, text="<html>down</html>")
            return handler(request)

        return handle

    async def telegram(request: web.Request) -> web.Response:
        sources.sent.append((await request.json())["text"])
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_get(
        "/ecos/StatisticSearch/{key}/json/kr/1/100/{stat}/D/{start}/{end}/{item}", guarded("ecos", ecos)
    )
    app.router.add_get("/fred/fred/series/observations", guarded("fred", fred))
    app.router.add_get("/yahoo/v8/finance/chart/{symbol}", guarded("yahoo", yahoo))
    app.router.add_post("/telegram/bottoken/sendMessage", telegram)
    return app


@dataclass
class Setup:
    sources: Sources
    settings: Settings
    collector: MacroCollector
    alerts: Alerts
    recorder: ParquetRecorder


@pytest.fixture
async def setup(tmp_path: Path) -> AsyncIterator[Setup]:
    sources = Sources()
    server = TestServer(fake_server(sources))
    await server.start_server()
    base = str(server.make_url("")).rstrip("/")
    settings = replace(
        Settings(kis_app_key="", kis_app_secret=""),
        data_dir=tmp_path / "data",
        ecos_api_key="test-key",
        fred_api_key="test-key",
        ecos_url=f"{base}/ecos",
        fred_url=f"{base}/fred",
        yahoo_url=f"{base}/yahoo",
    )
    recorder = ParquetRecorder(settings.data_dir, flush_rows=1000, flush_seconds=3600)
    async with aiohttp.ClientSession() as session:
        alerts = Alerts(session, "token", "chat", f"{base}/telegram")
        collector = MacroCollector(
            settings, session, alerts, macro_recorder(recorder), CALENDAR, lambda: MORNING
        )
        yield Setup(sources, settings, collector, alerts, recorder)
        await alerts.close()
    recorder.close()
    await server.close()


def recorded(settings: Settings, feed: str) -> list[dict[str, Any]]:
    root = settings.data_dir / "macro" / f"date={DAY}" / f"feed={feed}"
    rows: list[dict[str, Any]] = []
    for partition in sorted(root.glob("symbol=*")):
        for path in partition_files(partition):
            rows += pq.read_table(path).to_pylist()
    return rows


async def test_a_failing_source_leaves_the_others_recorded_and_the_regime_decided(setup: Setup) -> None:
    setup.sources.down = {"yahoo"}

    macro = await setup.collector.run(DAY)

    assert set(macro.failed) == {Source.YAHOO}
    readings = {r["indicator"]: r for r in recorded(setup.settings, "readings")}
    assert set(readings) == {"usdkrw", "ktb3y", "us10y", "wti", "usdf"}
    assert readings["usdkrw"]["as_of"] == date(2026, 10, 9)
    assert readings["usdkrw"]["change_pct"] == pytest.approx(0.2029, abs=1e-4)
    assert readings["us10y"]["value"] == 4.15
    assert readings["usdf"]["value"] is None, "no USD futures bars were backfilled"
    [regime] = recorded(setup.settings, "regime")
    # Without SOX and the VIX, the won's +0.2% holds no rule.
    assert regime["regime"] == "neutral"
    assert json.loads(regime["failed"]) == {"yahoo": "HTTP 503"}

    setup.sources.down = set()
    await setup.collector.run(DAY, [Source.YAHOO])  # the retry: the readings so far are kept

    latest = recorded(setup.settings, "regime")[-1]  # rows are read in the order written
    assert latest["regime"] == "risk_off"
    assert json.loads(latest["reasons"]) == ["SOX -1.5% (-1% 이하)"]
    assert json.loads(latest["failed"]) == {}


async def test_a_source_failing_three_times_in_a_row_alerts_until_it_answers(setup: Setup) -> None:
    setup.sources.down = {"fred"}

    for _ in range(2):
        await setup.collector.run(DAY, [Source.FRED])
    await setup.alerts.close()
    assert setup.sources.sent == [], "two failures are not yet an alert"

    await setup.collector.run(DAY, [Source.FRED])
    await setup.collector.run(DAY, [Source.FRED])  # still down: one alert only
    await setup.alerts.close()
    assert setup.sources.sent == ["[장애] 매크로 출처 fred가 3번 연속 실패했습니다: HTTP 503"]

    setup.sources.down = set()
    await setup.collector.run(DAY, [Source.FRED])
    await setup.alerts.close()
    assert setup.sources.sent[-1] == "[복구] 매크로 출처 fred가 다시 응답합니다."


async def test_a_source_without_its_key_is_skipped(setup: Setup) -> None:
    collector = MacroCollector(
        replace(setup.settings, ecos_api_key=None),
        aiohttp.ClientSession(),
        setup.alerts,
        macro_recorder(setup.recorder),
        CALENDAR,
        lambda: MORNING,
    )

    macro = await collector.run(DAY)
    await collector._session.close()

    assert macro.skipped == [Source.ECOS]
    assert "ecos" not in setup.sources.calls
    assert "usdkrw" not in macro.readings


async def test_the_morning_tries_a_failing_source_three_times_ten_minutes_apart(setup: Setup) -> None:
    setup.sources.down = {"yahoo"}
    clock = [MORNING - timedelta(minutes=5)]
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        clock[0] += timedelta(seconds=seconds)
        if len(waits) > 3:  # the morning's tries are done: stop at the wait for tomorrow
            raise _Stop

    with pytest.raises(_Stop):
        await keep_macro(setup.collector, CALENDAR, time(7, 30), lambda: clock[0], sleep)

    assert waits[:3] == [300.0, 600.0, 600.0]
    assert setup.sources.calls["yahoo"] == 3 * 5, "five symbols, three tries"
    assert setup.sources.calls["ecos"] == 2, "the sources that answered are not asked again"


async def test_retries_stop_at_the_open(setup: Setup) -> None:
    # Restarted at 08:45 with Yahoo down: 08:45 and 08:55, but not 09:05 in the session.
    setup.sources.down = {"yahoo"}
    clock = [datetime.combine(DAY, time(8, 45), KST)]
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        clock[0] += timedelta(seconds=seconds)
        if len(waits) > 2:
            raise _Stop

    with pytest.raises(_Stop):
        await keep_macro(setup.collector, CALENDAR, time(7, 30), lambda: clock[0], sleep)

    assert waits[:2] == [0.0, 600.0]
    assert clock[0] == datetime.combine(date(2026, 10, 13), time(7, 30), KST), "then tomorrow"
    assert setup.sources.calls["yahoo"] == 2 * 5, "five symbols, two tries"


async def test_a_time_at_or_after_the_open_reads_nothing(setup: Setup) -> None:
    clock = [datetime.combine(DAY, time(7, 0), KST)]

    async def sleep(seconds: float) -> None:
        clock[0] += timedelta(seconds=seconds)
        if clock[0].date() > DAY:
            raise _Stop

    with pytest.raises(_Stop):
        await keep_macro(setup.collector, CALENDAR, time(9, 30), lambda: clock[0], sleep)

    assert setup.sources.calls == {}
    assert recorded(setup.settings, "regime") == []


class _Stop(Exception):
    pass


def backfill_usd_futures(settings: Settings, day: date, close: float) -> None:
    path = settings.data_dir / bars_key(day, FUTURES_BARS_TR, "USDF")
    path.parent.mkdir(parents=True, exist_ok=True)
    start = datetime.combine(day, time(15, 44), KST)
    row = {
        "symbol": "USDF",
        "bar_time": start,
        "contract": "A75610",
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 10,
        "cum_value": 0,
    }
    pq.write_table(pa.Table.from_pylist([row], schema=FUTURES_BAR_SCHEMA), path)


async def test_the_won_is_read_from_the_usd_futures_first(setup: Setup) -> None:
    # The futures closed on Thursday and Friday: a weaker won by 0.6%, a day fresher than ECOS.
    backfill_usd_futures(setup.settings, date(2026, 10, 8), 1380.0)
    backfill_usd_futures(setup.settings, date(2026, 10, 9), 1388.3)

    macro = await setup.collector.run(DAY)

    assert macro.readings["usdf"].change_pct == pytest.approx(0.6014, abs=1e-4)
    assert macro.decision is not None
    assert macro.decision.regime.value == "risk_off"
    assert "원/달러 +0.6% (+0.5% 이상, 원화 약세)" in macro.decision.reasons


@pytest.mark.parametrize(
    ("started", "first"),
    [
        (datetime.combine(DAY, time(7, 0), KST), MORNING),  # before 07:30: wait for it
        (
            datetime.combine(DAY, time(8, 10), KST),
            datetime.combine(DAY, time(8, 10), KST),
        ),  # restarted before the open
        (datetime.combine(DAY, time(11, 0), KST), datetime.combine(date(2026, 10, 13), time(7, 30), KST)),
    ],
)
def test_a_restart_reads_again_only_before_the_open(started: datetime, first: datetime) -> None:
    assert first_macro_run(CALENDAR, time(7, 30), started) == first


async def test_the_engine_api_shows_the_latest_regime_and_readings(setup: Setup, tmp_path: Path) -> None:
    setup.sources.down = {"yahoo"}
    await setup.collector.run(DAY)
    setup.sources.down = set()
    await setup.collector.run(DAY, [Source.YAHOO])
    engine = await run(tmp_path / "engine.db", [], until=at("09:00:00"))

    async with TestClient(TestServer(create_app(engine, data_dir=setup.settings.data_dir))) as client:
        macro = await get_json(client, "/v1/macro")

    assert (macro["day"], macro["regime"]) == ("2026-10-12", "risk_off")
    assert macro["reasons"] == ["SOX -1.5% (-1% 이하)"]
    assert macro["decided_at"] == "2026-10-12T07:30:00+09:00"
    assert (macro["failed"], macro["skipped"]) == ({}, [])
    readings = {r["indicator"]: r for r in macro["readings"]}
    assert list(readings)[:2] == ["usdkrw", "ktb3y"], "in the indicators' order"
    assert readings["sox"] == {
        "indicator": "sox",
        "label": "필라델피아 반도체지수",
        "source": "yahoo",
        "series": "^SOX",
        "as_of": "2026-10-09",
        "value": 12608.0,
        "change_pct": pytest.approx(-1.5),
    }


async def test_the_readings_shown_are_those_the_regime_shown_was_decided_from(
    setup: Setup, tmp_path: Path
) -> None:
    await setup.collector.run(DAY)
    # The collector restarted before the open, and Yahoo is down this time.
    setup.sources.down = {"yahoo"}
    restarted = MacroCollector(
        setup.settings,
        setup.collector._session,
        setup.alerts,
        macro_recorder(setup.recorder),
        CALENDAR,
        lambda: MORNING + timedelta(minutes=40),
    )
    await restarted.run(DAY)
    engine = await run(tmp_path / "engine.db", [], until=at("09:00:00"))

    async with TestClient(TestServer(create_app(engine, data_dir=setup.settings.data_dir))) as client:
        macro = await get_json(client, "/v1/macro")

    assert (macro["regime"], macro["failed"]) == ("neutral", {"yahoo": "HTTP 503"})
    assert "sox" not in {r["indicator"] for r in macro["readings"]}, "not from the read before the restart"


async def test_the_engine_api_shows_the_latest_day_decided_or_nothing(setup: Setup, tmp_path: Path) -> None:
    await setup.collector.run(DAY)
    tomorrow = await run(
        tmp_path / "engine.db", [], until=at("09:00:00", date(2026, 10, 13)), calendar=MarketCalendar({})
    )
    nothing_yet = await run(tmp_path / "other.db", [], until=at("09:00:00"))

    async with TestClient(TestServer(create_app(tomorrow, data_dir=setup.settings.data_dir))) as client:
        before_its_read = await get_json(client, "/v1/macro")
    async with TestClient(TestServer(create_app(nothing_yet, data_dir=tmp_path / "empty"))) as client:
        empty = await get_json(client, "/v1/macro")

    assert before_its_read["day"] == "2026-10-12", "until the day's own read, the day before's"
    assert empty == {
        "day": None,
        "decided_at": None,
        "regime": None,
        "reasons": [],
        "readings": [],
        "failed": {},
        "skipped": [],
    }


async def test_the_day_s_regime_is_the_one_decided_before_the_open(setup: Setup) -> None:
    await setup.collector.run(DAY)  # 07:30: risk_off
    setup.sources.down = {"yahoo"}
    late = MacroCollector(
        setup.settings,
        setup.collector._session,
        setup.alerts,
        macro_recorder(setup.recorder),
        CALENDAR,
        lambda: datetime.combine(DAY, time(10, 0), KST),
    )
    await late.run(DAY)  # `krx-quant macro` at 10:00, Yahoo down: neutral

    opened = datetime.combine(DAY, time(9, 0), KST)
    assert day_regime(setup.settings.data_dir, DAY, opened) == "risk_off"
    assert day_regime(setup.settings.data_dir, date(2026, 10, 13), opened) is None
