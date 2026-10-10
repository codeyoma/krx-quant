"""Macro collection: the sources over HTTP, a failing one, the alert and the regime recorded.

Input: a fake server answering as ECOS, FRED, Yahoo and Telegram do, and backfilled USD
futures bars. Output: the recorded readings and regime, and the Telegram messages.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import pyarrow.parquet as pq
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from krx_quant.archive import partition_files
from krx_quant.config import Settings
from krx_quant.feeds import KST
from krx_quant.macro import MacroCollector, keep_macro, recorder_writer
from krx_quant.market_calendar import MarketCalendar
from krx_quant.notify import Alerts
from krx_quant.recorder import ParquetRecorder

DAY = date(2026, 10, 12)  # a Monday
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
    values = {"DGS10": "4.15", "DCOILWTICO": "71.2", "VIXCLS": "18.5"}[request.query["series_id"]]
    return web.json_response(
        {"observations": [{"date": "2026-10-08", "value": "."}, {"date": "2026-10-09", "value": values}]}
    )


def yahoo(request: web.Request) -> web.Response:
    closes = {"^SOX": [12800.0, 12608.0]}.get(request.match_info["symbol"], [100.0, 101.0])  # SOX -1.5%
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
            settings, session, alerts, recorder_writer(recorder, settings), lambda: MORNING
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

    assert set(macro.failed) == {"yahoo"}
    readings = {r["indicator"]: r for r in recorded(setup.settings, "readings")}
    assert set(readings) == {"usdkrw", "ktb3y", "us10y", "wti", "vix", "usdf"}
    assert readings["usdkrw"]["as_of"] == date(2026, 10, 9)
    assert readings["usdkrw"]["change_pct"] == pytest.approx(0.2029, abs=1e-4)
    assert readings["vix"]["value"] == 18.5
    assert readings["usdf"]["value"] is None, "no USD futures bars were backfilled"
    [regime] = recorded(setup.settings, "regime")
    # Without SOX, the won's +0.2% and a VIX of 18.5 hold no rule.
    assert regime["regime"] == "neutral"
    assert json.loads(regime["failed"]) == {"yahoo": "HTTP 503"}

    setup.sources.down = set()
    await setup.collector.run(DAY, ["yahoo"])  # the retry: the readings so far are kept

    latest = recorded(setup.settings, "regime")[-1]  # rows are read in the order written
    assert latest["regime"] == "risk_off"
    assert json.loads(latest["reasons"]) == ["SOX -1.5% (-1% 이하)"]
    assert json.loads(latest["failed"]) == {}


async def test_a_source_failing_three_times_in_a_row_alerts_until_it_answers(setup: Setup) -> None:
    setup.sources.down = {"fred"}

    for _ in range(2):
        await setup.collector.run(DAY, ["fred"])
    await setup.alerts.close()
    assert setup.sources.sent == [], "two failures are not yet an alert"

    await setup.collector.run(DAY, ["fred"])
    await setup.collector.run(DAY, ["fred"])  # still down: one alert only
    await setup.alerts.close()
    assert setup.sources.sent == ["[장애] 매크로 출처 fred가 3번 연속 실패했습니다: HTTP 503"]

    setup.sources.down = set()
    await setup.collector.run(DAY, ["fred"])
    await setup.alerts.close()
    assert setup.sources.sent[-1] == "[복구] 매크로 출처 fred가 다시 응답합니다."


async def test_a_source_without_its_key_is_skipped(setup: Setup) -> None:
    collector = MacroCollector(
        replace(setup.settings, ecos_api_key=None),
        aiohttp.ClientSession(),
        setup.alerts,
        recorder_writer(setup.recorder, setup.settings),
        lambda: MORNING,
    )

    macro = await collector.run(DAY)
    await collector._session.close()

    assert macro.skipped == ["ecos"]
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
        await keep_macro(setup.collector, MarketCalendar({DAY: True}), time(7, 30), lambda: clock[0], sleep)

    assert waits[:3] == [300.0, 600.0, 600.0]
    assert setup.sources.calls["yahoo"] == 3 * 4, "four symbols, three tries"
    assert setup.sources.calls["ecos"] == 2, "the sources that answered are not asked again"


class _Stop(Exception):
    pass
