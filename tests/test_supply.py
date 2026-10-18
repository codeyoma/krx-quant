"""The daily supply data kept for the chart's lower indicators (SupplyKeeper, #226), through
the fake KIS and the fake Toss, and served by ``GET /v1/symbols/{symbol}/supply``.

Every number here is made up: a day's values are worked out from its date, so a test can
tell which source and which query a stored value came from.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from aiohttp.test_utils import TestClient, TestServer

from krx_quant import supply
from krx_quant.api import MAX_SUPPLY_DAYS, create_app
from krx_quant.fake_kis import FakeKis
from krx_quant.feeds.base import KST
from krx_quant.market_calendar import MarketCalendar
from krx_quant.ratelimit import RateLimiter
from krx_quant.supply import (
    CREDIT,
    FIELDS,
    HISTORY_YEARS,
    INVESTORS,
    KIS,
    LENDING,
    NET_MARKET,
    NXT_OPENED,
    SHORT_SALE,
    TOSS,
    SupplyKeeper,
    history_floor,
    krx_history_stored,
    read_supply,
    stored_supply,
    write_supply,
)
from krx_quant.toss import TossClient, TossTokens
from tests.api_schema import check
from tests.fake_toss import CLIENT_ID, CLIENT_SECRET, FakeToss, fake_toss
from tests.kis_rest import requests_for, rest_client
from tests.replay_events import at, get_json, run

# Friday the 9th is Hangul Day: closed.
CALENDAR = MarketCalendar({date(2026, 10, 9): False})
EVENING = datetime(2026, 10, 13, 21, 0, tzinfo=KST)  # Tuesday, the day over (after 20:30)
MORNING = datetime(2026, 10, 14, 7, 20, tzinfo=KST)  # the next morning's restart
SYMBOL = "005930"


def trading_days(first: date, last: date) -> list[date]:
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    return [d for d in days if CALENDAR.is_trading_day(d)]


def ymd(day: date) -> str:
    return day.strftime("%Y%m%d")


def n(day: date) -> int:
    """A made-up number of the day."""
    return day.toordinal() % 10_000


# KIS's value of each field on a day, and Toss's (a different one, to tell them apart).
def kis_value(name: str, day: date) -> int:
    return {
        "short_volume": 1_000 + n(day),
        "individual_net": -(2_000 + n(day)),
        "institution_net": 3_000 + n(day),
        "foreign_net": -(4_000 + n(day)),
        "lending_balance": 500_000 + n(day),
        "credit_balance": 60_000 + n(day),
    }[name]


def krx_value(name: str, day: date) -> int:
    """KIS's KRX (``J``) net buys of a day: KRX alone, another number than the integrated one."""
    return {
        "individual_net": -(12_000 + n(day)),
        "institution_net": 13_000 + n(day),
        "foreign_net": -(14_000 + n(day)),
    }[name]


def toss_value(name: str, day: date) -> int | float:
    return {
        "short_volume": 7_000 + n(day),
        "individual_net": -(8_000 + n(day)),
        "institution_net": 9_000 + n(day),
        "foreign_registered": 11_000 + n(day),
        "foreign_holding": 1_000_000 + n(day),
        "foreign_ratio": round((5_000 + n(day) % 100) / 100, 2),  # %
        "lending_balance": 700_000 + n(day),
        "credit_balance": 80_000 + n(day),
    }[name]


def ok(**body: Any) -> dict[str, Any]:
    return {"rt_cd": "0", "msg_cd": "MCA00000", "msg1": "정상처리 되었습니다.", **body}


