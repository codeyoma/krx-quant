"""Day candles kept from KIS's period chart query (CandleKeeper, #111), through the fake KIS seam."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from aiohttp.test_utils import TestClient, TestServer

from krx_quant.api import MAX_CANDLES, create_app
from krx_quant.candles import (
    CANDLE_SCHEMA,
    CANDLES_TR,
    CHECKED,
    EX_CODE,
    KRX_CHECKED,
    KRX_DAY_SCHEMA,
    NOTED,
    CandleKeeper,
    Notes,
    Replaced,
    candles_path,
    krx_days_path,
    period_start,
    read_candles,
    read_krx_days,
    read_notes,
    stored_candles,
    write_candles,
)
from krx_quant.fake_kis import FakeKis
from krx_quant.feeds.base import KST
from krx_quant.market_calendar import MarketCalendar
from krx_quant.ratelimit import RateLimiter
from tests.api_schema import check
from tests.kis_rest import requests_for, rest_client, rest_grants
from tests.replay_events import at, get_json, run

# Friday the 9th is Hangul Day: closed.
CALENDAR = MarketCalendar({date(2026, 10, 9): False})
AFTER_HOURS = datetime(2026, 10, 13, 21, 0, tzinfo=KST)  # Tuesday, its candle final
PER_QUERY = 100

Candles = Callable[[str, str], list[dict[str, str]]]


def trading_days(first: date, last: date) -> list[date]:
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    return [d for d in days if CALENDAR.is_trading_day(d)]


def kis_candles(days: list[date], base: int = 50_000) -> list[dict[str, str]]:
    """A candle per day in KIS's fields, oldest first; the close moves with the day's index."""
    rows = []
    for i, day in enumerate(days):
        close = base + 10 * i
        rows.append(
            {
                "stck_bsop_date": day.strftime("%Y%m%d"),
                "stck_oprc": str(close - 50),
                "stck_hgpr": str(close + 100),
                "stck_lwpr": str(close - 100),
                "stck_clpr": str(close),
                "acml_vol": str(1000 + i),
                "acml_tr_pbmn": str(close * (1000 + i)),
                "mod_yn": "N",
            }
        )
    return rows


def kis_periods(days: list[dict[str, str]], code: str, label: str = "last") -> list[dict[str, str]]:
    """KIS's candles of the period ``code`` (D, W, M, Y) made of its day candles, oldest first:
    each labelled with its period's ``label`` ("first" or "last") trading day. The period
    still going holds every day there is, as KIS's does during it."""
    if code == "D":
        return days
    period = {"W": "week", "M": "month", "Y": "year"}[code]
    groups: dict[date, list[dict[str, str]]] = {}
    for r in days:
        groups.setdefault(
            period_start(datetime.strptime(r["stck_bsop_date"], "%Y%m%d").date(), period), []
        ).append(r)
    out = []
    for start in sorted(groups):
        group = groups[start]
        out.append(
            {
                "stck_bsop_date": (group[-1] if label == "last" else group[0])["stck_bsop_date"],
                "stck_oprc": group[0]["stck_oprc"],
                "stck_hgpr": str(max(int(r["stck_hgpr"]) for r in group)),
                "stck_lwpr": str(min(int(r["stck_lwpr"]) for r in group)),
                "stck_clpr": group[-1]["stck_clpr"],
                "acml_vol": str(sum(int(r["acml_vol"]) for r in group)),
                "acml_tr_pbmn": str(sum(int(r["acml_tr_pbmn"]) for r in group)),
                "mod_yn": "N",
            }
        )
    return out


def candles_api(candles: Candles, label: str = "last") -> Callable[[dict[str, str]], dict[str, Any]]:
    """The period chart query: the newest 100 candles of the period asked, made of the day
    candles ``candles(market, symbol)``, labelled from ``FID_INPUT_DATE_1`` to
    ``FID_INPUT_DATE_2``, newest first, padded like KIS."""

    def respond(params: dict[str, str]) -> dict[str, Any]:
        assert params["FID_ORG_ADJ_PRC"] == "0"
        first, last = params["FID_INPUT_DATE_1"], params["FID_INPUT_DATE_2"]
        days = candles(params["FID_COND_MRKT_DIV_CODE"], params["FID_INPUT_ISCD"])
        rows = kis_periods(days, params["FID_PERIOD_DIV_CODE"], label)
        within = [r for r in reversed(rows) if first <= r["stck_bsop_date"] <= last][:PER_QUERY]
        return {
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "정상처리 되었습니다.",
            "output1": {"hts_kor_isnm": "삼성전자"},
            "output2": within or [{"stck_bsop_date": "", "stck_clpr": ""}],
        }

    return respond


async def keep(
    kis: FakeKis,
    data_dir: Path,
    now: datetime,
    symbols: tuple[str, ...] = ("005930",),
    per_second: float = 1000,
) -> int:
    pace = RateLimiter(1, 1 / per_second, margin=0)  # the backfill's, in the collector
    async with rest_client(kis, data_dir, now) as client:
        return await CandleKeeper(client, CALENDAR, data_dir, symbols, pace).run(now)


