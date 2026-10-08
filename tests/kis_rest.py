"""Canned KIS REST answers for the fake KIS server, shaped like live responses (2026-10-08)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp

from krx_quant.auth import tokens_and_client
from krx_quant.fake_kis import FakeKis
from krx_quant.kis import RestClient
from tests.test_collector import settings_for

Responder = Callable[[dict[str, str]], dict[str, Any]]

HOLIDAYS = "CTCA0903R"
MINUTE_BARS = "FHKST03010230"

# KIS answers about 24 days per holiday query.
_HOLIDAY_PAGE_DAYS = 24


def holiday_api(closed: Iterable[date] = (), last: date = date(2027, 12, 31)) -> Responder:
    """The holiday query: days from ``BASS_DT`` on, weekends and ``closed`` not open."""
    closed = set(closed)

    def respond(params: dict[str, str]) -> dict[str, Any]:
        start = datetime.strptime(params["BASS_DT"], "%Y%m%d").date()
        days = [start + timedelta(days=i) for i in range(_HOLIDAY_PAGE_DAYS)]
        rows = [
            {
                "bass_dt": d.strftime("%Y%m%d"),
                "wday_dvsn_cd": f"{d.isoweekday() % 7 + 1:02d}",
                "bzdy_yn": "N" if d.weekday() >= 5 or d in closed else "Y",
                "tr_day_yn": "Y",
                "opnd_yn": "N" if d.weekday() >= 5 or d in closed else "Y",
                "sttl_day_yn": "N" if d.weekday() >= 5 or d in closed else "Y",
            }
            for d in days
            if d <= last
        ]
        return {"rt_cd": "0", "msg_cd": "KIOK0500", "msg1": "조회가 계속됩니다", "output": rows}

    return respond


@asynccontextmanager
async def rest_client(kis: FakeKis, data_dir: Path, now: datetime) -> AsyncIterator[RestClient]:
    """A REST client for the fake KIS, as the collector builds it."""
    async with aiohttp.ClientSession() as session:
        _, client = tokens_and_client(session, settings_for(kis, data_dir), lambda: now)
        yield client


def requests_for(kis: FakeKis, tr_id: str) -> list[dict[str, str]]:
    return [query for _, t, _, query in kis.rest_requests if t == tr_id]


# KIS answers at most 120 bars per minute-bar query.
_BARS_PER_QUERY = 120


def day_bars(day: date, count: int, first: str = "080000") -> list[dict[str, str]]:
    """``count`` one-minute bars on ``day`` from ``first`` on, oldest first, in KIS fields."""
    start = datetime.combine(day, datetime.strptime(first, "%H%M%S").time())
    bars = []
    for i in range(count):
        price = 80000 + 100 * (i % 7)
        bars.append(
            {
                "stck_bsop_date": day.strftime("%Y%m%d"),
                "stck_cntg_hour": (start + timedelta(minutes=i)).strftime("%H%M%S"),
                "stck_prpr": str(price),
                "stck_oprc": str(price - 100),
                "stck_hgpr": str(price + 100),
                "stck_lwpr": str(price - 200),
                "cntg_vol": str(1000 + i),
                "acml_tr_pbmn": str(80_000_000 * (i + 1)),
            }
        )
    return bars


def minute_bars_api(bars: Callable[[str, str, date], list[dict[str, str]]]) -> Responder:
    """The minute-bar query for one day (``FID_PW_DATA_INCU_YN=N``): the day's bars at or
    before ``FID_INPUT_HOUR_1``, newest first, at most 120. ``bars(market, symbol, day)`` is
    the day, by market code (``UN`` integrated, ``J`` KRX).
    """

    def respond(params: dict[str, str]) -> dict[str, Any]:
        day = datetime.strptime(params["FID_INPUT_DATE_1"], "%Y%m%d").date()
        hour = params["FID_INPUT_HOUR_1"]
        that_day = bars(params["FID_COND_MRKT_DIV_CODE"], params["FID_INPUT_ISCD"], day)
        rows = [b for b in reversed(that_day) if b["stck_cntg_hour"] <= hour]
        return {
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "정상처리 되었습니다.",
            "output1": {"hts_kor_isnm": "삼성전자"},
            "output2": rows[:_BARS_PER_QUERY],
        }

    return respond
