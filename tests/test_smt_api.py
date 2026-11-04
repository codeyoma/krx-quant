"""``GET /v1/symbols/{symbol}/smt`` (#521): the SMT events of the pairs holding a predicted symbol,
each day's from the engine's live ones where it has them, else the day made again (its backfilled
bars first, else its recording), kept by day; within the contract.

Input: an engine at 10:40 on Tuesday 2026-10-13 over 005930, 000660 and 009150 (in no pair), and
the days before it: the 5th and the 7th with backfilled bars, the 6th with nothing, the 8th with
backfilled bars and a live event, the 12th recorded. Output: the events and where each day's
came from.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from krx_quant import api
from krx_quant.api import create_app
from krx_quant.engine import PredictionEngine
from krx_quant.market_calendar import MarketCalendar
from krx_quant.smt import BEARISH, BULLISH, LIVE, Reconstruction, SmtEvent, reconstruct
from tests.api_schema import check
from tests.replay_events import get_json, run
from tests.test_smt import BARE, DAY, NEXT, A, B, F, at, day_bars, minute_trades, record, store_backfill

C = "009150"  # predicted, in no pair
OLDER, EMPTY, BACKFILLED_DAY, LIVE_DAY = (
    date(2026, 10, 5),
    date(2026, 10, 6),
    date(2026, 10, 7),
    date(2026, 10, 8),
)
RECORDED_DAY, TODAY = DAY, NEXT  # Monday the 12th (the 9th is Hangul Day) and Tuesday the 13th
WEEK = MarketCalendar(
    {d: True for d in (OLDER, EMPTY, BACKFILLED_DAY, LIVE_DAY, DAY, NEXT)} | {date(2026, 10, 9): False}
)
# Kept by the engine: on the 8th 000660 past its high alone (005930's side of the pair), today the
# futures past their low alone.
LIVE_EVENT = SmtEvent(
    at("11:01", LIVE_DAY),
    A,
    B,
    BEARISH,
    15,
    B,
    LIVE,
    a_extreme=at("10:52", LIVE_DAY),
    b_extreme=at("10:59", LIVE_DAY),
)
TODAY_EVENT = SmtEvent(
    at("09:31", TODAY), A, F, BULLISH, 30, F, LIVE, a_extreme=at("09:12", TODAY), b_extreme=at("09:30", TODAY)
)


@dataclass
class Week:
    engine: PredictionEngine
    data: Path
    client: TestClient[Any, Any]
    made: list[date]  # each day made again, in the order asked


@pytest_asyncio.fixture
async def week(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Week]:
    data = tmp_path / "data"
    store_backfill(data, OLDER, day_bars({(B, "13:00"): (1011, 990)}, day=OLDER))
    store_backfill(data, BACKFILLED_DAY, day_bars({(A, "10:00"): (1011, 990)}, day=BACKFILLED_DAY))
    store_backfill(data, LIVE_DAY, day_bars({(B, "11:00"): (1011, 990)}, day=LIVE_DAY))
    record(data, minute_trades({(B, "10:10"): 999.0}))  # the 12th's trades: 000660 below its low
    engine = await run(
        tmp_path / "engine.sqlite3", [], at("10:40", TODAY), calendar=WEEK, symbols=(A, B, C), **BARE
    )
    engine.store.add_smt_events([LIVE_EVENT, TODAY_EVENT], at("10:40", TODAY))
    made: list[date] = []

    def counted(data_dir: Path, day: date, calendar: MarketCalendar, first: str) -> Reconstruction | None:
        made.append(day)
        return reconstruct(data_dir, day, calendar, first)

    monkeypatch.setattr(api, "reconstruct", counted)
    async with TestClient(TestServer(create_app(engine, data_dir=data))) as client:
        yield Week(engine, data, client, made)
    engine.store.close()


async def smt(client: TestClient[Any, Any], symbol: str, query: str = "") -> Any:
    body = await get_json(client, f"/v1/symbols/{symbol}/smt{query}")
    check("smt", body)
    return body


Seen = tuple[str, str, str, str, int, str, str]  # bar close, a, b, direction, L, swept, source


def seen(body: Any) -> list[Seen]:
    return [
        (e["bar_close"], e["a"], e["b"], e["direction"], e["minutes"], e["swept"], e["source"])
        for e in body["events"]
    ]


def made_again(day: date, hhmm: str, a: str, b: str, direction: str, swept: str) -> list[Seen]:
    """A day's event made again for both L, as served."""
    close = at(hhmm, day).isoformat()
    return [(close, a, b, direction, minutes, swept, "reconstructed") for minutes in (15, 30)]