@dataclass
class KisSupply:
    """KIS's four daily supply queries over ``days`` (oldest first), as the probe saw them:
    the newest rows up to the day asked, newest first, 100 (short sale, lending) or 30
    (investors, credit) a call, padded with a blank row. The investor query answers both
    markets: integrated (``UN``, ``kis_value``) and KRX (``J``, ``krx_value``)."""

    days: list[date]
    credit_days: list[date] | None = None  # the settlement days the credit has (default: days)

    def short_sale(self, params: dict[str, str]) -> dict[str, Any]:
        assert params["FID_COND_MRKT_DIV_CODE"] == "J"
        first, last = params["FID_INPUT_DATE_1"], params["FID_INPUT_DATE_2"]
        rows = [
            {
                "stck_bsop_date": ymd(d),
                "ssts_cntg_qty": str(kis_value("short_volume", d)),
                "ssts_vol_rlim": "1.23",
            }
            for d in reversed(self.days)
            if first <= ymd(d) <= last
        ][:100]
        return ok(output1={"stck_prpr": "1"}, output2=[*rows, {"stck_bsop_date": "", "ssts_cntg_qty": ""}])

    def investors(self, params: dict[str, str]) -> dict[str, Any]:
        market = params["FID_COND_MRKT_DIV_CODE"]
        assert market in ("UN", "J")
        value = kis_value if market == "UN" else krx_value
        last = params["FID_INPUT_DATE_1"]
        rows = [
            {
                "stck_bsop_date": ymd(d),
                "prsn_ntby_qty": str(value("individual_net", d)),
                "orgn_ntby_qty": str(value("institution_net", d)),
                "frgn_ntby_qty": str(value("foreign_net", d)),
                "frgn_reg_ntby_qty": str(toss_value("foreign_registered", d)),
            }
            for d in reversed(self.days)
            if ymd(d) <= last
        ][:30]
        return ok(output1={"stck_prpr": "1"}, output2=rows)

    def lending(self, params: dict[str, str]) -> dict[str, Any]:
        assert params["MRKT_DIV_CLS_CODE"] == "3"
        first, last = params["START_DATE"], params["END_DATE"]
        rows = [
            {"bsop_date": ymd(d), "rmnd_stcn": str(kis_value("lending_balance", d)), "new_stcn": "5"}
            for d in reversed(self.days)
            if first <= ymd(d) <= last
        ][:100]
        return ok(output1=rows)

    def credit(self, params: dict[str, str]) -> dict[str, Any]:
        assert params["FID_COND_SCR_DIV_CODE"] == "20476"
        last = params["FID_INPUT_DATE_1"]
        settled = self.credit_days if self.credit_days is not None else self.days
        rows = [
            {
                "deal_date": ymd(d - timedelta(days=2)),  # not the day kept: the settlement day is
                "stlm_date": ymd(d),
                "whol_loan_rmnd_stcn": str(kis_value("credit_balance", d)),
                "whol_loan_rmnd_rate": "0.12",
            }
            for d in reversed(settled)
            if ymd(d) <= last
        ][:30]
        return ok(output=rows)

    def serve(self, kis: FakeKis) -> None:
        kis.rest_responses[SHORT_SALE.tr] = self.short_sale
        kis.rest_responses[INVESTORS.tr] = self.investors
        kis.rest_responses[LENDING.tr] = self.lending
        kis.rest_responses[CREDIT.tr] = self.credit


def toss_records(days: list[date]) -> Callable[[str, str], list[dict[str, Any]] | tuple[int, str]]:
    """Toss's four endpoints over ``days`` (oldest first): records newest first. Its foreigner is
    the registered ones; each value is Toss's own (``toss_value``)."""

    def value(name: str, day: date) -> str:
        return str(toss_value(name, day))

    def records(symbol: str, name: str) -> list[dict[str, Any]] | tuple[int, str]:
        out = []
        for d in reversed(days):
            record: dict[str, Any] = {"date": d.isoformat(), "updatedAt": f"{d.isoformat()}T18:00:00+09:00"}
            if name == "investor-trading":
                record |= {
                    "individual": {"netBuyVolume": value("individual_net", d)},
                    "institution": {"netBuyVolume": value("institution_net", d)},
                    "foreigner": {"netBuyVolume": value("foreign_registered", d)},
                    "foreignerHolding": {
                        "holdingQuantity": value("foreign_holding", d),
                        "limitQuantity": "2000000",
                        "holdingRate": str(round(float(toss_value("foreign_ratio", d)) / 100, 4)),
                    },
                }
            elif name == "short-selling":
                record |= {"shortSellingVolume": value("short_volume", d), "shortSellingVolumeRate": "0.01"}
            elif name == "securities-lending":
                record |= {"balanceQuantity": value("lending_balance", d)}
            elif name == "credit-trades":
                record |= {"marginLoan": {"balanceQuantity": value("credit_balance", d)}}
            out.append(record)
        return out

    return records


@asynccontextmanager
async def toss_maker(toss: FakeToss, state_dir: Path) -> AsyncIterator[Callable[[], TossClient]]:
    """Toss clients as the collector makes them (one token, shared), without the pace."""
    async with aiohttp.ClientSession() as session:
        tokens = TossTokens(session, toss.url, CLIENT_ID, CLIENT_SECRET, state_dir, lambda: datetime.now(KST))
        yield lambda: TossClient(session, toss.url, tokens, 0)


async def keep(
    kis: FakeKis,
    tmp_path: Path,
    now: datetime,
    toss: Callable[[], TossClient] | None = None,
    symbols: tuple[str, ...] = (SYMBOL,),
) -> int:
    pace = RateLimiter(1, 0.001, margin=0)  # the backfill's, in the collector
    async with rest_client(kis, tmp_path, now) as client:
        return await SupplyKeeper(client, CALENDAR, tmp_path / "state", symbols, pace, toss).run(now)