def closes(data_dir: Path, symbol: str = "005930", period: str = "day") -> list[tuple[date, int]]:
    return [(r["date"], r["close"]) for r in read_candles(data_dir, period, symbol)]


def day_queries(kis: FakeKis) -> list[dict[str, str]]:
    """The day candles' queries, but the KRX day candles' (#264: the market J beside them)."""
    return [
        q
        for q in requests_for(kis, CANDLES_TR)
        if q["FID_PERIOD_DIV_CODE"] == "D" and q["FID_COND_MRKT_DIV_CODE"] == "UN"
    ]


def krx_day_queries(kis: FakeKis) -> list[dict[str, str]]:
    return [
        q
        for q in requests_for(kis, CANDLES_TR)
        if q["FID_PERIOD_DIV_CODE"] == "D" and q["FID_COND_MRKT_DIV_CODE"] == "J"
    ]


async def test_the_first_run_takes_every_candle_kis_has_paging_back_100_at_a_time(
    kis: FakeKis, tmp_path: Path
) -> None:
    days = trading_days(date(2025, 3, 3), date(2026, 10, 13))  # about 400
    history = kis_candles(days)
    kis.rest_responses[CANDLES_TR] = candles_api(lambda market, symbol: history)

    assert await keep(kis, tmp_path, AFTER_HOURS) == 5, "a file each of days, weeks, months, years, KRX days"

    stored = read_candles(tmp_path, "day", "005930")
    assert [r["date"] for r in stored] == days
    assert stored[-1] == {
        "symbol": "005930",
        "date": date(2026, 10, 13),
        "venue": "UN",
        "open": int(history[-1]["stck_clpr"]) - 50,
        "high": int(history[-1]["stck_clpr"]) + 100,
        "low": int(history[-1]["stck_clpr"]) - 100,
        "close": int(history[-1]["stck_clpr"]),
        "volume": 1000 + len(days) - 1,
        "value": int(history[-1]["acml_tr_pbmn"]),
    }
    asked = day_queries(kis)
    assert len(asked) == math.ceil(len(days) / PER_QUERY) + 1, "every page, then one that finds nothing older"
    second_end = days[-PER_QUERY] - timedelta(days=1)
    assert [q["FID_INPUT_DATE_2"] for q in asked[:2]] == ["20261013", second_end.strftime("%Y%m%d")]
    assert {
        q["FID_COND_MRKT_DIV_CODE"] for q in requests_for(kis, CANDLES_TR) if q not in krx_day_queries(kis)
    } == {"UN"}


async def test_the_next_day_only_the_new_candle_is_added(kis: FakeKis, tmp_path: Path) -> None:
    days = trading_days(date(2026, 6, 1), date(2026, 10, 14))
    history = kis_candles(days)
    kis.rest_responses[CANDLES_TR] = candles_api(lambda market, symbol: history)
    await keep(kis, tmp_path, AFTER_HOURS)
    kis.rest_requests.clear()

    next_night = datetime(2026, 10, 14, 21, 0, tzinfo=KST)
    assert await keep(kis, tmp_path, next_night) == 2, "the days and the KRX days"

    assert closes(tmp_path)[-2:] == [(date(2026, 10, 13), 50_000 + 10 * (len(days) - 2))] + [
        (date(2026, 10, 14), 50_000 + 10 * (len(days) - 1))
    ]
    assert len(closes(tmp_path)) == len(days)
    [asked] = day_queries(kis)
    assert (asked["FID_INPUT_DATE_1"], asked["FID_INPUT_DATE_2"]) == ("20261006", "20261014"), (
        "the last five stored days (the 9th closed), to see that KIS did not adjust the oldest and"
        " to replace any it finalized since (#264), and the new one"
    )


async def test_a_candle_is_stored_only_once_its_day_is_over(kis: FakeKis, tmp_path: Path) -> None:
    days = trading_days(date(2026, 9, 1), date(2026, 10, 13))
    kis.rest_responses[CANDLES_TR] = candles_api(lambda market, symbol: kis_candles(days))

    await keep(kis, tmp_path, datetime(2026, 10, 13, 15, 40, tzinfo=KST))  # after the close, before 20:30

    assert closes(tmp_path)[-1][0] == date(2026, 10, 12)
    assert day_queries(kis)[0]["FID_INPUT_DATE_2"] == "20261012"

    await keep(kis, tmp_path, datetime(2026, 10, 10, 12, 0, tzinfo=KST))  # a Saturday: nothing new

    assert closes(tmp_path)[-1][0] == date(2026, 10, 12)