def live(event: SmtEvent) -> Seen:
    return (event.bar_close.isoformat(), event.a, event.b, event.direction, event.minutes, event.swept, LIVE)


async def test_each_day_is_its_live_events_where_the_engine_has_them_else_the_day_made_again(
    week: Week,
) -> None:
    body = await smt(week.client, A, f"?from={OLDER}&to={TODAY}")

    assert (body["symbol"], body["from"], body["to"]) == (A, "2026-10-05", "2026-10-13")
    assert body["pairs"] == [{"a": A, "b": B}, {"a": A, "b": F}]
    assert body["days"] == [
        {"date": "2026-10-05", "source": "reconstructed", "bars": "backfilled"},
        {"date": "2026-10-06", "source": None, "bars": None},  # nothing to make it again from
        {"date": "2026-10-07", "source": "reconstructed", "bars": "backfilled"},
        # The 8th's bars would give 000660 and the futures too: a day with live events is those alone.
        {"date": "2026-10-08", "source": "live", "bars": None},
        {"date": "2026-10-12", "source": "reconstructed", "bars": "recorded"},  # no backfilled bars
        {"date": "2026-10-13", "source": "live", "bars": None},
    ]
    assert seen(body) == [
        *made_again(OLDER, "13:01", A, B, BEARISH, B),
        *made_again(BACKFILLED_DAY, "10:01", A, B, BEARISH, A),
        *made_again(BACKFILLED_DAY, "10:01", A, F, BEARISH, A),
        live(LIVE_EVENT),
        *made_again(RECORDED_DAY, "10:11", A, B, BULLISH, B),
        live(TODAY_EVENT),
    ]
    events = {(e["bar_close"], e["minutes"], e["source"]): e for e in body["events"]}
    # 000660 below its look-back's lowest (every minute at 1000, the latest kept), 005930 at it.
    assert events[("2026-10-12T10:11:00+09:00", 15, "reconstructed")] == {
        "bar_close": "2026-10-12T10:11:00+09:00",
        "bar_start": "2026-10-12T10:10:00+09:00",
        "a": A,
        "b": B,
        "direction": "bullish",
        "minutes": 15,
        "swept": B,
        "other": A,
        "swept_extreme_at": "2026-10-12T10:09:00+09:00",
        "other_extreme_at": "2026-10-12T10:09:00+09:00",
        "definition": "rolling_extreme",
        "version": 1,
        "source": "reconstructed",
    }
    kept = events[("2026-10-08T11:01:00+09:00", 15, "live")]
    assert (kept["swept"], kept["other"]) == (B, A)
    assert (kept["swept_extreme_at"], kept["other_extreme_at"]) == (
        "2026-10-08T10:59:00+09:00",
        "2026-10-08T10:52:00+09:00",
    )


async def test_a_symbol_has_the_events_of_every_pair_holding_it_whichever_side_swept(week: Week) -> None:
    body = await smt(week.client, B, f"?from={LIVE_DAY}&to={TODAY}")
    alone = await smt(week.client, C, f"?from={LIVE_DAY}&to={TODAY}")

    assert body["pairs"] == [{"a": A, "b": B}, {"a": B, "b": F}]
    assert seen(body) == [
        live(LIVE_EVENT),
        *made_again(RECORDED_DAY, "10:11", A, B, BULLISH, B),
        *made_again(RECORDED_DAY, "10:11", B, F, BULLISH, B),
    ]
    assert [d["source"] for d in body["days"]] == ["live", "reconstructed", "live"], "today: the engine's"
    assert alone == {
        "symbol": C,
        "from": "2026-10-08",
        "to": "2026-10-13",
        "pairs": [],
        "days": [],
        "events": [],
    }