def kept(
    tmp_path: Path, first: date = date(2000, 1, 1), last: date = date(2030, 1, 1)
) -> list[dict[str, Any]]:
    return stored_supply(tmp_path / "state", SYMBOL, first, last)


def toss_calls(toss: FakeToss, name: str) -> list[dict[str, str]]:
    return [query for _, path, query, _ in toss.requests if path.endswith(f"/{name}")]


def investor_calls(kis: FakeKis, market: str) -> list[dict[str, str]]:
    """The investor query's calls of one market (``UN`` or ``J``)."""
    return [q for q in requests_for(kis, INVESTORS.tr) if q["FID_COND_MRKT_DIV_CODE"] == market]


def pages(days: list[date], per_call: int, floor: date) -> int:
    """The calls paging back ``days`` (oldest first) ``per_call`` at a time, from the newest,
    until a page reaches ``floor``."""
    calls, end = 0, len(days)
    while end > 0:
        calls += 1
        if days[max(0, end - per_call)] <= floor:
            break
        end -= per_call
    return calls


def test_the_history_kept_is_ten_years() -> None:
    assert HISTORY_YEARS == 10
    assert history_floor(date(2026, 10, 13)) == date(2016, 10, 13)
    assert history_floor(date(2028, 2, 29)) == date(2018, 2, 28)