async def test_an_adjusted_past_takes_the_whole_history_again(kis: FakeKis, tmp_path: Path) -> None:
    days = trading_days(date(2026, 8, 3), date(2026, 10, 14))
    before_split = kis_candles(days, base=2_500_000)
    after_split = kis_candles(days, base=50_000)  # a 50:1 split on the 14th: KIS adjusts the past
    answer = {"history": before_split}
    kis.rest_responses[CANDLES_TR] = candles_api(lambda market, symbol: answer["history"])
    await keep(kis, tmp_path, AFTER_HOURS)
    assert closes(tmp_path)[0][1] == 2_500_000

    answer["history"] = after_split
    await keep(kis, tmp_path, datetime(2026, 10, 14, 21, 0, tzinfo=KST))

    assert closes(tmp_path) == [(d, 50_000 + 10 * i) for i, d in enumerate(days)]


async def test_a_past_adjusted_before_any_new_candle_is_taken_again_at_once(
    kis: FakeKis, tmp_path: Path
) -> None:
    days = trading_days(date(2026, 8, 3), date(2026, 10, 13))
    answer = {"history": kis_candles(days, base=2_500_000)}
    kis.rest_responses[CANDLES_TR] = candles_api(lambda market, symbol: answer["history"])
    await keep(kis, tmp_path, AFTER_HOURS)

    answer["history"] = kis_candles(days, base=50_000)  # a split effective at the 14th's open
    restart = datetime(2026, 10, 14, 7, 20, tzinfo=KST)  # the morning's restart: no new candle yet
    assert await keep(kis, tmp_path, restart) == 4, "the days, weeks, months (no year is over yet), KRX days"

    assert closes(tmp_path) == [(d, 50_000 + 10 * i) for i, d in enumerate(days)]


async def test_symbols_without_integrated_candles_take_krx_and_keep_it(kis: FakeKis, tmp_path: Path) -> None:
    days = trading_days(date(2026, 9, 1), date(2026, 10, 14))

    def candles(market: str, symbol: str) -> list[dict[str, str]]:
        on_nxt = symbol == "005930"  # 133690, an ETF, trades on KRX only
        return kis_candles(days) if market == "J" or on_nxt else []

    kis.rest_responses[CANDLES_TR] = candles_api(candles)
    await keep(kis, tmp_path, AFTER_HOURS, symbols=("005930", "133690"))
    kis.rest_requests.clear()
    await keep(kis, tmp_path, datetime(2026, 10, 14, 21, 0, tzinfo=KST), symbols=("005930", "133690"))

    assert {r["venue"] for r in read_candles(tmp_path, "day", "005930")} == {"UN"}
    assert {r["venue"] for r in read_candles(tmp_path, "day", "133690")} == {"KRX"}
    assert closes(tmp_path, "133690")[-1][0] == date(2026, 10, 14)
    assert [
        q["FID_COND_MRKT_DIV_CODE"] for q in requests_for(kis, CANDLES_TR) if q["FID_INPUT_ISCD"] == "133690"
    ][:1] == ["J"], "the next day asks the market the history came from"


async def test_a_symbol_kis_has_nothing_for_is_asked_again_next_run(kis: FakeKis, tmp_path: Path) -> None:
    answer: dict[str, list[dict[str, str]]] = {"history": []}
    kis.rest_responses[CANDLES_TR] = candles_api(lambda market, symbol: answer["history"])

    assert await keep(kis, tmp_path, AFTER_HOURS) == 0
    assert read_candles(tmp_path, "day", "005930") == []

    answer["history"] = kis_candles(trading_days(date(2026, 10, 1), date(2026, 10, 13)))
    assert await keep(kis, tmp_path, AFTER_HOURS) == 3, "the days, the finished weeks and the KRX days"


async def test_a_failing_symbol_does_not_stop_the_others(kis: FakeKis, tmp_path: Path) -> None:
    days = trading_days(date(2026, 10, 1), date(2026, 10, 13))

    def respond(params: dict[str, str]) -> dict[str, Any]:
        if params["FID_INPUT_ISCD"] == "000660":
            return {"rt_cd": "1", "msg_cd": "OPSQ0001", "msg1": "조회 처리 중 오류가 발생했습니다."}
        return candles_api(lambda market, symbol: kis_candles(days))(params)

    kis.rest_responses[CANDLES_TR] = respond

    assert (
        await keep(kis, tmp_path, AFTER_HOURS, symbols=("000660", "005930")) == 3
    )  # 005930's days, weeks, KRX
    assert closes(tmp_path)[-1][0] == date(2026, 10, 13)


