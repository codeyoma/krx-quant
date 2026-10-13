"""Day candles kept from KIS's period chart query (CandleKeeper, #111), through the fake KIS seam."""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from krx_quant.api import MAX_CANDLES, create_app
from krx_quant.candles import CANDLES_TR, CandleKeeper, read_candles, stored_candles, write_candles
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


def candles_api(candles: Candles) -> Callable[[dict[str, str]], dict[str, Any]]:
    """The period chart query: the newest 100 of ``candles(market, symbol)`` from
    ``FID_INPUT_DATE_1`` to ``FID_INPUT_DATE_2``, newest first, padded like KIS."""

    def respond(params: dict[str, str]) -> dict[str, Any]:
        assert params["FID_PERIOD_DIV_CODE"] == "D" and params["FID_ORG_ADJ_PRC"] == "0"
        first, last = params["FID_INPUT_DATE_1"], params["FID_INPUT_DATE_2"]
        rows = candles(params["FID_COND_MRKT_DIV_CODE"], params["FID_INPUT_ISCD"])
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


def closes(data_dir: Path, symbol: str = "005930") -> list[tuple[date, int]]:
    return [(r["date"], r["close"]) for r in read_candles(data_dir, "day", symbol)]


async def test_the_first_run_takes_every_candle_kis_has_paging_back_100_at_a_time(
    kis: FakeKis, tmp_path: Path
) -> None:
    days = trading_days(date(2025, 3, 3), date(2026, 10, 13))  # about 400
    history = kis_candles(days)
    kis.rest_responses[CANDLES_TR] = candles_api(lambda market, symbol: history)

    assert await keep(kis, tmp_path, AFTER_HOURS) == 1

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
    asked = requests_for(kis, CANDLES_TR)
    assert len(asked) == math.ceil(len(days) / PER_QUERY) + 1, "every page, then one that finds nothing older"
    second_end = days[-PER_QUERY] - timedelta(days=1)
    assert [q["FID_INPUT_DATE_2"] for q in asked[:2]] == ["20261013", second_end.strftime("%Y%m%d")]
    assert {q["FID_COND_MRKT_DIV_CODE"] for q in asked} == {"UN"}


async def test_the_next_day_only_the_new_candle_is_added(kis: FakeKis, tmp_path: Path) -> None:
    days = trading_days(date(2026, 6, 1), date(2026, 10, 14))
    history = kis_candles(days)
    kis.rest_responses[CANDLES_TR] = candles_api(lambda market, symbol: history)
    await keep(kis, tmp_path, AFTER_HOURS)
    kis.rest_requests.clear()

    next_night = datetime(2026, 10, 14, 21, 0, tzinfo=KST)
    assert await keep(kis, tmp_path, next_night) == 1

    assert closes(tmp_path)[-2:] == [(date(2026, 10, 13), 50_000 + 10 * (len(days) - 2))] + [
        (date(2026, 10, 14), 50_000 + 10 * (len(days) - 1))
    ]
    assert len(closes(tmp_path)) == len(days)
    [asked] = requests_for(kis, CANDLES_TR)
    assert (asked["FID_INPUT_DATE_1"], asked["FID_INPUT_DATE_2"]) == ("20261013", "20261014"), (
        "the last stored day, to see that KIS did not adjust it, and the new one"
    )


async def test_a_candle_is_stored_only_once_its_day_is_over(kis: FakeKis, tmp_path: Path) -> None:
    days = trading_days(date(2026, 9, 1), date(2026, 10, 13))
    kis.rest_responses[CANDLES_TR] = candles_api(lambda market, symbol: kis_candles(days))

    await keep(kis, tmp_path, datetime(2026, 10, 13, 15, 40, tzinfo=KST))  # after the close, before 20:30

    assert closes(tmp_path)[-1][0] == date(2026, 10, 12)
    assert requests_for(kis, CANDLES_TR)[0]["FID_INPUT_DATE_2"] == "20261012"

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
    assert await keep(kis, tmp_path, restart) == 1

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
    assert await keep(kis, tmp_path, AFTER_HOURS) == 1


async def test_a_failing_symbol_does_not_stop_the_others(kis: FakeKis, tmp_path: Path) -> None:
    days = trading_days(date(2026, 10, 1), date(2026, 10, 13))

    def respond(params: dict[str, str]) -> dict[str, Any]:
        if params["FID_INPUT_ISCD"] == "000660":
            return {"rt_cd": "1", "msg_cd": "OPSQ0001", "msg1": "조회 처리 중 오류가 발생했습니다."}
        return candles_api(lambda market, symbol: kis_candles(days))(params)

    kis.rest_responses[CANDLES_TR] = respond

    assert await keep(kis, tmp_path, AFTER_HOURS, symbols=("000660", "005930")) == 1
    assert closes(tmp_path)[-1][0] == date(2026, 10, 13)


async def test_the_queries_keep_the_backfill_pace_inside_the_rest_limit(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    let_through = rest_grants(monkeypatch)
    days = trading_days(date(2024, 1, 2), date(2026, 10, 13))  # about 690: 7 full pages each
    kis.rest_responses[CANDLES_TR] = candles_api(lambda market, symbol: kis_candles(days))

    await keep(kis, tmp_path, AFTER_HOURS, symbols=("005930", "000660"), per_second=20)

    assert (
        len(let_through) == len(requests_for(kis, CANDLES_TR)) == 2 * (math.ceil(len(days) / PER_QUERY) + 1)
    )
    assert max(sum(1 for u in let_through if t <= u < t + 1.0) for t in let_through) <= 20


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
            for q in ("", "period=week", "period=day&limit=0", f"period=day&limit={MAX_CANDLES + 1}")
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