async def test_the_first_run_takes_the_history_of_each_kis_query_paging_back(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(supply, "HISTORY_YEARS", 2)  # ten in the engine: fewer calls here
    days = trading_days(date(2023, 1, 2), date(2026, 10, 13))
    KisSupply(days).serve(kis)

    changed = await keep(kis, tmp_path, EVENING)

    assert changed == 1, "one file: KIS's (no Toss client)"
    floor = date(2024, 10, 13)  # a Sunday
    stored = read_supply(tmp_path / "state", SYMBOL, KIS)
    assert [r["date"] for r in stored] == [d for d in days if d >= floor], "every day, back to the floor"
    assert stored[-1] == {
        "date": date(2026, 10, 13),
        **{
            f: kis_value(f, date(2026, 10, 13))
            for f in FIELDS
            if f not in ("foreign_holding", "foreign_ratio")
        },
        NET_MARKET: "UN",
    }
    before = date(2025, 3, 3)  # the last day before NXT opened: its net buys are KRX's
    assert next(r for r in stored if r["date"] == before) == {
        "date": before,
        **{
            f: (krx_value if f.endswith("_net") else kis_value)(f, before)
            for f in FIELDS
            if f not in ("foreign_holding", "foreign_ratio")
        },
        NET_MARKET: "J",
    }
    # Each query pages back from the last finished day, from the day before its oldest row,
    # until a page reaches the floor.
    short = requests_for(kis, SHORT_SALE.tr)
    assert (short[0]["FID_INPUT_DATE_1"], short[0]["FID_INPUT_DATE_2"]) == ("20251013", "20261013")
    assert short[1]["FID_INPUT_DATE_2"] == ymd(days[-100] - timedelta(days=1))
    assert len(short) == pages(days, 100, floor)
    # The net buys: integrated back to NXT's opening, KRX from the day before it to the floor.
    integrated = investor_calls(kis, "UN")
    assert [q["FID_INPUT_DATE_1"] for q in integrated[:2]] == ["20261013", ymd(days[-30] - timedelta(days=1))]
    assert len(integrated) == pages(days, 30, NXT_OPENED)
    krx = investor_calls(kis, "J")
    assert krx[0]["FID_INPUT_DATE_1"] == "20250303"
    assert len(krx) == pages([d for d in days if d < NXT_OPENED], 30, floor)
    assert len(requests_for(kis, INVESTORS.tr)) == len(integrated) + len(krx)
    assert len(requests_for(kis, LENDING.tr)) == pages(days, 100, floor)
    assert len(requests_for(kis, CREDIT.tr)) == pages(days, 30, floor)
    assert not read_supply(tmp_path / "state", SYMBOL, TOSS)


async def test_the_merged_days_take_kis_first_toss_where_kis_lacks_a_day_and_toss_s_holdings(
    kis: FakeKis, tmp_path: Path
) -> None:
    days = trading_days(date(2026, 6, 1), date(2026, 10, 13))
    missing = date(2026, 10, 7)  # KIS has no row that day; Toss does
    KisSupply([d for d in days if d != missing]).serve(kis)

    async with fake_toss(toss_records(days)) as toss, toss_maker(toss, tmp_path / "state") as maker:
        changed = await keep(kis, tmp_path, MORNING, maker)

    assert changed == 2, "KIS's file and Toss's"
    by_day = {r["date"]: r for r in kept(tmp_path)}
    day = date(2026, 10, 8)
    assert by_day[day] == {
        "date": day,
        "short_volume": kis_value("short_volume", day),
        "individual_net": kis_value("individual_net", day),
        "institution_net": kis_value("institution_net", day),
        "foreign_net": kis_value("foreign_net", day),
        "foreign_holding": toss_value("foreign_holding", day),
        "foreign_ratio": toss_value("foreign_ratio", day),
        "lending_balance": kis_value("lending_balance", day),
        "credit_balance": kis_value("credit_balance", day),
    }
    assert by_day[missing] == {
        "date": missing,
        "short_volume": toss_value("short_volume", missing),
        "individual_net": toss_value("individual_net", missing),
        "institution_net": toss_value("institution_net", missing),
        # Toss's foreigners are the registered ones only, not KIS's: no secondary.
        "foreign_net": None,
        "foreign_holding": toss_value("foreign_holding", missing),
        "foreign_ratio": toss_value("foreign_ratio", missing),
        "lending_balance": toss_value("lending_balance", missing),
        "credit_balance": toss_value("credit_balance", missing),
    }
    assert sorted(by_day) == days


async def test_today_is_taken_from_kis_after_20_30_and_from_toss_only_the_next_run(
    kis: FakeKis, tmp_path: Path
) -> None:
    days = trading_days(date(2026, 9, 1), date(2026, 10, 14))  # KIS and Toss answer with the 14th too
    KisSupply(days).serve(kis)
    afternoon = datetime(2026, 10, 13, 16, 0, tzinfo=KST)

    async with fake_toss(toss_records(days)) as toss, toss_maker(toss, tmp_path / "state") as maker:
        await keep(kis, tmp_path, afternoon, maker)
        before = {r["date"]: r for r in kept(tmp_path)}
        await keep(kis, tmp_path, EVENING, maker)
        evening = {r["date"]: r for r in kept(tmp_path)}
        await keep(kis, tmp_path, MORNING, maker)
        morning = {r["date"]: r for r in kept(tmp_path)}

    # Before 20:30 the day is not over: up to the 12th.
    assert max(before) == date(2026, 10, 12)
    # At 21:00 KIS's day is in, but Toss's record of the day is provisional: not its holdings yet.
    assert max(evening) == date(2026, 10, 13)
    assert evening[date(2026, 10, 13)]["short_volume"] == kis_value("short_volume", date(2026, 10, 13))
    assert evening[date(2026, 10, 13)]["foreign_holding"] is None
    assert evening[date(2026, 10, 12)]["foreign_holding"] == toss_value("foreign_holding", date(2026, 10, 12))
    # The next morning's run takes Toss's record of the 13th; the 14th is not over.
    assert max(morning) == date(2026, 10, 13)
    assert morning[date(2026, 10, 13)]["foreign_holding"] == toss_value("foreign_holding", date(2026, 10, 13))
    until = [q["until"] for q in toss_calls(toss, "investor-trading")]
    assert until == ["2026-10-12", "2026-10-12", "2026-10-13"]


async def test_a_later_run_asks_back_to_the_newest_day_held_and_takes_revised_values(
    kis: FakeKis, tmp_path: Path
) -> None:
    days = trading_days(date(2025, 1, 2), date(2026, 10, 13))
    supply = KisSupply(days)
    supply.serve(kis)
    await keep(kis, tmp_path, EVENING)
    first_calls = len(kis.rest_requests)

    # KIS revises a day of last week, and the next day is over.
    revised = date(2026, 10, 7)
    original = supply.investors

    def investors(params: dict[str, str]) -> dict[str, Any]:
        answer = original(params)
        for row in answer["output2"]:
            if row["stck_bsop_date"] == ymd(revised):
                row["prsn_ntby_qty"] = "-1"
        return answer

    kis.rest_responses[INVESTORS.tr] = investors
    supply.days = trading_days(date(2025, 1, 2), date(2026, 10, 14))
    changed = await keep(kis, tmp_path, datetime(2026, 10, 14, 21, 0, tzinfo=KST))

    assert changed == 1
    asked = [t for _, t, _, _ in kis.rest_requests[first_calls:] if t != "CTCA0903R"]
    assert sorted(asked) == sorted([SHORT_SALE.tr, INVESTORS.tr, LENDING.tr, CREDIT.tr]), "a page each"
    by_day = {r["date"]: r for r in kept(tmp_path)}
    assert by_day[revised]["individual_net"] == -1
    assert max(by_day) == date(2026, 10, 14)
    assert by_day[date(2026, 10, 14)]["credit_balance"] == kis_value("credit_balance", date(2026, 10, 14))


async def test_the_credit_balance_is_kept_by_its_settlement_day(kis: FakeKis, tmp_path: Path) -> None:
    days = trading_days(date(2026, 8, 3), date(2026, 10, 13))
    KisSupply(days, credit_days=days[:-1]).serve(kis)  # the 13th's balance is not out yet

    await keep(kis, tmp_path, EVENING)

    by_day = {r["date"]: r for r in kept(tmp_path)}
    assert by_day[date(2026, 10, 12)]["credit_balance"] == kis_value("credit_balance", date(2026, 10, 12))
    assert by_day[date(2026, 10, 13)]["credit_balance"] is None
    assert by_day[date(2026, 10, 13)]["short_volume"] is not None


async def test_a_query_kis_refuses_leaves_the_others_and_is_asked_again_next_run(
    kis: FakeKis, tmp_path: Path
) -> None:
    days = trading_days(date(2026, 8, 3), date(2026, 10, 13))
    KisSupply(days).serve(kis)
    kis.rest_responses[LENDING.tr] = lambda params: {
        "rt_cd": "1",
        "msg_cd": "OPSQ0002",
        "msg1": "없는 서비스",
    }

    await keep(kis, tmp_path, EVENING)

    stored = read_supply(tmp_path / "state", SYMBOL, KIS)
    assert all(r["lending_balance"] is None for r in stored)
    assert all(r["short_volume"] is not None for r in stored)
    KisSupply(days).serve(kis)
    await keep(kis, tmp_path, EVENING)
    assert all(r["lending_balance"] is not None for r in read_supply(tmp_path / "state", SYMBOL, KIS))


async def test_toss_refusing_the_address_stops_toss_for_the_run_and_kis_is_kept(
    kis: FakeKis, tmp_path: Path
) -> None:
    days = trading_days(date(2026, 8, 3), date(2026, 10, 13))
    KisSupply(days).serve(kis)

    async with fake_toss(toss_records(days)) as toss, toss_maker(toss, tmp_path / "state") as maker:
        toss.api_ip_allowed = False
        changed = await keep(kis, tmp_path, EVENING, maker, symbols=(SYMBOL, "000660"))
        asked = len(toss.requests)

    assert changed == 2, "KIS's two files"
    assert asked == 1, "one call refused, then no more"
    assert not read_supply(tmp_path / "state", SYMBOL, TOSS)


async def test_toss_s_stock_not_found_on_one_endpoint_leaves_its_others(kis: FakeKis, tmp_path: Path) -> None:
    days = trading_days(date(2026, 8, 3), date(2026, 10, 13))
    KisSupply(days).serve(kis)
    records = toss_records(days)

    def some(symbol: str, name: str) -> list[dict[str, Any]] | tuple[int, str]:
        return (404, "stock-not-found") if name == "credit-trades" else records(symbol, name)

    async with fake_toss(some) as toss, toss_maker(toss, tmp_path / "state") as maker:
        await keep(kis, tmp_path, EVENING, maker)

    stored = read_supply(tmp_path / "state", SYMBOL, TOSS)
    assert stored and all(r["credit_balance"] is None and r["foreign_holding"] is not None for r in stored)


def test_stored_supply_merges_the_sources_by_day_within_the_days_asked(tmp_path: Path) -> None:
    state = tmp_path / "state"
    write_supply(
        state,
        SYMBOL,
        KIS,
        [
            {"date": date(2026, 10, 5), "short_volume": 1, "foreign_net": 2},
            {"date": date(2026, 10, 6), "short_volume": None, "credit_balance": 3},
        ],
    )
    write_supply(
        state,
        SYMBOL,
        TOSS,
        [
            {"date": date(2026, 10, 6), "short_volume": 10, "foreign_ratio": 50.5, "credit_balance": 30},
            {"date": date(2026, 10, 7), "foreign_holding": 40},
        ],
    )

    rows = stored_supply(state, SYMBOL, date(2026, 10, 6), date(2026, 10, 7))

    empty = dict.fromkeys(FIELDS)
    assert rows == [
        {"date": date(2026, 10, 6), **empty, "short_volume": 10, "foreign_ratio": 50.5, "credit_balance": 3},
        {"date": date(2026, 10, 7), **empty, "foreign_holding": 40},
    ]
    assert stored_supply(state, "000660", date(2026, 10, 1), date(2026, 10, 7)) == []


def test_kis_s_no_figure_zeros_are_none_and_take_toss_s_but_a_real_0_stays(tmp_path: Path) -> None:
    """KIS's integrated investor query answers 0 shares where it has no figure: the days before
    NXT opened (2025-03-04), and a day all three are 0 (#270). The files the keeper wrote before
    keep those zeros; reading takes them as none. After NXT opened, one class at 0 beside the
    others' figures is a real 0."""
    state = tmp_path / "state"
    before, only_foreign, nxt, real, blank = (
        date(2025, 2, 27),
        date(2025, 2, 28),
        date(2025, 3, 4),
        date(2025, 3, 5),
        date(2025, 3, 6),
    )
    nets = {
        before: (0, 0, 0),  # the sentinel: no figure
        only_foreign: (-4, 4, 0),  # before NXT: the foreign 0 is no figure, the others are figures
        nxt: (-3, 2, 1),
        real: (-3, 0, 3),  # an institution bought and sold as many shares: a real 0
        blank: (0, 0, 0),  # all three at 0 after NXT too: no figure
    }
    write_supply(
        state,
        SYMBOL,
        KIS,
        [
            {"date": d, "short_volume": 5, "individual_net": i, "institution_net": o, "foreign_net": f}
            for d, (i, o, f) in nets.items()
        ],
    )
    write_supply(state, SYMBOL, TOSS, [{"date": d, "individual_net": -7, "institution_net": 8} for d in nets])

    rows = read_supply(state, SYMBOL, KIS)

    assert [(r["individual_net"], r["institution_net"], r["foreign_net"]) for r in rows] == [
        (None, None, None),
        (-4, 4, None),
        (-3, 2, 1),
        (-3, 0, 3),
        (None, None, None),
    ]
    assert all(r["short_volume"] == 5 for r in rows), "the other queries' values stay"
    merged = {r["date"]: r for r in stored_supply(state, SYMBOL, before, blank)}
    assert (merged[before]["individual_net"], merged[before]["institution_net"]) == (-7, 8), "Toss's"
    assert merged[before]["foreign_net"] is None, "no secondary for the foreign net buy"
    assert merged[real]["institution_net"] == 0, "KIS's real 0, not Toss's"


async def test_the_keeper_stores_kis_s_no_figure_zeros_as_none(kis: FakeKis, tmp_path: Path) -> None:
    days = trading_days(date(2025, 2, 24), date(2025, 3, 7))
    supply = KisSupply(days)
    supply.serve(kis)
    original = supply.investors

    def investors(params: dict[str, str]) -> dict[str, Any]:
        answer = original(params)
        for row in answer["output2"]:
            day = datetime.strptime(row["stck_bsop_date"], "%Y%m%d").date()
            if day < date(2025, 3, 4):
                row |= {"prsn_ntby_qty": "0", "orgn_ntby_qty": "0", "frgn_ntby_qty": "0"}
            elif day == date(2025, 3, 5):
                row["orgn_ntby_qty"] = "0"
        return answer

    kis.rest_responses[INVESTORS.tr] = investors
    await keep(kis, tmp_path, datetime(2025, 3, 7, 21, 0, tzinfo=KST))

    by_day = {r["date"]: r for r in read_supply(tmp_path / "state", SYMBOL, KIS)}
    assert all(by_day[d]["foreign_net"] is None for d in days if d < date(2025, 3, 4))
    assert by_day[date(2025, 3, 4)]["foreign_net"] == kis_value("foreign_net", date(2025, 3, 4))
    assert by_day[date(2025, 3, 5)]["institution_net"] == 0, "a real 0 after NXT opened"
    assert by_day[date(2025, 2, 24)]["short_volume"] == kis_value("short_volume", date(2025, 2, 24))


# The net buys' date split (#264, #297): KRX (J) before NXT opened, integrated (UN) from it.
SPRING = datetime(2025, 4, 30, 21, 0, tzinfo=KST)  # a Wednesday, the day over
NEXT_EVENING = datetime(2025, 5, 2, 21, 0, tzinfo=KST)  # Friday (the 1st is a weekday here too)


def nets(row: dict[str, Any]) -> tuple[Any, ...]:
    return (row["individual_net"], row["institution_net"], row["foreign_net"], row[NET_MARKET])


async def test_the_net_buys_are_krx_s_before_nxt_opened_and_integrated_after_krx_asked_once(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(supply, "HISTORY_YEARS", 1)
    supplied = KisSupply(trading_days(date(2024, 3, 4), date(2025, 4, 30)))
    supplied.serve(kis)

    await keep(kis, tmp_path, SPRING)

    by_day = {r["date"]: r for r in read_supply(tmp_path / "state", SYMBOL, KIS)}
    assert min(by_day) == date(2024, 4, 30), "back to the floor"
    for day, row in by_day.items():
        value, market = (krx_value, "J") if day < NXT_OPENED else (kis_value, "UN")
        assert nets(row) == (*(value(f, day) for f in supply.NET_BUYS), market), day
        assert row["short_volume"] == kis_value("short_volume", day), "the other queries' days stay"
    # UN is never asked for a day before NXT opened, nor J for one after.
    assert min(q["FID_INPUT_DATE_1"] for q in investor_calls(kis, "UN")) >= "20250304"
    assert max(q["FID_INPUT_DATE_1"] for q in investor_calls(kis, "J")) == "20250303"
    krx_calls = len(investor_calls(kis, "J"))
    # KRX's history is closed: the next run tops up the integrated days only.
    supplied.days = trading_days(date(2024, 3, 4), date(2025, 5, 2))
    first = len(investor_calls(kis, "UN"))
    await keep(kis, tmp_path, NEXT_EVENING)
    assert len(investor_calls(kis, "J")) == krx_calls
    assert len(investor_calls(kis, "UN")) == first + 1, "a page"
    assert nets(by_day[date(2025, 3, 3)]) == nets(
        {r["date"]: r for r in read_supply(tmp_path / "state", SYMBOL, KIS)}[date(2025, 3, 3)]
    )


async def test_krx_zeros_before_nxt_opened_stay_no_figure_and_are_not_asked_again(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If KRX also has no figure before NXT opened, nothing changes: the days stay none (Toss's
    values where Toss has them), and KRX is not asked again."""
    monkeypatch.setattr(supply, "HISTORY_YEARS", 1)
    supplied = KisSupply(trading_days(date(2024, 3, 4), date(2025, 4, 30)))
    supplied.serve(kis)

    def investors(params: dict[str, str]) -> dict[str, Any]:
        answer = supplied.investors(params)
        if params["FID_COND_MRKT_DIV_CODE"] == "J":
            for row in answer["output2"]:
                row |= {"prsn_ntby_qty": "0", "orgn_ntby_qty": "0", "frgn_ntby_qty": "0"}
        return answer

    kis.rest_responses[INVESTORS.tr] = investors
    await keep(kis, tmp_path, SPRING)
    krx_calls = len(investor_calls(kis, "J"))
    await keep(kis, tmp_path, NEXT_EVENING)

    rows = read_supply(tmp_path / "state", SYMBOL, KIS)
    before = [r for r in rows if r["date"] < NXT_OPENED]
    assert before and all(nets(r) == (None, None, None, "J") for r in before)
    assert all(r["short_volume"] is not None for r in before)
    assert all(
        nets(r)[:3] == tuple(kis_value(f, r["date"]) for f in supply.NET_BUYS) for r in rows[len(before) :]
    )
    assert krx_calls > 1 and len(investor_calls(kis, "J")) == krx_calls, "asked once"


async def test_a_file_written_before_takes_krx_s_history_at_the_next_run(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server's files from before: every day's net buys from UN, those before NXT opened its
    0 (none). The next run asks KRX for the days before NXT, back to the floor, and tops up the
    integrated days by a page; a KRX query KIS refuses is asked again the run after."""
    monkeypatch.setattr(supply, "HISTORY_YEARS", 1)
    state = tmp_path / "state"
    days = trading_days(date(2024, 3, 4), date(2025, 4, 30))
    old = pa.schema([("date", pa.date32())] + [(f, pa.int64()) for f in supply.KIS_SCHEMA.names[1:-1]])
    path = supply.supply_path(state, SYMBOL, KIS)
    path.parent.mkdir(parents=True)
    rows = [
        {
            "date": d,
            "short_volume": kis_value("short_volume", d),
            **{f: 0 if d < NXT_OPENED else kis_value(f, d) for f in supply.NET_BUYS},
        }
        for d in days
        if d >= date(2024, 4, 30)
    ]
    pq.write_table(pa.Table.from_pylist(rows, schema=old), path)
    written = read_supply(state, SYMBOL, KIS)
    assert {r[NET_MARKET] for r in written} == {"UN"}, "from UN, the one investor query then"
    assert all(nets(r)[:3] == (None, None, None) for r in written if r["date"] < NXT_OPENED)

    supplied = KisSupply(days)
    supplied.serve(kis)
    kis.rest_responses[INVESTORS.tr] = lambda params: (
        {"rt_cd": "1", "msg_cd": "OPSQ0002", "msg1": "없는 서비스"}
        if params["FID_COND_MRKT_DIV_CODE"] == "J"
        else supplied.investors(params)
    )
    await keep(kis, tmp_path, SPRING)
    assert not krx_history_stored(state, [SYMBOL]), "the walk failed: not stored"
    assert len(investor_calls(kis, "UN")) == 1, "a page: the newest day held is the 30th"
    assert all(
        nets(r)[:3] == (None, None, None) for r in read_supply(state, SYMBOL, KIS) if r["date"] < NXT_OPENED
    )

    supplied.serve(kis)
    await keep(kis, tmp_path, SPRING)
    by_day = {r["date"]: r for r in read_supply(state, SYMBOL, KIS)}
    assert nets(by_day[date(2024, 4, 30)]) == (
        *(krx_value(f, date(2024, 4, 30)) for f in supply.NET_BUYS),
        "J",
    )
    assert nets(by_day[NXT_OPENED]) == (*(kis_value(f, NXT_OPENED) for f in supply.NET_BUYS), "UN")
    assert {r[NET_MARKET] for r in by_day.values()} == {"J", "UN"}
    assert krx_history_stored(state, [SYMBOL])


async def test_every_day_before_nxt_is_marked_asked_of_krx_also_one_krx_gives_no_row(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KRX's answers may stop short of the floor while the other queries go further: those
    days were asked of KRX all the same (net buys none), so the history counts as stored and
    KRX is not asked again."""
    monkeypatch.setattr(supply, "HISTORY_YEARS", 1)
    supplied = KisSupply(trading_days(date(2024, 3, 4), date(2025, 4, 30)))
    supplied.serve(kis)
    first_row = date(2025, 1, 2)

    def investors(params: dict[str, str]) -> dict[str, Any]:
        answer = supplied.investors(params)
        if params["FID_COND_MRKT_DIV_CODE"] == "J":
            answer["output2"] = [r for r in answer["output2"] if r["stck_bsop_date"] >= ymd(first_row)]
        return answer

    kis.rest_responses[INVESTORS.tr] = investors
    state = tmp_path / "state"
    assert not krx_history_stored(state, [SYMBOL]), "no file yet"
    await keep(kis, tmp_path, SPRING)
    calls = len(investor_calls(kis, "J"))
    await keep(kis, tmp_path, NEXT_EVENING)

    rows = read_supply(state, SYMBOL, KIS)
    before = [r for r in rows if r["date"] < NXT_OPENED]
    assert before[0]["date"] < first_row and all(r[NET_MARKET] == "J" for r in before)
    assert all(nets(r)[:3] == (None, None, None) for r in before if r["date"] < first_row)
    assert nets(next(r for r in before if r["date"] == first_row))[:3] == tuple(
        krx_value(f, first_row) for f in supply.NET_BUYS
    )
    assert krx_history_stored(state, [SYMBOL])
    assert len(investor_calls(kis, "J")) == calls, "not asked again"


async def test_the_api_gives_the_days_asked_oldest_first_and_refuses_bad_ranges(tmp_path: Path) -> None:
    state = tmp_path / "state"
    days = trading_days(date(2025, 9, 1), date(2026, 10, 8))
    write_supply(state, SYMBOL, KIS, [{"date": d, "short_volume": n(d), "foreign_net": -n(d)} for d in days])
    write_supply(state, SYMBOL, TOSS, [{"date": d, "foreign_ratio": 50.25} for d in days])
    engine = await run(tmp_path / "engine.db", [], until=at("10:00:00"))  # Monday the 12th

    async with TestClient(TestServer(create_app(engine, state_dir=state))) as client:
        year = await get_json(client, f"/v1/symbols/{SYMBOL}/supply")
        week = await get_json(client, f"/v1/symbols/{SYMBOL}/supply?from=2026-10-05&to=2026-10-11")
        bad = [
            (await client.get(f"/v1/symbols/{SYMBOL}/supply?{q}")).status
            for q in (
                "from=2026-10-1",
                "from=2026-10-12&to=2026-10-11",
                f"from={(date(2026, 10, 12) - timedelta(days=MAX_SUPPLY_DAYS)).isoformat()}&to=2026-10-12",
            )
        ]
        unknown = (await client.get("/v1/symbols/999999/supply")).status
    async with TestClient(TestServer(create_app(engine))) as client:
        none = await get_json(client, f"/v1/symbols/{SYMBOL}/supply?from=2026-10-01&to=2026-10-12")

    for body in (year, week, none):
        check("supply", body)
    assert (year["from"], year["to"]) == ("2025-10-13", "2026-10-12"), "the year to today"
    assert year["days"][0]["date"] == "2025-10-13"
    assert year["days"][-1]["date"] == "2026-10-08"
    assert week == {
        "symbol": SYMBOL,
        "from": "2026-10-05",
        "to": "2026-10-11",
        "days": [
            {
                "date": d.isoformat(),
                **dict.fromkeys(FIELDS),
                "short_volume": n(d),
                "foreign_net": -n(d),
                "foreign_ratio": 50.25,
            }
            for d in trading_days(date(2026, 10, 5), date(2026, 10, 8))
        ],
    }
    assert bad == [400, 400, 400]
    assert unknown == 404
    assert none["days"] == [], "an engine without the state directory has none"