async def test_the_queries_keep_the_backfill_pace_inside_the_rest_limit(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    let_through = rest_grants(monkeypatch)
    days = trading_days(date(2024, 1, 2), date(2026, 10, 13))  # about 690: 7 full pages each
    kis.rest_responses[CANDLES_TR] = candles_api(lambda market, symbol: kis_candles(days))

    await keep(kis, tmp_path, AFTER_HOURS, symbols=("005930", "000660"), per_second=20)

    assert len(let_through) == len(requests_for(kis, CANDLES_TR))
    assert len(day_queries(kis)) == 2 * (math.ceil(len(days) / PER_QUERY) + 1)
    assert len(krx_day_queries(kis)) == 2 * (math.ceil(len(days) / PER_QUERY) + 1)
    assert max(sum(1 for u in let_through if t <= u < t + 1.0) for t in let_through) <= 20


@pytest.mark.parametrize("label", ["last", "first"])
async def test_weeks_months_and_years_are_stored_by_their_first_day_once_over(
    kis: FakeKis, tmp_path: Path, label: str
) -> None:
    days = trading_days(date(2024, 12, 2), date(2026, 10, 13))
    history = kis_candles(days)
    kis.rest_responses[CANDLES_TR] = candles_api(lambda market, symbol: history, label)

    await keep(kis, tmp_path, AFTER_HOURS)  # Tuesday the 13th

    weeks = read_candles(tmp_path, "week", "005930")
    # Every week up to the one before the 13th's, by its Monday, whichever day KIS labels it.
    assert (weeks[0]["date"], weeks[-1]["date"]) == (date(2024, 12, 2), date(2026, 10, 5))
    assert all(w["date"].weekday() == 0 for w in weeks)
    # 5–8 October (Friday the 9th closed): Monday's open, the highest, the lowest, Thursday's close.
    week = [r for r in history if "20261005" <= r["stck_bsop_date"] <= "20261008"]
    assert {k: weeks[-1][k] for k in ("open", "high", "low", "close", "volume")} == {
        "open": int(week[0]["stck_oprc"]),
        "high": max(int(r["stck_hgpr"]) for r in week),
        "low": min(int(r["stck_lwpr"]) for r in week),
        "close": int(week[-1]["stck_clpr"]),
        "volume": sum(int(r["acml_vol"]) for r in week),
    }
    months = [r["date"] for r in read_candles(tmp_path, "month", "005930")]
    assert (months[0], months[-1], len(months)) == (date(2024, 12, 1), date(2026, 9, 1), 22)
    assert [r["date"] for r in read_candles(tmp_path, "year", "005930")] == [
        date(2024, 1, 1),
        date(2025, 1, 1),
    ]


async def test_a_week_is_added_once_it_is_over(kis: FakeKis, tmp_path: Path) -> None:
    answer = {"history": kis_candles(trading_days(date(2026, 8, 3), date(2026, 10, 13)))}
    kis.rest_responses[CANDLES_TR] = candles_api(lambda market, symbol: answer["history"])
    await keep(kis, tmp_path, AFTER_HOURS)
    answer["history"] = kis_candles(trading_days(date(2026, 8, 3), date(2026, 10, 19)))
    kis.rest_requests.clear()

    await keep(kis, tmp_path, datetime(2026, 10, 16, 21, 0, tzinfo=KST))  # Friday: its week still going
    assert closes(tmp_path, period="week")[-1][0] == date(2026, 10, 5)
    await keep(kis, tmp_path, datetime(2026, 10, 19, 21, 0, tzinfo=KST))  # Monday: the week before is over

    assert [d for d, _ in closes(tmp_path, period="week")][-2:] == [date(2026, 10, 5), date(2026, 10, 12)]
    weekly = [q for q in requests_for(kis, CANDLES_TR) if q["FID_PERIOD_DIV_CODE"] == "W"]
    assert [(q["FID_INPUT_DATE_1"], q["FID_INPUT_DATE_2"]) for q in weekly] == [
        ("20260907", "20261016"),
        ("20260907", "20261019"),
    ], "from the last five stored weeks, once a run"


async def test_the_api_gives_finished_weeks_months_and_years_never_the_one_going(tmp_path: Path) -> None:
    data = tmp_path / "data"

    def candle(day: date, close: int) -> dict[str, Any]:
        return {
            "symbol": "005930",
            "date": day,
            "venue": "UN",
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": 1,
            "value": 1,
        }

    mondays = [date(2026, 9, 21), date(2026, 9, 28), date(2026, 10, 5), date(2026, 10, 12)]  # the 12th's too
    write_candles(data, "week", "005930", [candle(d, i) for i, d in enumerate(mondays)])
    write_candles(data, "month", "005930", [candle(date(2026, m, 1), m) for m in (8, 9, 10)])
    write_candles(data, "year", "005930", [candle(date(y, 1, 1), y) for y in (2025, 2026)])
    engine = await run(tmp_path / "engine.db", [], until=at("10:00:00"))  # Monday the 12th

    async with TestClient(TestServer(create_app(engine, data_dir=data))) as client:
        weeks = await get_json(client, "/v1/symbols/005930/candles?period=week")
        older = await get_json(
            client, "/v1/symbols/005930/candles?period=week&before=2026-10-05T00:00:00%2B09:00&limit=1"
        )
        months = await get_json(client, "/v1/symbols/005930/candles?period=month")
        years = await get_json(client, "/v1/symbols/005930/candles?period=year")

    for body in (weeks, older, months, years):
        check("candles", body)
    assert [c["date"] for c in weeks["candles"]] == ["2026-09-21", "2026-09-28", "2026-10-05"]
    assert weeks["period"] == "week"
    assert [c["date"] for c in older["candles"]] == ["2026-09-28"]
    assert [c["date"] for c in months["candles"]] == ["2026-08-01", "2026-09-01"]
    assert [c["date"] for c in years["candles"]] == ["2025-01-01"]


def test_stored_candles_before_a_time_oldest_first_without_today(tmp_path: Path) -> None:
    days = trading_days(date(2026, 9, 1), date(2026, 10, 13))
    rows = [
        {
            "symbol": "005930",
            "date": d,
            "venue": "UN",
            "open": 1,
            "high": 2,
            "low": 1,
            "close": i,
            "volume": 1,
            "value": 1,
        }
        for i, d in enumerate(days)
    ]
    write_candles(tmp_path, "day", "005930", rows)
    today = date(2026, 10, 13)

    venue, latest = stored_candles(tmp_path, "day", "005930", datetime(2026, 10, 14, tzinfo=KST), 3, today)
    assert venue == "UN"
    assert [r["date"] for r in latest] == [date(2026, 10, 7), date(2026, 10, 8), date(2026, 10, 12)]

    _, older = stored_candles(tmp_path, "day", "005930", datetime(2026, 10, 7, tzinfo=KST), 2, today)
    assert [r["date"] for r in older] == [date(2026, 10, 5), date(2026, 10, 6)]

    assert stored_candles(tmp_path, "day", "000660", datetime(2026, 10, 14, tzinfo=KST), 3, today) == (
        None,
        [],
    )


async def test_the_api_gives_stored_candles_before_a_time_oldest_first_never_today(tmp_path: Path) -> None:
    data = tmp_path / "data"
    days = trading_days(date(2026, 9, 1), date(2026, 10, 12))
    rows = [
        {
            "symbol": "005930",
            "date": d,
            "venue": "KRX",
            "open": 100 + i,
            "high": 110 + i,
            "low": 90 + i,
            "close": 105 + i,
            "volume": 1000 + i,
            "value": 0,
        }
        for i, d in enumerate(days)
    ]
    write_candles(data, "day", "005930", rows)  # the 12th's too, as if stored after the day
    engine = await run(tmp_path / "engine.db", [], until=at("10:00:00"))  # the 12th

    async with TestClient(TestServer(create_app(engine, data_dir=data))) as client:
        latest = await get_json(client, "/v1/symbols/005930/candles?period=day&limit=3")
        older = await get_json(
            client, "/v1/symbols/005930/candles?period=day&before=2026-10-07T00:00:00%2B09:00&limit=2"
        )
        bad = [
            (await client.get(f"/v1/symbols/005930/candles?{q}")).status
            for q in ("", "period=minute", "period=day&limit=0", f"period=day&limit={MAX_CANDLES + 1}")
        ]
    async with TestClient(TestServer(create_app(engine, data_dir=tmp_path / "empty"))) as client:
        none = await get_json(client, "/v1/symbols/005930/candles?period=day")

    for body in (latest, older, none):
        check("candles", body)
    assert latest["venue"] == "KRX"
    assert [c["date"] for c in latest["candles"]] == ["2026-10-06", "2026-10-07", "2026-10-08"]
    n = len(days) - 2  # the 8th's
    assert latest["candles"][-1] == {
        "date": "2026-10-08",
        "open": 100 + n,
        "high": 110 + n,
        "low": 90 + n,
        "close": 105 + n,
        "volume": 1000 + n,
    }
    assert [c["date"] for c in older["candles"]] == ["2026-10-05", "2026-10-06"]
    assert none == {"symbol": "005930", "period": "day", "venue": None, "candles": []}
    assert bad == [400] * 4


async def test_the_krx_day_candles_are_kept_beside_the_integrated_ones(kis: FakeKis, tmp_path: Path) -> None:
    """#264: the KRX market's day candles (J), whose open is the opening auction's (the integrated
    one opens at NXT's 08:00 pre-market), in the day folder's krx.parquet; the next day adds the
    new one, and an adjusted past takes them all again."""
    days = trading_days(date(2026, 8, 3), date(2026, 10, 14))
    answer = {"base": 50_000}

    def candles(market: str, symbol: str) -> list[dict[str, str]]:
        rows = kis_candles(days, base=answer["base"])
        if market == "UN":  # the integrated open is NXT's pre-market's
            return [{**r, "stck_oprc": str(int(r["stck_oprc"]) + 300)} for r in rows]
        return rows

    kis.rest_responses[CANDLES_TR] = candles_api(candles)
    await keep(kis, tmp_path, AFTER_HOURS)  # the 13th's night

    krx = read_krx_days(tmp_path, "005930")
    assert krx_days_path(tmp_path, "005930") == tmp_path / "candles/period=day/symbol=005930/krx.parquet"
    assert [r["date"] for r in krx] == days[:-1]
    assert {r["venue"] for r in krx} == {"KRX"}
    integrated = read_candles(tmp_path, "day", "005930")
    assert [r["open"] - k["open"] for r, k in zip(integrated, krx, strict=True)] == [300] * len(krx)
    assert [r["close"] for r in integrated] == [k["close"] for k in krx]

    kis.rest_requests.clear()
    assert await keep(kis, tmp_path, datetime(2026, 10, 14, 21, 0, tzinfo=KST)) == 2
    assert read_krx_days(tmp_path, "005930")[-1]["date"] == date(2026, 10, 14)
    [asked] = krx_day_queries(kis)
    assert (asked["FID_INPUT_DATE_1"], asked["FID_INPUT_DATE_2"]) == ("20261006", "20261014")

    answer["base"] = 1_000  # a 50:1 split: KIS adjusts the past
    await keep(kis, tmp_path, datetime(2026, 10, 15, 7, 20, tzinfo=KST))
    assert [r["close"] for r in read_krx_days(tmp_path, "005930")] == [
        1_000 + 10 * i for i in range(len(days))
    ]


def provisional(rows: list[dict[str, str]], day: date) -> list[dict[str, str]]:
    """KIS's candles with ``day``'s as KIS gives it the evening it ends (#264): the close is the
    latest trade (the after-market's), and the high and volume take the evening's trades in."""
    ymd = day.strftime("%Y%m%d")
    return [
        {
            **r,
            "stck_clpr": str(int(r["stck_clpr"]) + 70),
            "stck_hgpr": str(int(r["stck_hgpr"]) + 70),
            "acml_vol": str(int(r["acml_vol"]) + 5),
        }
        if r["stck_bsop_date"] == ymd
        else r
        for r in rows
    ]


async def test_a_day_stored_the_evening_it_ends_has_its_provisional_close_replaced_the_next_run(
    kis: FakeKis, tmp_path: Path
) -> None:
    """#264: at 20:30 KIS's candle of the day is provisional, and KIS finalizes it later (the KRX
    day candle's close to the 15:30 closing auction). The next run asks the last stored days
    again and replaces the day, in the integrated and the KRX day candles alike, without taking
    the whole history again, and the file notes the day, the run and the fields."""
    days = trading_days(date(2026, 6, 1), date(2026, 10, 13))
    final = kis_candles(days)
    answer = {"history": provisional(final, date(2026, 10, 13))}
    kis.rest_responses[CANDLES_TR] = candles_api(lambda market, symbol: answer["history"])
    await keep(kis, tmp_path, AFTER_HOURS)  # the 13th, 21:00
    evening = {r["date"]: r for r in read_krx_days(tmp_path, "005930")}[date(2026, 10, 13)]
    assert evening["close"] == int(final[-1]["stck_clpr"]) + 70, "stored as KIS gave it that evening"

    answer["history"] = final  # KIS finalized it overnight
    kis.rest_requests.clear()
    morning = datetime(2026, 10, 14, 7, 20, tzinfo=KST)  # the morning's restart: no new day yet
    assert await keep(kis, tmp_path, morning) == 2, "the days and the KRX days; no week is over"

    for rows in (read_candles(tmp_path, "day", "005930"), read_krx_days(tmp_path, "005930")):
        assert [r["date"] for r in rows] == days
        assert rows[-1]["close"] == int(final[-1]["stck_clpr"])
        assert rows[-1]["high"] == int(final[-1]["stck_hgpr"])
        assert rows[-1]["volume"] == int(final[-1]["acml_vol"])
    assert len(day_queries(kis)) == 1 and len(krx_day_queries(kis)) == 1, "one query each, not the history"
    for path, level in (
        (candles_path(tmp_path, "day", "005930"), CHECKED),
        (krx_days_path(tmp_path, "005930"), KRX_CHECKED),
    ):
        assert read_notes(path) == Notes(
            level, (Replaced(date(2026, 10, 13), morning, ("high", "close", "volume")),)
        )
    assert read_notes(candles_path(tmp_path, "week", "005930")) == Notes(CHECKED, ())


async def test_a_day_kis_finalizes_after_the_next_one_is_stored_is_still_replaced(
    kis: FakeKis, tmp_path: Path
) -> None:
    """#264: before, a run asked only the newest stored day again, so a day KIS finalized once the
    next one was stored, still provisional, kept its provisional close. Now it is asked again
    while it is one of the last five stored."""
    days = trading_days(date(2026, 9, 1), date(2026, 10, 15))
    final = kis_candles(days)
    the_13th, the_14th, the_15th = date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 15)
    answer = {"history": provisional(provisional(final, the_13th), the_14th)}
    kis.rest_responses[CANDLES_TR] = candles_api(lambda market, symbol: answer["history"])
    await keep(kis, tmp_path, AFTER_HOURS)  # the 13th stored provisional
    await keep(kis, tmp_path, datetime(2026, 10, 14, 21, 0, tzinfo=KST))  # the 14th too; the 13th still so

    # The 13th final now, the 14th and the 15th still provisional: the newest stored (the 14th) is
    # unchanged, so the old rule added the 15th and kept the 13th as it was.
    answer["history"] = provisional(provisional(final, the_14th), the_15th)
    kis.rest_requests.clear()
    await keep(kis, tmp_path, datetime(2026, 10, 15, 21, 0, tzinfo=KST))

    closes_now = {r["date"]: r["close"] for r in read_krx_days(tmp_path, "005930")}
    finals = {d: int(r["stck_clpr"]) for d, r in zip(days, final, strict=True)}
    assert closes_now[the_13th] == finals[the_13th]
    assert (closes_now[the_14th], closes_now[the_15th]) == (finals[the_14th] + 70, finals[the_15th] + 70)
    assert [r.day for r in read_notes(krx_days_path(tmp_path, "005930")).replaced] == [the_13th]
    assert len(krx_day_queries(kis)) == 1, "one query, not the history"

    answer["history"] = final
    await keep(kis, tmp_path, datetime(2026, 10, 16, 7, 20, tzinfo=KST))
    assert [r["close"] for r in read_krx_days(tmp_path, "005930")] == [finals[d] for d in days]