async def test_the_range_is_the_last_five_trading_days_by_default_and_ends_today(week: Week) -> None:
    default = await smt(week.client, A)
    later = await smt(week.client, A, f"?from={TODAY}&to=2026-10-20")

    assert (default["from"], default["to"]) == ("2026-10-06", "2026-10-13")
    assert [d["date"] for d in default["days"]] == [
        "2026-10-06",
        "2026-10-07",
        "2026-10-08",
        "2026-10-12",
        "2026-10-13",
    ]
    assert (later["to"], [d["date"] for d in later["days"]]) == ("2026-10-20", ["2026-10-13"])
    for query, status in (
        ("?from=2026-10-1", 400),
        ("?from=2026-10-13&to=2026-10-12", 400),
        (f"?from=2026-09-12&to={TODAY}", 400),  # 32 days
    ):
        response = await week.client.get(f"/v1/symbols/{A}/smt{query}")
        assert response.status == status, query
        check("error", await response.json())
    assert (await smt(week.client, A, f"?from=2026-09-13&to={TODAY}"))["from"] == "2026-09-13", "31 days"
    unknown = await week.client.get("/v1/symbols/999999/smt")
    assert unknown.status == 404
    check("error", await unknown.json())


async def test_a_day_made_again_is_kept_and_one_with_nothing_is_looked_for_again(week: Week) -> None:
    await smt(week.client, A, f"?from={OLDER}&to={TODAY}")
    await smt(week.client, B, f"?from={OLDER}&to={TODAY}")

    # Never the live days, nor today.
    assert week.made == [OLDER, EMPTY, BACKFILLED_DAY, RECORDED_DAY, EMPTY]


async def test_the_days_kept_are_few_and_the_least_recently_asked_goes_first(
    week: Week, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "SMT_CACHED_DAYS", 2)
    async with TestClient(TestServer(create_app(week.engine, data_dir=week.data))) as client:
        for day in (OLDER, BACKFILLED_DAY, OLDER, RECORDED_DAY, OLDER, BACKFILLED_DAY):
            await smt(client, A, f"?from={day}&to={day}")

    # OLDER asked again before the 12th is made: the 7th, asked least recently, is let go.
    assert week.made == [OLDER, BACKFILLED_DAY, RECORDED_DAY, BACKFILLED_DAY]


async def test_questions_asked_at_once_make_each_day_once(week: Week) -> None:
    query = f"?from={OLDER}&to={RECORDED_DAY}"

    first, second = await asyncio.gather(smt(week.client, A, query), smt(week.client, B, query))

    assert sorted(week.made) == [OLDER, EMPTY, EMPTY, BACKFILLED_DAY, RECORDED_DAY]
    assert first["days"] == second["days"]


async def test_today_is_the_engines_even_before_its_first_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    record(data, minute_trades({(B, "10:10"): 999.0}))  # today's trades so far, recorded
    engine = await run(
        tmp_path / "engine.sqlite3", [], at("10:40", DAY), calendar=WEEK, symbols=(A, B), **BARE
    )
    made: list[date] = []
    monkeypatch.setattr(api, "reconstruct", lambda data_dir, day, calendar, first: made.append(day))

    async with TestClient(TestServer(create_app(engine, data_dir=data))) as client:
        body = await smt(client, A, f"?from={DAY}&to={DAY}")

    assert (body["days"], body["events"], made) == (
        [{"date": "2026-10-12", "source": "live", "bars": None}],
        [],
        [],
    )
    engine.store.close()