async def test_a_file_written_before_the_refresh_is_checked_whole_once(kis: FakeKis, tmp_path: Path) -> None:
    """#264: a file from before carries no check note. Its next run asks KIS for the whole
    history, replaces each stored candle KIS has otherwise (here a provisional day weeks back),
    keeps one KIS does not answer, and adds the new day; the run after asks the last few only."""
    days = trading_days(date(2026, 3, 2), date(2026, 10, 13))
    final = kis_candles(days)
    old = [{**r, "venue": "KRX"} for r in _candles_of(provisional(final, date(2026, 9, 15))[:-1])]
    gone = {**old[0], "date": date(2026, 2, 27)}  # a day KIS no longer answers
    for path in (krx_days_path(tmp_path, "005930"), candles_path(tmp_path, "day", "005930")):
        venue = "KRX" if path.name == "krx.parquet" else "UN"
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [{**r, "venue": venue} for r in [gone, *old]]
        pq.write_table(pa.Table.from_pylist(rows, schema=CANDLE_SCHEMA), path)  # no notes
    kis.rest_responses[CANDLES_TR] = candles_api(lambda market, symbol: final)

    await keep(kis, tmp_path, AFTER_HOURS)

    pages = math.ceil(len(days) / PER_QUERY) + 1
    assert len(krx_day_queries(kis)) == pages and len(day_queries(kis)) == pages, "the whole history"
    for path in (krx_days_path(tmp_path, "005930"), candles_path(tmp_path, "day", "005930")):
        rows = pq.read_table(path).to_pylist()
        assert [r["date"] for r in rows] == [date(2026, 2, 27), *days]
        assert [r["close"] for r in rows[1:]] == [int(r["stck_clpr"]) for r in final]
        notes = read_notes(path)
        assert notes.checked == (KRX_CHECKED if path.name == "krx.parquet" else CHECKED)
        assert [(r.day, r.at, r.fields) for r in notes.replaced] == [
            (date(2026, 9, 15), AFTER_HOURS, ("high", "close", "volume"))
        ]

    kis.rest_requests.clear()
    await keep(kis, tmp_path, datetime(2026, 10, 14, 7, 20, tzinfo=KST))
    assert len(krx_day_queries(kis)) == 1 and len(day_queries(kis)) == 1, "checked: the last few only"


def _candles_of(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    """KIS's day candles as the keeper stores them (the integrated venue)."""
    return [
        {
            "symbol": "005930",
            "date": datetime.strptime(r["stck_bsop_date"], "%Y%m%d").date(),
            "venue": "UN",
            "open": int(r["stck_oprc"]),
            "high": int(r["stck_hgpr"]),
            "low": int(r["stck_lwpr"]),
            "close": int(r["stck_clpr"]),
            "volume": int(r["acml_vol"]),
            "value": int(r["acml_tr_pbmn"]),
        }
        for r in rows
    ]


def with_ex_codes(rows: list[dict[str, str]], codes: dict[date, str]) -> list[dict[str, str]]:
    """KIS's candles with its 락 구분 (``flng_cls_code``, #310): ``codes``' days as given, 00 on the
    others, as the probe found KIS gives them."""
    return [
        {**r, "flng_cls_code": codes.get(datetime.strptime(r["stck_bsop_date"], "%Y%m%d").date(), "00")}
        for r in rows
    ]


async def test_the_krx_day_candles_keep_kis_s_ex_date_code(kis: FakeKis, tmp_path: Path) -> None:
    """#310: KIS's adjusted prices take no cash dividend out, and its 락 구분 marks the ex-dividend
    days (02 year-end, 05 interim): the KRX day candles keep it as given (``ex_code``), the
    integrated ones keep no such column. A code KIS changes on a day asked again is replaced and
    noted with the field."""
    days = trading_days(date(2026, 8, 3), date(2026, 10, 13))
    marks = {date(2026, 9, 29): "05", date(2026, 10, 8): "02"}
    answer = {"history": with_ex_codes(kis_candles(days), marks)}
    kis.rest_responses[CANDLES_TR] = candles_api(lambda market, symbol: answer["history"])

    await keep(kis, tmp_path, AFTER_HOURS)

    krx = read_krx_days(tmp_path, "005930")
    assert [r["date"] for r in krx] == days
    assert {r["date"]: r[EX_CODE] for r in krx if r[EX_CODE] != "00"} == marks
    assert pq.read_schema(krx_days_path(tmp_path, "005930")).names == KRX_DAY_SCHEMA.names
    assert pq.read_schema(candles_path(tmp_path, "day", "005930")).names == CANDLE_SCHEMA.names
    assert EX_CODE not in read_candles(tmp_path, "day", "005930")[-1]
    assert read_notes(krx_days_path(tmp_path, "005930")) == Notes(KRX_CHECKED, ())

    answer["history"] = with_ex_codes(kis_candles(days), {**marks, date(2026, 10, 13): "01"})
    kis.rest_requests.clear()
    morning = datetime(2026, 10, 14, 7, 20, tzinfo=KST)
    await keep(kis, tmp_path, morning)

    assert read_krx_days(tmp_path, "005930")[-1][EX_CODE] == "01"
    assert read_notes(krx_days_path(tmp_path, "005930")).replaced == (
        Replaced(date(2026, 10, 13), morning, (EX_CODE,)),
    )
    assert len(krx_day_queries(kis)) == 1, "the last few only"


async def test_a_krx_day_file_from_before_the_ex_date_code_is_filled_once_whole(
    kis: FakeKis, tmp_path: Path
) -> None:
    """#310: a KRX day file written before it (#264's check noted, no ``ex_code`` column) is asked
    whole once more: every candle KIS answers gets its code, one KIS does not answer stays without,
    the fills are not noted as KIS's changes (the earlier note stays), and the run after asks the
    last few only. The integrated file is not asked whole again."""
    days = trading_days(date(2026, 3, 2), date(2026, 10, 13))
    marks = {date(2026, 3, 30): "05", date(2026, 6, 29): "05", date(2026, 9, 29): "05"}
    final = kis_candles(days)
    gone = {**_candles_of(final[:1])[0], "date": date(2026, 2, 27)}  # a day KIS no longer answers
    earlier = Replaced(date(2026, 10, 12), datetime(2026, 10, 13, 7, 20, tzinfo=KST), ("close",))
    notes = {b"krx_quant": json.dumps({"checked": CHECKED, "replaced": [earlier.as_dict()]}).encode()}
    for path in (krx_days_path(tmp_path, "005930"), candles_path(tmp_path, "day", "005930")):
        venue = "KRX" if path.name == "krx.parquet" else "UN"
        rows = [{**r, "venue": venue} for r in [gone, *_candles_of(final)]]
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.Table.from_pylist(rows, schema=CANDLE_SCHEMA.with_metadata(notes)), path)  # #342's
    assert read_notes(krx_days_path(tmp_path, "005930")).checked == CHECKED < KRX_CHECKED
    kis.rest_responses[CANDLES_TR] = candles_api(lambda market, symbol: with_ex_codes(final, marks))

    await keep(kis, tmp_path, AFTER_HOURS)

    assert len(krx_day_queries(kis)) == math.ceil(len(days) / PER_QUERY) + 1, "the whole history"
    assert len(day_queries(kis)) == 1, "the integrated file was checked before: the last few only"
    krx = read_krx_days(tmp_path, "005930")
    assert [r["date"] for r in krx] == [date(2026, 2, 27), *days]
    assert krx[0][EX_CODE] is None, "KIS did not answer it: no code"
    assert {r["date"]: r[EX_CODE] for r in krx[1:] if r[EX_CODE] != "00"} == marks
    assert all(r[EX_CODE] == "00" for r in krx[1:] if r["date"] not in marks)
    assert [r["close"] for r in krx[1:]] == [int(r["stck_clpr"]) for r in final]
    assert read_notes(krx_days_path(tmp_path, "005930")) == Notes(KRX_CHECKED, (earlier,)), (
        "filled, not changed by KIS"
    )
    assert pq.read_schema(candles_path(tmp_path, "day", "005930")).names == CANDLE_SCHEMA.names

    kis.rest_requests.clear()
    await keep(kis, tmp_path, datetime(2026, 10, 14, 7, 20, tzinfo=KST))
    assert len(krx_day_queries(kis)) == 1, "filled: the last few only"


def test_a_file_notes_its_last_replaced_candles_only(tmp_path: Path) -> None:
    path = candles_path(tmp_path, "day", "005930")
    at_night = datetime(2026, 10, 13, 21, 0, tzinfo=KST)
    replaced = [
        Replaced(date(2026, 1, 1) + timedelta(days=i), at_night, ("close",)) for i in range(NOTED + 5)
    ]

    write_candles(tmp_path, "day", "005930", [], replaced)

    assert read_notes(path).replaced == tuple(replaced[-NOTED:])
    assert read_notes(tmp_path / "none.parquet") == Notes()
